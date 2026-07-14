"""Indeed 求人ページのスクレイピングロジック (Playwright 版)。

⚠️ 注意: Indeed の利用規約はスクレイピングを禁止しています。
本モジュールは技術検証・個人利用目的です。実運用時は公式 API を使用してください。

素の requests では TLS フィンガープリント / JS 実行チェックで 403 になるため、
実ブラウザ (Chromium via Playwright) を経由してアクセスする。
"""

from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .extractors import extract_phone_number
from .models import JobPosting

logger = logging.getLogger(__name__)

INDEED_BASE_URL = "https://jp.indeed.com"
JST = timezone(timedelta(hours=9))

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class ScrapingError(Exception):
    """スクレイピング処理中に発生したエラーの基底クラス。"""


class BanDetectedError(Exception):
    """403 が閾値回連続して観測された。上位で pause 状態に遷移する用。"""


class IndeedScraper:
    """Indeed 求人ページのスクレイパー (Playwright ベース)。

    Args:
        request_delay_seconds: リクエスト間の待機秒数（BAN 回避のため 3 秒以上推奨）
        headless: True でヘッドレスモード。ブロック検証時は False にすると挙動が見える
        user_agent: リクエストに使う User-Agent
        page_load_timeout_ms: ページロードのタイムアウト（ミリ秒）

    Usage:
        with IndeedScraper() as scraper:
            for posting in scraper.search(keyword="エンジニア", max_pages=1):
                print(posting)
    """

    def __init__(
        self,
        request_delay_seconds: float = 3.0,
        headless: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        page_load_timeout_ms: int = 30_000,
        ban_threshold: int = 3,
        proxy: Optional[dict] = None,
    ) -> None:
        """
        Args:
            proxy: Playwright に渡す proxy 設定辞書。
                例: {"server": "http://brd.superproxy.io:22225",
                     "username": "brd-customer-hl_xxx-zone-yyy",
                     "password": "..."}
                None なら直接接続 (家庭用 IP or GitHub Actions IP)。
                Bright Data Web Unlocker 経由なら anti-bot が透過される。
        """
        self.request_delay_seconds = request_delay_seconds
        self.headless = headless
        self.user_agent = user_agent
        self.page_load_timeout_ms = page_load_timeout_ms
        self.ban_threshold = ban_threshold
        self.proxy = proxy
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._consecutive_403 = 0

    def __enter__(self) -> "IndeedScraper":
        self._playwright = sync_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        # Bright Data の HTTPS proxy port (33335) は self-signed cert なので
        # proxy 使用時は cert error を無視する
        if self.proxy:
            launch_args.append("--ignore-certificate-errors")
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    @contextmanager
    def _new_page(self) -> Iterator[Page]:
        assert self._browser is not None, "Use IndeedScraper as context manager."
        ctx_kwargs = dict(
            user_agent=self.user_agent,
            locale="ja-JP",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        if self.proxy:
            ctx_kwargs["proxy"] = self.proxy
        context = self._browser.new_context(**ctx_kwargs)
        page = context.new_page()
        try:
            yield page
        finally:
            page.close()
            context.close()

    def _fetch_html(self, url: str) -> Optional[str]:
        """指定 URL のレンダリング済み HTML を返す。403/タイムアウト時は None。"""
        with self._new_page() as page:
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.page_load_timeout_ms,
                )
            except PlaywrightTimeoutError:
                logger.warning(f"ページロードタイムアウト: {url}")
                return None

            if response is None:
                logger.warning(f"レスポンス取得失敗: {url}")
                return None

            status = response.status
            if status == 403:
                self._consecutive_403 += 1
                logger.warning(
                    f"HTTP 403 at {url} (consecutive_403={self._consecutive_403}/{self.ban_threshold})"
                )
                if self._consecutive_403 >= self.ban_threshold:
                    raise BanDetectedError(
                        f"HTTP 403 が {self.ban_threshold} 回連続。BAN 疑い。"
                    )
                return None
            if status >= 400:
                logger.warning(f"HTTP {status} at {url}")
                return None
            self._consecutive_403 = 0

            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass

            return page.content()

    def search(
        self,
        keyword: str,
        location: str = "",
        max_pages: int = 1,
    ) -> Iterator[JobPosting]:
        """検索結果ページを巡回して求人を yield する。

        連続 2 ページで取得失敗した場合、そのクエリは死んでいると判断して abort する
        (地域名不正・Indeed 側の一時異常等で丸ごと 90 秒 x 3 待たされる問題の回避)。
        """
        consecutive_search_fail = 0
        for page_index in range(max_pages):
            params = {"q": keyword, "l": location, "start": page_index * 10}
            search_url = f"{INDEED_BASE_URL}/jobs?{urlencode(params)}"
            logger.info(f"Fetching search page {page_index + 1}/{max_pages}: {search_url}")

            html = self._fetch_html(search_url)
            if html is None:
                consecutive_search_fail += 1
                logger.warning(
                    f"検索ページ取得失敗 page={page_index} "
                    f"(consecutive_fail={consecutive_search_fail})"
                )
                if consecutive_search_fail >= 2:
                    logger.warning(
                        f"連続失敗のためクエリ '{keyword} @ {location}' を打ち切り"
                    )
                    return
                continue

            consecutive_search_fail = 0
            job_urls = list(self._extract_job_urls(html))
            logger.info(f"page={page_index + 1}: {len(job_urls)} 件の求人 URL を検出")

            for job_url in job_urls:
                self._sleep()
                detail_html = self._fetch_html(job_url)
                if detail_html is None:
                    continue
                posting = self._parse_job_detail(detail_html, job_url)
                if posting:
                    yield posting

            self._sleep()

    def _extract_job_urls(self, html: str) -> Iterator[str]:
        """検索結果 HTML から求人詳細ページの URL を抽出。

        /pagead/clk (広告リダイレクタ) は複数リダイレクトで proxy 経由だと
        タイムアウトが多発するので除外する。
        /viewjob?jk=xxx (実 URL) と /rc/clk (通常クリック) のみ拾う。
        """
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()

        # anchor 要素の候補セレクタ
        anchor_selectors = [
            "a[href*='/viewjob']",
            "a[href*='/rc/clk']",
            "a.jcs-JobTitle",
            "h2.jobTitle a",
        ]
        for sel in anchor_selectors:
            for anchor in soup.select(sel):
                href = anchor.get("href", "")
                if not href:
                    continue
                if "/pagead/clk" in href:
                    # 広告リダイレクタは除外
                    continue
                full_url = urljoin(INDEED_BASE_URL, href)
                if "/pagead/" in full_url:
                    continue
                if full_url in seen:
                    continue
                seen.add(full_url)
                yield full_url

    def _parse_job_detail(self, html: str, url: str) -> Optional[JobPosting]:
        """求人詳細ページの HTML をパースして JobPosting を返す。

        NOTE: セレクタは Indeed の DOM 変更により壊れやすい。
        電話番号は基本的に Indeed には掲載されないため、
        別途企業サイト・法人番号 API 等の併用が必要。
        """
        soup = BeautifulSoup(html, "lxml")

        company_name = self._select_text(soup, [
            "[data-testid='inlineHeader-companyName'] a",
            "[data-testid='inlineHeader-companyName']",
            "div[data-company-name] a",
            ".jobsearch-CompanyInfoContainer a",
            ".jobsearch-JobInfoHeader-companyNameSimple",
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

        phone_number = self._extract_phone_from_body(soup)

        return JobPosting(
            company_name=company_name,
            address=address,
            phone_number=phone_number,
            industry=industry,
            job_url=url,
            scraped_at=datetime.now(JST).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _extract_phone_from_body(soup: BeautifulSoup) -> Optional[str]:
        """求人詳細ページの本文テキストから電話番号を regex 抽出。

        Indeed の構造化フィールドには電話番号無いが、求人本文中に
        「連絡先」「お問い合わせ」等の項で記載されるケースが多い。
        """
        candidates = [
            "#jobDescriptionText",
            "[data-testid='jobsearch-JobDescriptionText']",
            ".jobsearch-jobDescriptionText",
            ".jobsearch-JobComponent-description",
        ]
        for sel in candidates:
            element = soup.select_one(sel)
            if not element:
                continue
            text = element.get_text(separator="\n", strip=True)
            phone = extract_phone_number(text)
            if phone:
                return phone
        return extract_phone_number(soup.get_text(separator="\n", strip=True))

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
