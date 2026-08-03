"""export コマンドが電話番号の正規化を通してから CSV を書くことのテスト。"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

import pytest

from src.main import _cmd_export
from src.models import JobPosting
from src.state import StateStore


def _posting(job_url: str, phone_number: str, scraped_at: str) -> JobPosting:
    return JobPosting(
        company_name="テスト会社",
        job_url=job_url,
        phone_number=phone_number,
        scraped_at=scraped_at,
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setenv("STATE_DB", str(path))
    return path


def _read_phone_column(csv_path: Path) -> list[str]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        return [row["電話番号"] for row in csv.DictReader(f)]


def test_export_normalizes_and_drops_invalid_phone_numbers(
    db_path: Path, tmp_path: Path
) -> None:
    # Arrange: 旧ロジックのハイフン誤り 1 件 + プレースホルダ 1 件 + 正常 1 件
    store = StateStore(db_path=db_path)
    for url, phone in [
        ("https://example.com/1", "013-826-9335"),
        ("https://example.com/2", "000-000-0000"),
        ("https://example.com/3", "03-1234-5678"),
    ]:
        store.save_posting(
            _posting(url, phone, "2026-07-20T10:00:00+09:00"),
            site="indeed",
            keyword="kw",
            location="loc",
        )
    output = tmp_path / "out.csv"
    args = argparse.Namespace(
        output=str(output), site="indeed", since=None, all_shards=False
    )

    # Act
    exit_code = _cmd_export(args, logging.getLogger(__name__))

    # Assert
    assert exit_code == 0
    assert set(_read_phone_column(output)) == {"0138-26-9335", "03-1234-5678"}
