"""Proxy 設定の env → Playwright 辞書変換。

対応する環境変数:
- BRIGHTDATA_PROXY_URL: "http://user:pass@host:port" 形式のフル URL
  例: "http://brd-customer-hl_xxx-zone-indeed:xxxx@brd.superproxy.io:22225"

将来的に他プロバイダも追加するなら PROXY_URL 汎用名を追加する。
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def load_proxy_from_env() -> Optional[dict]:
    """環境変数から Playwright 用の proxy 辞書を作る。無ければ None。"""
    raw = os.environ.get("BRIGHTDATA_PROXY_URL") or os.environ.get("PROXY_URL")
    if not raw:
        return None

    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        logger.warning(f"proxy URL パース失敗 (host/port 欠損): {raw}")
        return None

    proxy = {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password

    logger.info(
        f"proxy 有効化: {parsed.scheme}://{parsed.hostname}:{parsed.port} "
        f"(user={parsed.username or '(none)'})"
    )
    return proxy
