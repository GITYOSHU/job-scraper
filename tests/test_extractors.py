"""extractors のユニットテスト。"""

import pytest

from src.extractors import extract_phone_number


@pytest.mark.parametrize(
    "text,expected",
    [
        ("お問い合わせ: 03-1234-5678 まで", "03-1234-5678"),
        ("TEL 03(1234)5678", "03-1234-5678"),
        ("電話 090-1234-5678 担当まで", "090-1234-5678"),
        ("Tel: 0312345678", "03-1234-5678"),
        ("携帯 09012345678", "090-1234-5678"),
        ("お電話：０３－１２３４－５６７８", "03-1234-5678"),
        ("担当: 03（1234）5678", "03-1234-5678"),
        ("フリーダイヤル 0120-123-456", "0120-123-456"),
    ],
)
def test_extract_phone_number_success(text: str, expected: str) -> None:
    assert extract_phone_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "電話番号はありません",
        "郵便番号 100-0001",
        "〒100-6640 東京都",
        "〒１００－６６４０",
        "郵便番号: 150-0001",
        "12345",
        "税番号 12345678",
    ],
)
def test_extract_phone_number_no_match(text: str) -> None:
    assert extract_phone_number(text) is None


def test_zipcode_before_phone_number_is_not_confused() -> None:
    text = "〒100-6640 東京都千代田区 TEL 03-1234-5678"
    assert extract_phone_number(text) == "03-1234-5678"


def test_returns_first_match_when_multiple() -> None:
    text = "本社 03-1111-2222 / 支社 06-3333-4444"
    assert extract_phone_number(text) == "03-1111-2222"


def test_prefers_mobile_over_zipcode_like() -> None:
    # 携帯パターンが優先されるが、郵便番号は境界で除外される
    text = "〒150-0001 東京都渋谷区\n担当: 090-1234-5678"
    assert extract_phone_number(text) == "090-1234-5678"


def test_handles_none_and_whitespace_input() -> None:
    assert extract_phone_number("") is None
    assert extract_phone_number("   \n\t  ") is None
