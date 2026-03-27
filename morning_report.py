import os
import anthropic
from linebot.v3.messaging import (
    ApiClient, MessagingApi, Configuration,
    PushMessageRequest, TextMessage
)

# ── 設定 ──────────────────────────────
CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER   = os.environ.get("LINE_USER_ID", "")

# ── 各エージェントのプロンプトと朝の質問 ──────
AGENTS = {
    "営業": """あなたは玉樹商店の営業部門を支援するAIエージェントです。
10年選手の営業として、MUJI・ダイソー・アダストリア等の顧客対応、
見積・受注・納期調整をサポートします。""",

    "管理": """あなたは玉樹商店の経営管理部門を支援するAIエージェントです。
KPI・売上・コスト・予算管理の10年選手として、
経営者が意思決定しやすい形で情報を整理・提供します。""",

    "品管": """あなたは玉樹商店の品質管理部門を支援するAIエージェントです。
タイ・マレーシア・四日市の3工場の品質データを管理する10年選手として、
不良分析・クレーム対応・是正指示をサポートします。""",

    "工場": """あなたは玉樹商店の工場管理部門を支援するAIエージェントです。
タイ・マレーシア・四日市3工場の10年選手として、
生産配分・納期管理・資材調達をサポートします。""",

    "人事": """あなたは玉樹商店の人事部門を支援するAIエージェントです。
タイ・マレーシア・四日市・商社部門470名を管轄する10年選手として、
採用・勤怠・多文化対応をサポートします。""",

    "マーケ": """あなたは玉樹商店のマーケティング部門を支援するAIエージェントです。
食器市場のトレンド・競合・展示会に精通した10年選手として、
商品開発と営業に市場インテリジェンスを提供します。""",

    "商品開発": """あなたは玉樹商店の商品開発・企画部門を支援するAIエージェントです。
食器デザイン・素材・製造工程と市場トレンドを統合判断できる10年選手として、
企画書・仕様書・サンプル管理をサポートします。""",
}

MORNING_Q = (
    "今朝の報告を以下の形式で3行以内で返してください。\n"
    "①注意が必要な案件\n"
    "②今日の推奨アクション\n"
    "③リスクや懸念事項"
)

SUMMARY_SYSTEM = (
    "あなたは経営者の朝の意思決定を助けるアシスタントです。"
    "7つのエージェントからの報告を読んで、社長が今日最も注意すべき"
    "3件をLINEメッセージとして簡潔にまとめてください。"
)

# ── Claude を呼び出す関数 ──────────────
def ask_claude(system_prompt, question):
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": question}]
    )
    return msg.content[0].text

# ── LINEに送信する関数 ──────────────────
def send_line(text):
    config = Configuration(access_token=LINE_TOKEN)
    with ApiClient(config) as api_client:
        api = MessagingApi(api_client)
        api.push_message(
            PushMessageRequest(
                to=LINE_USER,
                messages=[TextMessage(text=text)]
            )
        )

# ── メイン処理 ──────────────────────────
def morning_report():
    # 7エージェントに朝の質問を送る
    reports = {}
    for name, prompt in AGENTS.items():
        try:
            reports[name] = ask_claude(prompt, MORNING_Q)
        except Exception as e:
            reports[name] = f"（取得エラー: {e}）"

    # 7つの報告を統合して社長向けにまとめる
    combined = "\n\n".join(
        [f"【{k}】\n{v}" for k, v in reports.items()]
    )
    summary_q = (
        f"以下は7エージェントの朝の報告です。\n\n{combined}\n\n"
        "社長向けに「今日最重要の3件」をまとめてください。\n"
        "形式：\n"
        "🌅 玉樹商店 朝の報告\n\n"
        "📌 今日の最重要3件\n"
        "①\n②\n③\n\n"
        "📊 各部門ひとこと（営業／管理／品管／工場）"
    )

    try:
        summary = ask_claude(SUMMARY_SYSTEM, summary_q)
    except Exception as e:
        summary = f"統合レポートエラー: {e}"

    # LINEに送信
    send_line(summary)
    print("朝の報告をLINEに送信しました")

if __name__ == "__main__":
    morning_report()
