"""
routes/status.py
点検エージェントがログインなしで読む状態確認ページ（/status）。

出すもの : LINEトークンの有効性、Googleスプレッドシート接続、環境変数の有無（値は出さない）、
           定期実行（/reminder・/report・/monthly）の最終実行日時と結果、
           日報の最新日付と提出人数、直近のエラー件数
出さないもの: 氏名・LINEユーザーID・日報の中身・トークンやキーの値

定期実行の記録はスプレッドシートの「_稼働記録」シートに残す。
Render無料プランはスリープでメモリが消えるため、メモリだけだと翌朝の点検で記録が空になる。

NOTE: app.py との循環インポートを避けるため、app.py からの import は関数内で遅延インポートする。
"""
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
JST = ZoneInfo('Asia/Tokyo')
STARTED_AT = datetime.now(JST)

status_bp = Blueprint('status', __name__)

# 定期実行として記録するURL（/afternoon は /reminder の旧URL）
CRON_PATHS = {
    '/reminder': 'reminder',
    '/afternoon': 'reminder',
    '/report': 'report',
    '/monthly': 'monthly',
}
RUN_LOG_SHEET = '_稼働記録'
RUN_LOG_HEADERS = ['処理', '最終実行日時', '結果', 'HTTPステータス']

_lock = threading.Lock()
_error_times: deque = deque(maxlen=1000)
_last_error = {'at': None, 'message': None}
_last_webhook_at = None
_cache: dict = {}   # 外部確認の結果キャッシュ {key: (時刻, 値)}


# ─────────────────────────────────────
# エラーログの件数を数える
# ─────────────────────────────────────

class _ErrorCounter(logging.Handler):
    def emit(self, record):
        now = datetime.now(JST)
        msg = re.sub(r'U[0-9a-f]{32}', 'U***', record.getMessage())
        with _lock:
            _error_times.append(now)
            _last_error['at'] = now
            _last_error['message'] = msg[:120]


logging.getLogger().addHandler(_ErrorCounter(level=logging.ERROR))


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


# ─────────────────────────────────────
# 定期実行の記録（app.after_request から呼ぶ）
# ─────────────────────────────────────

def _write_run_log(job: str, at: datetime, result: str, http_status: int):
    from app import SHEETS_ENABLED, get_spreadsheet, get_or_create_sheet
    if not SHEETS_ENABLED:
        return
    try:
        sheet = get_or_create_sheet(get_spreadsheet(), RUN_LOG_SHEET, RUN_LOG_HEADERS)
        row = [job, at.strftime('%Y/%m/%d %H:%M:%S'), result, http_status]
        names = sheet.col_values(1)
        if job in names:
            r = names.index(job) + 1
            sheet.update(range_name=f'A{r}:D{r}', values=[row])
        else:
            sheet.append_row(row, table_range='A1', insert_data_option='INSERT_ROWS')
    except Exception as e:
        # 記録に失敗しても本処理には影響させない（ERRORにすると件数に数えられるのでWARNING）
        logger.warning(f'稼働記録の書き込み失敗 ({job}): {e}')


def record_request(response):
    """定期実行URLとWebhookの呼び出しを記録する"""
    global _last_webhook_at
    path = request.path
    if path == '/webhook':
        with _lock:
            _last_webhook_at = datetime.now(JST)
        return response
    job = CRON_PATHS.get(path)
    if job:
        result = 'ok' if response.status_code < 400 else 'error'
        threading.Thread(
            target=_write_run_log,
            args=(job, datetime.now(JST), result, response.status_code),
            daemon=True,
        ).start()
    return response


def _read_run_log() -> dict:
    from app import SHEETS_ENABLED, get_spreadsheet
    if not SHEETS_ENABLED:
        return {}
    import gspread
    try:
        sheet = get_spreadsheet().worksheet(RUN_LOG_SHEET)
    except gspread.WorksheetNotFound:
        return {}
    out = {}
    for row in sheet.get_all_values()[1:]:
        if row and row[0]:
            out[row[0]] = {
                'last_run_at': row[1] if len(row) > 1 else None,
                'result': row[2] if len(row) > 2 else None,
                'http_status': row[3] if len(row) > 3 else None,
            }
    return out


# ─────────────────────────────────────
# 外部接続の確認
# ─────────────────────────────────────

def _check_line() -> str:
    from app import configuration
    from linebot.v3.messaging import ApiClient, MessagingApi
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).get_bot_info()
        return 'ok'
    except Exception as e:
        return f'error: {type(e).__name__}'


def _check_reports() -> dict:
    """日報シートの最新日付と、今日・最新日の提出人数（人数のみ）"""
    from app import SHEETS_ENABLED, get_spreadsheet, normalize_date
    if not SHEETS_ENABLED:
        return {'sheets': 'disabled'}
    try:
        sheet = get_spreadsheet().worksheet('日報')
        values = sheet.get_all_values()
    except Exception as e:
        return {'sheets': f'error: {type(e).__name__}'}
    if len(values) < 2:
        return {'sheets': 'ok', 'latest_report_date': None}
    headers = values[0]
    try:
        d_idx = headers.index('日付')
        u_idx = headers.index('ユーザー名')
    except ValueError:
        return {'sheets': 'error: 見出し行に「日付」「ユーザー名」がない'}
    by_date: dict = {}
    for row in values[1:]:
        if len(row) <= max(d_idx, u_idx):
            continue
        d = normalize_date(row[d_idx])
        if d:
            by_date.setdefault(d, set()).add(row[u_idx].strip())
    if not by_date:
        return {'sheets': 'ok', 'latest_report_date': None}
    latest = max(by_date)
    today = datetime.now(JST).date()
    return {
        'sheets': 'ok',
        'latest_report_date': latest.isoformat(),
        'users_on_latest_date': len(by_date[latest]),
        'users_today': len(by_date.get(today, set())),
    }


def _registered_users() -> int | None:
    from app import get_all_users
    try:
        return len(get_all_users())
    except Exception:
        return None


def _prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ─────────────────────────────────────
# /status
# ─────────────────────────────────────

@status_bp.route('/status')
def status():
    import os
    from app import SHEETS_ENABLED, LIFF_ENABLED

    now = datetime.now(JST)
    since = now - timedelta(hours=24)
    with _lock:
        errors_24h = sum(1 for t in _error_times if t >= since)
        last_error = dict(_last_error)
        last_webhook = _last_webhook_at

    line_token = _cached('line', 600, _check_line)
    reports = _cached('reports', 600, _check_reports)
    users = _cached('users', 600, _registered_users)
    try:
        runs = _cached('runs', 120, _read_run_log)
    except Exception as e:
        runs = {'error': type(e).__name__}

    config = {
        'LINE_CHANNEL_SECRET': bool(os.environ.get('LINE_CHANNEL_SECRET')),
        'LINE_CHANNEL_ACCESS_TOKEN': bool(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')),
        'GOOGLE_SHEET_ID': bool(os.environ.get('GOOGLE_SHEET_ID')),
        'GOOGLE_CREDENTIALS': bool(os.environ.get('GOOGLE_CREDENTIALS')),
        'LIFF_ID': bool(os.environ.get('LIFF_ID')),
        'LINE_LOGIN_CHANNEL_ID': bool(os.environ.get('LINE_LOGIN_CHANNEL_ID')),
        'LINE_USER_ID': bool(os.environ.get('LINE_USER_ID')),
    }

    problems, warnings = [], []
    if line_token != 'ok':
        problems.append('LINEトークンが無効、またはLINEに接続できない')
    if not SHEETS_ENABLED:
        problems.append('スプレッドシート連携が無効（GOOGLE_SHEET_ID / GOOGLE_CREDENTIALS 未設定）')
    elif reports.get('sheets') != 'ok':
        problems.append(f"スプレッドシートに接続できない ({reports.get('sheets')})")
    if not LIFF_ENABLED:
        problems.append('日報入力フォーム（LIFF）が無効')
    missing = [k for k, v in config.items() if not v]
    if missing:
        warnings.append('環境変数が未設定: ' + ', '.join(missing))

    # 定期実行の遅れ判定（平日のみ。前営業日以降に実行されていなければ注意）
    expected_since = _prev_weekday(now.date())
    if isinstance(runs, dict) and 'error' not in runs:
        for job, label in (('reminder', '18:00の日報通知'), ('report', '19:30の管理者レポート')):
            info = runs.get(job)
            if not info:
                warnings.append(f'{label}の実行記録がまだない（記録は今回の改修以降から）')
                continue
            if info.get('result') != 'ok':
                problems.append(f"{label}が失敗 (HTTP {info.get('http_status')})")
            try:
                last = datetime.strptime(info['last_run_at'], '%Y/%m/%d %H:%M:%S').date()
                if last < expected_since:
                    warnings.append(f'{label}が {last.isoformat()} 以降実行されていない')
            except (TypeError, ValueError):
                pass
    elif isinstance(runs, dict) and 'error' in runs:
        warnings.append(f"稼働記録を読めない ({runs['error']})")

    latest = reports.get('latest_report_date')
    if latest and latest < expected_since.isoformat():
        warnings.append(f'日報の最新日付が {latest}（前営業日の分が入っていない）')
    if errors_24h:
        warnings.append(f'直近のエラーログ {errors_24h} 件（起動後の分のみ）')

    overall = '異常' if problems else ('注意' if warnings else '正常')

    return jsonify({
        'app': 'daily-report',
        'overall': overall,
        'problems': problems,
        'warnings': warnings,
        'checked_at': _iso(now),
        'started_at': _iso(STARTED_AT),
        'uptime_minutes': int((now - STARTED_AT).total_seconds() // 60),
        'line_token': line_token,
        'liff_enabled': LIFF_ENABLED,
        'config_present': config,
        'reports': reports,
        'registered_users': users,
        'scheduled_runs': runs,
        'last_webhook_at': _iso(last_webhook),
        'errors_since_start_24h': errors_24h,
        'last_error_at': _iso(last_error['at']),
        'last_error_message': last_error['message'],
        'note': 'エラー件数とWebhook時刻はメモリ上の記録のため、スリープ復帰後の分だけです。'
                '定期実行の記録はスプレッドシート「_稼働記録」に残ります。',
    })
