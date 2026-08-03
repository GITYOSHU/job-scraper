"""extractors のユニットテスト。"""

import pytest

from src.extractors import extract_phone_number, normalize_phone_number


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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("TEL 0138-26-9335", "0138-26-9335"),  # 函館
        ("TEL 0138269335", "0138-26-9335"),
        ("お問い合わせ 0123-22-7400", "0123-22-7400"),  # 千歳
        ("代表 0942-31-1234", "0942-31-1234"),  # 久留米
        ("電話 0155123456", "0155-12-3456"),  # 帯広
    ],
)
def test_four_digit_area_code_is_split_after_four_digits(
    text: str, expected: str
) -> None:
    """4 桁市外局番は 0XXX-XX-XXXX に整形される (3 桁局番の既定分割にしない)。"""
    assert extract_phone_number(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("TEL 0522345678", "052-234-5678"),  # 名古屋
        ("TEL 0111234567", "011-123-4567"),  # 札幌
        ("TEL 0987654321", "098-765-4321"),  # 那覇
    ],
)
def test_three_digit_area_code_keeps_three_digit_split(
    text: str, expected: str
) -> None:
    assert extract_phone_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "お電話: 000-000-0000 までご連絡ください",  # プレースホルダ
        "TEL 090-0000-0000",  # 加入者番号が全て 0
        "番号 0000188459",  # 00 始まり (国際/事業者識別番号の領域)
        "連絡先 04102809506",  # 11 桁だが携帯/IP/0800 のいずれでもない
        "求人ID: 01234567890",  # 11 桁の連番 ID
        "担当 080-382-3863",  # 携帯なのに 10 桁 (桁欠け)
        "IP 050-221-8818",  # IP 電話なのに 10 桁 (桁欠け)
        "受付 01012345678",  # 010 は国際プレフィクスで加入者番号ではない
    ],
)
def test_rejects_invalid_phone_numbers(text: str) -> None:
    assert extract_phone_number(text) is None


def test_skips_placeholder_and_returns_real_number() -> None:
    """無効値が先に出現しても、後続の実在番号を拾う。"""
    text = "TEL 000-000-0000\n実際のお問い合わせ先: 052-234-5678"
    assert extract_phone_number(text) == "052-234-5678"


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("013-826-9335", "0138-26-9335"),  # 旧ロジックが壊した 4 桁局番
        ("094-231-1234", "0942-31-1234"),
        ("03-1234-5678", "03-1234-5678"),  # 既に正しいものは変わらない
        ("090-1234-5678", "090-1234-5678"),
        ("0120-123-456", "0120-123-456"),
        ("0312345678", "03-1234-5678"),  # ハイフン無しも整形する
    ],
)
def test_normalize_phone_number_reformats_stored_value(
    stored: str, expected: str
) -> None:
    """収集済みの電話番号文字列を現行ルールで整形し直す。"""
    assert normalize_phone_number(stored) == expected


@pytest.mark.parametrize(
    "stored",
    ["000-000-0000", "090-0000-0000", "000-018-8459", "041-0280-9506", "", "  "],
)
def test_normalize_phone_number_returns_none_for_invalid(stored: str) -> None:
    assert normalize_phone_number(stored) is None
