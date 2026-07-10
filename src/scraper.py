"""Indeed 求人ページのスクレイピングロジック。

⚠️ 注意: Indeed の利用規約はスクレイピングを禁止しています。
本モジュールは技術検証・個人利用目的です。実運用時は公式 API を使用してください。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .models import JobPosting

logger = logging.getLogger(__name__)

INDEED_BASE_URL = "https://jp.indeed.com"
JST = timezone(timedelta(hours=9))


class ScrapingError(Exception):
    """スクレイピング処理中に発生したエラーの基底クラス。"""


class IndeedScraper:
    """Indeed 求人ページのスクレイパー。

    Args:
        request_delay_seconds: リクエスト間の待機秒数（BAN 回避のため 3 秒以上推奨）
        rotate_user_agent: True なら毎リクエスト User-Agent を変更
        session: 外部から提供する requests.Session（テスト用途）
    """

    def __init__(
        self,
        request_delay_seconds: float = 3.0,
        rotate_user_agent: bool = True,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.rotate_user_agent = rotate_user_agent
        self.session = session or requests.Session()
        self._ua = UserAgent() if rotate_user_agent else None

    def _headers(self) -> dict[str, str]:
        ua = self._ua.random if self._ua else "Mozilla/5.0 (compatible; JobScraper/0.1)"
        return {
            "User-Agent": ua,
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((requests.RequestException, ScrapingError)),
        reraise=True,
    )
    def _fetch(self, url: str) -> str:
        logger.info(f"Fetching: {url}")
        response = self.session.get(url, headers=self._headers(), timeout=30)
        if response.status_code == 429:
            raise ScrapingError(f"Rate limited (429) at {url}")
        if response.status_code >= 400:
            raise ScrapingError(f"HTTP {response.status_code} at {url}")
        return response.text

    def search(
        self,
        keyword: str,
        location: str = "",
        max_pages: int = 1,
    ) -> Iterator[JobPosting]:
        """検索結果ページを巡回して求人を yield する。

        Args:
            keyword: 検索キーワード（職種等）
            location: 勤務地
            max_pages: 取得する検索結果ページ数

        Yields:
            JobPosting: 1 件ずつの求人データ
        """
        for page in range(max_pages):
            params = {"q": keyword, "l": location, "start": page * 10}
            search_url = f"{INDEED_BASE_URL}/jobs?{urlencode(params)}"
            try:
                html = self._fetch(search_url)
            except (requests.RequestException, ScrapingError) as e:
                logger.warning(f"検索ページ取得失敗 page={page}: {e}")
                continue

            job_urls = list(self._extract_job_urls(html))
            logger.info(f"page={page + 1}/{max_pages}: {len(job_urls)} 件の求人 URL を検出")

            for job_url in job_urls:
                self._sleep()
                try:
                    detail_html = self._fetch(job_url)
                    posting = self._parse_job_detail(detail_html, job_url)
                    if posting:
                        yield posting
                except (requests.RequestException, ScrapingError) as e:
                    logger.warning(f"求人詳細取得失敗 url={job_url}: {e}")
                    continue

            self._sleep()

    def _extract_job_urls(self, html: str) -> Iterator[str]:
        """検索結果 HTML から求人詳細ページの URL を抽出。

        NOTE: Indeed の HTML 構造は頻繁に変更されるため、
        実行時に動作しなくなった場合はセレクタを見直すこと。
        """
        soup = BeautifulSoup(html, "lxml")
        for anchor in soup.select("a[href*='/rc/clk'], a[href*='/viewjob']"):
            href = anchor.get("href", "")
            if href:
                yield urljoin(INDEED_BASE_URL, href)

    def _parse_job_detail(self, html: str, url: str) -> Optional[JobPosting]:
        """求人詳細ページの HTML をパースして JobPosting を返す。

        NOTE: セレクタは Indeed の DOM 変更により壊れやすい。
        代表者名・電話番号は基本的に Indeed には掲載されないため、
        別途企業サイト・法人番号 API 等の併用が必要。
        """
        soup = BeautifulSoup(html, "lxml")

        company_name = self._select_text(soup, [
            "[data-testid='inlineHeader-companyName'] a",
            "[data-testid='inlineHeader-companyName']",
            "div[data-company-name] a",
            ".jobsearch-CompanyInfoContainer a",
        ])
        if not company_name:
            logger.debug(f"会社名抽出失敗: {url}")
            return None

        address = self._select_text(soup, [
            "[data-testid='inlineHeader-companyLocation'] div",
            "[data-testid='job-location']",
            ".jobsearch-JobInfoHeader-subtitle div",
        ])

        industry = self._select_text(soup, [
            "[data-testid='job-industry']",
        ])

        return JobPosting(
            company_name=company_name,
            address=address,
            phone_number=None,
            industry=industry,
            representative_name=None,
            job_url=url,
            scraped_at=datetime.now(JST).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _select_text(soup: BeautifulSoup, selectors: list[str]) -> Optional[str]:
        for sel in selectors:
            element = soup.select_one(sel)
            if element:
                text = element.get_text(strip=True)
                if text:
                    return text
        return None

    def _sleep(self) -> None:
        jitter = random.uniform(0.5, 1.5)
        time.sleep(self.request_delay_seconds + jitter)
