"""ハローワークインターネットサービス スクレイパー。

厚労省運営の公的サービス。求人詳細ページに以下が構造化フィールドとして掲載:
- 事業所名 / 所在地 / 電話番号 / 代表者名 / 産業分類 / 法人番号 等

Indeed と違い電話番号・代表者名がほぼ全件明示的に記載されている。
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .models import JobPosting

logger = logging.getLogger(__name__)

BASE_URL = "https://www.hellowork.mhlw.go.jp/kensaku/"
SEARCH_ENTRY_URL = f"{BASE_URL}GECA110010.do?action=initDisp&screenId=GECA110010"
JST = timezone(timedelta(hours=9))

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class HelloWorkScrapingError(Exception):
    """ハローワークスクレイピング処理中に発生したエラー。"""


class HelloWorkScraper:
    """ハローワークインターネットサービスのスクレイパー。

    Args:
        request_delay_seconds: リクエスト間の待機秒数（公的サイトなので礼儀として長め）
        headless: True でヘッドレスモード
        user_agent: リクエスト用 User-Agent
        page_load_timeout_ms: ページロードのタイムアウト（ミリ秒）

    Usage:
        with HelloWorkScraper() as scraper:
            for posting in scraper.search(keyword="エンジニア", max_pages=1):
                print(posting)
    """

    def __init__(
        self,
        request_delay_seconds: float = 3.0,
        headless: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
        page_load_timeout_ms: int = 45_000,
    ) -> None:
        self.request_delay_seconds = request_delay_seconds
        self.headless = headless
        self.user_agent = user_agent
        self.page_load_timeout_ms = page_load_timeout_ms
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context = None

    def __enter__(self) -> "HelloWorkScraper":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="ja-JP",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def search(
        self,
        keyword: str,
        location: str = "",
        max_pages: int = 1,
    ) -> Iterator[JobPosting]:
        """検索結果から詳細ページを巡回して求人を yield する。

        NOTE: ハローワークのセッションは jGSHNo パラメータで結び付けられているため、
        検索と詳細取得は同一 context 内で完結させる必要がある。

        Args:
            keyword: フリーワード検索キーワード
            location: (現状未使用 / 都道府県絞り込みは modal 操作要のため保留)
            max_pages: 取得するページ数（1 ページ 30 件）
        """
        if location:
            logger.warning("ハローワークの location 絞り込みは未実装のため無視します。")

        result_page = self._context.new_page()
        detail_page = self._context.new_page()
        try:
            yield from self._crawl(result_page, detail_page, keyword, max_pages)
        finally:
            detail_page.close()
            result_page.close()

    def _crawl(
        self,
        result_page: Page,
        detail_page: Page,
        keyword: str,
        max_pages: int,
    ) -> Iterator[JobPosting]:
        """result_page は検索結果を保持し pagination で更新。detail_page は詳細取得用に使い回す。"""
        logger.info("検索ページを開く")
        result_page.goto(
            SEARCH_ENTRY_URL, wait_until="domcontentloaded", timeout=self.page_load_timeout_ms
        )
        result_page.fill("#ID_freeWordInput", keyword)

        logger.info(f"検索実行: keyword='{keyword}'")
        with result_page.expect_navigation(
            wait_until="domcontentloaded", timeout=self.page_load_timeout_ms
        ):
            result_page.locator("input[value='検索'], button:has-text('検索')").first.click()

        for page_index in range(max_pages):
            logger.info(f"結果ページ {page_index + 1}/{max_pages} 処理")
            detail_urls = self._extract_detail_urls(result_page.content())
            logger.info(f"  詳細 URL 数: {len(detail_urls)}")

            if not detail_urls:
                logger.info("求人が見つかりませんでした。")
                break

            for url in detail_urls:
                self._sleep()
                posting = self._fetch_and_parse_detail(detail_page, url)
                if posting:
                    yield posting

            if page_index + 1 < max_pages:
                if not self._go_next_page(result_page):
                    logger.info("次のページ無し。終了。")
                    break

    @staticmethod
    def _extract_detail_urls(html: str) -> list[str]:
        """検索結果ページ HTML から詳細ページ URL を抽出。"""
        hrefs = re.findall(r'id="ID_dispDetailBtn"[^>]*href="([^"]+)"', html)
        return [urljoin(BASE_URL, href.replace("&amp;", "&")) for href in hrefs]

    def _fetch_and_parse_detail(self, page: Page, url: str) -> Optional[JobPosting]:
        """詳細ページに遷移してパース。エラー時は None。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.page_load_timeout_ms)
        except PlaywrightTimeoutError:
            logger.warning(f"詳細ページタイムアウト: {url}")
            return None

        try:
            posting = self._parse_detail(page.content(), url)
        except Exception as e:
            logger.warning(f"詳細パース失敗 url={url}: {e}")
            return None

        # 検索結果ページに戻る (次の URL に遷移するには戻る必要無いが、
        # jGSHNo セッションが有効なら直接 URL 遷移で OK)
        return posting

    def _parse_detail(self, html: str, url: str) -> Optional[JobPosting]:
        """詳細ページ HTML をパース。

        ハローワークの詳細ページはユニーク ID が付与されており、
        テーブルレイアウトのラベル/値マッチではなく直接 ID selector で拾える。
        """
        soup = BeautifulSoup(html, "lxml")

        company_name = self._select_text(soup, ["#ID_jgshMei"])
        if not company_name:
            logger.debug(f"会社名抽出失敗: {url}")
            return None

        address_parts = [
            self._select_text(soup, ["#ID_szciYbn"]),
            self._select_text(soup, ["#ID_szci"]),
        ]
        address = " ".join(p for p in address_parts if p) or None

        phone_number = self._select_text(soup, ["#ID_ttsTel"])
        industry = self._select_text(soup, ["#ID_sngBrui"])
        representative_name = self._select_text(soup, ["#ID_dhshaMei"])

        return JobPosting(
            company_name=company_name,
            address=address,
            phone_number=phone_number,
            industry=industry,
            representative_name=representative_name,
            job_url=url,
            scraped_at=datetime.now(JST).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _select_text(soup: BeautifulSoup, selectors: list[str]) -> Optional[str]:
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text:
                    return text
        return None

    def _go_next_page(self, page: Page) -> bool:
        """検索結果の次ページに遷移。存在しなければ False。

        ハローワークは name="fwListNaviBtnNext" の submit ボタン。
        検索ボタン同様、hidden 状態の要素が混在するため JS 経由で click。
        """
        try:
            with page.expect_navigation(
                wait_until="domcontentloaded", timeout=self.page_load_timeout_ms
            ):
                clicked = page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll("input[name='fwListNaviBtnNext']"));
                    // 可視 (親要素あり) のものを優先、なければ最初の 1 個
                    const visible = btns.find(b => b.offsetParent !== null) || btns[0];
                    if (!visible) return false;
                    visible.click();
                    return true;
                }""")
                if not clicked:
                    return False
        except PlaywrightTimeoutError:
            logger.warning("次ページ遷移タイムアウト")
            return False
        except Exception as e:
            logger.warning(f"次ページ遷移失敗: {e}")
            return False
        return True

    def _sleep(self) -> None:
        jitter = random.uniform(0.5, 1.5)
        time.sleep(self.request_delay_seconds + jitter)
