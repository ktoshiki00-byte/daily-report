# LINE 日報ボット

食器メーカー国内営業スタッフ向けの LINE 日報システムです。
毎日午前・午後の 2 回、ボタンをタップするだけで 1〜2 分で日報を送れます。

---

## 機能概要

| 機能 | 内容 |
|------|------|
| 自動リマインダー | 毎朝 9:00・14:00 に Flex Message を全員に送信 |
| ボタン選択 | 商談 / 移動・外出 / メーカー訪問 / 展示会 / 社内作業 / 工場対応 |
| 行動別フロー | アクションに応じて 1〜2 ステップで入力完了 |
| 自動保存 | Google スプレッドシートにリアルタイム保存 |

---

## ファイル構成

```
daily_report/
├── app.py            # メインサーバー（Flask）
├── requirements.txt  # Pythonパッケージ一覧
├── Procfile          # Render.com 起動設定
├── .env.example      # 環境変数のサンプル
└── README.md         # このファイル
```

---

## セットアップ手順

### ① LINE Developers の設定

1. [LINE Developers コンソール](https://developers.line.biz/) にアクセスしログイン
2. **新規プロバイダー** を作成（または既存を選択）
3. **Messaging API チャネル** を新規作成
4. チャネル設定ページで以下を取得・メモ：
   - **チャネルシークレット**（Basic settings タブ）
   - **チャネルアクセストークン（長期）**（Messaging API タブ → Issue ボタンをクリック）
5. Messaging API タブで **Webhookの利用** を **オン** にする
   （Webhook URL は後ほど Render のデプロイ後に設定）
6. **応答メッセージ** と **あいさつメッセージ** を **オフ** にする（自動返信と競合するため）

---

### ② Google Cloud / スプレッドシートの設定

#### 2-1. Google Cloud プロジェクトを作成

1. [Google Cloud コンソール](https://console.cloud.google.com/) にアクセス
2. 画面上部の **プロジェクトを選択** → **新しいプロジェクト** を作成

#### 2-2. API を有効化

1. 左メニュー → **APIとサービス** → **ライブラリ**
2. 「Google Sheets API」を検索して **有効にする**
3. 「Google Drive API」を検索して **有効にする**

#### 2-3. サービスアカウントを作成

1. 左メニュー → **IAMと管理** → **サービスアカウント**
2. **サービスアカウントを作成** をクリック
3. 名前を入力（例: `daily-report-bot`）して作成
4. 作成したサービスアカウントをクリック → **キー** タブ → **キーを追加** → **新しいキーを作成**
5. 形式 **JSON** を選択 → ダウンロードされる `.json` ファイルを保存

#### 2-4. スプレッドシートを作成・共有

1. [Google スプレッドシート](https://sheets.google.com/) で新規スプレッドシートを作成
2. スプレッドシートの **URL** からシート ID をコピー
   例: `https://docs.google.com/spreadsheets/d/【ここがシートID】/edit`
3. スプレッドシートの **共有** ボタンを押し、サービスアカウントのメールアドレス
   （例: `daily-report-bot@your-project.iam.gserviceaccount.com`）を **編集者** として追加

> シートは初回起動時に自動作成されます。「日報」シートと「users」シートが作られます。

---

### ③ Render.com へのデプロイ

#### 3-1. GitHubにコードをプッシュ

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/あなたのユーザー名/リポジトリ名.git
git push -u origin main
```

#### 3-2. Render でウェブサービスを作成

1. [Render.com](https://render.com/) にログイン（GitHub アカウントで登録推奨）
2. ダッシュボードで **New +** → **Web Service** をクリック
3. **Connect a repository** でさきほどのGitHubリポジトリを選択
4. 以下を設定：

| 項目 | 値 |
|------|----|
| Name | 任意（例: `daily-report-bot`） |
| Region | Singapore（日本に近い） |
| Branch | main |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app` |
| Instance Type | **Free** |

5. **Create Web Service** をクリック

#### 3-3. 環境変数を設定

デプロイ完了後、Render ダッシュボードのサービスページ → **Environment** タブで以下を追加：

| Key | Value |
|-----|-------|
| `LINE_CHANNEL_SECRET` | LINE チャネルシークレット |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE チャネルアクセストークン |
| `GOOGLE_CREDENTIALS` | サービスアカウントJSONファイルの**中身全体**をそのまま貼り付け |
| `GOOGLE_SHEET_ID` | スプレッドシートのID |

> `GOOGLE_CREDENTIALS` の貼り付け方：ダウンロードした JSON ファイルをテキストエディタで開き、`{` から `}` までの全文字列をコピーして貼り付けてください。

設定後 **Save Changes** → サービスが自動的に再デプロイされます。

#### 3-4. デプロイURLの確認

Render ダッシュボードのサービス名の下に表示される URL（例: `https://daily-report-bot.onrender.com`）をメモしておきます。

---

### ④ LINE Webhook URL の設定

1. LINE Developers コンソール → 作成したチャネル → **Messaging API** タブ
2. **Webhook URL** に以下を入力：
   `https://あなたのRenderURL/callback`
   例: `https://daily-report-bot.onrender.com/callback`
3. **検証** ボタンを押して「Success」が表示されればOK
4. **Webhookの利用** が **オン** になっていることを確認

---

### ⑤ cron-job.org の設定

午前・午後のリマインダーを自動送信するために、外部サービス「cron-job.org」を使います。

1. [cron-job.org](https://cron-job.org/) にアクセスしてアカウント登録（無料）
2. ダッシュボードで **CREATE CRONJOB** をクリック

#### 午前 9:00 の設定

| 項目 | 値 |
|------|----|
| Title | 日報リマインダー（午前） |
| URL | `https://あなたのRenderURL/morning` |
| Schedule | **Custom** → Minutes: `0`, Hours: `0`（UTC） |

> ⚠️ **UTC と JST の変換**: cron-job.org は UTC で動作します。
> 日本時間 9:00 = UTC 0:00、日本時間 14:00 = UTC 5:00

| 時間帯 | 日本時間（JST） | UTC設定 |
|--------|---------------|---------|
| 午前リマインダー | 9:00 | Hours: `0`, Minutes: `0` |
| 午後リマインダー | 14:00 | Hours: `5`, Minutes: `0` |

#### 午後 14:00 の設定

| 項目 | 値 |
|------|----|
| Title | 日報リマインダー（午後） |
| URL | `https://あなたのRenderURL/afternoon` |
| Schedule | **Custom** → Minutes: `0`, Hours: `5`（UTC） |

3. それぞれ **CREATE** をクリックして保存

---

## ユーザー登録方法（スタッフ向け）

1. LINE で日報ボットを**友だち追加**
2. チャットで **「登録」** と送信
3. 「✅ ○○さんを登録しました！」と返信が来れば完了

登録後は毎朝 9:00 と 14:00 に自動でリマインダーが届きます。

---

## 日報入力フロー

```
リマインダー受信
    ↓
ボタンをタップ
    ↓
【商談 / メーカー訪問 / 展示会・イベント】
  → 訪問先の会社名を入力
  → 自由メモを入力（または「スキップ」）
  → ✅ 記録しました！

【移動・外出】
  → 移動先・目的地を入力
  → ✅ 記録しました！

【社内作業】
  → 作業内容を入力
  → ✅ 記録しました！

【工場対応】
  → 対応内容を入力
  → ✅ 記録しました！
```

---

## スプレッドシートの列構成

**「日報」シート**

| 列 | 内容 |
|----|------|
| A | 日付（例: 2024/04/01） |
| B | 時間（例: 09:15） |
| C | ユーザー名 |
| D | 午前 or 午後 |
| E | 行動種別 |
| F | 訪問先会社名 |
| G | 移動先 |
| H | 作業内容 |
| I | 工場対応内容 |
| J | 自由メモ |

**「users」シート**

| 列 | 内容 |
|----|------|
| A | LINE表示名 |
| B | ユーザーID |

---

## ローカル開発環境での動作確認

```bash
# 1. 仮想環境を作成・有効化
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. 依存パッケージをインストール
pip install -r requirements.txt

# 3. 環境変数ファイルを作成
cp .env.example .env
# .env を編集して各値を入力

# 4. サーバーを起動
python app.py

# 5. ngrok でローカルサーバーを公開（別ターミナル）
ngrok http 5000
# → 表示されたhttps://xxxxx.ngrok.io/callback をLINE Webhook URLに設定
```

---

## よくあるトラブル

| 症状 | 確認ポイント |
|------|------------|
| Webhookの検証が失敗する | `LINE_CHANNEL_SECRET` の値が正しいか確認 |
| メッセージが届かない | `LINE_CHANNEL_ACCESS_TOKEN` が正しいか確認。Render のログを確認 |
| スプレッドシートに保存されない | サービスアカウントのメールがスプレッドシートの「編集者」に追加されているか確認 |
| リマインダーが届かない | cron-job.org の URL・時刻設定（UTC）を再確認 |
| Render が起動しない | `GOOGLE_CREDENTIALS` に JSON が正しく貼れているか確認（改行・クォートに注意） |

---

## 注意事項

- **Render 無料プラン**: 15 分間アクセスがないとサーバーがスリープします。cron-job.org のリクエストで自動的に起動しますが、最初の応答に 30〜60 秒かかることがあります。
- **インメモリ状態管理**: ユーザーの入力待ち状態はサーバーメモリで管理しています。サーバー再起動時に入力途中の状態はリセットされます（通常運用では問題ありません）。
