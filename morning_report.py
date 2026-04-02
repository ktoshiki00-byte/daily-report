import os
import time
import anthropic
from linebot.v3.messaging import (
    ApiClient, MessagingApi, Configuration,
    PushMessageRequest, TextMessage
)

# ── 設定 ──────────────────────────────
CLAUDE_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER   = os.environ.get("LINE_USER_ID", "")

# ── 各エージェントのプロンプト ──────
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
}

MORNING_Q = (
    "今朝の報告を以下の形式で3行以内で返してください。\n"
    "①注意が必要な案件\n"
    "②今日の推奨アクション\n"
    "③リスクや懸念事項"
)

# ── 市場トレンド調査プロンプト ──────
MARKET_PROMPT = """あなたは食器市場の市場調査専門家です。
以下の観点で今週の市場動向を分析してください：
- 蔦屋書店・フランフラン・actusなどインテリア雑貨店のトレンド
- Amazon・楽天の食器カテゴリ売れ筋
- 海外（北米・欧州・東南アジア）の食器トレンド
- 展示会・新商品情報

玉樹商店はタイ・マレーシア・四日市で食器を製造する会社です。
陶磁器・メラミン・各種素材の食器を製造できます。"""

MARKET_Q = """今週の食器市場トレンドを分析して以下を教えてください：
①今最も売れている・注目されている食器カテゴリ（3つ）
②蔦屋・フランフラン等インテリア雑貨店向けに今提案すべき商品の特徴
③Amazon・ECで伸びている食器の特徴
④海外輸出で狙えるカテゴリ
⑤玉樹商店が今すぐ取り組むべき提案アクション（具体的に2つ）"""

SUMMARY_SYSTEM = (
    "あなたは経営者の朝の意思決定を助けるアシスタントです。"
    "各部門の報告と市場情報を読んで、社長が今日最も注意すべき"
    "内容をLINEメッセージとして簡潔にまとめてください。"
)

# ── Claude を呼び出す関数 ──────────────
def ask_claude(system_prompt, question):
    client = anthropic.Anthropic(api_key=CLAUDE_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
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
    # 5エージェントに朝の質問
    reports = {}
    for name, prompt in AGENTS.items():
        try:
            reports[name] = ask_claude(prompt, MORNING_Q)
            time.sleep(1)
        except Exception as e:
            reports[name] = f"（取得エラー: {e}）"

    # 市場トレンド調査
    try:
        market_report = ask_claude(MARKET_PROMPT, MARKET_Q)
    except Exception as e:
        market_report = f"（市場調査エラー: {e}）"

    time.sleep(1)

    # 統合レポート生成
    combined = "\n\n".join(
        [f"【{k}】\n{v}" for k, v in reports.items()]
    )
    summary_q = (
        f"以下は各部門の朝の報告と市場情報です。\n\n"
        f"=== 各部門報告 ===\n{combined}\n\n"
        f"=== 市場トレンド ===\n{market_report}\n\n"
        "社長向けに以下の形式でまとめてください：\n"
        "🌅 玉樹商店 朝の報告\n\n"
        "📌 今日の最重要3件\n"
        "①\n②\n③\n\n"
        "🛍️ 今週の市場チャンス\n"
        "（蔦屋・EC・海外で今すぐ提案できる商品・切り口）\n\n"
        "⚡ 今日やるべきアクション\n"
        "（具体的に2つ）"
    )

    try:
        summary = ask_claude(SUMMARY_SYSTEM, summary_q)
    except Exception as e:
        summary = f"統合レポートエラー: {e}"

    send_line(summary)
    print("朝の報告をLINEに送信しました")

if __name__ == "__main__":
    morning_report()
