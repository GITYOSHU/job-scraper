"""StateStore の since フィルタ（差分エクスポート）テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models import JobPosting
from src.state import StateStore


def _make_posting(job_url: str, scraped_at: str) -> JobPosting:
    return JobPosting(
        company_name="テスト会社",
        job_url=job_url,
        phone_number="03-1234-5678",
        scraped_at=scraped_at,
    )


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.db")


def test_export_with_phone_without_since_returns_all(store: StateStore) -> None:
    # Arrange
    store.save_posting(
        _make_posting("https://example.com/1", "2026-07-13T20:26:06+09:00"),
        site="indeed",
        keyword="kw",
        location="loc",
    )
    store.save_posting(
        _make_posting("https://example.com/2", "2026-07-17T05:56:56.648Z"),
        site="indeed",
        keyword="kw",
        location="loc",
    )

    # Act
    result = store.export_with_phone("indeed")

    # Assert
    assert len(result) == 2


def test_export_with_phone_since_excludes_older_records(store: StateStore) -> None:
    # Arrange: 基準時刻より前後で1件ずつ保存
    store.save_posting(
        _make_posting("https://example.com/old", "2026-07-13T20:26:06+09:00"),
        site="indeed",
        keyword="kw",
        location="loc",
    )
    store.save_posting(
        _make_posting("https://example.com/new", "2026-07-20T10:00:00+09:00"),
        site="indeed",
        keyword="kw",
        location="loc",
    )

    # Act: 7/17 を基準にすると old は除外され new だけ残る
    result = store.export_with_phone("indeed", since="2026-07-17T05:56:56.648Z")

    # Assert
    assert [p.job_url for p in result] == ["https://example.com/new"]


def test_export_with_phone_since_handles_mixed_timezone_formats(
    store: StateStore,
) -> None:
    """Z 形式 (UTC) と +09:00 形式 (JST) が混在しても実時刻で正しく比較される。

    2026-07-17T05:56:56+09:00 は UTC 換算で 2026-07-16T20:56:56Z。
    基準値 2026-07-17T00:00:00Z (=JST 09:00) より前なので除外されるべき。
    文字列比較だと "+09:00" < "Z" の文字コード順で誤って残ってしまうケース。
    """
    # Arrange
    store.save_posting(
        _make_posting("https://example.com/jst-before-utc-day", "2026-07-17T05:56:56+09:00"),
        site="indeed",
        keyword="kw",
        location="loc",
    )
    store.save_posting(
        _make_posting("https://example.com/utc-after", "2026-07-17T01:00:00.000Z"),
        site="indeed",
        keyword="kw",
        location="loc",
    )

    # Act
    result = store.export_with_phone("indeed", since="2026-07-17T00:00:00Z")

    # Assert: 実時刻で基準より後なのは utc-after のみ
    assert [p.job_url for p in result] == ["https://example.com/utc-after"]


def test_export_with_phone_since_naive_scraped_at_is_not_dropped(
    store: StateStore,
) -> None:
    """scraped_at にタイムゾーン情報が無い異常データでも例外で落ちないこと。"""
    store.save_posting(
        _make_posting("https://example.com/naive", "2026-07-17T05:56:56"),
        site="indeed",
        keyword="kw",
        location="loc",
    )

    # Act / Assert: 例外を出さずに処理できる（除外扱いでも許容）
    result = store.export_with_phone("indeed", since="2026-07-01T00:00:00Z")
    assert isinstance(result, list)
