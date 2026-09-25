"""BrightData 汎用プロキシ経由で Indeed への 403 再現性を確認するスクリプト。

新しい context (= 新しい IP ローテーション) を毎回作り、複数回 jp.indeed.com に
アクセスして 403 が継続的に出るか、IP によって結果が変わるかを見る。検証用の使い捨て。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from src.proxy_config import load_proxy_from_env

INDEED_URL = "https://jp.indeed.com/jobs?q=%E8%AD%A6%E5%82%99%E5%93%A1&l=%E6%9D%B1%E4%BA%AC"
ATTEMPTS = 5


def main() -> None:
    proxy = load_proxy_from_env()
    print(f"proxy configured: {proxy is not None}")
    if not proxy:
        print("BRIGHTDATA_PROXY_URL が未設定です。中断します。")
        sys.exit(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i in range(1, ATTEMPTS + 1):
            context = browser.new_context(proxy=proxy)
            page = context.new_page()

            try:
                ip_resp = page.goto("https://httpbin.org/ip", timeout=30_000)
                ip_body = page.content()
            except Exception as e:
                ip_body = f"IP取得失敗: {e}"

            start = time.time()
            try:
                resp = page.goto(INDEED_URL, wait_until="domcontentloaded", timeout=30_000)
                elapsed = time.time() - start
                status = resp.status if resp else None
                print(f"[attempt {i}] indeed status={status} elapsed={elapsed:.1f}s ip_info={ip_body[:200]}")
            except Exception as e:
                elapsed = time.time() - start
                print(f"[attempt {i}] indeed FAILED elapsed={elapsed:.1f}s error={e} ip_info={ip_body[:200]}")

            context.close()

        browser.close()


if __name__ == "__main__":
    main()
