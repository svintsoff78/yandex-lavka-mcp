"""Finish 3-D Secure in a headless browser, the way lavka.yandex.ru does.

After submit, Lavka's payment status is ``wait_user_action`` with a
``redirectUrl`` to ``trust.yandex.ru/web/open_3ds``. The site loads that URL in
a hidden iframe: the page runs the 3DS 2.0 "method" step (it sends browser data
to the card network and the issuing bank) and, for a saved card, the bank
approves without asking the user anything (frictionless). Seen in a HAR of a
real checkout, 2026-11-01. Plain HTTP can't do it — the page needs JavaScript —
so we open it in headless Chrome and let it run while the caller polls status.

Needs the optional ``playwright`` package (extra ``browser``). Without it, or
if no browser starts, ``open_3ds_page`` yields False and the caller falls back
to handing the user the redirect URL.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

log = logging.getLogger(__name__)

# Turn off with YANDEX_LAVKA_MCP_AUTO_3DS=0 (then the user opens redirect_url).
AUTO_3DS_ENV = "YANDEX_LAVKA_MCP_AUTO_3DS"


def auto_3ds_enabled() -> bool:
    return os.environ.get(AUTO_3DS_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _browser_cookies(cookies: Mapping[str, str]) -> list[dict[str, Any]]:
    # The trust page reads the Yandex session (yandex_login, yandexuid); give it
    # the same cookies a logged-in browser would send to *.yandex.ru.
    return [
        {"name": k, "value": v, "domain": ".yandex.ru", "path": "/", "secure": True}
        for k, v in cookies.items()
        if v
    ]


@contextlib.asynccontextmanager
async def open_3ds_page(
    url: str, cookies: Mapping[str, str], *, user_agent: str | None = None
) -> AsyncIterator[bool]:
    """Keep ``url`` open in headless Chrome for the duration of the block.

    Yields True if the page was opened, False if no browser is available. Never
    raises for browser problems: the order is already placed by now, and the
    fallback (user opens the link) must still work.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.info("playwright not installed; 3-D Secure left to the user")
        yield False
        return

    try:
        pw_cm = async_playwright()
        pw = await pw_cm.__aenter__()
    except Exception:  # noqa: BLE001
        log.warning("playwright failed to start", exc_info=True)
        yield False
        return

    browser = None
    try:
        # Installed Chrome first (no download needed), then Playwright's Chromium.
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = await pw.chromium.launch(headless=True, **kwargs)
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            log.warning("no headless browser could be launched")
            yield False
            return
        context = await browser.new_context(
            locale="ru-RU", **({"user_agent": user_agent} if user_agent else {})
        )
        await context.add_cookies(_browser_cookies(cookies))
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception:  # noqa: BLE001
            # A slow or never-ending load is normal here (the page waits for a
            # parent frame that doesn't exist); its scripts are already running.
            log.info("3DS page load did not settle; keeping it open", exc_info=True)
        yield True
    finally:
        with contextlib.suppress(Exception):
            if browser is not None:
                await browser.close()
        with contextlib.suppress(Exception):
            await pw_cm.__aexit__(None, None, None)
