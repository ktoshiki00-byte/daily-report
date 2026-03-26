"""
routes/monthly.py
前月の日報データを集計し、各ユーザーに個人実績・管理者に全体集計を送信するBlueprint。

エンドポイント:
  GET/POST /monthly

送信内容:
  各ユーザー → 提出日数・提出率・主な活動TOP3
  管理者     → 全体提出率・提出率80%未満の要注意者リスト

NOTE: app.py との循環インポートを避けるため、app.py からの import は
      ルート関数の内部で遅延インポートしている。
"""
import calendar
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify
from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    TextMessage,
)

logger = logging.getLogger(__name__)
JST    = ZoneInfo('Asia/Tokyo')

# Blueprintの定義
monthly_bp = Blueprint('monthly', __name__)


# ─────────────────────────────────────
# 内部ヘルパー
# ─────────────────────────────────────

def _get_last_month_weekdays() -> tuple[list[str], int, str]:
    """前月の平日日付リスト・平日日数・表示ラベルを返す。

    戻り値:
        weekday_strs    前月の平日日付リスト（'YYYY/MM/DD' 形式）
        total_days      前月の平日日数
        month_label     表示用ラベル（例: '2月'）
    """
    today = datetime.now(JST)

    # 前月の年・月を算出（1月なら前年12月）
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1

    _, last_day_num = calendar.monthrange(year, month)
    first = date(year, month, 1)
    last  = date(year, month, last_day_num)

    # 月〜金（weekday 0〜4）のみ抽出
    weekdays = [
        first + timedelta(days=i)
        for i in range((last - first).days + 1)
        if (first + timedelta(days=i)).weekday() < 5
    ]
    weekday_strs = [d.strftime('%Y/%m/%d') for d in weekdays]
    return weekday_strs, len(weekdays), f'{month}月'


# ─────────────────────────────────────
# ルート
# ─────────────────────────────────────

@monthly_bp.route('/monthly', methods=['GET', 'POST'])
def monthly_report():
    """前月分の日報を集計して各ユーザーと管理者に送信するエンドポイント"""
    # 循環インポート回避のためルート内で遅延インポートする
    from app import (
        SHEETS_ENABLED, LINE_USER_ID,
        configuration,
        get_all_users, get_reports_by_date_range,
    )

    if not SHEETS_ENABLED:
        return jsonify({'status': 'skip', 'message': 'スプレッドシート未設定のためスキップ'})

    try:
        weekday_strs, total_days, month_label = _get_last_month_weekdays()
        weekday_set = set(weekday_strs)
        print(f'[DEBUG] target_month={month_label} total_weekdays={total_days}')
        print(f'[DEBUG] date_range={weekday_strs[0]} to {weekday_strs[-1]}')

        users        = get_all_users()
        last_records = get_reports_by_date_range(weekday_strs)
        print(f'[DEBUG] users={len(users)} records={len(last_records)}')
        for u in users:
            print(f'[DEBUG]   user={u["name"]} id={u["id"]}')

        if not users:
            return jsonify({'status': 'skip', 'message': '登録ユーザーが0名のためスキップ'})

        # ─── ユーザーごとに集計 ───
        # { name: { 'days': {日付, ...}, 'actions': [行動種別, ...] } }
        user_stats: dict[str, dict] = {
            u['name']: {'days': set(), 'actions': []}
            for u in users
        }
        for record in last_records:
            name     = record.get('ユーザー名', '')
            date_str = record.get('日付', '')
            action   = record.get('行動種別', '')
            if name in user_stats and date_str in weekday_set:
                user_stats[name]['days'].add(date_str)
                if action:
                    user_stats[name]['actions'].append(action)

        # ─── 各ユーザーへ個人実績を送信 ───
        alert_users: list[str] = []  # 提出率80%未満の要注意者（管理者レポート用）
        total_rate_sum = 0.0

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)

            for user in users:
                name  = user['name']
                uid   = user['id']
                stats = user_stats[name]

                submitted_days = len(stats['days'])
                rate = (submitted_days / total_days * 100) if total_days > 0 else 0.0
                total_rate_sum += rate

                # 提出率80%未満は要注意リストに追加
                if rate < 80:
                    alert_users.append(
                        f'・{name}：{rate:.0f}%（{submitted_days}/{total_days}日）'
                    )

                # 主な活動TOP3を集計
                top3 = Counter(stats['actions']).most_common(3)
                top3_text = (
                    '\n'.join(
                        f'　{i + 1}位 {action}（{count}件）'
                        for i, (action, count) in enumerate(top3)
                    )
                    if top3 else '　（データなし）'
                )

                message = (
                    f'先月の実績（{month_label}）\n{name}さん\n\n'
                    f'提出日数：{submitted_days}日 / {total_days}日（{rate:.0f}%）\n'
                    f'主な活動TOP3\n{top3_text}'
                )

                print(f'[DEBUG] sending to {name} ({uid}) submitted={submitted_days}/{total_days} rate={rate:.0f}%')
                try:
                    result = line_bot_api.push_message(PushMessageRequest(
                        to=uid,
                        messages=[TextMessage(text=message)],
                    ))
                    print(f'[DEBUG] send OK ({name}): {result}')
                    logger.info(f'月次レポート送信完了: {name} ({uid})')
                except Exception as e:
                    print(f'[DEBUG] send ERROR ({name}): {e}')
                    logger.error(f'月次レポート送信失敗 ({uid}): {e}')

        # ─── 管理者に全体集計を送信 ───
        if LINE_USER_ID:
            overall_rate  = total_rate_sum / len(users) if users else 0.0
            alert_section = (
                '\n'.join(alert_users) if alert_users else '　（全員80%以上）'
            )
            admin_message = (
                f'先月の全体レポート（{month_label}）\n\n'
                f'全体提出率：{overall_rate:.0f}%（{len(users)}名）\n\n'
                f'要注意（80%未満）\n{alert_section}'
            )
            print(f'[DEBUG] sending admin report to {LINE_USER_ID}')
            try:
                with ApiClient(configuration) as api_client:
                    result = MessagingApi(api_client).push_message(PushMessageRequest(
                        to=LINE_USER_ID,
                        messages=[TextMessage(text=admin_message)],
                    ))
                print(f'[DEBUG] admin send OK: {result}')
                logger.info('月次管理者レポート送信完了')
            except Exception as e:
                print(f'[DEBUG] admin send ERROR: {e}')
                logger.error(f'月次管理者レポート送信失敗: {e}')
        else:
            logger.warning('LINE_USER_IDが未設定のため管理者レポートをスキップ')

        return jsonify({'status': 'ok', 'message': '月次レポートを送信しました'})

    except Exception as e:
        logger.error(f'monthly_report エラー: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500
