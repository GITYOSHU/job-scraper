"""StateStore の export および電話番号重複排除 (dedupe_by_phone) のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models import JobPosting
from src.state import StateStore, dedupe_by_phone


def _make_posting(
    job_url: str, scraped_at: str, phone_number: str = "03-1234-5678"
) -> JobPosting:
    return JobPosting(
        company_name="テスト会社",
        job_url=job_url,
        phone_number=phone_number,
        scraped_at=scraped_at,
    )


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.db")


def test_export_with_phone_returns_all_with_phone_numbers(store: StateStore) -> None:
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


class TestDedupeByPhone:
    """架電リストの重複防止: 同一電話番号は最新 1 件のみ残す。"""

    def test_keeps_only_latest_when_phone_duplicated(self) -> None:
        # Arrange: 同じ電話番号で URL 違いの求人が2件 (別店舗/別求人扱い)
        older = _make_posting(
            "https://example.com/old", "2026-07-13T10:00:00+09:00", "03-1234-5678"
        )
        newer = _make_posting(
            "https://example.com/new", "2026-07-20T10:00:00+09:00", "03-1234-5678"
        )

        # Act
        result = dedupe_by_phone([older, newer])

        # Assert: 最新の1件だけ残る
        assert len(result) == 1
        assert result[0].job_url == "https://example.com/new"

    def test_distinct_phone_numbers_are_all_kept(self) -> None:
        # Arrange
        a = _make_posting("https://example.com/a", "2026-07-13T10:00:00+09:00", "03-1111-1111")
        b = _make_posting("https://example.com/b", "2026-07-14T10:00:00+09:00", "03-2222-2222")

        # Act
        result = dedupe_by_phone([a, b])

        # Assert
        assert {p.job_url for p in result} == {"https://example.com/a", "https://example.com/b"}

    def test_since_excludes_phone_first_seen_before_since(self) -> None:
        """コア要件: 旧ファイル (since 以前) で既出の電話番号は、since 以降に
        別 URL で再度出現しても差分ファイルには含めない (架電リスト重複防止)。
        """
        # Arrange: 電話番号 X は 7/13 (旧ファイル分=since以前) に初出
        old_export = _make_posting(
            "https://example.com/old", "2026-07-13T10:00:00+09:00", "03-1234-5678"
        )
        # 同じ電話番号 X が 7/20 (since以降) に別求人 URL で再検出された
        reappeared = _make_posting(
            "https://example.com/reappeared", "2026-07-20T10:00:00+09:00", "03-1234-5678"
        )

        # Act: since=7/17 で差分エクスポート
        result = dedupe_by_phone(
            [old_export, reappeared], since="2026-07-17T00:00:00+09:00"
        )

        # Assert: 旧ファイルで既出の電話番号なので差分には出さない
        assert result == []

    def test_since_keeps_genuinely_new_phone(self) -> None:
        # Arrange: since 以前のデータには存在しない、真に新規の電話番号
        old_other_phone = _make_posting(
            "https://example.com/old", "2026-07-13T10:00:00+09:00", "03-0000-0000"
        )
        genuinely_new = _make_posting(
            "https://example.com/new", "2026-07-20T10:00:00+09:00", "03-9999-9999"
        )

        # Act
        result = dedupe_by_phone(
            [old_other_phone, genuinely_new], since="2026-07-17T00:00:00+09:00"
        )

        # Assert
        assert [p.job_url for p in result] == ["https://example.com/new"]

    def test_since_handles_mixed_timezone_formats(self) -> None:
        """Z (UTC) と +09:00 (JST) 混在でも実時刻で正しく比較する。

        2026-07-17T05:56:56+09:00 は UTC 換算で 2026-07-16T20:56:56Z であり、
        基準値 2026-07-17T00:00:00Z (=JST 09:00) より前 → 除外対象。
        """
        # Arrange
        before_utc_day = _make_posting(
            "https://example.com/before", "2026-07-17T05:56:56+09:00", "03-1234-5678"
        )

        # Act
        result = dedupe_by_phone([before_utc_day], since="2026-07-17T00:00:00Z")

        # Assert
        assert result == []

    def test_without_since_just_dedupes_all(self) -> None:
        # Arrange: since 未指定なら全件対象に dedup のみ行う
        older = _make_posting(
            "https://example.com/old", "2026-07-13T10:00:00+09:00", "03-1234-5678"
        )
        newer = _make_posting(
            "https://example.com/new", "2026-07-20T10:00:00+09:00", "03-1234-5678"
        )

        # Act
        result = dedupe_by_phone([older, newer], since=None)

        # Assert
        assert len(result) == 1
        assert result[0].job_url == "https://example.com/new"

    def test_naive_scraped_at_is_excluded_when_since_given(self) -> None:
        """scraped_at にタイムゾーン情報が無い異常データは例外を出さず除外側に倒す。"""
        naive = _make_posting(
            "https://example.com/naive", "2026-07-17T05:56:56", "03-1234-5678"
        )

        # Act / Assert: 例外を出さず空リストになる (安全側)
        result = dedupe_by_phone([naive], since="2026-07-01T00:00:00Z")
        assert result == []

    def test_result_sorted_by_scraped_at_descending(self) -> None:
        a = _make_posting("https://example.com/a", "2026-07-13T10:00:00+09:00", "03-1111-1111")
        b = _make_posting("https://example.com/b", "2026-07-20T10:00:00+09:00", "03-2222-2222")

        result = dedupe_by_phone([a, b])

        assert [p.job_url for p in result] == ["https://example.com/b", "https://example.com/a"]
