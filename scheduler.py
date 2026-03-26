"""
scheduler.py
APSchedulerを使った定期実行ジョブの定義と起動。

登録ジョブ一覧:
  morning_reminder   平日 9:00  午前リマインダーを全ユーザーに送信
  afternoon_reminder 平日 14:00 午後リマインダーを全ユーザーに送信
  daily_report       平日 17:00 管理者向け日報レポートを送信
  weekly_report      金曜 17:30 週次振り返りレポートを全ユーザーに送信

NOTE: app.py からインポートされるため、モジュール先頭では app.py を逆インポートしない。
      ジョブ関数の内部で遅延インポートすることで循環インポートを回避している。
"""
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# 日本標準時（app.py と同じ設定）
JST = ZoneInfo('Asia/Tokyo')


# ─────────────────────────────────────
# ジョブ関数
# ─────────────────────────────────────

def _morning_job():
    """平日 9:00: 午前リマインダーを全ユーザーに送信する"""
    # 循環インポート回避のため、実行時に遅延インポートする
    from app import send_flex_to_all
    logger.info('[Scheduler] 午前リマインダー実行')
    send_flex_to_all('午前')


def _afternoon_job():
    """平日 14:00: 午後リマインダーを全ユーザーに送信する"""
    from app import send_flex_to_all
    logger.info('[Scheduler] 午後リマインダー実行')
    send_flex_to_all('午後')


def _daily_report_job():
    """平日 17:00: 管理者向け日報レポートを送信する"""
    from app import _send_daily_report
    logger.info('[Scheduler] 日報レポート実行')
    try:
        _send_daily_report()
    except Exception as e:
        logger.error(f'[Scheduler] 日報レポートエラー: {e}')


def _weekly_report_job():
    """金曜 17:30: 週次振り返りレポートを全ユーザーに送信する"""
    from app import _send_weekly_report
    logger.info('[Scheduler] 週次レポート実行')
    try:
        _send_weekly_report()
    except Exception as e:
        logger.error(f'[Scheduler] 週次レポートエラー: {e}')


# ─────────────────────────────────────
# スケジューラー起動
# ─────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    """APSchedulerを起動してすべてのジョブを登録する。
    app.run() の直前に呼び出すこと。"""
    scheduler = BackgroundScheduler(timezone=JST)

    # 平日 11:55: 午前リマインダー
    scheduler.add_job(
        _morning_job,
        CronTrigger(day_of_week='mon-fri', hour=11, minute=55, timezone=JST),
        id='morning_reminder',
    )

    # 平日 18:00: 午後リマインダー
    scheduler.add_job(
        _afternoon_job,
        CronTrigger(day_of_week='mon-fri', hour=18, minute=0, timezone=JST),
        id='afternoon_reminder',
    )

    # 平日 18:30: 管理者向け日報レポート
    scheduler.add_job(
        _daily_report_job,
        CronTrigger(day_of_week='mon-fri', hour=18, minute=30, timezone=JST),
        id='daily_report',
    )

    # 金曜 18:15: 週次振り返りレポート
    scheduler.add_job(
        _weekly_report_job,
        CronTrigger(day_of_week='fri', hour=18, minute=15, timezone=JST),
        id='weekly_report',
    )

    scheduler.start()
    logger.info('スケジューラー起動完了（4ジョブ登録）')
    return scheduler
