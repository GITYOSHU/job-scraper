"""PHONE_FOCUSED_KEYWORDS の間引き結果を固定するテスト。

実測で 5 円/件 を超えたキーワードは pool から外している。誤って戻さないよう、
除外リストをテストで固定する (再追加したければ実測データを添えてここを直す)。
"""

from __future__ import annotations

import pytest

from src.query_pools import PHONE_FOCUSED_KEYWORDS, INDEED_LOCATIONS

# 2026-07-31 の実測で 1 件あたり 5 円を超えたキーワード (クライアント予算 3-5 円)
PRUNED_KEYWORDS: dict[str, float] = {
    "整体": 25.3,
    "配達": 10.7,
    "マッサージ": 9.6,
    "ネイリスト": 9.6,
    "キャバクラ": 9.0,
    "バー": 8.3,
    "エステ": 7.5,
    "アロマ": 7.5,
    "鍼灸": 7.2,
    "ドライバー": 7.0,
    "居酒屋": 7.0,
    "ホストクラブ": 6.9,
    "柔道整復": 6.3,
    "美容師": 5.6,
}


@pytest.mark.parametrize("keyword", sorted(PRUNED_KEYWORDS))
def test_low_yield_keyword_is_not_in_pool(keyword: str) -> None:
    assert keyword not in PHONE_FOCUSED_KEYWORDS


def test_pool_keeps_high_yield_keywords() -> None:
    """単価が安かった建設・警備・清掃系は残っていること。"""
    for keyword in ["左官", "鉄筋", "解体", "造園", "配管工", "清掃", "警備員", "塗装"]:
        assert keyword in PHONE_FOCUSED_KEYWORDS


def test_pool_has_no_duplicates() -> None:
    assert len(PHONE_FOCUSED_KEYWORDS) == len(set(PHONE_FOCUSED_KEYWORDS))


def test_pool_size_leaves_room_for_unexplored_locations() -> None:
    """クエリ総数が 2,000 を下回るとプールを早く枯らして重複率が悪化する。"""
    total_queries = len(PHONE_FOCUSED_KEYWORDS) * len(INDEED_LOCATIONS)
    assert total_queries >= 2000, f"クエリ総数 {total_queries} は間引きすぎ"
