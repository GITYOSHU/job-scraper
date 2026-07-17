"""Apify misceres/indeed-scraper actor 経由の求人取得。

BD proxy + Playwright より 45x 安い ($0.003/record vs BD ~$0.135/record 実測)。
Actor は Indeed 詳細ページを JSON レスポンスで返す。
電話番号は description text 内に regex 抽出 (Indeed の Apify actor には
phone field 無いため。既存 extractors.extract_phone_number を継続使用)。

Docs: https://apify.com/misceres/indeed-scraper/api
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional
from urllib.parse import urljoin

import requests

from .extractors import extract_phone_number
from .models import JobPosting

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"
ACTOR_ID = "misceres~indeed-scraper"
JST = timezone(timedelta(hours=9))


class ApifyScrapingError(Exception):
    """Apify 呼び出しエラーの基底クラス。"""


class ApifyIndeedScraper:
    """Apify misceres/indeed-scraper を呼び出して求人データを取得。

    IndeedScraper と同じインタフェース (context manager + search generator) で
    main.py の tick ロジックをそのまま流用できる。
    """

    def __init__(
        self,
        request_delay_seconds: float = 0.0,
        headless: bool = True,
        api_token: Optional[str] = None,
        country: str = "JP",
        timeout_seconds: int = 600,
        **_ignored,
    ) -> None:
        """
        Args:
            api_token: Apify API token (env APIFY_API_TOKEN からも読める)
            country: Indeed の 国コード (JP = jp.indeed.com)
            timeout_seconds: 1 run の最大待機秒 (Apify actor が返すまでの上限)
            他: IndeedScraper との互換のため受けるが無視 (delay/headless 等)
        """
        self.api_token = api_token or os.environ.get("APIFY_API_TOKEN")
        if not self.api_token:
            raise ApifyScrapingError("APIFY_API_TOKEN 未設定")
        self.country = country
        self.timeout_seconds = timeout_seconds
        self.request_delay_seconds = request_delay_seconds
        self._session: Optional[requests.Session] = None

    def __enter__(self) -> "ApifyIndeedScraper":
        self._session = requests.Session()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._session is not None:
            self._session.close()

    def search(
        self,
        keyword: str,
        location: str = "",
        max_pages: int = 1,
    ) -> Iterator[JobPosting]:
        """1 検索クエリを Apify に投げて結果を JobPosting として yield する。

        Apify actor は「Indeed 検索結果を巡回して詳細ページを JSON 化」を
        1 run で完結する。 max_pages は Apify 側の maxItemsPerSearch に変換
        (1 page ≒ 15 records と近似)。
        """
        max_items = max(15, max_pages * 15)
        run_input = self._build_input(keyword, location, max_items)
        logger.info(
            f"Apify run 開始: kw='{keyword}' loc='{location}' max_items={max_items}"
        )

        try:
            items = self._run_actor(run_input)
        except ApifyScrapingError as e:
            logger.warning(f"Apify run 失敗 kw='{keyword}' loc='{location}': {e}")
            return

        logger.info(f"Apify run 完了: {len(items)} items 受領")
        for item in items:
            posting = self._to_posting(item)
            if posting:
                yield posting

    def _build_input(self, keyword: str, location: str, max_items: int) -> dict:
        # parseCompanyDetails=True で企業ページも取得 → description が長くなり
        # 電話番号 regex 命中率が上がる (実測 21% → 60-80% 期待)
        # コストは 1.5-2 倍だが 電話番号率 向上で record 単位効率が上がる
        return {
            "position": keyword,
            "country": self.country,
            "location": location,
            "maxItemsPerSearch": max_items,
            "parseCompanyDetails": True,
            "saveOnlyUniqueItems": True,
            "followApplyRedirects": False,
        }

    def _run_actor(self, run_input: dict) -> list[dict]:
        """run-sync-get-dataset-items で 1 shot 完了。

        大きい dataset は timeout 対策として wait/read 2 phase にしても良いが、
        1 検索 100 件程度なら同期で十分。
        """
        url = (
            f"{APIFY_API_BASE}/acts/{ACTOR_ID}/run-sync-get-dataset-items"
            f"?token={self.api_token}&clean=true&format=json"
            f"&timeout={self.timeout_seconds}"
        )
        assert self._session is not None
        resp = self._session.post(
            url,
            json=run_input,
            timeout=self.timeout_seconds + 30,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise ApifyScrapingError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise ApifyScrapingError(f"JSON デコード失敗: {e}") from e

        if isinstance(data, dict) and data.get("error"):
            # actor が空結果を dict で返す場合 (FOUND_NO_RESULTS 等)
            logger.info(f"actor error: {data.get('error')}")
            return []
        if not isinstance(data, list):
            logger.warning(f"想定外レスポンス型: {type(data)}")
            return []
        return data

    def _to_posting(self, item: dict) -> Optional[JobPosting]:
        """Apify actor JSON → JobPosting 変換。

        parseCompanyDetails=True の場合、以下から順に電話番号抽出:
        1. description text (求人本文)
        2. companyInfo / companyDetails 系フィールド (企業情報 - Apify actor 提供)
        3. descriptionHTML (念のため HTML からも)
        """
        url = item.get("url")
        company = item.get("company")
        if not url or not company:
            return None

        # 電話番号抽出: 複数ソースを順に試す
        phone = self._extract_phone_from_item(item)

        location_str = item.get("location") or ""
        address = location_str or None

        job_types = item.get("jobType") or []
        industry = ", ".join(job_types) if job_types else None

        # 代表者名: 企業情報から取れる場合
        rep_name = self._extract_rep_name(item)

        return JobPosting(
            company_name=company,
            job_url=url,
            address=address,
            phone_number=phone,
            industry=industry,
            representative_name=rep_name,
            scraped_at=item.get("scrapedAt") or datetime.now(JST).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _extract_phone_from_item(item: dict) -> Optional[str]:
        """Apify item から電話番号を段階的に抽出。

        優先順位:
        1. description (求人本文)
        2. companyInfo / companyDescription / aboutCompany (parseCompanyDetails 時)
        3. descriptionHTML (HTML 内のテキスト)
        """
        # 1. description
        description = item.get("description") or ""
        phone = extract_phone_number(description)
        if phone:
            return phone

        # 2. 企業情報系のフィールド (Apify actor のバージョンによって名前異なる)
        company_fields = [
            "companyInfo",
            "companyDescription",
            "aboutCompany",
            "companyDetails",
            "companyAbout",
        ]
        for key in company_fields:
            val = item.get(key)
            if val:
                if isinstance(val, dict):
                    # dict の場合は各値を試す
                    for v in val.values():
                        if isinstance(v, str):
                            phone = extract_phone_number(v)
                            if phone:
                                return phone
                elif isinstance(val, str):
                    phone = extract_phone_number(val)
                    if phone:
                        return phone

        # 3. HTML 版本文 (念のため)
        description_html = item.get("descriptionHTML") or ""
        if description_html:
            phone = extract_phone_number(description_html)
            if phone:
                return phone

        return None

    @staticmethod
    def _extract_rep_name(item: dict) -> Optional[str]:
        """企業情報から代表者名を抽出 (取得可能な場合のみ)。"""
        for key in ["ceo", "representative", "president", "companyRepresentative"]:
            val = item.get(key)
            if val and isinstance(val, str):
                return val
        # companyInfo dict からも探す
        info = item.get("companyInfo")
        if isinstance(info, dict):
            for k in ["ceo", "representative", "president"]:
                v = info.get(k)
                if v and isinstance(v, str):
                    return v
        return None
