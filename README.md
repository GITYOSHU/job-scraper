# job-scraper

求人サイト（ハローワーク / Indeed）から企業情報を収集して CSV / Google スプレッドシートに書き込むツール。

## 対応サイト

| サイト | 電話番号 | 代表者名 | 業種 | BAN リスク | 推奨度 |
|--------|----------|----------|------|-----------|--------|
| **ハローワーク**（デフォルト） | ✅ 93% | ✅ 93% | ✅ 100% | 低（公的サイト） | ⭐⭐⭐⭐⭐ |
| Indeed | ❌ regex 抽出のみ | ❌ | ❌ | 高（ToS 違反） | ⭐ |

## ⚠️ 注意事項

- **Indeed の利用規約はスクレイピングを禁止**しています。Indeed モードは技術検証・個人利用目的のみ。連続アクセスで即 IP ブロックされます。
- ハローワークは公的サイトのため BAN リスクは低いですが、大量アクセスは避けてください（`REQUEST_DELAY_SECONDS` を長めに）。

## 収集項目

| 項目 | ハローワーク | Indeed |
|------|-------------|--------|
| 会社名 | ✅ 100% | ✅ 100% |
| 住所 | ✅ 93% | ✅ 100% |
| 電話番号 | ✅ 93% | △ 求人本文から regex 抽出 |
| 業種（産業分類） | ✅ 100% | ❌ |
| 代表者名 | ✅ 93% | ❌ |
| 掲載求人 URL | ✅ 100% | ✅ 100% |
| 取得日時 | ✅ 自動付与 (JST) | ✅ 自動付与 (JST) |

## 技術スタック

- Python 3.11+
- requests + BeautifulSoup4 (HTML パーサ)
- gspread + google-auth (Google Sheets API)
- python-dotenv (環境変数管理)

## セットアップ

### 1. Python 環境

```bash
cd /Users/yoshu/job-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. (オプション) Google Sheets API 設定

**CSV 出力だけで使う場合はスキップ可**。スプシに直接書き込みたい場合のみ設定。

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクト作成
2. Google Sheets API + Google Drive API を有効化
3. サービスアカウント作成 → JSON 鍵をダウンロード
4. `config/service-account.json` として配置
5. 書き込み先スプレッドシートをサービスアカウントのメールアドレスに共有
6. `cp .env.example .env` → `SPREADSHEET_ID` を記入

詳細: [`docs/setup-google-sheets.md`](./docs/setup-google-sheets.md)

## 使い方

### CSV 出力（デフォルト）

```bash
# ハローワーク（デフォルト・電話/代表者名まで取得）
python -m src.main --keyword "エンジニア" --max-pages 1

# Indeed に切替（電話/代表者名は基本取れない・BAN リスクあり）
python -m src.main --site indeed --keyword "エンジニア" --location "東京" --max-pages 1

# ファイル名指定
python -m src.main --keyword "エンジニア" --filename result.csv

# 出力先ディレクトリ指定
python -m src.main --keyword "エンジニア" --output-dir ~/Desktop
```

出力 CSV は BOM 付き UTF-8。Excel でそのまま開いても文字化けしない。

### スプシに直接書き込み（要: サービスアカウント設定）

```bash
python -m src.main --keyword "エンジニア" --sheets
```

### 動作確認（書き込み無し）

```bash
python -m src.main --keyword "エンジニア" --max-pages 1 --dry-run
```

## ディレクトリ構成

```
job-scraper/
├── src/
│   ├── __init__.py
│   ├── main.py           # CLI エントリポイント（--site で切替）
│   ├── hellowork.py      # ハローワークスクレイパー（デフォルト）
│   ├── scraper.py        # Indeed スクレイパー
│   ├── extractors.py     # 求人本文からの regex 抽出（Indeed 電話番号用）
│   ├── csv_writer.py     # CSV 書き出し（デフォルト出力）
│   ├── sheets.py         # Google Sheets 書き込み（--sheets 指定時）
│   └── models.py         # データモデル
├── tests/                # ユニットテスト
├── output/               # CSV 出力先（.gitignore 対象）
├── config/               # 認証情報（.gitignore 対象）
├── docs/                 # ドキュメント
├── .env.example
├── .gitignore
├── requirements.txt
├── requirements-dev.txt  # pytest 等
└── README.md
```

## GitHub Actions で自動実行

`.github/workflows/scrape-hellowork.yml` により GitHub 上で完全自動実行可能。
PC オフラインでも動作する。

### 手動実行

1. https://github.com/GITYOSHU/job-scraper/actions を開く
2. 左メニュー「Scrape HelloWork」を選択
3. 「Run workflow」ボタンを押す
4. パラメータを指定して実行:
   - `keyword`: 検索キーワード（デフォルト: エンジニア）
   - `max_pages`: 取得ページ数（デフォルト: 3 = 90 件）
   - `request_delay_seconds`: リクエスト間隔秒（デフォルト: 3）

### 定期実行

デフォルトで **毎日 JST 9:00** に自動実行。`.github/workflows/scrape-hellowork.yml` の `cron` 部分で調整可能。

### CSV の受け取り

実行完了後、workflow ページの `Artifacts` セクションから CSV ダウンロード可能（保存期間 30 日）。

### GitHub Actions 無料枠

- Private リポジトリ: 月 2000 分（33 時間）
- Public リポジトリ: 無制限
- 1000 件取得 = 約 80 分 → 月 20 回程度が上限目安

## ライセンス

Private / 個人利用専用
