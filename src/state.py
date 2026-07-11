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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator, Optional

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
                    status TEXT DEFAULT 'running'
                );

                CREATE TABLE IF NOT EXISTS pause_state (
                    site TEXT PRIMARY KEY,
                    paused_until TEXT NOT NULL,
                    reason TEXT
                );
            """)

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
    ) -> bool:
        """新規求人を保存。既存 URL は False を返す。"""
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
        status: str = "completed",
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE runs
                SET finished_at = ?, items_new = ?, items_dup = ?, status = ?
                WHERE id = ?
                """,
                (
                    datetime.now(JST).isoformat(timespec="seconds"),
                    items_new,
                    items_dup,
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
        """電話番号ありの求人を JobPosting のリストで返す。"""
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
