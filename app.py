import base64
import calendar
import hashlib
import hmac
import os
import json
import logging
import re
import threading
import unicodedata

from collections import Counter
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import gspread
import requests
from google.oauth2.service_account import Credentials
from flask import Flask, request, abort, jsonify, render_template
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

# LIFF設定（日報入力フォーム）
# LIFF_ID              : LINEログインチャネルのLIFFタブで発行されるID
# LINE_LOGIN_CHANNEL_ID: そのLINEログインチャネルのチャネルID。
#                        IDトークン検証時の client_id（aud）に使う。
# ※ LINEログインチャネルは、Botと同じプロバイダーに作ること。
#    プロバイダーが違うとユーザーIDが別値になり、本人を特定できない。
LIFF_ID               = os.environ.get('LIFF_ID', '')
LINE_LOGIN_CHANNEL_ID = os.environ.get('LINE_LOGIN_CHANNEL_ID', '')
LIFF_ENABLED          = bool(LIFF_ID and LINE_LOGIN_CHANNEL_ID)
if not LIFF_ENABLED:
    logger.warning(
        'LIFF_ID または LINE_LOGIN_CHANNEL_ID が未設定です。'
        '日報入力フォームは利用できません。'
    )

# IDトークン検証エンドポイント
LINE_VERIFY_URL = 'https://api.line.me/oauth2/v2.1/verify'

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
# サーバー（Render）はUTCで動くため、日付・時刻の判定は必ずJSTに変換して行う。
JST = ZoneInfo('Asia/Tokyo')

# ─────────────────────────────────────
# 定数定義
# ─────────────────────────────────────

# 日報の締切。
# 入力フォームは常に「送信した瞬間のJSTの日付」で保存するため（save_report参照）、
# 24:00を過ぎた入力は自動的に翌日の日報になる。遡り入力の手段は用意していない。
# 締切を過ぎても未入力者のシートへの自動書き込みは行わず、未提出のままとする。
DEADLINE_NOTE = '入力は本日24時までです。'

# postbackのaction名
ACTION_MY_WEEKLY = 'my_weekly'      # 本人の週次まとめ

# 登録完了メッセージ（スプレッドシート連携の有無にかかわらず同じ文面を使う）
REGISTERED_MESSAGE_TEMPLATE = (
    '{name}さんを登録しました！\n'
    '平日18時にお知らせを送ります。\n'
    '「日報を入力」を押すとフォームが開き、午前・午後をまとめて入力できます。\n'
    + DEADLINE_NOTE
)

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
    (['日報', '提出'],  '日報は「日報入力」と送信するとフォームが開きます。\n'
                        '18:00に届くお知らせのボタンからも開けます。'),
]
# どのキーワードにもマッチしない場合のデフォルト返信
INQUIRY_DEFAULT_REPLY = INQUIRY_DEFAULT_REPLY = ''

# 日報の入力はLIFFフォーム（/liff/report）で行う。
# 行動種別ごとに、訪問先/移動先の欄をどのラベルで表示するかの定義。
# valueは日報シートの列に対応する:
#   company     → 訪問先会社名
#   destination → 移動先
#   （なし）    → 詳細欄は作業内容のみ
ACTION_PLACE_FIELD = {
    '商談':             ('company',     '訪問先'),
    'メーカー訪問':     ('company',     '訪問先'),
    '展示会・イベント': ('company',     '会場・イベント名'),
    '移動・外出':       ('destination', '移動先・目的地'),
    '社内作業':         (None,          ''),
    '工場対応':         (None,          ''),
}

# 行動種別ごとの「作業内容」欄のラベル。
# 工場対応は工場対応内容列、それ以外は作業内容列に入る。
ACTION_CONTENT_FIELD = {
    '商談':             ('work_content',    '商談内容'),
    'メーカー訪問':     ('work_content',    '訪問内容'),
    '展示会・イベント': ('work_content',    '内容'),
    '移動・外出':       ('work_content',    '内容'),
    '社内作業':         ('work_content',    '作業内容'),
    '工場対応':         ('factory_content', '対応内容'),
}

# ─────────────────────────────────────
# ユーザー状態管理（インメモリ）
# ─────────────────────────────────────
# Render.com無料プランはシングルワーカーのためインメモリで問題なし
#
# 日報の入力はLIFFフォームに移行したため、ここで扱うのは休暇申請フローのみ。
#
# 形式:
# {
#   user_id: {
#     'state':       str,   # 現在の入力待ち状態（例: 'leave_reason'）
#     'leave_type':  str,
#     'leave_start': str,
#     'leave_end':   str,
#     'leave_days':  int,
#   }
# }
user_states: dict = {}
_display_name_cache: dict = {}
_DISPLAY_NAME_TTL = 3600

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


def append_row_safely(sheet, row: list) -> int:
    """シート末尾に1行追記し、実際に書き込まれた行番号を返す。

    insert_data_option を明示する。gspread の既定は未指定で、その場合
    Sheets API 側の既定 OVERWRITE が使われる。OVERWRITE は書き込み先を
    「表範囲の次の行」と判定するため、途中に空行があると表範囲の検出結果
    次第で既存行を上書きし得る。INSERT_ROWS なら常に行を挿入する。

    行番号はAPIの応答（updatedRange）から取得する。get_all_records()の
    件数から計算すると空行で件数が途切れ、別の行を指してしまう。
    書き込み自体はAPI側で原子的に行われるため、同時保存でも競合しない。
    """
    resp = sheet.append_row(
        row,
        table_range='A1',
        insert_data_option='INSERT_ROWS',
    )
    updated_range = resp.get('updates', {}).get('updatedRange', '')
    m = re.search(r'![A-Z]+(\d+)', updated_range)
    if not m:
        logger.warning(f'書き込み行を特定できません: updatedRange={updated_range!r}')
        return -1
    return int(m.group(1))


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
    written_row = append_row_safely(sheet, row)
    logger.info(
        f'日報保存完了: {display_name} / {time_slot} / {action} '
        f'（{written_row}行目に書き込み）'
    )


def register_user(user_id: str, display_name: str) -> str:
    """ユーザーを「users」シートに登録する。既登録の場合はその旨を返す。
    スプレッドシート連携が無効な場合はログのみ出力する。"""
    if not SHEETS_ENABLED:
        logger.info(f'[SHEETS無効] ユーザー登録: {display_name} ({user_id})')
        return REGISTERED_MESSAGE_TEMPLATE.format(name=display_name)

    spreadsheet = get_spreadsheet()
    users_sheet = get_or_create_sheet(
        spreadsheet, 'users', ['LINE表示名', 'ユーザーID', '登録日']
    )

    # 重複チェックは get_all_users() を使う。get_all_records() は空行で止まるため、
    # 空行より下に登録済みの本人がいると二重登録になる
    if any(u['id'] == user_id for u in get_all_users()):
        return f'{display_name}さんはすでに登録済みです！'

    today = datetime.now(JST).strftime('%Y/%m/%d')
    append_row_safely(users_sheet, [display_name, user_id, today])
    return REGISTERED_MESSAGE_TEMPLATE.format(name=display_name)


def get_all_user_ids() -> list[str]:
    """登録済み全ユーザーのIDリストを取得する。
    スプレッドシート連携が無効な場合は空リストを返す。

    usersシートの読み取りは get_all_users() に統一する。
    get_all_records() は空行で止まるため、usersシートの途中に空行があると
    それ以降のユーザーに通知が届かなくなる。
    """
    return [u['id'] for u in get_all_users()]


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
    append_row_safely(sheet, row)
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
    """LINE APIからユーザーの表示名を取得する（TTLキャッシュ付き）。
    失敗した場合はusersシートの登録名にフォールバックする。"""
    import time as _time
    cached = _display_name_cache.get(user_id)
    if cached and _time.time() - cached['ts'] < _DISPLAY_NAME_TTL:
        return cached['name']
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            profile = line_bot_api.get_profile(user_id)
            _display_name_cache[user_id] = {'name': profile.display_name, 'ts': _time.time()}
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

def create_reminder_flex_message() -> FlexMessage:
    """18:00の日報リマインダー用Flex Messageを作成する。
    ボタンを押すと午前→午後の順に入力するフローが始まる。"""
    flex_dict = {
        'type': 'bubble',
        'size': 'mega',
        'header': {
            'type': 'box',
            'layout': 'vertical',
            'backgroundColor': '#1565C0',
            'paddingAll': '15px',
            'contents': [
                {
                    'type': 'text',
                    'text': '今日の日報入力',
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
                    'text': '今日も一日お疲れ様でした。\n'
                            '今日の日報を入力してください。\n'
                            '午前・午後をまとめて入力できます。',
                    'wrap': True,
                    'color': '#555555',
                    'size': 'sm',
                    'margin': 'xs',
                },
                {
                    'type': 'button',
                    'action': {
                        'type': 'uri',
                        'label': '日報を入力',
                        'uri': liff_url(),
                    },
                    'style': 'primary',
                    'margin': 'md',
                    'height': 'sm',
                },
                {
                    'type': 'button',
                    'action': {
                        'type': 'postback',
                        'label': '週次まとめを見る',
                        'data': f'action={ACTION_MY_WEEKLY}',
                        'displayText': '週次まとめ',
                    },
                    'style': 'link',
                    'margin': 'sm',
                    'height': 'sm',
                },
            ],
        },
    }

    return FlexMessage(
        alt_text='今日の日報を入力してください',
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

    def body_text(text: str, color: str = '#34495E', margin: str = 'sm') -> dict:
        return {
            'type': 'text',
            'text': text,
            'size': 'sm',
            'color': color,
            'wrap': True,
            'margin': margin,
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
                # ── 日報の入力 ──
                section_header('日報の入力', '#E74C3C'),
                body_text(
                    '18:00に届くお知らせの「日報を入力」を押すと、'
                    '入力フォームが開きます。'
                ),
                body_text('「日報入力」と送信してもフォームを開けます。'),
                body_text(
                    '午前・午後を1画面でまとめて入力できます。'
                    'どちらかがない日は「なし」を選んでください。'
                ),
                body_text(
                    f'⏰ {DEADLINE_NOTE}\n'
                    '24時を過ぎると翌日の日報になります。'
                    '前の日にさかのぼっての入力はできません。',
                    color='#C0392B',
                ),
                body_text(
                    '※ 初回はプロフィールへのアクセス許可を求められます。'
                    '「許可する」を押してください。',
                    color='#E67E22',
                ),
                {'type': 'separator', 'margin': 'md'},
                # ── 新しいスタッフの登録 ──
                section_header('新しく入った方へ', '#16A085'),
                body_text('① 日報ボットを友だち追加する'),
                body_text('② 「登録する」を押す（または「登録」と送信）'),
                body_text('③ 「登録しました！」が返れば完了。翌日から18時にお知らせが届きます'),
                {'type': 'separator', 'margin': 'md'},
                # ── 基本コマンド ──
                section_header('コマンド', '#2980B9'),
                cmd_row('日報入力',   '入力フォームを開く'),
                cmd_row('週次まとめ', '今週の自分の日報を確認'),
                cmd_row('確認',   '日報の照会\n1日分・月別・期間指定'),
                cmd_row('登録',   'ユーザー登録'),
                cmd_row('使い方', 'この説明を表示'),
                {'type': 'separator', 'margin': 'md'},
                section_header('休暇・有給', '#8E44AD'),
                cmd_row('休暇申請',   '休暇の申請'),
                cmd_row('有給残日数', '残日数を確認'),
                cmd_row('申請履歴',   '直近5件を表示'),
                {'type': 'separator', 'margin': 'md'},
                # ── 自動送信スケジュール ──
                section_header('自動送信スケジュール', '#E67E22'),
                schedule_row('平日 18:00', '日報入力のお知らせ'),
                schedule_row('金曜 19:30', '今週の自分の日報のまとめ'),
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


def send_my_weekly_to_all(all_users: list[dict]):
    """各スタッフに、その人自身の週次まとめを個別にプッシュ送信する。
    1人分の送信に失敗しても他の人への送信は続ける。"""
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        for user in all_users:
            try:
                text = build_my_weekly_report_text(user['name'])
                api.push_message(PushMessageRequest(
                    to=user['id'],
                    messages=[TextMessage(text=text)],
                ))
            except Exception as e:
                logger.error(f'週次まとめの送信失敗 ({user.get("name")}): {e}')
    logger.info(f'週次まとめを個別送信: {len(all_users)}名')


def send_reminder_to_all():
    """登録済み全ユーザーに日報リマインダーをプッシュ送信する"""
    try:
        user_ids = get_all_user_ids()
        if not user_ids:
            logger.info('登録ユーザーが0名のため送信をスキップ')
            return

        flex_msg = create_reminder_flex_message()

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

class SaveReportError(Exception):
    """日報の保存に失敗した。savedはそれまでに保存できた件数。"""

    def __init__(self, saved: int):
        super().__init__(f'{saved}件保存後に失敗')
        self.saved = saved


def save_report_rows(display_name: str, rows: list[dict]) -> int:
    """日報の各行を保存し、保存できた件数を返す。
    途中で失敗した場合は、それまでに保存した件数を添えて SaveReportError を送出する。

    rowsは午前・午後それぞれのスロットで、1件が日報シートの1行になる。
    LIFFフォーム（/liff/report/submit）から呼ばれる。
    """
    saved = 0
    try:
        for row in rows:
            save_report(display_name=display_name, **row)
            saved += 1
    except Exception as e:
        # 入力内容をログに残す（シートに残らないため復旧の手がかりになる）
        logger.error(
            f'日報の保存に失敗: {display_name} / {saved}/{len(rows)}件保存済み / '
            f'{rows}',
            exc_info=True
        )
        raise SaveReportError(saved) from e
    return saved


def reply_my_weekly_report(user_id: str, reply_token: str):
    """本人の今週分のまとめを返信する。
    名前はLINEの表示名から取得するため、他人の分は取得できない。"""
    if not SHEETS_ENABLED:
        reply_text(reply_token, 'スプレッドシートが未設定のため確認できません。')
        return
    display_name = get_display_name(user_id)
    reply_text(reply_token, build_my_weekly_report_text(display_name))


# ─────────────────────────────────────
# LIFF（日報入力フォーム）
# ─────────────────────────────────────

def liff_url() -> str:
    """LIFFフォームのURLを返す"""
    return f'https://liff.line.me/{LIFF_ID}'


def reply_report_form_link(reply_token: str):
    """日報入力フォームへのリンクを返信する"""
    if not LIFF_ENABLED:
        reply_text(reply_token, '日報入力フォームが未設定です。管理者にご連絡ください。')
        return
    reply_text(reply_token, f'こちらから日報を入力してください。\n{liff_url()}')


def verify_id_token(id_token: str) -> dict | None:
    """LIFFのIDトークンをLINEのAPIで検証し、ペイロードを返す。
    検証に失敗した場合はNoneを返す。

    改ざんされたトークンで他人になりすませないよう、必ずサーバー側で検証する。
    client_idにはLIFFが属するLINEログインチャネルのチャネルIDを指定する。
    """
    try:
        resp = requests.post(
            LINE_VERIFY_URL,
            data={'id_token': id_token, 'client_id': LINE_LOGIN_CHANNEL_ID},
            timeout=10,
        )
    except Exception:
        logger.error('IDトークン検証のリクエストに失敗', exc_info=True)
        return None

    if resp.status_code != 200:
        logger.warning(f'IDトークン検証に失敗: {resp.status_code} {resp.text[:200]}')
        return None

    payload = resp.json()
    # 念のため発行先チャネルを確認する（検証APIも確認するが二重に守る）
    if str(payload.get('aud')) != str(LINE_LOGIN_CHANNEL_ID):
        logger.warning(f'IDトークンのaudが不一致: {payload.get("aud")!r}')
        return None
    return payload


def _build_slot_row(time_slot: str, form: dict, memo: str) -> dict | None:
    """フォームの1スロット分の入力から、日報シート1行分のデータを作る。
    行動種別が未選択（「なし」）の場合はNoneを返す。"""
    action = (form.get('action') or '').strip()
    if not action:
        return None
    if action not in ACTION_PLACE_FIELD:
        raise ValueError(f'不正な行動種別: {action}')

    row = {
        'time_slot':       time_slot,
        'action':          action,
        'company':         '',
        'destination':     '',
        'work_content':    '',
        'factory_content': '',
        'memo':            memo,
    }

    # 訪問先 or 移動先（行動種別によってどちらの列に入れるかが決まる）
    place_key, _ = ACTION_PLACE_FIELD[action]
    if place_key:
        row[place_key] = (form.get('place') or '').strip()

    # 作業内容 or 工場対応内容
    content_key, _ = ACTION_CONTENT_FIELD[action]
    row[content_key] = (form.get('content') or '').strip()

    return row


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
    # get_all_records()は空行で止まるため get_all_values() を使用
    all_values = users_sheet.get_all_values()
    if len(all_values) < 2:
        return []
    headers = all_values[0]
    try:
        name_idx = headers.index('LINE表示名')
        id_idx   = headers.index('ユーザーID')
    except ValueError:
        return []
    result = []
    for row in all_values[1:]:
        uid  = row[id_idx].strip()   if len(row) > id_idx   else ''
        name = row[name_idx].strip() if len(row) > name_idx else ''
        if uid:
            result.append({'name': name, 'id': uid})
    return result


def normalize_date(value) -> date | None:
    """日付の表記ゆれを吸収してdateオブジェクトに変換する。
    '2026/07/16'、'2026/7/16'、'2026-07-16' をすべて同じ日付として扱う。

    年は必須。'7/16' のような年なしの値は解釈せずNoneを返す。
    年を補うと、シートに残っている年なしの古い行が当日の日報として
    誤ってヒットするため。save_report は必ず年付き（%Y/%m/%d）で保存する。

    日付として解釈できない場合はNoneを返す。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    # 末尾の時刻などは無視し、先頭3つの数値を 年・月・日 として扱う
    nums = [int(n) for n in re.findall(r'\d+', str(value))]
    if len(nums) < 3:
        return None

    year, month, day = nums[0], nums[1], nums[2]
    if not 1000 <= year <= 9999:   # 年なし（'7/16 09:30' 等）は解釈しない
        return None

    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_name(value) -> str:
    """ユーザー名の表記ゆれを吸収して比較用の文字列に変換する。
    日報シートの『ユーザー名』はLINEプロフィールの現在の表示名、
    usersシートの『LINE表示名』は登録時点の表示名のため、両者はずれることがある。
    全角/半角・前後や途中の空白・大文字小文字の違いを吸収する。"""
    s = unicodedata.normalize('NFKC', str(value or ''))
    return ''.join(s.split()).casefold()


def get_reports_by_date_range(date_strs: list[str]) -> list[dict]:
    """指定した日付リストに一致する日報レコードをすべて返す。
    日付は表記ゆれを吸収して比較する。
    スプレッドシート連携が無効な場合は空リストを返す。"""
    if not SHEETS_ENABLED:
        return []
    spreadsheet = get_spreadsheet()
    report_sheet = get_or_create_sheet(
        spreadsheet, '日報',
        ['日付', '時間', 'ユーザー名', '午前or午後', '行動種別',
         '訪問先会社名', '移動先', '作業内容', '工場対応内容', '自由メモ']
    )
    # get_all_records()は空行で止まるため get_all_values() を使用
    all_values = report_sheet.get_all_values()
    if len(all_values) < 2:
        return []
    headers  = all_values[0]
    date_set = {d for d in map(normalize_date, date_strs) if d is not None}
    try:
        date_idx = headers.index('日付')
    except ValueError:
        return []
    result = []
    for row in all_values[1:]:
        if len(row) <= date_idx:
            continue
        if normalize_date(row[date_idx]) in date_set:
            row_dict = {
                headers[i]: row[i]
                for i in range(len(headers))
                if i < len(row)
            }
            result.append(row_dict)
    return result


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
    sheet   = get_leave_application_sheet()
    now_str = datetime.now(JST).strftime('%Y/%m/%d %H:%M')
    # 実際に書き込んだ行番号を使う。get_all_records()の件数から計算すると
    # 空行で件数が途切れ、承認時に別の行を書き換えてしまう
    return append_row_safely(sheet, [now_str, display_name, leave_type,
                                     start_date, end_date, days, reason,
                                     '申請中', '', ''])


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
    target  = normalize_name(name)
    user_records = [r for r in records if normalize_name(r.get('ユーザー名')) == target]
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
    target       = normalize_name(name)
    user_records = [r for r in records if normalize_name(r.get('ユーザー名')) == target]

    lines = [f'{name}さんの日報', label, '']

    if not user_records:
        # 名前が一致しない場合の切り分け用。シート上の名前と照会名をログに残す
        if records:
            sheet_names = sorted({str(r.get('ユーザー名', '')) for r in records})
            logger.info(
                f'期間照会で該当なし: 照会名={name!r} (正規化: {target!r}) / '
                f'期間内のシート上の名前={sheet_names}'
            )
        lines.append('この期間のデータはありません。')
        return '\n'.join(lines)

    by_date: dict[date, dict] = {}
    for rec in user_records:
        d    = normalize_date(rec.get('日付'))
        slot = rec.get('午前or午後', '')
        if d is None:
            continue
        by_date.setdefault(d, {})[slot] = rec

    for date_str in date_strs:
        d = normalize_date(date_str)
        if d is None or d not in by_date:
            continue
        slots  = by_date[d]
        d_lbl  = f'{d.month}/{d.day}'
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
    """LINE Webhookを受け取るエンドポイント（即時200返却＋バックグラウンド処理）"""
    signature = request.headers.get('X-Line-Signature', '')
    body      = request.get_data(as_text=True)
    logger.info('Webhook受信')

    # 署名検証のみ同期で実施（HMAC-SHA256）
    gen_sig = base64.b64encode(
        hmac.new(LINE_CHANNEL_SECRET.encode('utf-8'),
                 body.encode('utf-8'), hashlib.sha256).digest()
    ).decode('utf-8')
    if not hmac.compare_digest(gen_sig, signature):
        logger.error('署名検証エラー')
        abort(400)
        return

    # イベント処理はバックグラウンドで実行し即時200を返す
    def process_in_background():
        try:
            handler.handle(body, signature)
        except Exception as e:
            logger.error(f'バックグラウンド処理エラー: {e}')

    threading.Thread(target=process_in_background, daemon=True).start()
    return 'OK', 200


@app.route('/liff/report', methods=['GET'])
def liff_report_form():
    """日報入力フォーム（LIFFアプリ）を返す。
    LINE Developersコンソールで、このURLをLIFFのエンドポイントURLに設定する。"""
    return render_template(
        'liff_report.html',
        liff_id=LIFF_ID,
        liff_enabled=LIFF_ENABLED,
        actions=[a['value'] for a in ACTIONS],
        place_fields={k: {'key': v[0], 'label': v[1]}
                      for k, v in ACTION_PLACE_FIELD.items()},
        content_fields={k: {'key': v[0], 'label': v[1]}
                        for k, v in ACTION_CONTENT_FIELD.items()},
    )


@app.route('/liff/report/submit', methods=['POST'])
def liff_report_submit():
    """日報入力フォームからの送信を受け取り、日報シートに保存する。
    ユーザーはLIFFのIDトークンで特定する（クライアントの申告は信用しない）。"""
    if not LIFF_ENABLED:
        return jsonify({'ok': False, 'message': 'フォームが未設定です。管理者にご連絡ください。'}), 503

    data     = request.get_json(silent=True) or {}
    id_token = data.get('idToken') or ''
    if not id_token:
        return jsonify({'ok': False, 'message': 'ログイン情報が取得できませんでした。'}), 401

    payload = verify_id_token(id_token)
    if not payload or not payload.get('sub'):
        return jsonify({'ok': False, 'message': 'ログイン情報を確認できませんでした。開き直してください。'}), 401

    user_id = payload['sub']

    # 名前は既存の保存処理と同じ経路で取得し、シート上の表記と揃える。
    # 取得できない場合はIDトークンの名前を使う
    try:
        display_name = get_display_name(user_id)
    except Exception:
        display_name = payload.get('name', '')
    if not display_name:
        logger.error(f'表示名を取得できません: {user_id}')
        return jsonify({'ok': False, 'message': 'ユーザー情報を取得できませんでした。'}), 500

    memo = (data.get('memo') or '').strip()
    try:
        rows = [
            row for row in (
                _build_slot_row('午前', data.get('am') or {}, memo),
                _build_slot_row('午後', data.get('pm') or {}, memo),
            ) if row is not None
        ]
    except ValueError as e:
        logger.warning(f'不正な入力: {e}')
        return jsonify({'ok': False, 'message': '入力内容が不正です。開き直してください。'}), 400

    if not rows:
        return jsonify({
            'ok': False,
            'message': '午前・午後のどちらかは入力してください。',
        }), 400

    if not SHEETS_ENABLED:
        logger.info(f'[SHEETS無効] LIFF日報: {display_name} / {rows}')
        return jsonify({'ok': True, 'message': '記録しました'})

    try:
        save_report_rows(display_name, rows)
    except SaveReportError as e:
        if e.saved:
            # 一部だけ保存された。やり直すと重複するため、その旨を伝える
            return jsonify({
                'ok': False,
                'message': f'保存に失敗しました。もう一度送信してください。'
                           f'（{e.saved}件は記録済みのため、重複したら管理者にご連絡ください）',
            }), 500
        return jsonify({
            'ok': False,
            'message': '保存に失敗しました。もう一度送信してください。',
        }), 500

    logger.info(f'LIFFから日報保存: {display_name} / {len(rows)}件')
    return jsonify({'ok': True, 'message': '記録しました'})


@app.route('/reminder', methods=['GET', 'POST'])
def reminder():
    """平日18:00 JSTにcron-job.orgから呼ばれるエンドポイント。
    全ユーザーに日報入力の通知を1回だけ送る（午前・午後をまとめて入力する）。"""
    logger.info('日報リマインダー送信開始')
    send_reminder_to_all()
    return jsonify({'status': 'ok', 'message': '日報リマインダーを送信しました'})


@app.route('/report', methods=['GET', 'POST'])
def daily_report_to_admin():
    """19:30 JSTにcron-job.orgから呼ばれるエンドポイント。
    当日の全ユーザーの日報をまとめて管理者にプッシュ送信する。
    金曜はこれに続けて週次レポートも送る（cronジョブを1本にまとめるため）。"""
    logger.info('日次レポート送信開始')

    try:
        today     = datetime.now(JST).date()
        date_str  = today.strftime('%Y/%m/%d')
        date_label = f'{today.month}月{today.day}日'

        # 当日の全日報データを取得
        records = get_reports_by_date_range([date_str])

        # 当日分として拾った行を、シート上の生の日付つきでログに残す
        logger.info(
            f'日次レポート対象={date_str} / 該当 {len(records)} 件: '
            + str([
                (r.get('日付'), r.get('ユーザー名'), r.get('時間'))
                for r in records
            ])
        )

        # 登録ユーザー一覧を取得
        all_users = get_all_users()

        if not all_users:
            logger.info('登録ユーザーが0名のため日次レポート送信をスキップ')
            return jsonify({'status': 'ok', 'message': 'ユーザーなし'})

        # ユーザーごとに日報を整理
        lines = [f'📋 {date_label}の日報レポート\n']

        # 提出判定は表記ゆれを吸収した名前で行い、表示にはシート上の名前を使う
        submitted_users = set()
        by_user = {}
        for rec in records:
            name = rec.get('ユーザー名', '')
            if name:
                submitted_users.add(normalize_name(name))
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
            if normalize_name(u['name']) not in submitted_users
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
        # 締切前の集計であることを明示する（この時点の未提出は確定ではない）
        lines.append(DEADLINE_NOTE)

        report_text = '\n'.join(lines)
        _push_to_admins(report_text)
        logger.info('日次レポート送信完了')

        # 金曜は続けて週次まとめも送る（管理者に全員分、各スタッフに自分の分）
        weekly_sent = False
        if today.weekday() == 4:
            weekly_text = build_weekly_report_text()
            if weekly_text:
                _push_to_admins(weekly_text)
                weekly_sent = True
                logger.info('週次レポート送信完了（管理者）')
            send_my_weekly_to_all(all_users)

        return jsonify({
            'status': 'ok',
            'message': '日次レポートを送信しました',
            'weekly_sent': weekly_sent,
        })

    except Exception as e:
        logger.error(f'日次レポートエラー: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _this_week_dates() -> tuple[list[str], str]:
    """今週の月〜金（今日まで）の日付リストと週ラベルを返す"""
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
    return date_strs, week_label


def _summarize_user_week(name: str, records: list[dict]) -> tuple[int, str, list[dict]]:
    """1人分の週次集計を返す: (提出日数, 主な活動の要約, その人の日報)"""
    target = normalize_name(name)
    user_records = [
        r for r in records
        if normalize_name(r.get('ユーザー名')) == target
    ]

    # 提出日数（ユニークな日付数）。表記ゆれを吸収してから数える
    submitted_dates = {
        d for d in (normalize_date(r.get('日付')) for r in user_records)
        if d is not None
    }

    # アクション別の回数集計
    action_counter = Counter(r.get('行動種別', '') for r in user_records)
    action_summary = ' '.join(
        f'{ACTION_EMOJI.get(a, "")}{ACTION_SHORT.get(a, a)}({c})'
        for a, c in action_counter.most_common(3)
    )
    return len(submitted_dates), action_summary, user_records


def build_my_weekly_report_text(name: str) -> str:
    """本人の今週分（月〜金）のまとめを組み立てて返す。
    nameはLINEの表示名から取得した本人の名前で、他人の分は参照できない。"""
    date_strs, week_label = _this_week_dates()
    total_days = len(date_strs)
    records    = get_reports_by_date_range(date_strs)

    submitted_count, action_summary, user_records = _summarize_user_week(
        name, records
    )

    lines = [f'📊 {name}さんの週次まとめ', week_label, '']

    if not user_records:
        lines.append('今週の日報はまだありません。')
        return '\n'.join(lines)

    rate = round(submitted_count / total_days * 100) if total_days else 0
    lines.append(f'提出: {submitted_count}/{total_days}日({rate}%)')
    if action_summary:
        lines.append(f'主な活動: {action_summary}')
    lines.append('')

    # 日ごとの内容
    by_date: dict[date, dict] = {}
    for rec in user_records:
        d = normalize_date(rec.get('日付'))
        if d is None:
            continue
        by_date.setdefault(d, {})[rec.get('午前or午後', '')] = rec

    for date_str in date_strs:
        d = normalize_date(date_str)
        if d is None or d not in by_date:
            continue
        slots      = by_date[d]
        slot_texts = [
            f'{slot}:{format_action_label(slots[slot])}'
            for slot in ['午前', '午後'] if slot in slots
        ]
        lines.append(f'{d.month}/{d.day} {" ".join(slot_texts)}')

    return '\n'.join(lines)


def build_weekly_report_text() -> str | None:
    """今週（月〜金）の全ユーザーの日報サマリーを組み立てて返す。
    登録ユーザーが0名の場合はNoneを返す。"""
    date_strs, week_label = _this_week_dates()

    # 全日報データを取得
    records = get_reports_by_date_range(date_strs)

    # 登録ユーザー一覧
    all_users = get_all_users()

    if not all_users:
        logger.info('登録ユーザーが0名のため週次レポートをスキップ')
        return None

    total_days = len(date_strs)

    if total_days == 0:
        logger.info('対象の平日が0日のため週次レポートをスキップ')
        return None

    lines = [f'📊 週次レポート（{week_label}）\n']

    # ユーザーごとに集計
    for user in sorted(all_users, key=lambda u: u['name']):
        name = user['name']
        submitted_count, action_summary, _ = _summarize_user_week(name, records)

        if submitted_count == 0:
            lines.append(f'❌ {name}: 提出なし')
            continue

        rate = round(submitted_count / total_days * 100)
        icon = '✅' if rate >= 80 else '⚠️'
        lines.append(
            f'{icon} {name}: {submitted_count}/{total_days}日({rate}%) '
            f'{action_summary}'
        )

    # 全体統計
    all_submitted = {
        n for n in (normalize_name(r.get('ユーザー名')) for r in records) if n
    }
    lines.append('')
    lines.append(
        f'全体: {len(all_submitted)}/{len(all_users)}名が1件以上提出'
    )

    return '\n'.join(lines)

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
        # 午前 → 午後 の順に入力する。送るたびに最初からやり直せる
        if text == '日報入力':
            reply_report_form_link(reply_token)
            return

        # ──── 週次まとめコマンド（本人の分のみ） ────
        if text == '週次まとめ':
            reply_my_weekly_report(user_id, reply_token)
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
                target       = normalize_name(u['name'])
                user_records = [
                    r for r in records
                    if normalize_name(r.get('ユーザー名')) == target
                ]
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
                target       = normalize_name(uname)
                user_records = [
                    r for r in records
                    if normalize_name(r.get('ユーザー名')) == target
                ]
                lines.append(f'\n{uname}')
                if not user_records:
                    lines.append('  データなし')
                else:
                    by_date: dict[date, dict] = {}
                    for rec in user_records:
                        d    = normalize_date(rec.get('日付'))
                        slot = rec.get('午前or午後', '')
                        if d is None:
                            continue
                        by_date.setdefault(d, {})[slot] = rec
                    for date_str in date_strs:
                        d = normalize_date(date_str)
                        if d is None or d not in by_date:
                            continue
                        slots      = by_date[d]
                        d_lbl      = f'{d.month}/{d.day}'
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

        # ──── 友達追加：後で登録する ────
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

        # ──── 週次まとめ（本人の分のみ） ────
        if action == ACTION_MY_WEEKLY:
            reply_my_weekly_report(user_id, reply_token)
            return

        logger.warning(f'未定義のアクション: {action}')
        reply_text(reply_token, '少し待ってからもう一度お試しください')

    except Exception as e:
        logger.error(f'ポストバック処理エラー: {e}')
        reply_error(reply_token)


# ─────────────────────────────────────
# 起動
# ─────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)


