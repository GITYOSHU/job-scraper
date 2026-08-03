"""既成データセット (Bright Data 等) の無料サンプルを購入前に判定する。

判定したいこと:
- 日本のレコードがどれだけ含まれるか
- description から電話番号がどの割合で取れるか
- 企業サイト URL の充足率 (取れなかった分を別手段で補えるか)
- そこから逆算した 1 件あたりの調達単価
"""

from __future__ import annotations

import csv
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .extractors import extract_phone_number


@dataclass(frozen=True)
class DatasetRecord:
    """ベンダー差を吸収した後の 1 レコード。"""

    company_name: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    country_code: Optional[str] = None
    website: Optional[str] = None
    url: Optional[str] = None

    @property
    def is_japan(self) -> bool:
        """日本のレコードか。国コードがあればそれを優先し、無ければ住所で判定する。"""
        if self.country_code:
            return self.country_code.strip().lower() in _JAPAN_CODES
        return _looks_japanese_address(self.location)


def load_records(path: Path) -> list[dict]:
    """データセットのサンプルファイルを読み込む。

    対応形式: .json (配列 / {"data": [...]} 形式) / .ndjson / .jsonl / .csv。
    いずれも .gz 圧縮に対応する。Parquet は非対応 (別形式での再取得を促す)。
    """
    path = Path(path)
    suffixes = [s.lower() for s in path.suffixes]
    is_gzipped = suffixes and suffixes[-1] == ".gz"
    fmt = suffixes[-2] if is_gzipped and len(suffixes) >= 2 else (
        suffixes[-1] if suffixes else ""
    )

    if fmt not in _SUPPORTED_FORMATS:
        raise ValueError(
            f"未対応の形式です: {fmt or path.name}。"
            f"{'/'.join(sorted(_SUPPORTED_FORMATS))} のいずれかで再取得してください"
        )

    opener = gzip.open if is_gzipped else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        if fmt == ".csv":
            return [dict(row) for row in csv.DictReader(f)]
        text = f.read()

    if fmt == ".json":
        return _unwrap_json(json.loads(text))
    return [json.loads(line) for line in text.splitlines() if line.strip()]


_SUPPORTED_FORMATS = frozenset({".json", ".ndjson", ".jsonl", ".csv"})

# 配列を包んで配布されることがあるキー
_ARRAY_WRAPPER_KEYS = ("data", "records", "items", "results")


def _unwrap_json(parsed: object) -> list[dict]:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in _ARRAY_WRAPPER_KEYS:
            value = parsed.get(key)
            if isinstance(value, list):
                return value
        return [parsed]
    raise ValueError(f"JSON のトップレベルが配列でもオブジェクトでもありません: {type(parsed)}")


@dataclass(frozen=True)
class ProbeReport:
    """サンプルから読み取った購入判断用の実測値。"""

    total: int = 0
    japan: int = 0
    with_description: int = 0
    with_phone: int = 0
    unique_phones: int = 0
    without_phone: int = 0
    without_phone_with_website: int = 0

    @property
    def japan_rate(self) -> float:
        return self.japan / self.total if self.total else 0.0

    @property
    def phone_rate(self) -> float:
        return self.with_phone / self.japan if self.japan else 0.0


@dataclass(frozen=True)
class Estimate:
    """サンプルの実測値から逆算した、実際に買った場合の単価と総額。"""

    delivered: int = 0
    total_cost_jpy: float = 0.0
    records_needed_for_target: float = 0.0
    target_cost_jpy: float = 0.0

    @property
    def unit_cost_jpy(self) -> float:
        return self.total_cost_jpy / self.delivered if self.delivered else 0.0


def estimate(
    report: ProbeReport,
    record_price_usd: float,
    jpy_rate: float,
    contact_price_usd: Optional[float] = None,
    contact_success_rate: float = 0.7,
    target: Optional[int] = None,
) -> Estimate:
    """サンプル 1 本ぶんの実測から、購入時の単価・総額を逆算する。

    contact_price_usd を渡すと、description に電話が無かったレコードを
    企業サイト経由で補完した場合を上乗せする (成功時のみ課金される前提)。
    """
    cost = report.total * record_price_usd * jpy_rate
    delivered = report.unique_phones

    if contact_price_usd is not None:
        found = report.without_phone_with_website * contact_success_rate
        cost += found * contact_price_usd * jpy_rate
        delivered += int(found)

    unit = cost / delivered if delivered else 0.0
    needed = (report.total / delivered * target) if (target and delivered) else 0.0
    return Estimate(
        delivered=delivered,
        total_cost_jpy=cost,
        records_needed_for_target=needed,
        target_cost_jpy=(target * unit) if target else 0.0,
    )


def probe(raw_records: Iterable[dict]) -> ProbeReport:
    """サンプルレコード群から購入判断用の実測値を数える。

    電話番号は納品時と同じ抽出・検証ロジック (extractors) を通すため、
    ここで出る命中率はそのまま実運用の見込み値になる。
    """
    total = japan = with_description = with_phone = 0
    without_phone = without_phone_with_website = 0
    phones: set[str] = set()

    for raw in raw_records:
        total += 1
        record = normalize_record(raw)
        if not record.is_japan:
            continue
        japan += 1
        if record.description:
            with_description += 1
        phone = extract_phone_number(record.description or "")
        if phone:
            with_phone += 1
            phones.add(phone)
            continue
        without_phone += 1
        if record.website:
            without_phone_with_website += 1

    return ProbeReport(
        total=total,
        japan=japan,
        with_description=with_description,
        with_phone=with_phone,
        unique_phones=len(phones),
        without_phone=without_phone,
        without_phone_with_website=without_phone_with_website,
    )


_JAPAN_CODES = frozenset({"jp", "jpn", "japan", "日本", "392"})

# 住所から日本を判定するための手がかり (国コードが無いデータセット向け)
_JAPAN_ADDRESS_HINTS = ("日本", "都", "道", "府", "県", "市", "区", "町", "村")

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": ("company_name", "company", "employer", "companyName"),
    "description": (
        "description",
        "job_description",
        "descriptionText",
        "jobDescription",
        "description_text",
    ),
    "location": ("location", "job_location", "city", "address"),
    "country_code": ("country_code", "country", "countryCode"),
    "website": (
        "website",
        "company_website",
        "companyWebsite",
        "company_link",
        "company_url",
    ),
    "url": ("url", "job_url", "link", "jobUrl", "current_url"),
}


def normalize_record(raw: dict) -> DatasetRecord:
    """ベンダーごとに異なるキー名を吸収して DatasetRecord にする。

    値が空文字・空白のみの場合は None として扱う (充足率を正しく測るため)。
    """
    return DatasetRecord(
        **{field: _pick(raw, aliases) for field, aliases in _FIELD_ALIASES.items()}
    )


def _pick(raw: dict, aliases: tuple[str, ...]) -> Optional[str]:
    for alias in aliases:
        value = raw.get(alias)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _looks_japanese_address(location: Optional[str]) -> bool:
    if not location:
        return False
    return any(hint in location for hint in _JAPAN_ADDRESS_HINTS)
