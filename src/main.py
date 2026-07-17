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

from .apify_scraper import ApifyIndeedScraper, ApifyScrapingError
from .csv_writer import CsvWriter
from .hellowork import HelloWorkScraper
from .proxy_config import load_proxy_from_env
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
    # Indeed の engine 選択: SCRAPER_ENGINE env で切替
    #   - "apify" (default): Apify misceres/indeed-scraper (安価・効率的)
    #   - "bright_data": Playwright + Bright Data proxy (旧方式)
    engine = os.environ.get("SCRAPER_ENGINE", "apify").lower()
    if engine == "apify":
        return ApifyIndeedScraper(request_delay_seconds=delay, headless=headless)
    proxy = load_proxy_from_env()
    return IndeedScraper(
        request_delay_seconds=delay, headless=headless, proxy=proxy
    )


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
    """1 tick で N クエリ連続実行 (Playwright browser は使い回し)。

    QUERIES_PER_TICK env で 1 tick あたりのクエリ数を制御。デフォルト 10。
    BAN 検知時は即中断・pause 遷移。
    """
    store = StateStore()
    site = args.site

    if reason := store.is_paused(site):
        logger.warning(f"pause 中のため skip: {reason}")
        return 0

    pool = indeed_query_pool() if site == "indeed" else hellowork_query_pool()
    # shard filter: round-robin で pool を分割
    if args.total_shards > 1:
        pool = [q for i, q in enumerate(pool) if i % args.total_shards == args.shard]
        logger.info(
            f"shard={args.shard}/{args.total_shards} で {len(pool)} クエリを担当"
        )
    headless = os.getenv("HEADLESS", "true").lower() == "true"
    delay = float(os.getenv("REQUEST_DELAY_SECONDS", "60" if site == "indeed" else "3"))
    queries_per_tick = int(os.getenv("QUERIES_PER_TICK", "10"))

    total_new = 0
    total_dup = 0
    tick_status = "completed"

    logger.info(f"tick 開始: site={site} queries_per_tick={queries_per_tick}")

    try:
        with _make_scraper(site, delay, headless) as scraper:
            for q_index in range(queries_per_tick):
                if store.is_paused(site):
                    logger.warning(f"tick 中に pause 検知 ({q_index}/{queries_per_tick} で中断)")
                    break

                keyword, location = store.pick_next_query(site, pool)
                logger.info(
                    f"query {q_index + 1}/{queries_per_tick}: "
                    f"keyword='{keyword}' location='{location}'"
                )
                run_id = store.start_run(site, keyword, location)
                items_new = 0
                items_dup = 0
                query_status = "completed"

                try:
                    for posting in scraper.search(
                        keyword=keyword, location=location, max_pages=args.max_pages
                    ):
                        if store.save_posting(posting, site, keyword, location):
                            items_new += 1
                        else:
                            items_dup += 1
                except BanDetectedError as e:
                    logger.error(f"BAN 検知 (query {q_index + 1}): {e}")
                    pause_sec = int(os.getenv("BAN_PAUSE_SECONDS", "3600"))
                    store.set_pause(site, pause_sec, reason=str(e))
                    query_status = "banned"
                    tick_status = "banned"
                except ApifyScrapingError as e:
                    logger.error(f"Apify エラー (query {q_index + 1}): {e}")
                    query_status = "error"
                    # Apify quota/auth 系 は pause 相当 (BAN と同扱い)
                    if "402" in str(e) or "429" in str(e) or "quota" in str(e).lower():
                        pause_sec = int(os.getenv("BAN_PAUSE_SECONDS", "3600"))
                        store.set_pause(site, pause_sec, reason=str(e))
                        tick_status = "banned"
                except Exception as e:
                    logger.exception(f"query {q_index + 1} 中エラー: {e}")
                    query_status = "error"
                finally:
                    store.finish_run(run_id, items_new, items_dup, status=query_status)
                    total_new += items_new
                    total_dup += items_dup

                if query_status == "banned":
                    break
    except Exception as e:
        logger.exception(f"tick 中の scraper エラー: {e}")
        tick_status = "error"

    counts = store.counts(site)
    logger.info(
        f"tick 完了: new={total_new} dup={total_dup} status={tick_status} "
        f"total={counts['total']} with_phone={counts['with_phone']}"
    )
    return 0 if tick_status in ("completed", "banned") else 1


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
    """電話番号あり求人を CSV に書き出す。

    --all-shards が指定されている場合、data/state-shard-*.db 全てを読み込み、
    job_url で UNION (最新の scraped_at 優先) して出力する。
    """
    output_path = Path(args.output)

    if getattr(args, "all_shards", False):
        shard_dbs = sorted(Path("data").glob("state-shard-*.db"))
        if not shard_dbs:
            logger.warning("state-shard-*.db が見つかりません。デフォルト state.db にフォールバック")
            store = StateStore()
            postings = store.export_with_phone(args.site)
        else:
            logger.info(f"{len(shard_dbs)} 個の shard DB を merge: {[p.name for p in shard_dbs]}")
            by_url: dict[str, object] = {}
            for db_path in shard_dbs:
                shard_store = StateStore(db_path=db_path)
                for posting in shard_store.export_with_phone(args.site):
                    existing = by_url.get(posting.job_url)
                    if existing is None or (
                        (posting.scraped_at or "") > (existing.scraped_at or "")
                    ):
                        by_url[posting.job_url] = posting
            postings = list(by_url.values())
            postings.sort(key=lambda p: p.scraped_at or "", reverse=True)
    else:
        store = StateStore()
        postings = store.export_with_phone(args.site)

    logger.info(f"電話番号あり {len(postings)} 件を CSV に書き出します。")
    writer = CsvWriter(output_dir=output_path.parent, filename=output_path.name)
    path, written = writer.write(postings)
    print(f"完了: {written} 件を書き出しました → {path}")
    return 0


def _cmd_validate(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Bright Data proxy 経由で N 件を実際に叩き、成功率と電話番号率を測定する。

    proxy 未設定なら明示的にエラー。sample 上限まで tick を回し、
    SQLite に保存された結果から成功率 / 電話率を集計。
    """
    from .query_pools import indeed_query_pool

    proxy = load_proxy_from_env()
    if not proxy:
        logger.error(
            "BRIGHTDATA_PROXY_URL (or PROXY_URL) が未設定です。"
            "validate は proxy 経由での実測を目的とします。"
        )
        return 2

    logger.info(
        f"validate 開始: target_samples={args.samples} "
        f"max_pages_per_tick={args.max_pages_per_tick}"
    )

    store = StateStore()
    site = "indeed"
    delay = float(os.getenv("REQUEST_DELAY_SECONDS", "1"))
    headless = os.getenv("HEADLESS", "true").lower() == "true"

    pool = indeed_query_pool()
    baseline = store.counts(site)
    baseline_total = baseline["total"]
    baseline_phone = baseline["with_phone"]

    ticks = 0
    while True:
        counts = store.counts(site)
        newly_added = counts["total"] - baseline_total
        if newly_added >= args.samples:
            break
        if store.is_paused(site):
            logger.warning("pause 検知: validate 中断")
            break

        keyword, location = store.pick_next_query(site, pool)
        logger.info(
            f"tick #{ticks + 1}: keyword='{keyword}' location='{location}' "
            f"(取得済 {newly_added}/{args.samples})"
        )
        run_id = store.start_run(site, keyword, location)
        items_new = 0
        items_dup = 0
        status = "completed"

        try:
            with IndeedScraper(
                request_delay_seconds=delay, headless=headless, proxy=proxy
            ) as scraper:
                for posting in scraper.search(
                    keyword=keyword,
                    location=location,
                    max_pages=args.max_pages_per_tick,
                ):
                    require_phone = os.environ.get("REQUIRE_PHONE", "").lower() in (
                        "true", "1", "yes"
                    )
                    if store.save_posting(
                        posting, site, keyword, location, require_phone=require_phone
                    ):
                        items_new += 1
                    else:
                        items_dup += 1
                    if (store.counts(site)["total"] - baseline_total) >= args.samples:
                        break
        except BanDetectedError as e:
            logger.error(f"validate 中に BAN 検知: {e}")
            status = "banned"
        except Exception as e:
            logger.exception(f"validate 中エラー: {e}")
            status = "error"
        finally:
            store.finish_run(run_id, items_new, items_dup, status=status)

        ticks += 1
        if status != "completed":
            break

    final = store.counts(site)
    delta_total = final["total"] - baseline_total
    delta_phone = final["with_phone"] - baseline_phone
    phone_rate = (delta_phone / delta_total * 100) if delta_total else 0.0

    print("=" * 50)
    print("validate 結果")
    print("=" * 50)
    print(f"tick 回数           : {ticks}")
    print(f"新規取得件数        : {delta_total}")
    print(f"電話番号あり        : {delta_phone}")
    print(f"電話番号率          : {phone_rate:.1f}%")
    print(f"累積総件数          : {final['total']}")
    print(f"累積電話番号あり    : {final['with_phone']}")
    print("=" * 50)
    print("5000 件到達必要 fetch (電話率実測ベース):")
    if delta_phone > 0:
        needed = int(5000 / (delta_phone / delta_total))
        print(f"  推定必要 fetch 数 : {needed:,} 件")
        cost_est = needed * 1.50 / 1000
        print(f"  Bright Data PAYG コスト概算 : ${cost_est:.2f}")
    print("=" * 50)
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
    p_tick.add_argument(
        "--shard",
        type=int,
        default=0,
        help="matrix 並列用 shard 番号 (0-indexed)",
    )
    p_tick.add_argument(
        "--total-shards",
        type=int,
        default=1,
        help="matrix 並列時の総 shard 数",
    )

    # status
    p_status = sub.add_parser("status", help="進捗確認")
    p_status.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")

    # export
    p_export = sub.add_parser("export", help="電話番号あり求人を CSV エクスポート")
    p_export.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")
    p_export.add_argument("--output", required=True)
    p_export.add_argument(
        "--all-shards",
        action="store_true",
        help="data/state-shard-*.db を全て merge して出力",
    )

    # validate (Bright Data 疎通 + 電話率実測)
    p_validate = sub.add_parser(
        "validate", help="Bright Data proxy 経由で N 件叩いて成功率 + 電話率を測定"
    )
    p_validate.add_argument("--samples", type=int, default=100, help="目標サンプル件数")
    p_validate.add_argument(
        "--max-pages-per-tick", type=int, default=1, help="1 tick 内の検索ページ数"
    )

    return parser


def _parse_args_with_legacy() -> argparse.Namespace:
    """後方互換: 旧 CLI (subcommand なし) を scrape として扱う。"""
    argv = sys.argv[1:]
    known_commands = {"scrape", "tick", "status", "export", "validate"}
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
    if args.command == "validate":
        return _cmd_validate(args, logger)
    _build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
