# job-scraper

Indeed 等の求人サイトから企業情報を収集して Google スプレッドシートに書き込むツール。

## ⚠️ 重要な注意事項

- **Indeed の利用規約はスクレイピングを明確に禁止しています**。本ツールは技術検証・個人利用目的で作成されており、商用利用・大規模な自動収集は行わないでください。
- IP ブロック、法的リスク（不正競争防止法、著作権法違反等）を承知の上で使用してください。
- 実運用が必要な場合は Indeed Publisher API 等の公式 API 利用を検討してください。

## 収集項目

| 項目 | 備考 |
|------|------|
| 会社名 | 求人ページから抽出 |
| 住所 | 記載がある場合のみ |
| 電話番号 | Indeed には基本非掲載。企業サイト経由の追加取得が必要 |
| 業種 | 求人カテゴリ・企業情報から推定 |
| 掲載求人 URL | 求人詳細ページの URL |

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
# output/jobs-YYYYMMDD-HHMMSS.csv に書き出し
python -m src.main --keyword "エンジニア" --location "東京" --max-pages 3

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
│   ├── main.py           # エントリポイント
│   ├── scraper.py        # 求人サイトスクレイピング
│   ├── csv_writer.py     # CSV 書き出し（デフォルト）
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

## ライセンス

Private / 個人利用専用
