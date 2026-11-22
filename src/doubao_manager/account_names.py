"""Read the signed-in display name without changing the Doubao page."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse


READ_DOUBAO_NAME = r"""() => {
    const account = window._ROUTER_DATA?.loaderData?.chat_layout?.chat_layout?.accountInfo?.data;
    if (account?.is_visitor_account === true) return '';
    const avatar = document.querySelector('img[data-testid="chat_header_avatar_button"]');
    const name = avatar?.parentElement?.querySelector('div.flex-col > div > span')?.textContent;
    return typeof name === 'string' ? name.trim().replace(/[\r\n\t]+/g, ' ').slice(0, 80) : '';
}"""


async def read_doubao_account_name(page) -> str:
    """Use the same header field as Qianhuan; ignore other sites and stale reads."""
    try:
        url = page.url
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ("www.doubao.com", "doubao.com"):
            return ""
        name = await asyncio.wait_for(page.evaluate(READ_DOUBAO_NAME), timeout=2)
        if page.url != url or not isinstance(name, str):
            return ""
        return name.strip()[:80]
    except Exception:
        # Login, navigation and closing the window must not interrupt account use.
        return ""
