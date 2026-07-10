"""求人情報のデータモデル定義。"""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass(frozen=True)
class JobPosting:
    """1件の求人情報を表すイミュータブルなデータクラス。

    Attributes:
        company_name: 会社名（必須）
        address: 住所（記載がある場合のみ）
        phone_number: 電話番号（Indeed には基本非掲載）
        industry: 業種・職種カテゴリ
        representative_name: 代表者名（Indeed には非掲載）
        job_url: 求人詳細ページ URL（必須）
        scraped_at: 取得日時（ISO8601 文字列）
    """

    company_name: str
    job_url: str
    address: Optional[str] = None
    phone_number: Optional[str] = None
    industry: Optional[str] = None
    representative_name: Optional[str] = None
    scraped_at: Optional[str] = None

    def to_row(self) -> list[str]:
        """スプレッドシート書き込み用の 1 行データに変換。列順は固定。"""
        return [
            self.company_name,
            self.address or "",
            self.phone_number or "",
            self.industry or "",
            self.representative_name or "",
            self.job_url,
            self.scraped_at or "",
        ]

    def to_dict(self) -> dict:
        return asdict(self)


SHEET_HEADER: list[str] = [
    "会社名",
    "住所",
    "電話番号",
    "業種",
    "代表者名",
    "掲載求人URL",
    "取得日時",
]
