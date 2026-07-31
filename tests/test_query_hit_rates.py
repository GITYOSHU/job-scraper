"""キーワード×地域ごとの電話番号命中率集計 (query_hit_rates) のテスト。

命中率の低い組み合わせを pool から間引くための分析機能。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.state import StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(db_path=tmp_path / "state.db")


def _run(
    store: StateStore,
    keyword: str,
    location: str,
    items_new: int,
    items_dup: int,
    items_no_phone: int,
) -> None:
    run_id = store.start_run("indeed", keyword, location)
    store.finish_run(run_id, items_new, items_dup, items_no_phone)


class TestFinishRunRecordsNoPhoneCount:
    def test_items_no_phone_defaults_to_zero(self, store: StateStore) -> None:
        # Arrange
        run_id = store.start_run("indeed", "kw", "loc")

        # Act: items_no_phone 省略時は 0 扱い (既存呼び出しとの後方互換)
        store.finish_run(run_id, items_new=5, items_dup=1)

        # Assert
        runs = store.recent_runs("indeed", limit=1)
        assert runs[0]["items_new"] == 5

    def test_items_no_phone_is_persisted(self, store: StateStore) -> None:
        # Arrange
        run_id = store.start_run("indeed", "kw", "loc")

        # Act
        store.finish_run(run_id, items_new=3, items_dup=0, items_no_phone=7)

        # Assert: query_hit_rates の集計に反映されている (直接カラムを見るテストがないので経由で確認)
        rates = store.query_hit_rates("indeed")
        assert rates[0]["fetched"] == 10  # 3 + 0 + 7
        assert rates[0]["with_phone"] == 3


class TestQueryHitRates:
    def test_computes_hit_rate_per_keyword_location(self, store: StateStore) -> None:
        # Arrange: 「美容師×東京」は 10 件中 5 件電話あり (50%)
        _run(store, "美容師", "東京", items_new=5, items_dup=0, items_no_phone=5)

        # Act
        rates = store.query_hit_rates("indeed")

        # Assert
        assert len(rates) == 1
        assert rates[0]["keyword"] == "美容師"
        assert rates[0]["location"] == "東京"
        assert rates[0]["fetched"] == 10
        assert rates[0]["with_phone"] == 5
        assert rates[0]["hit_rate"] == pytest.approx(0.5)

    def test_aggregates_multiple_runs_for_same_query(self, store: StateStore) -> None:
        # Arrange: 同じ keyword×location が複数 tick で実行された場合は合算
        _run(store, "美容師", "東京", items_new=5, items_dup=0, items_no_phone=5)
        _run(store, "美容師", "東京", items_new=3, items_dup=1, items_no_phone=1)

        # Act
        rates = store.query_hit_rates("indeed")

        # Assert: fetched=10+5=15, with_phone=5+3=8
        assert len(rates) == 1
        assert rates[0]["fetched"] == 15
        assert rates[0]["with_phone"] == 8
        assert rates[0]["hit_rate"] == pytest.approx(8 / 15)

    def test_sorted_by_hit_rate_ascending(self, store: StateStore) -> None:
        # Arrange: 低命中率が先頭に来るべき (間引き候補を見つけやすくする)
        _run(store, "高命中", "東京", items_new=8, items_dup=0, items_no_phone=2)  # 80%
        _run(store, "低命中", "東京", items_new=1, items_dup=0, items_no_phone=9)  # 10%
        _run(store, "中命中", "東京", items_new=5, items_dup=0, items_no_phone=5)  # 50%

        # Act
        rates = store.query_hit_rates("indeed")

        # Assert
        assert [r["keyword"] for r in rates] == ["低命中", "中命中", "高命中"]

    def test_excludes_queries_with_zero_fetched(self, store: StateStore) -> None:
        # Arrange: fetch 総数が 0 (エラーで即終了等) の run は分析対象外
        _run(store, "空振り", "東京", items_new=0, items_dup=0, items_no_phone=0)
        _run(store, "実績あり", "東京", items_new=1, items_dup=0, items_no_phone=1)

        # Act
        rates = store.query_hit_rates("indeed")

        # Assert
        assert [r["keyword"] for r in rates] == ["実績あり"]

    def test_different_sites_are_not_mixed(self, store: StateStore) -> None:
        # Arrange
        run_id = store.start_run("hellowork", "kw", "loc")
        store.finish_run(run_id, items_new=1, items_dup=0, items_no_phone=0)

        # Act
        rates = store.query_hit_rates("indeed")

        # Assert
        assert rates == []
