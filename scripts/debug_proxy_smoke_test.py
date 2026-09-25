"""BrightData 汎用プロキシの生死切り分け用スモークテスト。

Indeed 以外のサイト (httpbin.org, example.com) と Indeed 自体への到達性を
同一プロキシ経由で比較し、プロキシ自体が死んでいるのか Indeed 側のブロックなのかを切り分ける。
検証用の使い捨てスクリプト。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from src.proxy_config import load_proxy_from_env

TARGETS = [
    ("httpbin", "https://httpbin.org/ip"),
    ("example", "https://example.com"),
    ("indeed", "https://jp.indeed.com/jobs?q=%E8%AD%A6%E5%82%99%E5%93%A1&l=%E6%9D%B1%E4%BA%AC"),
]


def main() -> None:
    proxy = load_proxy_from_env()
    print(f"proxy configured: {proxy is not None}")
    if not proxy:
        print("BRIGHTDATA_PROXY_URL が未設定です。中断します。")
        sys.exit(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(proxy=proxy)
        page = context.new_page()

        for name, url in TARGETS:
            start = time.time()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                elapsed = time.time() - start
                status = resp.status if resp else None
                print(f"[{name}] OK status={status} elapsed={elapsed:.1f}s")
                if name == "httpbin":
                    print(f"[{name}] body: {page.content()[:500]}")
            except Exception as e:
                elapsed = time.time() - start
                print(f"[{name}] FAILED elapsed={elapsed:.1f}s error={e}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
