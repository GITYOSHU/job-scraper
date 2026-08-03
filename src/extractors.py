"""求人本文からの構造化情報抽出ユーティリティ (電話番号・業種等)。

Indeed の構造化フィールドに無い項目は本文テキストから正規表現で拾う。
"""

from __future__ import annotations

import re
from typing import Optional

# 前後に数字が続かないこと (郵便番号 100-6640 の内側 00-6640 を誤検出しないため)
_BOUNDARY_L = r"(?<![\d\-])"
_BOUNDARY_R = r"(?!\d)"

# セパレータ: ハイフン各種 / 空白 / カッコ (全角は事前 normalize で半角化)
_SEP = r"[-\s()]"

# 携帯優先 (070/080/090 で 11 桁) → フリーダイヤル → IP 電話 → 東京/大阪 →
# 4 桁市外局番 → 3 桁市外局番 → ハイフン無し 10/11 桁の順で試行。
_PHONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_BOUNDARY_L}0[789]0{_SEP}?\d{{4}}{_SEP}?\d{{4}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}0120{_SEP}?\d{{3}}{_SEP}?\d{{3}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}0800{_SEP}?\d{{3}}{_SEP}?\d{{4}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}050{_SEP}?\d{{4}}{_SEP}?\d{{4}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}0[36]{_SEP}\d{{4}}{_SEP}\d{{4}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}0\d{{3}}{_SEP}\d{{2}}{_SEP}\d{{4}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}0\d{{2}}{_SEP}\d{{3}}{_SEP}\d{{4}}{_BOUNDARY_R}"),
    re.compile(rf"{_BOUNDARY_L}0\d{{9,10}}{_BOUNDARY_R}"),
)

# 固定電話は必ず 10 桁で、末尾 4 桁が加入者番号。残りを市外局番と市内局番に割る。
# 市外局番の桁数は closed set で判定し、いずれにも該当しなければ 4 桁とみなす。
#
# 既知の限界 (ハイフン位置のみの問題で、数字列は常に正しい):
# - 3 桁局番と 4 桁局番が先頭を共有する場合は数字だけでは判別できない
#   (例: 098=那覇 と 0985=宮崎)。この実装は 3 桁側を優先する。
#   実データ 8,321 番号中の該当は約 17 番号 (0.2%)。
# - 5 桁市外局番 (離島・過疎地) は非対応。実データでの出現は 0 件。
_AREA_CODE_2 = frozenset({"03", "06"})
_AREA_CODE_3 = frozenset({
    "011", "017", "018", "019", "022", "023", "024", "025", "026", "027",
    "028", "029", "042", "043", "044", "045", "046", "047", "048", "049",
    "052", "053", "054", "055", "058", "059", "072", "073", "075", "076",
    "077", "078", "079", "082", "083", "084", "086", "087", "088", "089",
    "092", "093", "095", "096", "097", "098", "099",
})

# 携帯 (070/080/090) と IP 電話 (050) は 11 桁でなければならない。
_MOBILE_AND_IP_PREFIXES = ("050", "070", "080", "090")
# 加入者番号として払い出されない先頭 3 桁 (010=国際, 020=M2M/ポケベル, 060=未割当)。
_NON_SUBSCRIBER_PREFIXES = ("010", "020", "060")
# 加入者番号 (末尾) がこの桁数ぶん同一数字ならプレースホルダとみなす。
_PLACEHOLDER_TAIL_LEN = 8

_PHONE_NORMALIZE = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "－": "-", "ー": "-", "―": "-", "−": "-",
    "（": "(", "）": ")",
    "　": " ",
})


def extract_phone_number(text: str) -> Optional[str]:
    """テキストから最初にマッチした有効な電話番号を返す。無ければ None。

    - 全角数字・全角ハイフン・全角カッコを半角に正規化
    - 携帯 → フリーダイヤル → IP → 固定 → その他 の優先順で試行
    - 郵便番号 (7桁 XXX-XXXX) は境界条件で除外
    - `000-000-0000` のようなプレースホルダや桁欠けは無効として読み飛ばし、
      同じテキスト内の後続候補を探す
    """
    if not text or not text.strip():
        return None
    normalized = text.translate(_PHONE_NORMALIZE)
    for pattern in _PHONE_PATTERNS:
        for match in pattern.finditer(normalized):
            digits = re.sub(r"\D", "", match.group(0))
            if is_valid_phone_digits(digits):
                return _reformat(match.group(0))
    return None


def normalize_phone_number(value: Optional[str]) -> Optional[str]:
    """収集済みの電話番号文字列を現行の整形ルールで作り直す。無効なら None。

    抽出済みの値を対象とするため本文サーチは行わない。過去の整形ロジックが
    付けたハイフン位置の誤りを直し、無効値 (プレースホルダ・桁欠け) を落とす。
    """
    if not value:
        return None
    digits = re.sub(r"\D", "", value.translate(_PHONE_NORMALIZE))
    if not is_valid_phone_digits(digits):
        return None
    return _reformat(digits)


def is_valid_phone_digits(digits: str) -> bool:
    """数字のみの文字列が日本の加入者電話番号として成立するかを判定する。

    実在する番号かまでは判定できない (市外局番と市内局番の割当表は持たない)。
    明らかに番号でないもの (プレースホルダ・桁欠け・非加入者プレフィクス) を弾く。
    """
    if len(digits) not in (10, 11):
        return False
    if not digits.startswith("0") or digits.startswith("00"):
        return False
    if digits[:3] in _NON_SUBSCRIBER_PREFIXES:
        return False
    if len(set(digits[-_PLACEHOLDER_TAIL_LEN:])) == 1:
        return False
    if len(digits) == 11:
        return digits.startswith(_MOBILE_AND_IP_PREFIXES)
    return not digits.startswith(_MOBILE_AND_IP_PREFIXES)


def _area_code_length(digits: str) -> int:
    """固定電話 10 桁の市外局番が何桁かを返す (2 / 3 / 4)。"""
    if digits[:2] in _AREA_CODE_2:
        return 2
    if digits[:3] in _AREA_CODE_3:
        return 3
    return 4


def _reformat(raw: str) -> str:
    """電話番号文字列を `-` 区切り 3 セグメントに整形。"""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        if digits.startswith("0120"):
            return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
        area_len = _area_code_length(digits)
        return f"{digits[:area_len]}-{digits[area_len:6]}-{digits[6:]}"
    if len(digits) == 11:
        if digits.startswith("0800"):
            return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return raw
