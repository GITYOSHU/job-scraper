"""SQLite ベースの永続状態管理。

役割:
- URL dedup (取得済み URL の記録)
- キーワード/地域の巡回進捗
- BAN 検知後の pause 状態
- 電話番号あり求人のカウント
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator, Optional

from .extractors import normalize_phone_number
from .models import JobPosting

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
DEFAULT_DB_PATH = Path("data/state.db")


def _resolve_db_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("STATE_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def _parse_flexible_iso(value: str) -> Optional[datetime]:
    """"...Z" (UTC) と "...+09:00" (JST) が混在する scraped_at を比較可能な形にする。

    naive (tzinfo 無し) な値やパース不能な値は None を返す
    (since フィルタでは除外側に倒す＝安全側)。
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def normalize_phone_numbers(postings: list[JobPosting]) -> list[JobPosting]:
    """収集済み求人の電話番号を現行ルールで整形し直し、無効なものを除外する。

    抽出時のバリデーションを後から追加したため、既に DB にある値には
    旧ロジックのハイフン誤りやプレースホルダ (`000-000-0000` 等) が残っている。
    export の直前に通すことで、納品 CSV 側だけでも品質を揃える。
    元の JobPosting は書き換えず、新しいインスタンスを返す。
    """
    result: list[JobPosting] = []
    for posting in postings:
        normalized = normalize_phone_number(posting.phone_number)
        if not normalized:
            continue
        if normalized == posting.phone_number:
            result.append(posting)
        else:
            result.append(replace(posting, phone_number=normalized))
    return result


def dedupe_by_phone(
    postings: list[JobPosting], since: Optional[str] = None
) -> list[JobPosting]:
    """電話番号ベースで重複排除する (架電リストの二重架電防止)。

    同一電話番号の求人が複数 (別店舗・別 URL 含む) あっても、最新の scraped_at
    1 件だけ残す。site をまたいだ複数 shard の生データを merge した後に
    1 回だけ適用する想定 (shard 単位では重複判定できないため)。

    since 指定時は「電話番号の初出 (全 postings 中で最も古い scraped_at) が
    since より後」のものだけを残す。同じ電話番号が since 以前に一度でも
    出現していれば、since 以降の再出現分も除外する
    (旧 export 済みファイルとの重複を避ける差分エクスポート用)。
    タイムゾーン不明・パース不能な scraped_at は安全側 (除外) に倒す。
    """
    since_dt = _parse_flexible_iso(since) if since else None

    parsed = [
        (p, _parse_flexible_iso(p.scraped_at or ""))
        for p in postings
        if p.phone_number
    ]

    # 電話番号ごとの初出時刻。パース不能な scraped_at が一度でも混ざれば
    # None (＝安全側で「since 以前」扱い) にする。
    first_seen_at: dict[str, Optional[datetime]] = {}
    for p, dt in parsed:
        phone = p.phone_number
        if phone not in first_seen_at:
            first_seen_at[phone] = dt
            continue
        existing = first_seen_at[phone]
        if existing is None or dt is None:
            first_seen_at[phone] = None
        elif dt < existing:
            first_seen_at[phone] = dt

    ordered = sorted(
        parsed,
        key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    seen_phones: set[str] = set()
    result: list[JobPosting] = []
    for p, dt in ordered:
        phone = p.phone_number
        if phone in seen_phones:
            continue
        if since_dt is not None:
            if dt is None or dt <= since_dt:
                continue
            first_dt = first_seen_at.get(phone)
            if first_dt is None or first_dt <= since_dt:
                continue
        seen_phones.add(phone)
        result.append(p)

    return result


class StateStore:
    """SQLite ベースの状態ストア。スレッドセーフではない (単一プロセス想定)。

    DB パス優先順位: 明示引数 > 環境変数 STATE_DB > デフォルト data/state.db
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = _resolve_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS postings (
                    job_url TEXT PRIMARY KEY,
                    company_name TEXT,
                    address TEXT,
                    phone_number TEXT,
                    industry TEXT,
                    representative_name TEXT,
                    site TEXT NOT NULL,
                    keyword TEXT,
                    location TEXT,
                    scraped_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_postings_site ON postings(site);
                CREATE INDEX IF NOT EXISTS idx_postings_phone
                    ON postings(phone_number) WHERE phone_number IS NOT NULL AND phone_number != '';

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT NOT NULL,
                    keyword TEXT,
                    location TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    items_new INTEGER DEFAULT 0,
                    items_dup INTEGER DEFAULT 0,
                    items_no_phone INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'running'
                );

                CREATE TABLE IF NOT EXISTS pause_state (
                    site TEXT PRIMARY KEY,
                    paused_until TEXT NOT NULL,
                    reason TEXT
                );
            """)
            # items_no_phone 追加前の既存 DB (GitHub Actions artifact 等) をマイグレーション
            cols = [row[1] for row in c.execute("PRAGMA table_info(runs)").fetchall()]
            if "items_no_phone" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN items_no_phone INTEGER DEFAULT 0")

    def is_url_known(self, job_url: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM postings WHERE job_url = ? LIMIT 1", (job_url,)
            ).fetchone()
            return row is not None

    def save_posting(
        self,
        posting: JobPosting,
        site: str,
        keyword: str,
        location: str,
        require_phone: bool = False,
    ) -> bool:
        """新規求人を保存。既存 URL は False を返す。

        require_phone=True の場合、電話番号が無い求人は保存せず False を返す
        (dedup + 電話番号ありのみ保存)。
        """
        if require_phone and not posting.phone_number:
            return False
        try:
            with self._conn() as c:
                c.execute(
                    """
                    INSERT INTO postings (
                        job_url, company_name, address, phone_number, industry,
                        representative_name, site, keyword, location, scraped_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        posting.job_url,
                        posting.company_name,
                        posting.address,
                        posting.phone_number,
                        posting.industry,
                        posting.representative_name,
                        site,
                        keyword,
                        location,
                        posting.scraped_at
                        or datetime.now(JST).isoformat(timespec="seconds"),
                    ),
                )
                return True
        except sqlite3.IntegrityError:
            return False

    def start_run(self, site: str, keyword: str, location: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO runs (site, keyword, location, started_at)
                VALUES (?, ?, ?, ?)
                """,
                (site, keyword, location, datetime.now(JST).isoformat(timespec="seconds")),
            )
            return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        items_new: int,
        items_dup: int,
        items_no_phone: int = 0,
        status: str = "completed",
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE runs
                SET finished_at = ?, items_new = ?, items_dup = ?,
                    items_no_phone = ?, status = ?
                WHERE id = ?
                """,
                (
                    datetime.now(JST).isoformat(timespec="seconds"),
                    items_new,
                    items_dup,
                    items_no_phone,
                    status,
                    run_id,
                ),
            )

    def set_pause(self, site: str, seconds: int, reason: str = "") -> None:
        until = datetime.now(JST) + timedelta(seconds=seconds)
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO pause_state (site, paused_until, reason)
                VALUES (?, ?, ?)
                """,
                (site, until.isoformat(timespec="seconds"), reason),
            )
        logger.warning(f"pause set: site={site} until={until.isoformat()} reason={reason}")

    def is_paused(self, site: str) -> Optional[str]:
        """pause 中なら理由文字列を返す。解除済なら None。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT paused_until, reason FROM pause_state WHERE site = ?", (site,)
            ).fetchone()
            if not row:
                return None
            paused_until = datetime.fromisoformat(row["paused_until"])
            if datetime.now(JST) >= paused_until:
                c.execute("DELETE FROM pause_state WHERE site = ?", (site,))
                return None
            return f"paused until {paused_until.isoformat()} ({row['reason']})"

    def pick_next_query(self, site: str, candidates: list[tuple[str, str]]) -> tuple[str, str]:
        """最も直近 run が古い (or 未実行) (keyword, location) の組を返す。"""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT keyword, location, MAX(started_at) AS last_started
                FROM runs
                WHERE site = ?
                GROUP BY keyword, location
                """,
                (site,),
            ).fetchall()
            last_by_key = {(r["keyword"], r["location"]): r["last_started"] for r in rows}

        return min(candidates, key=lambda kv: last_by_key.get(kv, ""))

    def counts(self, site: str) -> dict[str, int]:
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM postings WHERE site = ?", (site,)
            ).fetchone()[0]
            with_phone = c.execute(
                "SELECT COUNT(*) FROM postings WHERE site = ? AND phone_number IS NOT NULL AND phone_number != ''",
                (site,),
            ).fetchone()[0]
        return {"total": total, "with_phone": with_phone}

    def export_with_phone(self, site: str) -> list[JobPosting]:
        """電話番号ありの求人を JobPosting のリストで返す (URL 単位で一意、全件)。

        電話番号ベースの重複排除・差分抽出は dedupe_by_phone() で
        複数 shard の merge 後にまとめて行う (shard 単位では正しく判定できないため)。
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT company_name, address, phone_number, industry,
                       representative_name, job_url, scraped_at
                FROM postings
                WHERE site = ? AND phone_number IS NOT NULL AND phone_number != ''
                ORDER BY scraped_at DESC
                """,
                (site,),
            ).fetchall()
        return [
            JobPosting(
                company_name=r["company_name"],
                address=r["address"],
                phone_number=r["phone_number"],
                industry=r["industry"],
                representative_name=r["representative_name"],
                job_url=r["job_url"],
                scraped_at=r["scraped_at"],
            )
            for r in rows
        ]

    def query_hit_rates(self, site: str) -> list[dict]:
        """keyword×location ごとの fetch 総数と電話番号命中率を集計する。

        命中率が低い順にソートして返す (pool から間引く候補を見つけやすくする)。
        fetch 総数 (items_new + items_dup + items_no_phone) が 0 の組は含めない。
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT keyword, location,
                       SUM(items_new) AS new_sum,
                       SUM(items_dup) AS dup_sum,
                       SUM(items_no_phone) AS no_phone_sum
                FROM runs
                WHERE site = ?
                GROUP BY keyword, location
                """,
                (site,),
            ).fetchall()

        result = []
        for r in rows:
            new_sum = r["new_sum"] or 0
            fetched = new_sum + (r["dup_sum"] or 0) + (r["no_phone_sum"] or 0)
            if fetched == 0:
                continue
            result.append(
                {
                    "keyword": r["keyword"],
                    "location": r["location"],
                    "fetched": fetched,
                    "with_phone": new_sum,
                    "hit_rate": new_sum / fetched,
                }
            )
        result.sort(key=lambda d: d["hit_rate"])
        return result

    def recent_runs(self, site: str, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, keyword, location, started_at, finished_at,
                       items_new, items_dup, status
                FROM runs
                WHERE site = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (site, limit),
            ).fetchall()
        return [dict(r) for r in rows]
