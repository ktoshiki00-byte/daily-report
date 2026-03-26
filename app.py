import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    PostbackEvent,
)

# ─────────────────────────────────────
# 初期設定
# ─────────────────────────────────────

# .envファイルから環境変数を読み込む（ローカル開発用）
load_dotenv()

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Flaskアプリ初期化
app = Flask(__name__)

# Blueprintの登録
from routes.monthly import monthly_bp   # noqa: E402（循環インポート回避のためここで import）
app.register_blueprint(monthly_bp)

# LINE Bot設定
LINE_CHANNEL_SECRET      = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets設定
# 環境変数が未設定の場合はスプレッドシート連携を無効化してログのみ出力する
GOOGLE_SHEET_ID   = os.environ.get('GOOGLE_SHEET_ID', '')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', '')
SHEETS_ENABLED    = bool(GOOGLE_SHEET_ID and GOOGLE_CREDENTIALS)
if not SHEETS_ENABLED:
    logger.warning(
        'GOOGLE_SHEET_ID または GOOGLE_CREDENTIALS が未設定です。'
        'スプレッドシートへの保存をスキップしてログに出力します。'
    )
GOOGLE_SCOPES   = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
]

# 管理者LINE User ID（日報レポート送信先）
LINE_USER_ID = os.environ.get('LINE_USER_ID', '')

# タイムゾーン（日本標準時）
JST = ZoneInfo('Asia/Tokyo')

# ─────────────────────────────────────
# 定数定義
# ─────────────────────────────────────

# 日報ボタンの定義（label: 表示名, value: 記録値）
ACTIONS = [
    {'label': '🤝 商談',          'value': '商談'},
    {'label': '🚗 移動・外出',    'value': '移動・外出'},
    {'label': '🏢 メーカー訪問',  'value': 'メーカー訪問'},
    {'label': '🏭 展示会・イベント', 'value': '展示会・イベント'},
    {'label': '💻 社内作業',      'value': '社内作業'},
    {'label': '🏗️ 工場対応',     'value': '工場対応'},
]

# アクション名に対応する絵文字（週次レポートの表示用）
ACTION_EMOJI = {
    '商談':              '🤝',
    '移動・外出':        '🚗',
    'メーカー訪問':      '🏢',
    '展示会・イベント':  '🏭',
    '社内作業':          '💻',
    '工場対応':          '🏗️',
}

# アクション名の短縮表示マッピング（レポートで長い名前を簡略化）
ACTION_SHORT = {
    '移動・外出':        '移動',
    '展示会・イベント':  '展示会',
}

# 各アクションに対応する「入力待ち状態」と「質問文」の定義
#
# フロー別:
#   商談 / メーカー訪問 / 展示会・イベント
#     → waiting_for_company（訪問先会社名）
#     → waiting_for_memo（自由メモ、スキップ可）
#     → 保存
#
#   移動・外出
#     → waiting_for_destination（移動先・目的地）
#     → 保存
#
#   社内作業
#     → waiting_for_work_content（作業内容）
#     → 保存
#
#   工場対応
#     → waiting_for_factory_content（対応内容）
#     → 保存

# アクション → 最初の入力待ち状態へのマッピング
ACTION_FIRST_STATE = {
    '商談':         'waiting_for_company',
    'メーカー訪問': 'waiting_for_company',
    '展示会・イベント': 'waiting_for_company',
    '移動・外出':   'waiting_for_destination',
    '社内作業':     'waiting_for_work_content',
    '工場対応':     'waiting_for_factory_content',
}

# 入力待ち状態 → ユーザーへの質問文
STATE_QUESTION = {
    'waiting_for_company':        '訪問先の会社名を入力してください🏢',
    'waiting_for_memo':           '自由メモがあれば入力してください📝\n（スキップする場合は「スキップ」と送信）',
    'waiting_for_destination':    '移動先・目的地を入力してください🚗',
    'waiting_for_work_content':   '作業内容を入力してください💻',
    'waiting_for_factory_content':'対応内容を入力してください🏗️',
}

# ─────────────────────────────────────
# ユーザー状態管理（インメモリ）
# ─────────────────────────────────────
# Render.com無料プランはシングルワーカーのためインメモリで問題なし
#
# 形式:
# {
#   user_id: {
#     'state':           str,   # 現在の入力待ち状態
#     'time_slot':       str,   # '午前' or '午後'
#     'action':          str,   # 選択されたアクション名
#     'company':         str,   # 訪問先会社名
#     'destination':     str,   # 移動先
#     'work_content':    str,   # 作業内容
#     'factory_content': str,   # 工場対応内容
#     'memo':            str,   # 自由メモ
#   }
# }
user_states: dict = {}

# ─────────────────────────────────────
# Google Sheets ヘルパー
# ─────────────────────────────────────

def get_spreadsheet():
    """Google Sheetsへの接続を取得する"""
    credentials_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(credentials_dict, scopes=GOOGLE_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(GOOGLE_SHEET_ID)


def get_or_create_sheet(spreadsheet, sheet_name: str, headers: list = None):
    """シートを取得する。存在しない場合は作成してヘッダーを追加する"""
    try:
        sheet = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=20)
        if headers:
            sheet.append_row(headers)
    return sheet


def save_report(
    display_name: str,
    time_slot: str,
    action: str,
    company: str = '',
    destination: str = '',
    work_content: str = '',
    factory_content: str = '',
    memo: str = '',
):
    """日報データをGoogle Sheetsの「日報」シートに保存する。
    スプレッドシート連携が無効な場合はログに出力してスキップする。"""
    now = datetime.now(JST)

    # スプレッドシートの列順:
    # 日付 / 時間 / ユーザー名 / 午前or午後 / 行動種別 /
    # 訪問先会社名 / 移動先 / 作業内容 / 工場対応内容 / 自由メモ
    row = [
        now.strftime('%Y/%m/%d'),
        now.strftime('%H:%M'),
        display_name,
        time_slot,
        action,
        company,
        destination,
        work_content,
        factory_content,
        memo,
    ]

    # スプレッドシート連携が無効な場合はログのみ
    if not SHEETS_ENABLED:
        logger.info(f'[SHEETS無効] 日報データ: {row}')
        return

    spreadsheet = get_spreadsheet()
    sheet = get_or_create_sheet(
        spreadsheet, '日報',
        ['日付', '時間', 'ユーザー名', '午前or午後', '行動種別',
         '訪問先会社名', '移動先', '作業内容', '工場対応内容', '自由メモ']
    )
    sheet.append_row(row)
    logger.info(f'日報保存完了: {display_name} / {time_slot} / {action}')


def register_user(user_id: str, display_name: str) -> str:
    """ユーザーを「users」シートに登録する。既登録の場合はその旨を返す。
    スプレッドシート連携が無効な場合はログのみ出力する。"""
    if not SHEETS_ENABLED:
        logger.info(f'[SHEETS無効] ユーザー登録: {display_name} ({user_id})')
        return (
            f'✅ {display_name}さんを登録しました！\n'
            '毎朝9時と14時に日報リマインダーを送ります📋\n'
            'ボタンをタップして1〜2分で入力できます。'
        )

    spreadsheet = get_spreadsheet()
    users_sheet = get_or_create_sheet(
        spreadsheet, 'users', ['LINE表示名', 'ユーザーID']
    )
    records = users_sheet.get_all_records()

    if any(r.get('ユーザーID') == user_id for r in records):
        return f'✅ {display_name}さんはすでに登録済みです！'

    users_sheet.append_row([display_name, user_id])
    return (
        f'✅ {display_name}さんを登録しました！\n'
        '毎朝9時と14時に日報リマインダーを送ります📋\n'
        'ボタンをタップして1〜2分で入力できます。'
    )


def get_all_user_ids() -> list[str]:
    """登録済み全ユーザーのIDリストを取得する。
    スプレッドシート連携が無効な場合は空リストを返す。"""
    if not SHEETS_ENABLED:
        logger.info('[SHEETS無効] ユーザーIDの取得をスキップ')
        return []

    spreadsheet = get_spreadsheet()
    users_sheet = get_or_create_sheet(
        spreadsheet, 'users', ['LINE表示名', 'ユーザーID']
    )
    records = users_sheet.get_all_records()
    return [r['ユーザーID'] for r in records if r.get('ユーザーID')]

# ─────────────────────────────────────
# LINE API ヘルパー
# ─────────────────────────────────────

def get_display_name(user_id: str) -> str:
    """LINE APIからユーザーの表示名を取得する"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            return profile.display_name
    except Exception as e:
        logger.error(f'プロフィール取得エラー ({user_id}): {e}')
        return '名無し'


def reply_text(reply_token: str, text: str):
    """テキストメッセージを返信する"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=text)],
        ))


def reply_error(reply_token: str):
    """エラー時に汎用メッセージを返信する"""
    try:
        reply_text(reply_token, '少し待ってからもう一度お試しください🙏')
    except Exception as e:
        logger.error(f'エラー返信にも失敗: {e}')

# ─────────────────────────────────────
# Flex Message 生成
# ─────────────────────────────────────

def create_flex_message(time_slot: str) -> FlexMessage:
    """日報入力用Flex Messageを作成する"""
    # 時間帯でヘッダー色とタイトルを変える
    if time_slot == '午前':
        header_color = '#FF8C00'      # オレンジ（朝）
        title        = '☀️ 午前の日報入力'
    else:
        header_color = '#1565C0'      # ブルー（午後）
        title        = '🌆 午後の日報入力'

    # ボタンを生成
    # 訪問系アクション（company入力あり）→ primary、それ以外 → secondary
    visit_actions = {'商談', 'メーカー訪問', '展示会・イベント'}
    buttons = []
    for action in ACTIONS:
        style = 'primary' if action['value'] in visit_actions else 'secondary'
        buttons.append({
            'type': 'button',
            'action': {
                'type': 'postback',
                'label': action['label'],
                'data': f"action={action['value']}&time_slot={time_slot}",
                'displayText': action['label'],
            },
            'style': style,
            'margin': 'sm',
            'height': 'sm',
        })

    flex_dict = {
        'type': 'bubble',
        'size': 'mega',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': header_color,
            'paddingAll': '15px',
            'contents': [
                {
                    'type': 'text',
                    'text': title,
                    'weight': 'bold',
                    'color': '#ffffff',
                    'size': 'lg',
                }
            ],
        },
        'body': {
            'type': 'box',
            'layout': 'vertical',
            'spacing': 'sm',
            'paddingAll': '13px',
            'contents': [
                {
                    'type': 'text',
                    'text': '今の活動を選んでください👇',
                    'wrap': True,
                    'color': '#555555',
                    'size': 'sm',
                    'margin': 'xs',
                },
                *buttons,
            ],
        },
    }

    return FlexMessage(
        alt_text=f'{time_slot}の日報を入力してください',
        contents=FlexContainer.from_dict(flex_dict),
    )


def send_flex_to_all(time_slot: str):
    """登録済み全ユーザーにFlex Messageをプッシュ送信する"""
    try:
        user_ids = get_all_user_ids()
        if not user_ids:
            logger.info('登録ユーザーが0名のため送信をスキップ')
            return

        flex_msg = create_flex_message(time_slot)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            for uid in user_ids:
                try:
                    line_bot_api.push_message(PushMessageRequest(
                        to=uid,
                        messages=[flex_msg],
                    ))
                    logger.info(f'プッシュ送信完了: {uid}')
                except Exception as e:
                    logger.error(f'プッシュ送信失敗 ({uid}): {e}')

    except Exception as e:
        logger.error(f'send_flex_to_all エラー: {e}')

# ─────────────────────────────────────
# サマリーテキスト生成
# ─────────────────────────────────────

def build_summary(state: dict, display_name: str) -> str:
    """記録完了時のサマリー文字列を生成する"""
    lines = [
        '✅ 記録しました！',
        '━━━━━━━━━━━',
        f'👤 {display_name}',
        f'🕐 {state["time_slot"]}',
        f'📌 {state["action"]}',
    ]
    if state.get('company'):
        lines.append(f'🏢 訪問先: {state["company"]}')
    if state.get('destination'):
        lines.append(f'🚗 移動先: {state["destination"]}')
    if state.get('work_content'):
        lines.append(f'💻 作業内容: {state["work_content"]}')
    if state.get('factory_content'):
        lines.append(f'🏗️ 対応内容: {state["factory_content"]}')
    if state.get('memo'):
        lines.append(f'📝 メモ: {state["memo"]}')
    return '\n'.join(lines)

# ─────────────────────────────────────
# 状態遷移ロジック
# ─────────────────────────────────────

def get_next_state(current_state: str, action: str) -> str | None:
    """現在の状態とアクションから次の状態を返す。保存完了なら None を返す"""
    if current_state == 'waiting_for_company':
        # 訪問先会社名を受け取った後は自由メモへ
        return 'waiting_for_memo'

    # その他の状態はすべて入力1回で完了（保存）
    return None


def finalize_and_save(user_id: str, display_name: str):
    """user_statesの内容をGoogleスプレッドシートに保存する"""
    s = user_states[user_id]
    save_report(
        display_name       = display_name,
        time_slot          = s['time_slot'],
        action             = s['action'],
        company            = s.get('company', ''),
        destination        = s.get('destination', ''),
        work_content       = s.get('work_content', ''),
        factory_content    = s.get('factory_content', ''),
        memo               = s.get('memo', ''),
    )

# ─────────────────────────────────────
# レポート用ヘルパー
# ─────────────────────────────────────

def format_action_label(row: dict, with_emoji: bool = False) -> str:
    """日報1件をレポート用の短縮テキストに変換する。
    例: 商談 + 丸善商事 → '商談/丸善商事'、with_emoji=True なら '🤝商談/丸善商事'"""
    action      = row.get('行動種別', '')
    company     = row.get('訪問先会社名', '').strip()
    destination = row.get('移動先', '').strip()

    emoji = ACTION_EMOJI.get(action, '') if with_emoji else ''
    short = ACTION_SHORT.get(action, action)   # 短縮名があれば使う

    if company:
        return f'{emoji}{short}/{company}'
    if destination:
        return f'{emoji}{short}/{destination}'
    return f'{emoji}{short}'


def get_all_users() -> list[dict]:
    """登録済み全ユーザーの名前とIDを辞書リストで返す。
    スプレッドシート連携が無効な場合は空リストを返す。"""
    if not SHEETS_ENABLED:
        logger.info('[SHEETS無効] ユーザー一覧の取得をスキップ')
        return []
    spreadsheet = get_spreadsheet()
    users_sheet = get_or_create_sheet(
        spreadsheet, 'users', ['LINE表示名', 'ユーザーID']
    )
    records = users_sheet.get_all_records()
    return [
        {'name': r['LINE表示名'], 'id': r['ユーザーID']}
        for r in records
        if r.get('ユーザーID')
    ]


def get_reports_by_date_range(date_strs: list[str]) -> list[dict]:
    """指定した日付リストに一致する日報レコードをすべて返す。
    スプレッドシート連携が無効な場合は空リストを返す。"""
    if not SHEETS_ENABLED:
        return []
    spreadsheet = get_spreadsheet()
    report_sheet = get_or_create_sheet(
        spreadsheet, '日報',
        ['日付', '時間', 'ユーザー名', '午前or午後', '行動種別',
         '訪問先会社名', '移動先', '作業内容', '工場対応内容', '自由メモ']
    )
    all_records = report_sheet.get_all_records()
    date_set = set(date_strs)
    return [r for r in all_records if r.get('日付') in date_set]


def _send_daily_report():
    """日報レポートを管理者に送信するコア処理。
    スプレッドシートまたはLINE_USER_IDが未設定の場合はスキップする。
    ビュー関数（/report）とスケジューラーの両方から呼び出せる。"""
    if not SHEETS_ENABLED:
        logger.info('日報レポートをスキップ（スプレッドシート未設定）')
        return
    if not LINE_USER_ID:
        logger.info('日報レポートをスキップ（LINE_USER_ID未設定）')
        return

    today      = datetime.now(JST)
    today_str  = today.strftime('%Y/%m/%d')
    date_label = f'{today.month}月{today.day}日'

    # 全ユーザーと今日の日報を取得
    users         = get_all_users()
    today_records = get_reports_by_date_range([today_str])

    # ユーザー名をキーに午前・午後の最新エントリをまとめる（同一時間帯は後勝ち）
    user_report_map: dict[str, dict] = {}
    for record in today_records:
        name = record.get('ユーザー名', '')
        slot = record.get('午前or午後', '')
        if name not in user_report_map:
            user_report_map[name] = {}
        user_report_map[name][slot] = record

    # 提出済み・未提出に分類
    submitted, not_submitted = [], []
    for user in users:
        name = user['name']
        if name in user_report_map:
            submitted.append((name, user_report_map[name]))
        else:
            not_submitted.append(name)

    # メッセージ本文を組み立てる
    lines = [f'📊 本日の日報レポート（{date_label}）', '']
    lines.append(f'✅ 提出済み（{len(submitted)}名）')
    for name, slots in submitted:
        parts = [
            f'{slot}:{format_action_label(slots[slot])}'
            for slot in ['午前', '午後'] if slot in slots
        ]
        lines.append(f'・{name}｜{" ".join(parts)}')
    lines.append('')
    lines.append(f'❌ 未提出（{len(not_submitted)}名）')
    for name in not_submitted:
        lines.append(f'・{name}')

    # 管理者に送信
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(PushMessageRequest(
            to=LINE_USER_ID,
            messages=[TextMessage(text='\n'.join(lines))],
        ))
    logger.info('日報レポート送信完了')


def _send_weekly_report():
    """週次振り返りレポートを全登録ユーザーに個別送信するコア処理。
    ビュー関数（/weekly）とスケジューラーの両方から呼び出せる。"""
    if not SHEETS_ENABLED:
        logger.info('週次レポートをスキップ（スプレッドシート未設定）')
        return

    today  = datetime.now(JST)
    # 今週の月曜日を算出
    monday = today - timedelta(days=today.weekday())
    week_dates     = [monday + timedelta(days=i) for i in range(5)]
    week_date_strs = [d.strftime('%Y/%m/%d') for d in week_dates]
    week_label = (
        f'{monday.month}/{monday.day}〜'
        f'{week_dates[-1].month}/{week_dates[-1].day}'
    )

    users        = get_all_users()
    week_records = get_reports_by_date_range(week_date_strs)

    if not users:
        logger.info('週次レポートをスキップ（登録ユーザーが0名）')
        return

    DAY_NAMES = ['月', '火', '水', '木', '金']

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        for user in users:
            name = user['name']
            uid  = user['id']

            # このユーザーの今週分レコードを日付×時間帯で整理（後勝ち）
            user_week: dict[str, dict] = {}
            for record in week_records:
                if record.get('ユーザー名') != name:
                    continue
                date = record.get('日付', '')
                slot = record.get('午前or午後', '')
                if date not in user_week:
                    user_week[date] = {}
                user_week[date][slot] = record

            # 曜日ごとの行を生成（連続する未入力日はまとめて表示）
            day_lines: list[str]      = []
            missing_streak: list[str] = []

            for day_name, date_str in zip(DAY_NAMES, week_date_strs):
                slots  = user_week.get(date_str, {})
                has_am = '午前' in slots
                has_pm = '午後' in slots

                if not has_am and not has_pm:
                    missing_streak.append(day_name)
                else:
                    if missing_streak:
                        day_lines.append(
                            f'{missing_streak[0]}｜未入力' if len(missing_streak) == 1
                            else f'{missing_streak[0]}〜{missing_streak[-1]}｜未入力'
                        )
                        missing_streak = []
                    am_text = format_action_label(slots['午前'], with_emoji=True) if has_am else '未入力'
                    pm_text = format_action_label(slots['午後'], with_emoji=True) if has_pm else '未入力'
                    day_lines.append(f'{day_name}｜午前:{am_text} 午後:{pm_text}')

            # 末尾に残った連続未入力をまとめて出力
            if missing_streak:
                day_lines.append(
                    f'{missing_streak[0]}｜未入力' if len(missing_streak) == 1
                    else f'{missing_streak[0]}〜{missing_streak[-1]}｜未入力'
                )

            # 今週の商談件数を集計
            negotiation_count = sum(
                1 for r in week_records
                if r.get('ユーザー名') == name and r.get('行動種別') == '商談'
            )

            message = (
                f'📅 今週の振り返り（{week_label}）\n{name}さん\n\n'
                + '\n'.join(day_lines)
                + f'\n\n今週の商談件数：{negotiation_count}件'
            )

            try:
                line_bot_api.push_message(PushMessageRequest(
                    to=uid,
                    messages=[TextMessage(text=message)],
                ))
                logger.info(f'週次レポート送信完了: {name} ({uid})')
            except Exception as e:
                logger.error(f'週次レポート送信失敗 ({uid}): {e}')


# ─────────────────────────────────────
# Flaskルート
# ─────────────────────────────────────

@app.route('/')
def index():
    """ヘルスチェック用エンドポイント"""
    return 'LINE日報ボット稼働中！'


@app.route('/webhook', methods=['POST'])
def callback():
    """LINE Webhookを受け取るエンドポイント"""
    signature = request.headers.get('X-Line-Signature', '')
    body      = request.get_data(as_text=True)
    logger.info('Webhook受信')

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error('署名検証エラー')
        abort(400)

    return 'OK'


@app.route('/morning', methods=['GET', 'POST'])
def morning():
    """午前9:00にcron-job.orgから呼ばれるエンドポイント"""
    logger.info('午前プッシュ開始')
    send_flex_to_all('午前')
    return jsonify({'status': 'ok', 'message': '午前の日報を送信しました'})


@app.route('/afternoon', methods=['GET', 'POST'])
def afternoon():
    """午後14:00にcron-job.orgから呼ばれるエンドポイント"""
    logger.info('午後プッシュ開始')
    send_flex_to_all('午後')
    return jsonify({'status': 'ok', 'message': '午後の日報を送信しました'})


@app.route('/report', methods=['GET', 'POST'])
def daily_report():
    """管理者向け日報レポートをLINE_USER_IDに送信するエンドポイント。
    コアロジックは _send_daily_report() に集約している。"""
    if not SHEETS_ENABLED:
        return jsonify({'status': 'skip', 'message': 'スプレッドシート未設定のためスキップ'})
    if not LINE_USER_ID:
        return jsonify({'status': 'skip', 'message': 'LINE_USER_IDが未設定のためスキップ'})
    try:
        _send_daily_report()
        return jsonify({'status': 'ok', 'message': '日報レポートを送信しました'})
    except Exception as e:
        logger.error(f'daily_report エラー: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/weekly', methods=['GET', 'POST'])
def weekly_report():
    """週次振り返りレポートを全登録ユーザーに個別送信するエンドポイント。
    コアロジックは _send_weekly_report() に集約している。"""
    if not SHEETS_ENABLED:
        return jsonify({'status': 'skip', 'message': 'スプレッドシート未設定のためスキップ'})
    try:
        _send_weekly_report()
        return jsonify({'status': 'ok', 'message': '週次レポートを送信しました'})
    except Exception as e:
        logger.error(f'weekly_report エラー: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ─────────────────────────────────────
# LINEイベントハンドラ
# ─────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """テキストメッセージを処理する"""
    user_id     = event.source.user_id
    text        = event.message.text.strip()
    reply_token = event.reply_token

    try:
        # ──── 登録コマンド ────
        if text == '登録':
            display_name = get_display_name(user_id)
            result_msg   = register_user(user_id, display_name)
            reply_text(reply_token, result_msg)
            return

        # ──── ユーザー状態を確認 ────
        state = user_states.get(user_id)

        if state is None:
            # 未登録またはフロー外のメッセージ → 使い方を案内
            reply_text(
                reply_token,
                '「登録」と送ると日報リマインダーの登録ができます📋\n'
                '日報はリマインダーのボタンから入力してください。'
            )
            return

        current_state = state['state']
        display_name  = get_display_name(user_id)

        # ──── 訪問先会社名の入力 ────
        if current_state == 'waiting_for_company':
            user_states[user_id]['company'] = text
            user_states[user_id]['state']   = 'waiting_for_memo'
            reply_text(reply_token, STATE_QUESTION['waiting_for_memo'])
            return

        # ──── 自由メモの入力（スキップ可） ────
        if current_state == 'waiting_for_memo':
            # 「スキップ」の場合はメモなしで保存
            user_states[user_id]['memo'] = '' if text == 'スキップ' else text
            finalize_and_save(user_id, display_name)
            summary = build_summary(user_states[user_id], display_name)
            del user_states[user_id]
            reply_text(reply_token, summary)
            return

        # ──── 移動先の入力 ────
        if current_state == 'waiting_for_destination':
            user_states[user_id]['destination'] = text
            finalize_and_save(user_id, display_name)
            summary = build_summary(user_states[user_id], display_name)
            del user_states[user_id]
            reply_text(reply_token, summary)
            return

        # ──── 社内作業内容の入力 ────
        if current_state == 'waiting_for_work_content':
            user_states[user_id]['work_content'] = text
            finalize_and_save(user_id, display_name)
            summary = build_summary(user_states[user_id], display_name)
            del user_states[user_id]
            reply_text(reply_token, summary)
            return

        # ──── 工場対応内容の入力 ────
        if current_state == 'waiting_for_factory_content':
            user_states[user_id]['factory_content'] = text
            finalize_and_save(user_id, display_name)
            summary = build_summary(user_states[user_id], display_name)
            del user_states[user_id]
            reply_text(reply_token, summary)
            return

    except Exception as e:
        logger.error(f'メッセージ処理エラー: {e}')
        reply_error(reply_token)


@handler.add(PostbackEvent)
def handle_postback(event):
    """ボタンタップ時のポストバックイベントを処理する"""
    user_id       = event.source.user_id
    reply_token   = event.reply_token
    postback_data = event.postback.data

    try:
        # ポストバックデータをパース（例: "action=商談&time_slot=午前"）
        params    = dict(item.split('=', 1) for item in postback_data.split('&'))
        action    = params.get('action', '')
        time_slot = params.get('time_slot', '午前')

        # アクションに対応する最初の入力待ち状態を取得
        first_state = ACTION_FIRST_STATE.get(action)
        if not first_state:
            logger.warning(f'未定義のアクション: {action}')
            reply_text(reply_token, '少し待ってからもう一度お試しください🙏')
            return

        # ユーザー状態を初期化
        user_states[user_id] = {
            'state':           first_state,
            'time_slot':       time_slot,
            'action':          action,
            'company':         '',
            'destination':     '',
            'work_content':    '',
            'factory_content': '',
            'memo':            '',
        }

        # 最初の質問を送信
        reply_text(reply_token, STATE_QUESTION[first_state])

    except Exception as e:
        logger.error(f'ポストバック処理エラー: {e}')
        reply_error(reply_token)


# ─────────────────────────────────────
# 起動
# ─────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
