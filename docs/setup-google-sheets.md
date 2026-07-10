# Google Sheets API セットアップ手順

## 1. Google Cloud プロジェクト作成

1. https://console.cloud.google.com/ にアクセス
2. 上部プロジェクト選択 → 「新しいプロジェクト」
3. プロジェクト名（例: `job-scraper`）を入力して作成

## 2. API を有効化

1. 左メニュー「API とサービス」→「ライブラリ」
2. 以下 2 つを検索して有効化：
   - **Google Sheets API**
   - **Google Drive API**

## 3. サービスアカウント作成

1. 左メニュー「API とサービス」→「認証情報」
2. 「認証情報を作成」→「サービスアカウント」
3. サービスアカウント名を入力（例: `job-scraper-sa`）
4. 「作成して続行」→ ロールは不要（スキップ）→「完了」

## 4. JSON 鍵を発行

1. 認証情報一覧から作成したサービスアカウントをクリック
2. 「キー」タブ →「鍵を追加」→「新しい鍵を作成」
3. JSON を選択 →「作成」→ JSON ファイルがダウンロードされる
4. ダウンロードしたファイルを `config/service-account.json` として配置

> ⚠️ この JSON ファイルは絶対に Git に commit しないこと。
> `.gitignore` で除外済みだが、念のため配置前に `git status` で確認推奨。

## 5. スプレッドシートを準備

1. https://sheets.google.com/ で新規スプレッドシートを作成
2. URL から ID を取得：
   ```
   https://docs.google.com/spreadsheets/d/【この部分がID】/edit
   ```
3. `.env` の `SPREADSHEET_ID` に貼り付け

## 6. サービスアカウントに共有権限を付与

1. スプレッドシート右上の「共有」ボタン
2. サービスアカウントのメールアドレス（`xxx@yyy.iam.gserviceaccount.com` 形式）を入力
3. 権限を「編集者」に設定して送信

> サービスアカウントのメールは JSON の `client_email` フィールドで確認可能

## 7. 動作確認

```bash
source .venv/bin/activate
python -m src.main --keyword "エンジニア" --location "東京" --max-pages 1 --dry-run
```

dry-run で求人取得を確認 → 問題なければ `--dry-run` を外して実行。
