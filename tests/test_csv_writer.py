"""CsvWriter のユニットテスト。"""

import csv
from pathlib import Path

import pytest

from src.csv_writer import CsvWriter
from src.models import JobPosting, SHEET_HEADER


@pytest.fixture
def sample_postings() -> list[JobPosting]:
    return [
        JobPosting(
            company_name="株式会社サンプル",
            job_url="https://example.com/job/1",
            address="東京都渋谷区",
            phone_number="03-1234-5678",
            industry="IT",
            scraped_at="2026-07-10T12:00:00+09:00",
        ),
        JobPosting(
            company_name="株式会社ミニマル",
            job_url="https://example.com/job/2",
        ),
    ]


def test_write_creates_file_with_header_and_rows(
    tmp_path: Path, sample_postings: list[JobPosting]
) -> None:
    writer = CsvWriter(output_dir=tmp_path, filename="test.csv")

    path, written = writer.write(sample_postings)

    assert path.exists()
    assert written == 2

    with path.open("r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    assert rows[0] == SHEET_HEADER
    assert rows[1][0] == "株式会社サンプル"
    assert rows[1][4] == "https://example.com/job/1"
    assert rows[2][0] == "株式会社ミニマル"
    assert rows[2][1] == ""


def test_write_creates_output_dir_if_missing(
    tmp_path: Path, sample_postings: list[JobPosting]
) -> None:
    nested = tmp_path / "nested" / "dir"
    writer = CsvWriter(output_dir=nested, filename="out.csv")

    path, _ = writer.write(sample_postings)

    assert nested.exists()
    assert path.parent == nested


def test_write_auto_generates_filename_when_none(
    tmp_path: Path, sample_postings: list[JobPosting]
) -> None:
    writer = CsvWriter(output_dir=tmp_path)

    path, _ = writer.write(sample_postings)

    assert path.name.startswith("jobs-")
    assert path.suffix == ".csv"


def test_write_handles_empty_postings(tmp_path: Path) -> None:
    writer = CsvWriter(output_dir=tmp_path, filename="empty.csv")

    path, written = writer.write([])

    assert written == 0
    with path.open("r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows == [SHEET_HEADER]
