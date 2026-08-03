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
import json
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
from .dataset_probe import estimate, load_records, probe
from .state import StateStore, dedupe_by_phone, normalize_phone_numbers


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
    require_phone = os.environ.get("REQUIRE_PHONE", "").lower() in ("true", "1", "yes")

    total_new = 0
    total_dup = 0
    total_no_phone = 0
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
                items_no_phone = 0
                query_status = "completed"

                try:
                    for posting in scraper.search(
                        keyword=keyword, location=location, max_pages=args.max_pages
                    ):
                        if require_phone and not posting.phone_number:
                            items_no_phone += 1
                            continue
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
                    store.finish_run(
                        run_id, items_new, items_dup, items_no_phone, status=query_status
                    )
                    total_new += items_new
                    total_dup += items_dup
                    total_no_phone += items_no_phone

                if query_status == "banned":
                    break
    except Exception as e:
        logger.exception(f"tick 中の scraper エラー: {e}")
        tick_status = "error"

    counts = store.counts(site)
    logger.info(
        f"tick 完了: new={total_new} dup={total_dup} no_phone={total_no_phone} "
        f"status={tick_status} total={counts['total']} with_phone={counts['with_phone']}"
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


def _cmd_analyze(args: argparse.Namespace, logger: logging.Logger) -> int:
    """keyword×location ごとの電話番号命中率を分析する。

    --all-shards 指定時は data/state-shard-*.db 全ての run 集計を合算する。
    命中率が低い組み合わせを pool から間引く判断材料にする。
    """
    if getattr(args, "all_shards", False):
        shard_dbs = sorted(Path("data").glob("state-shard-*.db"))
        if not shard_dbs:
            logger.warning("state-shard-*.db が見つかりません。デフォルト state.db にフォールバック")
            shard_dbs = [None]
        merged: dict[tuple[str, str], dict] = {}
        for db_path in shard_dbs:
            shard_store = StateStore(db_path=db_path) if db_path else StateStore()
            for r in shard_store.query_hit_rates(args.site):
                key = (r["keyword"], r["location"])
                existing = merged.get(key)
                if existing is None:
                    merged[key] = dict(r)
                else:
                    existing["fetched"] += r["fetched"]
                    existing["with_phone"] += r["with_phone"]
        rates = list(merged.values())
        for r in rates:
            r["hit_rate"] = r["with_phone"] / r["fetched"] if r["fetched"] else 0.0
        rates.sort(key=lambda d: d["hit_rate"])
    else:
        store = StateStore()
        rates = store.query_hit_rates(args.site)

    print(f"=== 命中率分析: site={args.site} (低い順、間引き候補) ===")
    print(f"{'キーワード':<14}{'地域':<10}{'fetch数':>8}{'電話あり':>8}{'命中率':>8}")
    for r in rates[: args.top]:
        print(
            f"{r['keyword']:<14}{r['location']:<10}{r['fetched']:>8}"
            f"{r['with_phone']:>8}{r['hit_rate'] * 100:>7.1f}%"
        )
    if rates:
        total_fetched = sum(r["fetched"] for r in rates)
        total_phone = sum(r["with_phone"] for r in rates)
        print("-" * 48)
        print(f"全体: fetch={total_fetched} 電話あり={total_phone} "
              f"命中率={total_phone / total_fetched * 100:.1f}%")
    return 0


def _cmd_probe_dataset(args: argparse.Namespace, logger: logging.Logger) -> int:
    """既成データセットの無料サンプルを解析し、購入判断に必要な数字を出す。

    納品時と同じ電話番号抽出ロジックを通すので、ここで出る命中率が
    そのまま実運用の見込み値になる。
    """
    path = Path(args.input)
    try:
        raw_records = load_records(path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"サンプルの読み込みに失敗しました: {e}")
        return 1

    report = probe(raw_records)
    print(f"=== データセットサンプル判定: {path.name} ===")
    print(f"  総レコード数          {report.total:>10,}")
    print(f"  うち日本             {report.japan:>10,}  ({report.japan_rate * 100:.1f}%)")
    if report.japan == 0:
        print("\n  日本のレコードが 0 件です。この データセットは使えません。")
        return 0

    print(f"  description あり      {report.with_description:>10,}")
    print(f"  電話番号 抽出成功      {report.with_phone:>10,}  ({report.phone_rate * 100:.1f}%)")
    print(f"  うちユニーク番号       {report.unique_phones:>10,}")
    print(f"  電話番号なし          {report.without_phone:>10,}")
    print(f"    うち企業サイトあり    {report.without_phone_with_website:>10,}"
          f"  ← contact-details-scraper で補完可能")

    target = getattr(args, "target", None)
    print("\n=== 単価試算 ===")
    for label, contact_price in [
        ("description のみ", None),
        ("+ 企業サイト補完", args.contact_price),
    ]:
        if contact_price is None and label.startswith("+"):
            continue
        est = estimate(
            report,
            record_price_usd=args.record_price,
            jpy_rate=args.jpy,
            contact_price_usd=contact_price,
            contact_success_rate=args.contact_success_rate,
            target=target,
        )
        print(f"  [{label}] 納品 {est.delivered:,} 件 / "
              f"費用 {est.total_cost_jpy:,.0f}円 / 単価 {est.unit_cost_jpy:.2f}円")
        if target:
            print(f"      → {target:,} 件には {est.records_needed_for_target:,.0f} レコード購入が必要 "
                  f"({est.target_cost_jpy:,.0f}円)")
    return 0


def _cmd_export(args: argparse.Namespace, logger: logging.Logger) -> int:
    """電話番号あり求人を CSV に書き出す。

    --all-shards が指定されている場合、data/state-shard-*.db 全てを読み込み、
    job_url で UNION (最新の scraped_at 優先) して出力する。
    そのうえで電話番号ベースの重複排除を行う (同一電話番号は最新 1 件のみ・
    二重架電防止)。--since 指定時はさらに、電話番号の初出が since 以前の
    求人を除外する (旧 export 済みファイルと重複しない差分エクスポート)。
    """
    output_path = Path(args.output)

    since = getattr(args, "since", None)
    if since:
        logger.info(f"差分エクスポート: since={since} より前に初出の電話番号は除外")

    if getattr(args, "all_shards", False):
        shard_dbs = sorted(Path("data").glob("state-shard-*.db"))
        if not shard_dbs:
            logger.warning("state-shard-*.db が見つかりません。デフォルト state.db にフォールバック")
            store = StateStore()
            raw_postings = store.export_with_phone(args.site)
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
            raw_postings = list(by_url.values())
    else:
        store = StateStore()
        raw_postings = store.export_with_phone(args.site)

    normalized = normalize_phone_numbers(raw_postings)
    if len(normalized) != len(raw_postings):
        logger.info(
            f"無効な電話番号を除外: {len(raw_postings)} 件 → {len(normalized)} 件"
        )

    postings = dedupe_by_phone(normalized, since=since)
    if len(postings) != len(normalized):
        logger.info(f"電話番号重複排除: {len(normalized)} 件 → {len(postings)} 件")

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

    # analyze (キーワード×地域の命中率分析)
    p_analyze = sub.add_parser(
        "analyze", help="キーワード×地域ごとの電話番号命中率を分析 (間引き候補の洗い出し)"
    )
    p_analyze.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")
    p_analyze.add_argument(
        "--all-shards",
        action="store_true",
        help="data/state-shard-*.db の run 集計を全て合算して分析",
    )
    p_analyze.add_argument("--top", type=int, default=30, help="表示する件数 (命中率の低い順)")

    # export
    p_export = sub.add_parser("export", help="電話番号あり求人を CSV エクスポート")
    p_export.add_argument("--site", choices=["hellowork", "indeed"], default="indeed")
    p_export.add_argument("--output", required=True)
    p_export.add_argument(
        "--all-shards",
        action="store_true",
        help="data/state-shard-*.db を全て merge して出力",
    )
    p_export.add_argument(
        "--since",
        default=None,
        help="この日時 (ISO8601) より後に取得した求人のみ出力 (差分エクスポート用)",
    )

    # probe-dataset (既成データセットの無料サンプルを購入前に判定)
    p_probe = sub.add_parser(
        "probe-dataset",
        help="既成データセットのサンプルを解析し、日本の件数・電話率・単価を出す",
    )
    p_probe.add_argument("--input", required=True, help="サンプルファイル (json/ndjson/jsonl/csv、.gz 可)")
    p_probe.add_argument(
        "--record-price", type=float, default=0.0025,
        help="1 レコードあたりの購入単価 (USD)。Bright Data Indeed は $0.0025",
    )
    p_probe.add_argument("--jpy", type=float, default=150.0, help="USD/JPY レート")
    p_probe.add_argument(
        "--contact-price", type=float, default=None,
        help="企業サイトから電話を補完する場合の成功時単価 (USD)。"
             "Apify contact-details-scraper は $0.0045",
    )
    p_probe.add_argument(
        "--contact-success-rate", type=float, default=0.7,
        help="企業サイトから電話が取れる想定成功率",
    )
    p_probe.add_argument(
        "--target", type=int, default=None, help="目標納品件数 (必要な購入レコード数を逆算)",
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
    known_commands = {"scrape", "tick", "status", "export", "validate", "analyze", "probe-dataset"}
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
    if args.command == "probe-dataset":
        return _cmd_probe_dataset(args, logger)

    if args.command == "analyze":
        return _cmd_analyze(args, logger)
    if args.command == "export":
        return _cmd_export(args, logger)
    if args.command == "validate":
        return _cmd_validate(args, logger)
    _build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
