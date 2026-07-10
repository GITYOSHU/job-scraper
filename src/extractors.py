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

_PHONE_NORMALIZE = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "－": "-", "ー": "-", "―": "-", "−": "-",
    "（": "(", "）": ")",
    "　": " ",
})


def extract_phone_number(text: str) -> Optional[str]:
    """テキストから最初にマッチした電話番号を返す。無ければ None。

    - 全角数字・全角ハイフン・全角カッコを半角に正規化
    - 携帯 → フリーダイヤル → IP → 固定 → その他 の優先順で試行
    - 郵便番号 (7桁 XXX-XXXX) は境界条件で除外
    """
    if not text or not text.strip():
        return None
    normalized = text.translate(_PHONE_NORMALIZE)
    for pattern in _PHONE_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return _reformat(match.group(0))
    return None


def _reformat(raw: str) -> str:
    """電話番号文字列を `-` 区切り 3 セグメントに整形。"""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        if digits.startswith("0120"):
            return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
        if digits.startswith(("03", "06")):
            return f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11:
        if digits.startswith("0800"):
            return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return raw
