import calendar
import os
import json
import logging
import threading

from datetime import date, datetime, timedelta
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
    QuickReply,
    QuickReplyItem,
    DatetimePickerAction,
    PostbackAction,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    PostbackEvent,
    FollowEvent,
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

# スプレッドシート接続キャッシュ
_spreadsheet_cache = None
_spreadsheet_lock = threading.Lock()

# 管理者LINE User ID（日報レポート送信先）
LINE_USER_ID = os.environ.get('LINE_USER_ID', '')

# タイムゾーン（日本標準時）
JST = ZoneInfo('Asia/Tokyo')

# ─────────────────────────────────────
# 定数定義
# ─────────────────────────────────────

# 日報ボタンの定義（label: 表示名, value: 記録値）
ACTIONS = [
    {'label': '商談',          'value': '商談'},
    {'label': '移動・外出',    'value': '移動・外出'},
    {'label': 'メーカー訪問',  'value': 'メーカー訪問'},
    {'label': '展示会・イベント', 'value': '展示会・イベント'},
    {'label': '社内作業',      'value': '社内作業'},
    {'label': '工場対応',      'value': '工場対応'},
]

# アクション名に対応する絵文字（週次レポートの表示用）
ACTION_EMOJI = {
    '商談':              '',
    '移動・外出':        '',
    'メーカー訪問':      '',
    '展示会・イベント':  '',
    '社内作業':          '',
    '工場対応':          '',
}

# アクション名の短縮表示マッピング（レポートで長い名前を簡略化）
ACTION_SHORT = {
    '移動・外出':        '移動',
    '展示会・イベント':  '展示会',
}

# ─────────────────────────────────────
# 問い合わせ自動返信キーワード定義
# ─────────────────────────────────────
# 各要素: ( [マッチキーワードリスト], 返信メッセージ )
# テキストにキーワードが1つでも含まれれば対応する返信を返す（先頭優先）
INQUIRY_KEYWORDS: list[tuple[list[str], str]] = [
    (['休暇', '有給'],  '休暇・有給申請は上長に口頭またはLINEで事前連絡してください。'),
    (['遅刻', '早退'],  '遅刻・早退の場合は出勤前に上長へLINEで連絡してください。'),
    (['日報', '提出'],  '日報はこのLINEBotに毎日送信してください。'),
]
# どのキーワードにもマッチしない場合のデフォルト返信
INQUIRY_DEFAULT_REPLY = INQUIRY_DEFAULT_REPLY = ''

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
    'waiting_for_company':        '商談内容を入力してください（例：〇〇社訪問、社内商談、オンライン商談など）',
    'waiting_for_memo':           '自由メモがあれば入力してください\n（スキップする場合は「スキップ」と送信）',
    'waiting_for_destination':    '移動先・目的地を入力してください',
    'waiting_for_work_content':   '作業内容を入力してください',
    'waiting_for_factory_content':'対応内容を入力してください',
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
    """Google Sheetsへの接続を取得する（接続をキャッシュして再認証を省略）"""
    global _spreadsheet_cache
    with _spreadsheet_lock:
        if _spreadsheet_cache is not None:
            try:
                _ = _spreadsheet_cache.id
                return _spreadsheet_cache
            except Exception:
                logger.warning('スプレッドシートキャッシュが無効。再接続します')
                _spreadsheet_cache = None
        credentials_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials.from_service_account_info(credentials_dict, scopes=GOOGLE_SCOPES)
        client = gspread.authorize(creds)
        _spreadsheet_cache = client.open_by_key(GOOGLE_SHEET_ID)
        return _spreadsheet_cache


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
            f'{display_name}さんを登録しました！\n'
            '毎日11時55分と18時00分に日報リマインダーを送ります\n'
            'ボタンをタップして1〜2分で入力できます。'
        )

    spreadsheet = get_spreadsheet()
    users_sheet = get_or_create_sheet(
        spreadsheet, 'users', ['LINE表示名', 'ユーザーID', '登録日']
    )
    records = users_sheet.get_all_records()

    if any(r.get('ユーザーID') == user_id for r in records):
        return f'{display_name}さんはすでに登録済みです！'

    today = datetime.now(JST).strftime('%Y/%m/%d')
    users_sheet.append_row([display_name, user_id, today])
    return (
        f'{display_name}さんを登録しました！\n'
        '毎日11時55分と18時00分に日報リマインダーを送ります\n'
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


def save_inquiry(display_name: str, message: str, auto_reply: str):
    """問い合わせ内容をスプレッドシートの「問い合わせ」シートに記録する。
    スプレッドシート連携が無効な場合はログのみ出力する。"""
    now = datetime.now(JST)
    row = [
        now.strftime('%Y/%m/%d %H:%M'),
        display_name,
        message,
        auto_reply,
    ]
    if not SHEETS_ENABLED:
        logger.info(f'[SHEETS無効] 問い合わせ記録: {row}')
        return
    spreadsheet = get_spreadsheet()
    sheet = get_or_create_sheet(
        spreadsheet, '問い合わせ',
        ['日時', '名前', 'メッセージ内容', '自動返信内容']
    )
    sheet.append_row(row)
    logger.info(f'問い合わせ記録完了: {display_name} / {message[:30]}')


def notify_admin_inquiry(display_name: str, message: str):
    """不明な問い合わせを全管理者にプッシュ通知する。
    管理者が未登録の場合はスキップする。"""
    notify_text = f'【問い合わせ】{display_name}さんからメッセージ：{message}'
    try:
        _push_to_admins(notify_text)
        logger.info(f'管理者への問い合わせ通知完了: {display_name}')
    except Exception as e:
        logger.error(f'管理者通知エラー: {e}')


# ─────────────────────────────────────
# LINE API ヘルパー
# ─────────────────────────────────────

def get_display_name(user_id: str) -> str:
    """LINE APIからユーザーの表示名を取得する。
    失敗した場合はusersシートの登録名にフォールバックする。"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            return profile.display_name
    except Exception as e:
        logger.error(f'プロフィール取得エラー ({user_id}): {e}')
        # フォールバック: usersシートに登録済みなら登録名を返す
        try:
            users = get_all_users()
            for u in users:
                if u['id'] == user_id:
                    logger.info(f'usersシートから名前を取得: {u["name"]}')
                    return u['name']
        except Exception:
            pass
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
        reply_text(reply_token, '少し待ってからもう一度お試しください')
    except Exception as e:
        logger.error(f'エラー返信にも失敗: {e}')

# ─────────────────────────────────────
# Flex Message 生成
# ─────────────────────────────────────

def create_flex_message(time_slot: str) -> FlexMessage:
    """日報入力用Flex Messageを作成する"""
    # 時間帯でヘッダー色・タイトル・サブテキストを変える
    if time_slot == '午前':
        header_color = '#FF8C00'      # オレンジ（朝）
        title        = '午前の日報入力'
        sub_text     = '今の活動を選んでください'
    else:
        header_color = '#1565C0'      # ブルー（午後）
        title        = '午後の日報入力'
        sub_text     = '今日も一日お疲れ様でした。\n今の活動を選んでください'

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
                    'text': sub_text,
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


def create_help_flex_message() -> FlexMessage:
    """使い方ガイド用Flex Messageを作成する"""

    def section_header(title: str, bg_color: str) -> dict:
        return {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': bg_color,
            'paddingAll': '6px',
            'cornerRadius': '4px',
            'contents': [{
                'type': 'text',
                'text': title,
                'color': '#FFFFFF',
                'weight': 'bold',
                'size': 'sm',
            }],
        }

    def cmd_row(cmd: str, desc: str) -> dict:
        return {
            'type': 'box',
            'layout': 'horizontal',
            'spacing': 'sm',
            'alignItems': 'center',
            'paddingTop': '4px',
            'contents': [
                {
                    'type': 'box',
                    'layout': 'vertical',
                    'width': '52px',
                    'backgroundColor': '#2980B9',
                    'paddingAll': '4px',
                    'cornerRadius': '4px',
                    'contents': [{
                        'type': 'text',
                        'text': cmd,
                        'color': '#FFFFFF',
                        'size': 'xs',
                        'weight': 'bold',
                        'align': 'center',
                        'wrap': True,
                    }],
                },
                {
                    'type': 'text',
                    'text': '\u2192',
                    'color': '#95A5A6',
                    'size': 'sm',
                    'flex': 0,
                },
                {
                    'type': 'text',
                    'text': desc,
                    'size': 'sm',
                    'color': '#34495E',
                    'wrap': True,
                    'flex': 1,
                },
            ],
        }

    def schedule_row(time: str, desc: str) -> dict:
        return {
            'type': 'box',
            'layout': 'horizontal',
            'spacing': 'sm',
            'alignItems': 'center',
            'paddingTop': '4px',
            'contents': [
                {
                    'type': 'text',
                    'text': time,
                    'size': 'xs',
                    'weight': 'bold',
                    'color': '#E67E22',
                    'flex': 0,
                    'minWidth': '80px',
                },
                {
                    'type': 'text',
                    'text': desc,
                    'size': 'sm',
                    'color': '#34495E',
                    'wrap': True,
                },
            ],
        }

    def inquiry_row(keyword: str, desc: str) -> dict:
        return {
            'type': 'box',
            'layout': 'horizontal',
            'spacing': 'sm',
            'alignItems': 'center',
            'paddingTop': '4px',
            'contents': [
                {
                    'type': 'box',
                    'layout': 'vertical',
                    'width': '72px',
                    'backgroundColor': '#27AE60',
                    'paddingAll': '4px',
                    'cornerRadius': '4px',
                    'contents': [{
                        'type': 'text',
                        'text': keyword,
                        'color': '#FFFFFF',
                        'size': 'xs',
                        'align': 'center',
                        'wrap': True,
                    }],
                },
                {
                    'type': 'text',
                    'text': '\u2192',
                    'color': '#95A5A6',
                    'size': 'sm',
                    'flex': 0,
                },
                {
                    'type': 'text',
                    'text': desc,
                    'size': 'sm',
                    'color': '#34495E',
                    'wrap': True,
                    'flex': 1,
                },
            ],
        }

    flex_dict = {
        'type': 'bubble',
        'size': 'giga',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': '#2C3E50',
            'paddingAll': '16px',
            'contents': [
                {
                    'type': 'text',
                    'text': '使い方ガイド',
                    'color': '#FFFFFF',
                    'weight': 'bold',
                    'size': 'xl',
                },
                {
                    'type': 'text',
                    'text': '日報ボットの操作方法',
                    'color': '#BDC3C7',
                    'size': 'sm',
                    'margin': 'xs',
                },
            ],
        },
        'body': {
            'type': 'box',
            'layout': 'vertical',
            'spacing': 'sm',
            'paddingAll': '16px',
            'contents': [
                # ── 基本コマンド ──
                section_header('基本コマンド', '#2980B9'),
                cmd_row('登録',   'ユーザー登録'),
                cmd_row('確認',   '日報の照会\n1日分・月別・期間指定'),
                cmd_row('使い方', 'この説明を表示'),{'type': 'separator', 'margin': 'md'},
                section_header('休暇・有給', '#8E44AD'),
                cmd_row('休暇申請',   '休暇の申請'),
                cmd_row('有給残日数', '残日数を確認'),
                cmd_row('申請履歴',   '直近5件を表示'),
                {'type': 'separator', 'margin': 'md'},
                section_header('日報', '#E74C3C'),
                cmd_row('日報入力',   '午前・午後の日報を入力'),
                {'type': 'separator', 'margin': 'md'},
                # ── 自動送信スケジュール ──
                section_header('自動送信スケジュール', '#E67E22'),
                schedule_row('平日 11:55', '午前リマインダー'),
                schedule_row('平日 18:00', '午後リマインダー'),
                schedule_row('金曜 18:15', '週次レポート'),
                {'type': 'separator', 'margin': 'md'},
                # ── 問い合わせ ──
                section_header('問い合わせ', '#27AE60'),
                inquiry_row('休暇/有給\n遅刻/早退', '自動案内'),
                inquiry_row('その他',              '担当者に転送'),
            ],
        },
    }

    return FlexMessage(
        alt_text='使い方ガイド',
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
        '記録しました！',
        '━━━━━━━━━━━',
        f'{display_name}',
        f'{state["time_slot"]}',
        f'{state["action"]}',
    ]
    if state.get('company'):
        lines.append(f'訪問先: {state["company"]}')
    if state.get('destination'):
        lines.append(f'移動先: {state["destination"]}')
    if state.get('work_content'):
        lines.append(f'作業内容: {state["work_content"]}')
    if state.get('factory_content'):
        lines.append(f'対応内容: {state["factory_content"]}')
    if state.get('memo'):
        lines.append(f'メモ: {state["memo"]}')
    return '\n'.join(lines)


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


def get_admins() -> list[dict]:
    """管理者シートから全管理者の {id, name} リストを返す。
    スプレッドシート連携が無効な場合は環境変数 LINE_USER_ID をフォールバックとして使う。"""
    if not SHEETS_ENABLED:
        if LINE_USER_ID:
            return [{'id': LINE_USER_ID, 'name': '管理者'}]
        return []
    spreadsheet  = get_spreadsheet()
    admin_sheet  = get_or_create_sheet(
        spreadsheet, '管理者', ['LINE_USER_ID', '名前']
    )
    records = admin_sheet.get_all_records()
    return [
        {'id': r['LINE_USER_ID'], 'name': r.get('名前', '')}
        for r in records
        if r.get('LINE_USER_ID')
    ]


def get_admin_ids() -> list[str]:
    """管理者のLINEユーザーIDリストを返す"""
    return [a['id'] for a in get_admins()]


def is_admin(user_id: str) -> bool:
    """指定ユーザーが管理者かどうかを返す"""
    return user_id in get_admin_ids()


def _push_to_admins(message: str):
    """全管理者に同じテキストメッセージをプッシュ送信する"""
    admin_ids = get_admin_ids()
    if not admin_ids:
        logger.warning('管理者が未登録のため送信をスキップ')
        return
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for uid in admin_ids:
            try:
                api.push_message(PushMessageRequest(
                    to=uid,
                    messages=[TextMessage(text=message)],
                ))
            except Exception as e:
                logger.error(f'管理者へのプッシュ失敗 ({uid}): {e}')


# ─────────────────────────────────────
# 有給・休暇申請ヘルパー
# ─────────────────────────────────────

def _calculate_leave_entitlement(hire_date: date) -> int:
    """入社日から法定有給付与日数を計算する（労基法115条の2に基づく）"""
    today  = datetime.now(JST).date()
    months = (today.year - hire_date.year) * 12 + (today.month - hire_date.month)
    if today.day < hire_date.day:
        months -= 1
    if months < 6:   return 0
    if months < 18:  return 10
    if months < 30:  return 11
    if months < 42:  return 12
    if months < 54:  return 14
    if months < 66:  return 16
    if months < 78:  return 18
    return 20


def get_leave_balance_sheet():
    """有給管理シートを取得または作成する"""
    spreadsheet = get_spreadsheet()
    return get_or_create_sheet(
        spreadsheet, '有給管理',
        ['名前', 'LINE_USER_ID', '入社日', '付与日数', '使用日数', '残日数', '最終付与日'],
    )


def get_leave_application_sheet():
    """休暇申請シートを取得または作成する"""
    spreadsheet = get_spreadsheet()
    return get_or_create_sheet(
        spreadsheet, '休暇申請',
        ['申請日時', '名前', '申請種別', '開始日', '終了日', '日数', '理由',
         'ステータス', '承認者', '承認日時'],
    )


def _get_user_id_by_name(name: str) -> str:
    """usersシートから表示名でLINEユーザーIDを検索する"""
    for u in get_all_users():
        if u['name'] == name:
            return u['id']
    return ''


def _get_leave_balance(user_id: str) -> dict | None:
    """有給管理シートからユーザーの有給情報を取得する。未登録なら None を返す"""
    sheet   = get_leave_balance_sheet()
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if r.get('LINE_USER_ID') == user_id:
            return {'row': i + 2, 'data': r}
    return None


def _submit_leave_application(
    display_name: str, leave_type: str,
    start_date: str, end_date: str, days: int, reason: str,
) -> int:
    """休暇申請シートに申請行を追加し、挿入した行番号を返す"""
    sheet            = get_leave_application_sheet()
    rows_before      = len(sheet.get_all_records())
    now_str          = datetime.now(JST).strftime('%Y/%m/%d %H:%M')
    sheet.append_row([now_str, display_name, leave_type,
                      start_date, end_date, days, reason, '申請中', '', ''])
    return rows_before + 2  # 行1=ヘッダー、データは行2〜


def _notify_admins_leave(
    display_name: str, leave_type: str,
    start_date: str, end_date: str, days: int, reason: str, row_num: int,
):
    """全管理者に休暇申請通知を送信する（承認・却下ボタン付き）"""
    admin_ids = get_admin_ids()
    if not admin_ids:
        logger.warning('管理者未登録のため休暇申請通知をスキップ')
        return
    msg_text = (
        f'休暇申請が届きました\n\n'
        f'申請者：{display_name}\n'
        f'種別：{leave_type}\n'
        f'期間：{start_date}〜{end_date}（{days}日）\n'
        f'理由：{reason}'
    )
    quick_reply = QuickReply(items=[
        QuickReplyItem(action=PostbackAction(
            label='承認',
            data=f'action=leave_approve&row={row_num}',
            display_text='承認',
        )),
        QuickReplyItem(action=PostbackAction(
            label='却下',
            data=f'action=leave_reject&row={row_num}',
            display_text='却下',
        )),
    ])
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for uid in admin_ids:
            try:
                api.push_message(PushMessageRequest(
                    to=uid,
                    messages=[TextMessage(text=msg_text, quick_reply=quick_reply)],
                ))
            except Exception as e:
                logger.error(f'申請通知送信失敗 ({uid}): {e}')


def _process_leave_decision(row_num: int, admin_id: str, approved: bool) -> str:
    """休暇申請の承認・却下を処理し、本人に通知する。結果メッセージを返す"""
    try:
        app_sheet = get_leave_application_sheet()
        row_data  = app_sheet.row_values(row_num)
        if not row_data or len(row_data) < 7:
            return '申請データが見つかりません。'

        current_status = row_data[7] if len(row_data) > 7 else ''
        if current_status in ('承認', '却下'):
            return f'この申請はすでに{current_status}済みです。'

        app_name   = row_data[1]
        app_type   = row_data[2]
        start_date = row_data[3]
        end_date   = row_data[4]
        try:
            app_days = int(float(row_data[5]))
        except (ValueError, IndexError):
            app_days = 0

        new_status = '承認' if approved else '却下'
        admin_name = get_display_name(admin_id)
        now_str    = datetime.now(JST).strftime('%Y/%m/%d %H:%M')

        app_sheet.update_cell(row_num, 8,  new_status)
        app_sheet.update_cell(row_num, 9,  admin_name)
        app_sheet.update_cell(row_num, 10, now_str)

        # 有給承認時は残日数を減算
        if approved and app_type == '有給' and app_days > 0:
            bal_sheet   = get_leave_balance_sheet()
            bal_records = bal_sheet.get_all_records()
            for i, r in enumerate(bal_records):
                if r.get('名前') == app_name:
                    used      = int(float(str(r.get('使用日数', 0)))) + app_days
                    remaining = int(float(str(r.get('残日数', 0)))) - app_days
                    bal_sheet.update_cell(i + 2, 5, used)
                    bal_sheet.update_cell(i + 2, 6, remaining)
                    break

        # 申請者に結果を通知
        applicant_id = _get_user_id_by_name(app_name)
        if applicant_id:
            if approved:
                notify_text = (
                    f'休暇申請が承認されました。\n'
                    f'種別：{app_type}\n'
                    f'期間：{start_date}〜{end_date}（{app_days}日）\n'
                    f'承認者：{admin_name}'
                )
            else:
                notify_text = (
                    f'休暇申請が却下されました。\n'
                    f'種別：{app_type}\n'
                    f'期間：{start_date}〜{end_date}\n'
                    f'担当者にご確認ください。'
                )
            try:
                with ApiClient(configuration) as api_client:
                    MessagingApi(api_client).push_message(PushMessageRequest(
                        to=applicant_id,
                        messages=[TextMessage(text=notify_text)],
                    ))
            except Exception as e:
                logger.error(f'申請者への結果通知失敗 ({applicant_id}): {e}')

        return f'{app_name}さんの申請を{new_status}しました。'

    except Exception as e:
        logger.error(f'申請決裁処理エラー: {e}')
        return 'エラーが発生しました。もう一度お試しください。'



def _build_report_text(name: str, date_sheet: str, date_label: str) -> str:
    """指定ユーザー・日付の日報テキストを返す。未提出の場合はその旨を返す。"""
    records = get_reports_by_date_range([date_sheet])
    user_records = [r for r in records if r.get('ユーザー名') == name]
    if not user_records:
        return f'{name}さんの{date_label}の日報\n\n未提出です。'
    lines = [f'{name}さんの{date_label}の日報\n']
    for rec in sorted(user_records, key=lambda r: r.get('午前or午後', '')):
        slot  = rec.get('午前or午後', '')
        label = format_action_label(rec, with_emoji=True)
        lines.append(f'{slot}：{label}')
    return '\n'.join(lines)


# ─────────────────────────────────────
# 期間照会ヘルパー
# ─────────────────────────────────────

def _generate_date_list(start: date, end: date) -> list[str]:
    """startからendまでの日付リストを'YYYY/MM/DD'形式で返す"""
    result  = []
    current = start
    while current <= end:
        result.append(current.strftime('%Y/%m/%d'))
        current += timedelta(days=1)
    return result


def _get_month_dates(year: int, month: int) -> tuple[list[str], str]:
    """指定年月のすべての日付リストとラベルを返す"""
    _, last_day = calendar.monthrange(year, month)
    first = date(year, month, 1)
    last  = date(year, month, last_day)
    return _generate_date_list(first, last), f'{year}年{month}月'


def _get_current_month_dates() -> tuple[list[str], str]:
    """今月のすべての日付リストとラベルを返す"""
    today = datetime.now(JST).date()
    return _get_month_dates(today.year, today.month)


def _get_last_month_dates() -> tuple[list[str], str]:
    """先月のすべての日付リストとラベルを返す"""
    today = datetime.now(JST)
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    return _get_month_dates(year, month)


def _build_range_report_text(name: str, date_strs: list[str], label: str) -> str:
    """指定ユーザー・日付リストの日報テキストを返す（複数日対応）"""
    records      = get_reports_by_date_range(date_strs)
    user_records = [r for r in records if r.get('ユーザー名') == name]

    lines = [f'{name}さんの日報', label, '']

    if not user_records:
        lines.append('この期間のデータはありません。')
        return '\n'.join(lines)

    by_date: dict[str, dict] = {}
    for rec in user_records:
        d    = rec.get('日付', '')
        slot = rec.get('午前or午後', '')
        if d not in by_date:
            by_date[d] = {}
        by_date[d][slot] = rec

    for date_str in date_strs:
        if date_str not in by_date:
            continue
        slots  = by_date[date_str]
        parts  = date_str.split('/')
        d_lbl  = f'{int(parts[1])}/{int(parts[2])}'
        slot_texts = [
            f'{slot}:{format_action_label(slots[slot])}'
            for slot in ['午前', '午後'] if slot in slots
        ]
        lines.append(f'{d_lbl} {" ".join(slot_texts)}')

    return '\n'.join(lines)


def _reply_range_or_select(reply_token: str, user_id: str, date_strs: list[str], label: str):
    """範囲レポートを管理者には選択UI、一般ユーザーには直接返信する"""
    if not date_strs:
        reply_text(reply_token, '対象日付が取得できませんでした。')
        return

    start_str = date_strs[0].replace('/', '-')   # YYYY/MM/DD → YYYY-MM-DD
    end_str   = date_strs[-1].replace('/', '-')

    if is_admin(user_id):
        users = get_all_users()
        items = [
            QuickReplyItem(
                action=PostbackAction(
                    label='全員',
                    data=f'action=view_range_all&start={start_str}&end={end_str}',
                    display_text='全員',
                )
            )
        ]
        for u in users:
            items.append(QuickReplyItem(
                action=PostbackAction(
                    label=u['name'],
                    data=f'action=view_range_user&start={start_str}&end={end_str}&name={u["name"]}',
                    display_text=u['name'],
                )
            ))
        quick_reply = QuickReply(items=items)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(
                    text=f'{label}の日報を確認するユーザーを選んでください',
                    quick_reply=quick_reply,
                )],
            ))
    else:
        display_name = get_display_name(user_id)
        report_text  = _build_range_report_text(display_name, date_strs, label)
        reply_text(reply_token, report_text)


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
def daily_report_to_admin():
    """平日18:30にcron-job.orgから呼ばれるエンドポイント。
    当日の全ユーザーの日報をまとめて管理者にプッシュ送信する。"""
    logger.info('日次レポート送信開始')

    try:
        today     = datetime.now(JST).date()
        date_str  = today.strftime('%Y/%m/%d')
        date_label = f'{today.month}月{today.day}日'

        # 当日の全日報データを取得
        records = get_reports_by_date_range([date_str])

        # 登録ユーザー一覧を取得
        all_users = get_all_users()

        if not all_users:
            logger.info('登録ユーザーが0名のため日次レポート送信をスキップ')
            return jsonify({'status': 'ok', 'message': 'ユーザーなし'})

        # ユーザーごとに日報を整理
        lines = [f'📋 {date_label}の日報レポート\n']

        submitted_users = set()
        by_user = {}
        for rec in records:
            name = rec.get('ユーザー名', '')
            if name:
                submitted_users.add(name)
                by_user.setdefault(name, []).append(rec)

        # 提出済みユーザー
        for name in sorted(by_user.keys()):
            user_records = by_user[name]
            slot_texts = []
            for rec in sorted(user_records, key=lambda r: r.get('午前or午後', '')):
                slot  = rec.get('午前or午後', '')
                label = format_action_label(rec, with_emoji=True)
                slot_texts.append(f'{slot}:{label}')
            lines.append(f'✅ {name}')
            lines.append(f'  {" / ".join(slot_texts)}')

        # 未提出ユーザー
        not_submitted = [
            u['name'] for u in all_users
            if u['name'] not in submitted_users
        ]
        if not_submitted:
            lines.append('')
            lines.append('⚠️ 未提出')
            for name in not_submitted:
                lines.append(f'  ・{name}')

        # 統計
        lines.append('')
        lines.append(
            f'提出: {len(submitted_users)}/{len(all_users)}名 '
            f'({round(len(submitted_users)/len(all_users)*100)}%)'
        )

        report_text = '\n'.join(lines)
        _push_to_admins(report_text)
        logger.info('日次レポート送信完了')

        return jsonify({'status': 'ok', 'message': '日次レポートを送信しました'})

    except Exception as e:
        logger.error(f'日次レポートエラー: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/weekly', methods=['GET', 'POST'])
def weekly_report_to_admin():
    """金曜18:15にcron-job.orgから呼ばれるエンドポイント。
    今週（月〜金）の全ユーザーの日報サマリーを管理者にプッシュ送信する。"""
    logger.info('週次レポート送信開始')

    try:
        today    = datetime.now(JST).date()
        # 今週の月曜日を起点にする
        monday   = today - timedelta(days=today.weekday())
        friday   = monday + timedelta(days=4)

        # 月〜金の日付リスト（平日のみ）
        date_strs = []
        current = monday
        while current <= min(friday, today):
            if current.weekday() < 5:
                date_strs.append(current.strftime('%Y/%m/%d'))
            current += timedelta(days=1)

        week_label = (
            f'{monday.month}/{monday.day}〜{friday.month}/{friday.day}'
        )

        # 全日報データを取得
        records = get_reports_by_date_range(date_strs)

        # 登録ユーザー一覧
        all_users = get_all_users()

        if not all_users:
            logger.info('登録ユーザーが0名のため週次レポート送信をスキップ')
            return jsonify({'status': 'ok', 'message': 'ユーザーなし'})

        total_days = len(date_strs)

        lines = [f'📊 週次レポート（{week_label}）\n']

        # ユーザーごとに集計
        from collections import Counter
        for user in sorted(all_users, key=lambda u: u['name']):
            name = user['name']
            user_records = [
                r for r in records if r.get('ユーザー名') == name
            ]

            # 提出日数（ユニークな日付数）
            submitted_dates = set(r.get('日付', '') for r in user_records)
            submitted_count = len(submitted_dates)

            if submitted_count == 0:
                lines.append(f'❌ {name}: 提出なし')
                continue

            rate = round(submitted_count / total_days * 100)

            # アクション別の回数集計
            action_counter = Counter(
                r.get('行動種別', '') for r in user_records
            )
            top_actions = action_counter.most_common(3)
            action_summary = ' '.join(
                f'{ACTION_EMOJI.get(a, "")}{ACTION_SHORT.get(a, a)}({c})'
                for a, c in top_actions
            )

            icon = '✅' if rate >= 80 else '⚠️'
            lines.append(
                f'{icon} {name}: {submitted_count}/{total_days}日({rate}%) '
                f'{action_summary}'
            )

        # 全体統計
        all_submitted = set(r.get('ユーザー名', '') for r in records)
        lines.append('')
        lines.append(
            f'全体: {len(all_submitted)}/{len(all_users)}名が1件以上提出'
        )

        report_text = '\n'.join(lines)
        _push_to_admins(report_text)
        logger.info('週次レポート送信完了')

        return jsonify({'status': 'ok', 'message': '週次レポートを送信しました'})

    except Exception as e:
        logger.error(f'週次レポートエラー: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ─────────────────────────────────────
# LINEイベントハンドラ
# ─────────────────────────────────────

@handler.add(FollowEvent)
def handle_follow(event):
    """友達追加時のウェルカムフロー"""
    reply_token = event.reply_token
    quick_reply = QuickReply(items=[
        QuickReplyItem(
            action=PostbackAction(
                label='登録する',
                data='action=follow_register',
                display_text='登録する',
            )
        ),
        QuickReplyItem(
            action=PostbackAction(
                label='後で登録する',
                data='action=follow_skip',
                display_text='後で登録する',
            )
        ),
    ])
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(
                    text='日報システムへようこそ！\nユーザー登録しますか？',
                    quick_reply=quick_reply,
                )],
            ))
    except Exception as e:
        logger.error(f'handle_follow エラー: {e}')


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """テキストメッセージを処理する"""
    user_id     = event.source.user_id
    text        = event.message.text.strip()
    reply_token = event.reply_token

    try:
        # ──── 日報入力コマンド ────
        if text == '日報入力':
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=PostbackAction(
                    label='午前', data='action=午前リマインダー&time_slot=午前', display_text='午前',
                )),
                QuickReplyItem(action=PostbackAction(
                    label='午後', data='action=午後リマインダー&time_slot=午後', display_text='午後',
                )),
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text='午前・午後どちらの日報ですか？',
                        quick_reply=quick_reply,
                    )],
                ))
            return
        # ──── 登録コマンド ────
        if text == '登録':
            display_name = get_display_name(user_id)
            result_msg   = register_user(user_id, display_name)
            reply_text(reply_token, result_msg)
            return

        # ──── ヘルプコマンド ────
        if text.lower() in ('使い方', 'ヘルプ', 'help'):
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[create_help_flex_message()],
                ))
            return

        # ──── 確認コマンド（日報照会） ────
        if text == '確認':
            quick_reply = QuickReply(items=[
                QuickReplyItem(
                    action=PostbackAction(
                        label='1日分',
                        data='action=view_1day',
                        display_text='1日分',
                    )
                ),
                QuickReplyItem(
                    action=PostbackAction(
                        label='月別',
                        data='action=view_month_menu',
                        display_text='月別',
                    )
                ),
                QuickReplyItem(
                    action=PostbackAction(
                        label='期間指定',
                        data='action=view_range_start',
                        display_text='期間指定',
                    )
                ),
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text='照会方法を選んでください',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 休暇申請コマンド ────
        if text == '休暇申請':
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=PostbackAction(
                    label='有給',     data='action=leave_type_select&type=有給',     display_text='有給',
                )),
                QuickReplyItem(action=PostbackAction(
                    label='欠勤',     data='action=leave_type_select&type=欠勤',     display_text='欠勤',
                )),
                QuickReplyItem(action=PostbackAction(
                    label='山休み',   data='action=leave_type_select&type=山休み',   display_text='山休み',
                )),
                QuickReplyItem(action=PostbackAction(
                    label='その他',   data='action=leave_type_select&type=その他',   display_text='その他',
                )),
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text='申請種別を選んでください',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 有給残日数コマンド ────
        if text == '有給残日数':
            if not SHEETS_ENABLED:
                reply_text(reply_token, 'スプレッドシートが未設定のため確認できません。')
                return
            balance = _get_leave_balance(user_id)
            if balance is None:
                reply_text(reply_token, '有給管理に登録されていません。管理者にご確認ください。')
                return
            d = balance['data']
            hire_date_str = str(d.get('入社日', ''))
            try:
                hire_date = datetime.strptime(hire_date_str, '%Y/%m/%d').date()
            except ValueError:
                try:
                    hire_date = datetime.strptime(hire_date_str, '%Y-%m-%d').date()
                except ValueError:
                    reply_text(reply_token, '入社日の形式が正しくありません。管理者にご確認ください。')
                    return
            granted = _calculate_leave_entitlement(hire_date)
            display_name = get_display_name(user_id)
            used = 0
            try:
                app_sheet = get_leave_application_sheet()
                records   = app_sheet.get_all_records()
                for r in records:
                    if (r.get('名前') == display_name
                            and r.get('申請種別') == '有給'
                            and r.get('ステータス') == '承認'):
                        used += int(float(str(r.get('日数', 0))))
            except Exception as e:
                logger.error(f'使用日数集計エラー: {e}')
            remaining = max(0, granted - used)
            today  = datetime.now(JST).date()
            months = (today.year - hire_date.year) * 12 + (today.month - hire_date.month)
            if today.day < hire_date.day:
                months -= 1
            grant_schedule = [6, 18, 30, 42, 54, 66, 78]
            grant_days_map = {6:10, 18:11, 30:12, 42:14, 54:16, 66:18, 78:20}
            next_info = ''
            for gm in grant_schedule:
                if months < gm:
                    next_date = datetime(
                        hire_date.year + (hire_date.month + gm - 1) // 12,
                        (hire_date.month + gm - 1) % 12 + 1,
                        hire_date.day
                    ).date()
                    next_info = f'\n\n次回付与：{next_date.strftime("%Y/%m/%d")}\n（{grant_days_map[gm]}日付与予定）'
                    break
            reply_text(
                reply_token,
                f'{display_name}さんの有給情報\n\n'
                f'付与日数：{granted}日\n'
                f'使用日数：{used}日\n'
                f'残日数：{remaining}日'
                f'{next_info}'
            )
            return

        # ──── 申請履歴コマンド ────
        if text == '申請履歴':
            if not SHEETS_ENABLED:
                reply_text(reply_token, 'スプレッドシートが未設定のため確認できません。')
                return
            display_name  = get_display_name(user_id)
            app_sheet     = get_leave_application_sheet()
            records       = app_sheet.get_all_records()
            user_records  = [r for r in records if r.get('名前') == display_name]
            recent        = user_records[-5:]
            if not recent:
                reply_text(reply_token, '申請履歴がありません。')
                return
            lines = ['申請履歴（直近5件）\n']
            for r in reversed(recent):
                lines.append(
                    f'[{r.get("ステータス", "?")}] {r.get("申請種別", "?")} '
                    f'{r.get("開始日", "?")}〜{r.get("終了日", "?")}（{r.get("日数", "?")}日）'
                )
            reply_text(reply_token, '\n'.join(lines))
            return

        # ──── ユーザー状態を確認 ────
        state = user_states.get(user_id)

        if state is None:
            # 日報フロー外のメッセージ → 問い合わせとして処理
            display_name = get_display_name(user_id)

            # キーワードマッチング（先頭のルールを優先）
            auto_reply = None
            for keywords, reply_msg in INQUIRY_KEYWORDS:
                if any(kw in text for kw in keywords):
                    auto_reply = reply_msg
                    break

            if auto_reply is None:
                # どのキーワードにもマッチしない → 管理者に転送して送信者に通知
                forward_text = (
                    f'【スタッフからの質問】\n'
                    f'送信者：{display_name}\n'
                    f'内容：{text}'
                )
                try:
                    _push_to_admins(forward_text)
                except Exception as e:
                    logger.error(f'管理者転送エラー: {e}')
                reply_text(reply_token, 'メッセージを管理者に転送しました')
                save_inquiry(display_name, text, 'メッセージを管理者に転送しました')
                return

            # キーワードマッチ：スプレッドシートに記録してユーザーに自動返信
            save_inquiry(display_name, text, auto_reply)
            reply_text(reply_token, auto_reply)

            return

        current_state = state['state']
        display_name  = get_display_name(user_id)

        # ──── 休暇申請：理由の入力 ────
        if current_state == 'leave_reason':
            leave_type = state.get('leave_type', '')
            start_date = state.get('leave_start', '')
            end_date   = state.get('leave_end', '')
            days       = state.get('leave_days', 0)
            reason     = text
            del user_states[user_id]
            if SHEETS_ENABLED:
                row_num = _submit_leave_application(
                    display_name, leave_type, start_date, end_date, days, reason
                )
                _notify_admins_leave(
                    display_name, leave_type, start_date, end_date, days, reason, row_num
                )
            reply_text(
                reply_token,
                f'申請を受け付けました。\n'
                f'種別：{leave_type}\n'
                f'期間：{start_date}〜{end_date}（{days}日）\n'
                f'理由：{reason}\n\n'
                f'承認後にLINEでお知らせします。'
            )
            return

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
            summary = build_summary(user_states[user_id], display_name)
            reply_text(reply_token, summary)  # 先に返信（LINE 3秒タイムアウト対策）
            snap = dict(user_states[user_id])
            snap_dname = display_name
            del user_states[user_id]
            def _bg_save_memo(snap=snap, dname=snap_dname):
                try:
                    save_report(display_name=dname, time_slot=snap.get('time_slot',''),
                        action=snap.get('action',''), company=snap.get('company',''),
                        destination=snap.get('destination',''), work_content=snap.get('work_content',''),
                        factory_content=snap.get('factory_content',''), memo=snap.get('memo',''))
                except Exception as e:
                    logger.error(f'バックグラウンド保存エラー: {e}')
            threading.Thread(target=_bg_save_memo, daemon=True).start()
            return

        # ──── 移動先の入力 ────
        if current_state == 'waiting_for_destination':
            user_states[user_id]['destination'] = text
            summary = build_summary(user_states[user_id], display_name)
            reply_text(reply_token, summary)  # 先に返信（LINE 3秒タイムアウト対策）
            snap = dict(user_states[user_id])
            snap_dname = display_name
            del user_states[user_id]
            def _bg_save_dest(snap=snap, dname=snap_dname):
                try:
                    save_report(display_name=dname, time_slot=snap.get('time_slot',''),
                        action=snap.get('action',''), company=snap.get('company',''),
                        destination=snap.get('destination',''), work_content=snap.get('work_content',''),
                        factory_content=snap.get('factory_content',''), memo=snap.get('memo',''))
                except Exception as e:
                    logger.error(f'バックグラウンド保存エラー: {e}')
            threading.Thread(target=_bg_save_dest, daemon=True).start()
            return

        # ──── 社内作業内容の入力 ────
        if current_state == 'waiting_for_work_content':
            user_states[user_id]['work_content'] = text
            summary = build_summary(user_states[user_id], display_name)
            reply_text(reply_token, summary)  # 先に返信（LINE 3秒タイムアウト対策）
            snap = dict(user_states[user_id])
            snap_dname = display_name
            del user_states[user_id]
            def _bg_save_work(snap=snap, dname=snap_dname):
                try:
                    save_report(display_name=dname, time_slot=snap.get('time_slot',''),
                        action=snap.get('action',''), company=snap.get('company',''),
                        destination=snap.get('destination',''), work_content=snap.get('work_content',''),
                        factory_content=snap.get('factory_content',''), memo=snap.get('memo',''))
                except Exception as e:
                    logger.error(f'バックグラウンド保存エラー: {e}')
            threading.Thread(target=_bg_save_work, daemon=True).start()
            return

        # ──── 工場対応内容の入力 ────
        if current_state == 'waiting_for_factory_content':
            user_states[user_id]['factory_content'] = text
            summary = build_summary(user_states[user_id], display_name)
            reply_text(reply_token, summary)  # 先に返信（LINE 3秒タイムアウト対策）
            snap = dict(user_states[user_id])
            snap_dname = display_name
            del user_states[user_id]
            def _bg_save_factory(snap=snap, dname=snap_dname):
                try:
                    save_report(display_name=dname, time_slot=snap.get('time_slot',''),
                        action=snap.get('action',''), company=snap.get('company',''),
                        destination=snap.get('destination',''), work_content=snap.get('work_content',''),
                        factory_content=snap.get('factory_content',''), memo=snap.get('memo',''))
                except Exception as e:
                    logger.error(f'バックグラウンド保存エラー: {e}')
            threading.Thread(target=_bg_save_factory, daemon=True).start()
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
        data_params = dict(item.split('=', 1) for item in postback_data.split('&'))
        action      = data_params.get('action', '')
        time_slot   = data_params.get('time_slot', '午前')

        # ──── 日報照会：日付選択後 ────
        if action == 'date_selected':
            selected_date = (event.postback.params or {}).get('date', '')  # 'YYYY-MM-DD'
            if not selected_date:
                logger.error('date_selected: paramsにdateが含まれていません')
                reply_text(reply_token, '日付の取得に失敗しました。もう一度お試しください。')
                return
            date_sheet    = selected_date.replace('-', '/')  # 'YYYY/MM/DD'
            d             = datetime.strptime(selected_date, '%Y-%m-%d')
            date_label    = f'{d.month}月{d.day}日'

            if is_admin(user_id):
                # 管理者: 全員 or 個人名のクイックリプライを表示
                users = get_all_users()
                items = [
                    QuickReplyItem(
                        action=PostbackAction(
                            label='全員',
                            data=f'action=view_all&date={selected_date}',
                            display_text='全員',
                        )
                    )
                ]
                for u in users:
                    items.append(QuickReplyItem(
                        action=PostbackAction(
                            label=u['name'],
                            data=f'action=view_user&date={selected_date}&name={u["name"]}',
                            display_text=u['name'],
                        )
                    ))
                quick_reply = QuickReply(items=items)
                with ApiClient(configuration) as api_client:
                    MessagingApi(api_client).reply_message(ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(
                            text=f'{date_label}の日報を確認するユーザーを選んでください',
                            quick_reply=quick_reply,
                        )],
                    ))
            else:
                # 一般ユーザー: 自分の日報を表示
                display_name = get_display_name(user_id)
                report_text  = _build_report_text(display_name, date_sheet, date_label)
                reply_text(reply_token, report_text)
            return

        # ──── 日報照会：管理者が「全員」を選択 ────
        if action == 'view_all':
            selected_date = data_params.get('date', '')
            date_sheet    = selected_date.replace('-', '/')
            d             = datetime.strptime(selected_date, '%Y-%m-%d')
            date_label    = f'{d.month}月{d.day}日'
            users         = get_all_users()
            if not users:
                reply_text(reply_token, '登録ユーザーが0名です。')
                return
            records = get_reports_by_date_range([date_sheet])
            lines   = [f'{date_label}の日報一覧\n']
            for u in users:
                user_records = [r for r in records if r.get('ユーザー名') == u['name']]
                if not user_records:
                    lines.append(f'{u["name"]}：未提出')
                else:
                    parts = []
                    for rec in sorted(user_records, key=lambda r: r.get('午前or午後', '')):
                        slot  = rec.get('午前or午後', '')
                        label = format_action_label(rec, with_emoji=True)
                        parts.append(f'{slot}:{label}')
                    lines.append(f'{u["name"]}：{" ".join(parts)}')
            reply_text(reply_token, '\n'.join(lines))
            return

        # ──── 日報照会：管理者が個人名を選択 ────
        if action == 'view_user':
            selected_date = data_params.get('date', '')
            name          = data_params.get('name', '')
            date_sheet    = selected_date.replace('-', '/')
            d             = datetime.strptime(selected_date, '%Y-%m-%d')
            date_label    = f'{d.month}月{d.day}日'
            report_text   = _build_report_text(name, date_sheet, date_label)
            reply_text(reply_token, report_text)
            return

        # ──── 日報照会：1日分（日付ピッカー表示） ────
        if action == 'view_1day':
            quick_reply = QuickReply(items=[
                QuickReplyItem(
                    action=DatetimePickerAction(
                        label='日付を選ぶ',
                        data='action=date_selected',
                        mode='date',
                    )
                )
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text='照会する日付を選んでください',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 日報照会：月別メニュー表示 ────
        if action == 'view_month_menu':
            quick_reply = QuickReply(items=[
                QuickReplyItem(
                    action=PostbackAction(
                        label='今月',
                        data='action=month_view&period=current',
                        display_text='今月',
                    )
                ),
                QuickReplyItem(
                    action=PostbackAction(
                        label='先月',
                        data='action=month_view&period=last',
                        display_text='先月',
                    )
                ),
                QuickReplyItem(
                    action=DatetimePickerAction(
                        label='月を入力',
                        data='action=month_input_selected',
                        mode='date',
                    )
                ),
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text='期間を選んでください',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 日報照会：今月 or 先月 ────
        if action == 'month_view':
            period = data_params.get('period', 'current')
            if period == 'current':
                date_strs, label = _get_current_month_dates()
            else:
                date_strs, label = _get_last_month_dates()
            _reply_range_or_select(reply_token, user_id, date_strs, label)
            return

        # ──── 日報照会：月を入力（日付ピッカーで月選択） ────
        if action == 'month_input_selected':
            selected_date = (event.postback.params or {}).get('date', '')
            if not selected_date:
                logger.error('month_input_selected: paramsにdateが含まれていません')
                reply_text(reply_token, '日付の取得に失敗しました。もう一度お試しください。')
                return
            d             = datetime.strptime(selected_date, '%Y-%m-%d')
            date_strs, label = _get_month_dates(d.year, d.month)
            _reply_range_or_select(reply_token, user_id, date_strs, label)
            return

        # ──── 日報照会：期間指定（開始日ピッカー表示） ────
        if action == 'view_range_start':
            quick_reply = QuickReply(items=[
                QuickReplyItem(
                    action=DatetimePickerAction(
                        label='開始日を選ぶ',
                        data='action=range_start_selected',
                        mode='date',
                    )
                )
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text='開始日を選んでください',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 日報照会：開始日選択後（終了日ピッカー表示） ────
        if action == 'range_start_selected':
            start_str = (event.postback.params or {}).get('date', '')
            if not start_str:
                logger.error('range_start_selected: paramsにdateが含まれていません')
                reply_text(reply_token, '日付の取得に失敗しました。もう一度お試しください。')
                return
            start_d     = datetime.strptime(start_str, '%Y-%m-%d')
            start_label = f'{start_d.month}月{start_d.day}日'
            quick_reply = QuickReply(items=[
                QuickReplyItem(
                    action=DatetimePickerAction(
                        label='終了日を選ぶ',
                        data=f'action=range_end_selected&start={start_str}',
                        mode='date',
                    )
                )
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text=f'終了日を選んでください（開始日：{start_label}）',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 日報照会：終了日選択後（レポート生成） ────
        if action == 'range_end_selected':
            start_str = data_params.get('start', '')
            end_str   = (event.postback.params or {}).get('date', '')
            if not start_str or not end_str:
                logger.error('range_end_selected: 日付パラメータが不足しています')
                reply_text(reply_token, '日付の取得に失敗しました。もう一度お試しください。')
                return
            start_d = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_d   = datetime.strptime(end_str,   '%Y-%m-%d').date()
            if end_d < start_d:
                reply_text(reply_token, '終了日は開始日より後の日付を選んでください。')
                return
            date_strs = _generate_date_list(start_d, end_d)
            label     = f'{start_d.month}/{start_d.day}〜{end_d.month}/{end_d.day}'
            _reply_range_or_select(reply_token, user_id, date_strs, label)
            return

        # ──── 日報照会：管理者が範囲「全員」を選択 ────
        if action == 'view_range_all':
            start_str = data_params.get('start', '')
            end_str   = data_params.get('end', '')
            start_d   = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_d     = datetime.strptime(end_str,   '%Y-%m-%d').date()
            date_strs = _generate_date_list(start_d, end_d)
            label     = f'{start_d.month}/{start_d.day}〜{end_d.month}/{end_d.day}'
            users     = get_all_users()
            if not users:
                reply_text(reply_token, '登録ユーザーが0名です。')
                return
            records = get_reports_by_date_range(date_strs)
            lines   = [f'{label}の日報一覧\n']
            for u in users:
                uname        = u['name']
                user_records = [r for r in records if r.get('ユーザー名') == uname]
                lines.append(f'\n{uname}')
                if not user_records:
                    lines.append('  データなし')
                else:
                    by_date: dict[str, dict] = {}
                    for rec in user_records:
                        d    = rec.get('日付', '')
                        slot = rec.get('午前or午後', '')
                        if d not in by_date:
                            by_date[d] = {}
                        by_date[d][slot] = rec
                    for date_str in date_strs:
                        if date_str not in by_date:
                            continue
                        slots      = by_date[date_str]
                        parts      = date_str.split('/')
                        d_lbl      = f'{int(parts[1])}/{int(parts[2])}'
                        slot_texts = [
                            f'{slot}:{format_action_label(slots[slot])}'
                            for slot in ['午前', '午後'] if slot in slots
                        ]
                        lines.append(f'  {d_lbl} {" ".join(slot_texts)}')
            text = '\n'.join(lines)
            if len(text) > 4900:
                text = text[:4900] + '\n...(以下省略)'
            reply_text(reply_token, text)
            return

        # ──── 日報照会：管理者が範囲の個人名を選択 ────
        if action == 'view_range_user':
            start_str   = data_params.get('start', '')
            end_str     = data_params.get('end', '')
            name        = data_params.get('name', '')
            start_d     = datetime.strptime(start_str, '%Y-%m-%d').date()
            end_d       = datetime.strptime(end_str,   '%Y-%m-%d').date()
            date_strs   = _generate_date_list(start_d, end_d)
            label       = f'{start_d.month}/{start_d.day}〜{end_d.month}/{end_d.day}'
            report_text = _build_range_report_text(name, date_strs, label)
            reply_text(reply_token, report_text)
            return

        # ──── 友達追加：登録する ────
        if action == 'follow_register':
            display_name = get_display_name(user_id)
            result_msg   = register_user(user_id, display_name)
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[
                        TextMessage(text=result_msg),
                        create_help_flex_message(),
                    ],
                ))
            return

        if action in ('午前リマインダー', '午後リマインダー'):
            flex_msg = create_flex_message(time_slot)
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[flex_msg],
                ))
            return# ──── 友達追加：後で登録する ────
        if action == 'follow_skip':
            reply_text(reply_token, '登録する時は「登録」と送ってください。')
            return

        # ──── 休暇申請：種別選択 ────
        if action == 'leave_type_select':
            leave_type = data_params.get('type', '')
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=DatetimePickerAction(
                    label='開始日を選ぶ',
                    data=f'action=leave_start_selected&type={leave_type}',
                    mode='date',
                ))
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text=f'【{leave_type}】\n開始日を選んでください',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 休暇申請：開始日選択後 ────
        if action == 'leave_start_selected':
            leave_type = data_params.get('type', '')
            start_str  = (event.postback.params or {}).get('date', '')
            if not start_str:
                reply_text(reply_token, '日付の取得に失敗しました。もう一度お試しください。')
                return
            start_date = start_str.replace('-', '/')
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=DatetimePickerAction(
                    label='終了日を選ぶ',
                    data=f'action=leave_end_selected&type={leave_type}&start={start_date}',
                    mode='date',
                ))
            ])
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(
                        text=f'終了日を選んでください（開始日：{start_date}）',
                        quick_reply=quick_reply,
                    )],
                ))
            return

        # ──── 休暇申請：終了日選択後 ────
        if action == 'leave_end_selected':
            leave_type = data_params.get('type', '')
            start_date = data_params.get('start', '')
            end_str    = (event.postback.params or {}).get('date', '')
            if not end_str or not start_date:
                reply_text(reply_token, '日付の取得に失敗しました。もう一度お試しください。')
                return
            end_date = end_str.replace('-', '/')
            start_d  = datetime.strptime(start_date, '%Y/%m/%d').date()
            end_d    = datetime.strptime(end_date,   '%Y/%m/%d').date()
            if end_d < start_d:
                reply_text(reply_token, '終了日は開始日より後の日付を選んでください。')
                return
            days = (end_d - start_d).days + 1
            user_states[user_id] = {
                'state':       'leave_reason',
                'leave_type':  leave_type,
                'leave_start': start_date,
                'leave_end':   end_date,
                'leave_days':  days,
            }
            reply_text(
                reply_token,
                f'休暇理由を入力してください。\n'
                f'（{leave_type} {start_date}〜{end_date}、{days}日間）'
            )
            return

        # ──── 休暇申請：承認 ────
        if action == 'leave_approve':
            if not is_admin(user_id):
                reply_text(reply_token, '管理者のみ操作できます。')
                return
            row_num = int(data_params.get('row', 0))
            result  = _process_leave_decision(row_num, user_id, approved=True)
            reply_text(reply_token, result)
            return

        # ──── 休暇申請：却下 ────
        if action == 'leave_reject':
            if not is_admin(user_id):
                reply_text(reply_token, '管理者のみ操作できます。')
                return
            row_num = int(data_params.get('row', 0))
            result  = _process_leave_decision(row_num, user_id, approved=False)
            reply_text(reply_token, result)
            return

        # ──── 日報入力ボタン ────
        # アクションに対応する最初の入力待ち状態を取得
        first_state = ACTION_FIRST_STATE.get(action)
        if not first_state:
            logger.warning(f'未定義のアクション: {action}')
            reply_text(reply_token, '少し待ってからもう一度お試しください')
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


