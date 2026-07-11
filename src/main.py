"""エントリポイント: CLI から実行する。

## 単発モード（scrape）
    python -m src.main scrape --site hellowork --keyword エンジニア --max-pages 1
    python -m src.main scrape --site indeed --keyword 事務 --location 東京 --max-pages 1

## 自動巡回 tick モード（SQLite で dedup + 状態管理）
    # 直近実行が最古の (keyword, location) を選んで 1 セット取得
    python -m src.main tick --site indeed --max-pages 1

## 進捗確認
    python -m src.main status --site indeed
    python -m src.main status --site hellowork

## SQLite → 電話番号あり求人のみを CSV に export
    python -m src.main export --site indeed --output output/indeed-phone.csv
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
from .query_pools import hellowork_query_pool, indeed_query_pool
from .scraper import BanDetectedError, IndeedScraper
from .state import StateStore


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


def _make_scraper(site: str, delay: float, headless: bool):
    if site == "hellowork":
        return HelloWorkScraper(request_delay_seconds=delay, headless=headless)
    return IndeedScraper(request_delay_seconds=delay, headless=headless)


def _cmd_scrape(args: argparse.Namespace, logger: logging.Logger) -> int:
    """単発スクレイプ（従来の挙動）。"""
    headless = os.getenv("HEADLESS", "true").lower() == "true"
    delay = float(os.getenv("REQUEST_DELAY_SECONDS", "3"))

    with _make_scraper(args.site, delay, headless) as scraper:
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
        return 1 if appended < 0 else 0

    writer = CsvWriter(output_dir=args.output_dir, filename=args.filename)
    path, written = writer.write(postings)
    logger.info(f"完了: {written} 件を CSV に書き出しました → {path}")
    return 0


def _cmd_tick(args: argparse.Namespace, logger: logging.Logger) -> int:
    """1 tick: 未実行 or 最古の (keyword, location) を選んで scrape、SQLite に保存。"""
    store = StateStore()
    site = args.site

    if reason := store.is_paused(site):
        logger.warning(f"pause 中のため skip: {reason}")
        return 0

    pool = indeed_query_pool() if site == "indeed" else hellowork_query_pool()
    keyword, location = store.pick_next_query(site, pool)
    logger.info(f"tick 開始: site={site} keyword='{keyword}' location='{location}'")

    run_id = store.start_run(site, keyword, location)
    headless = os.getenv("HEADLESS", "true").lower() == "true"
    delay = float(os.getenv("REQUEST_DELAY_SECONDS", "60" if site == "indeed" else "3"))

    items_new = 0
    items_dup = 0
    status = "completed"

    try:
        with _make_scraper(site, delay, headless) as scraper:
            for posting in scraper.search(
                keyword=keyword, location=location, max_pages=args.max_pages
            ):
                if store.save_posting(posting, site, keyword, location):
                    items_new += 1
                else:
                    items_dup += 1
    except BanDetectedError as e:
        logger.error(f"BAN 検知: {e}")
        pause_sec = int(os.getenv("BAN_PAUSE_SECONDS", "3600"))
        store.set_pause(site, pause_sec, reason=str(e))
        status = "banned"
    except Exception as e:
        logger.exception(f"tick 中エラー: {e}")
        status = "error"
    finally:
        store.finish_run(run_id, items_new, items_dup, status=status)

    counts = store.counts(site)
    logger.info(
        f"tick 完了: new={items_new} dup={items_dup} status={status} "
        f"total={counts['total']} with_phone={counts['with_phone']}"
    )
    return 0 if status == "completed" else 1


def _cmd_status(args: argparse.Namespace, logger: logging.Logger) -> int:
    store = StateStore()
    counts = store.counts(args.site)
    paused = store.is_paused(args.site)
    runs = store.recent_runs(args.site, limit=10)

    print(f"=== 状態: site={args.site} ===")
    print(f"総取得件数: {counts['total']}")
    print(f"電話番号あり: {counts['with_phone']}")
    print(f"pause 状態: {paused or 'なし'}")
    print(f"直近 10 run:")
    for r in runs:
        finished = r["finished_at"] or "(実行中)"
        print(
            f"  #{r['id']:>5}  kw={r['keyword']:<10}  loc={r['location']:<8}  "
            f"start={r['started_at']}  end={finished}  "
            f"new={r['items_new']}  dup={r['items_dup']}  status={r['status']}"
        )
    return 0


def _cmd_export(args: argparse.Namespace, logger: logging.Logger) -> int:
    store = StateStore()
    postings = store.export_with_phone(args.site)
    logger.info(f"電話番号あり {len(postings)} 件を CSV に書き出します。")

    output_path = Path(args.output)
    writer = CsvWriter(output_dir=output_path.parent, filename=output_path.name)
    path, written = writer.write(postings)
    print(f"完了: {written} 件を書き出しました → {path}")
    return 0


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="求人スクレイパー（ハローワーク / Indeed 対応・SQLite 状態管理）",
    )
    sub = parser.add_subparsers(dest="command")

    # scrape (legacy)
    p_scrape = sub.add_parser("scrape", help="単発スクレイプ（CSV 直接出力）")
    p_scrape.add_argument("--site", choices=["hellowork", "indeed"], default="hellowork")
    p_scrape.add_argument("--keyword", required=True)
    p_scrape.add_argument("--location", default="")
    p_scrape.add_argument("--max-pages", type=int, default=1)
    p_scrape.add_argument("--output-dir", default="output")
    p_scrape.add_argument("--filename", default=None)
    p_scrape.add_argument("--sheets", action="store_true")
    p_scrape.add_argument("--dry-run", action="store_true")

    # tick (auto rotation)
    p_tick = sub.add_parser("tick", help="自動巡回 1 tick（SQLite dedup）")
    p_tick.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")
    p_tick.add_argument("--max-pages", type=int, default=1)

    # status
    p_status = sub.add_parser("status", help="進捗確認")
    p_status.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")

    # export
    p_export = sub.add_parser("export", help="電話番号あり求人を CSV エクスポート")
    p_export.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")
    p_export.add_argument("--output", required=True)

    return parser


def _parse_args_with_legacy() -> argparse.Namespace:
    """後方互換: 旧 CLI (subcommand なし) を scrape として扱う。"""
    argv = sys.argv[1:]
    known_commands = {"scrape", "tick", "status", "export"}
    if not argv or (argv[0].startswith("-") and argv[0] not in known_commands):
        argv = ["scrape"] + argv
    return _build_parser().parse_args(argv)


def main() -> int:
    load_dotenv()
    _configure_logging(
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE"),
    )
    logger = logging.getLogger(__name__)

    args = _parse_args_with_legacy()
    if args.command == "scrape":
        return _cmd_scrape(args, logger)
    if args.command == "tick":
        return _cmd_tick(args, logger)
    if args.command == "status":
        return _cmd_status(args, logger)
    if args.command == "export":
        return _cmd_export(args, logger)
    _build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
