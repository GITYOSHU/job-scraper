"""エントリポイント: CLI から実行する。

使用例:
    python -m src.main --keyword "エンジニア" --location "東京" --max-pages 3
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .scraper import IndeedScraper
from .sheets import SheetsWriter


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
        description="Indeed 求人スクレイパー（技術検証・個人利用専用）",
    )
    parser.add_argument("--keyword", required=True, help="検索キーワード（例: エンジニア）")
    parser.add_argument("--location", default="", help="勤務地（例: 東京）")
    parser.add_argument("--max-pages", type=int, default=1, help="取得ページ数（デフォルト: 1）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="スプレッドシートに書き込まず標準出力に表示のみ",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()

    _configure_logging(
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE"),
    )
    logger = logging.getLogger(__name__)

    args = _parse_args()

    scraper = IndeedScraper(
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "3")),
        rotate_user_agent=os.getenv("USER_AGENT_ROTATION", "true").lower() == "true",
    )

    postings = list(
        scraper.search(
            keyword=args.keyword,
            location=args.location,
            max_pages=args.max_pages,
        )
    )
    logger.info(f"合計 {len(postings)} 件の求人を取得しました。")

    if args.dry_run:
        for posting in postings:
            print(posting.to_dict())
        return 0

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        logger.error("SPREADSHEET_ID が設定されていません。.env を確認してください。")
        return 1

    writer = SheetsWriter(
        service_account_path=os.getenv("SERVICE_ACCOUNT_PATH", "config/service-account.json"),
        spreadsheet_id=spreadsheet_id,
        worksheet_name=os.getenv("WORKSHEET_NAME", "求人リスト"),
    )
    appended = writer.append_postings(postings)
    logger.info(f"完了: {appended} 件をスプレッドシートに追記しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
