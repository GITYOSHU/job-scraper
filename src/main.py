"""エントリポイント: CLI から実行する。

使用例:
    # ハローワーク (デフォルト・電話番号/代表者名まで取得可・BAN リスク低)
    python -m src.main --keyword "エンジニア" --max-pages 1

    # Indeed に切替 (電話番号/代表者名は基本取れない)
    python -m src.main --keyword "エンジニア" --site indeed

    # Google Sheets に書き込み (要: config/service-account.json + .env)
    python -m src.main --keyword "エンジニア" --sheets

    # 標準出力のみ (書き込み無し)
    python -m src.main --keyword "エンジニア" --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .csv_writer import CsvWriter
from .hellowork import HelloWorkScraper
from .scraper import IndeedScraper


def _configure_logging(log_level: str, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="求人スクレイパー（ハローワーク / Indeed 対応）",
    )
    parser.add_argument(
        "--site",
        choices=["hellowork", "indeed"],
        default="hellowork",
        help="対象サイト（デフォルト: hellowork）",
    )
    parser.add_argument("--keyword", required=True, help="検索キーワード（例: エンジニア）")
    parser.add_argument("--location", default="", help="勤務地（Indeed のみ有効。例: 東京）")
    parser.add_argument("--max-pages", type=int, default=1, help="取得ページ数（デフォルト: 1）")
    parser.add_argument(
        "--output-dir",
        default="output",
        help="CSV 出力先ディレクトリ（デフォルト: output）",
    )
    parser.add_argument(
        "--filename",
        default=None,
        help="CSV ファイル名（省略時は jobs-YYYYMMDD-HHMMSS.csv）",
    )
    parser.add_argument(
        "--sheets",
        action="store_true",
        help="Google Sheets に書き込む（要: config/service-account.json + .env の SPREADSHEET_ID）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ファイル書き込みせず標準出力に表示のみ",
    )
    return parser.parse_args()


def _write_to_sheets(postings: list, logger: logging.Logger) -> int:
    """Google Sheets 書き込み。sheets モジュールは遅延 import で最適化。"""
    from .sheets import SheetsWriter

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        logger.error("SPREADSHEET_ID が設定されていません。.env を確認してください。")
        return -1

    writer = SheetsWriter(
        service_account_path=os.getenv("SERVICE_ACCOUNT_PATH", "config/service-account.json"),
        spreadsheet_id=spreadsheet_id,
        worksheet_name=os.getenv("WORKSHEET_NAME", "求人リスト"),
    )
    return writer.append_postings(postings)


def main() -> int:
    load_dotenv()

    _configure_logging(
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE"),
    )
    logger = logging.getLogger(__name__)

    args = _parse_args()

    headless = os.getenv("HEADLESS", "true").lower() == "true"
    delay = float(os.getenv("REQUEST_DELAY_SECONDS", "3"))

    if args.site == "hellowork":
        scraper_ctx = HelloWorkScraper(request_delay_seconds=delay, headless=headless)
    else:
        scraper_ctx = IndeedScraper(request_delay_seconds=delay, headless=headless)

    with scraper_ctx as scraper:
        postings = list(
            scraper.search(
                keyword=args.keyword,
                location=args.location,
                max_pages=args.max_pages,
            )
        )
    logger.info(f"合計 {len(postings)} 件の求人を取得しました（site={args.site}）。")

    if args.dry_run:
        for posting in postings:
            print(posting.to_dict())
        return 0

    if args.sheets:
        appended = _write_to_sheets(postings, logger)
        if appended < 0:
            return 1
        logger.info(f"完了: {appended} 件をスプレッドシートに追記しました。")
        return 0

    writer = CsvWriter(output_dir=args.output_dir, filename=args.filename)
    path, written = writer.write(postings)
    logger.info(f"完了: {written} 件を CSV に書き出しました → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
