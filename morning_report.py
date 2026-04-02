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
玉樹商店はタイ・マレーシア・四日市で食器を製造する会社です。
陶磁器・メラミン・各種素材の食器を製造できます。
主要顧客：MUJI・ダイソー・スタンダードプロダクツ・AEON・飲食店向け"""

MARKET_Q = """今週の食器市場トレンドを分析して以下を教えてください：
①今最も注目されている食器カテゴリ（3つ）
②蔦屋・フランフラン等インテリア雑貨店向けに今提案すべき商品の特徴
③Amazon・ECで伸びている食器の特徴
④海外輸出で狙えるカテゴリ"""

# ── 商品企画提案プロンプト ──────
PLANNING_PROMPT = """あなたは玉樹商店の商品企画専門家です。
【製造能力】
- タイ工場：陶磁器・メラミン（大量生産）
- マレーシア工場：大ロット生産
- 四日市工場：高付加価値・試作
【主要顧客】MUJI・ダイソー・スタンダードプロダクツ・AEON・飲食店
【強み】製造から販売まで一貫、美濃焼・漆器の知識"""

PLANNING_Q = """市場トレンドを踏まえて、今週玉樹商店が取り組むべき商品企画を提案してください：

①今すぐ提案できる商品アイデア（3つ）
　- 商品名・コンセプト
　- 提案先チャネル（蔦屋/EC/海外/既存顧客）
　- 製造拠点（タイ/マレーシア/四日市）
　- 想定単価

②今週アクションすべきこと（具体的に2つ）"""

SUMMARY_SYSTEM = (
    "あなたは経営者の朝の意思決定を助けるアシスタントです。"
    "各部門の報告と市場情報・商品企画を読んで、社長向けに"
    "LINEメッセージとして簡潔にまとめてください。"
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

    # 商品企画提案
    try:
        planning_q = f"【今週の市場トレンド】\n{market_report}\n\n{PLANNING_Q}"
        planning_report = ask_claude(PLANNING_PROMPT, planning_q)
    except Exception as e:
        planning_report = f"（企画提案エラー: {e}）"
    time.sleep(1)

    # 統合レポート生成
    combined = "\n\n".join(
        [f"【{k}】\n{v}" for k, v in reports.items()]
    )
   summary_q = (
        f"=== 各部門報告 ===\n{combined}\n\n"
        f"=== 市場トレンド ===\n{market_report}\n\n"
        f"=== 商品企画提案 ===\n{planning_report}\n\n"
        "社長向けに以下の形式でまとめてください：\n"
        "🌅 玉樹商店 朝の報告\n\n"
        "📌 今日の最重要3件\n"
        "①【営業】...\n"
        "②【品管/工場】...\n"
        "③【市場】...\n\n"
        "🛍️ 今週の商品企画チャンス\n"
        "（商品名・提案先・製造拠点・単価を明記）\n\n"
        "⚡ 今日やるべきアクション\n"
        "①...\n②..."
    )

    try:
        summary = ask_claude(SUMMARY_SYSTEM, summary_q)
    except Exception as e:
        summary = f"統合レポートエラー: {e}"

    send_line(summary)
    print("朝の報告をLINEに送信しました")

if __name__ == "__main__":
    morning_report()
