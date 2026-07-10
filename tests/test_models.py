"""JobPosting モデルのユニットテスト。"""

from src.models import JobPosting, SHEET_HEADER


def test_to_row_returns_columns_in_header_order():
    posting = JobPosting(
        company_name="株式会社テスト",
        job_url="https://example.com/job/1",
        address="東京都渋谷区",
        phone_number="03-1234-5678",
        industry="IT",
        scraped_at="2026-07-10T12:00:00+09:00",
    )
    row = posting.to_row()

    assert len(row) == len(SHEET_HEADER)
    assert row[0] == "株式会社テスト"
    assert row[4] == "https://example.com/job/1"
    assert row[5] == "2026-07-10T12:00:00+09:00"


def test_to_row_replaces_none_with_empty_string():
    posting = JobPosting(
        company_name="株式会社最小",
        job_url="https://example.com/job/2",
    )
    row = posting.to_row()

    assert row[1] == ""
    assert row[2] == ""
    assert row[3] == ""
    assert row[5] == ""


def test_sheet_header_has_expected_columns():
    assert SHEET_HEADER == [
        "会社名",
        "住所",
        "電話番号",
        "業種",
        "掲載求人URL",
        "取得日時",
    ]
