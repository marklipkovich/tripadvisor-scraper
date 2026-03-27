"""
Browser infrastructure: Camoufox plugin, proxy utilities, and viewport constants.

CamoufoxPlugin wraps Crawlee's PlaywrightBrowserPlugin to launch stealth Firefox
(Camoufox) instead of standard Playwright Firefox. Proxy exit identity is fetched
once per browser session via Playwright's APIRequestContext.
"""

from __future__ import annotations

import os as _os
import random
from typing import Any
from urllib.parse import unquote, urlparse

from apify import Actor
from camoufox import AsyncNewBrowser
from camoufox.exceptions import InvalidIP, InvalidProxy, NotInstalledGeoIPExtra
from crawlee.browsers import BrowserPool, PlaywrightBrowserController, PlaywrightBrowserPlugin
from playwright.async_api import Page
from typing_extensions import override


class CaptchaBlockedError(Exception):
    """Raised when DataDome captcha cannot be bypassed with the current proxy."""


# ══════════════════════════════════════════════════════════════════════════════
#  VIEWPORT / TIMEZONE CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]

_DEFAULT_TIMEZONE = "Europe/London"


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _apify_proxy_url_to_playwright_proxy(proxy_url: str) -> dict[str, str]:
    """Parse Apify/Crawlee proxy URL into Playwright/Camoufox `proxy` dict."""
    p = urlparse(proxy_url)
    if not p.scheme or not p.hostname:
        raise ValueError(f"Invalid proxy URL: {proxy_url!r}")
    port = p.port or (443 if p.scheme == "https" else 80)
    server = f"{p.scheme}://{p.hostname}:{port}"
    out: dict[str, str] = {"server": server}
    if p.username is not None:
        out["username"] = unquote(p.username)
    if p.password is not None:
        out["password"] = unquote(p.password)
    return out


async def _fetch_proxy_exit_identity_via_playwright(
    page: Page,
) -> tuple[str, str, str, str] | None:
    """Return (timezone, source_label, exit_ip, country) using the context proxy.

    Uses Playwright's APIRequestContext so traffic matches the browser's proxy
    tunnel (same exit IP Camoufox targets with geoip=True).
    """
    request_ctx = page.context.request
    endpoints: list[tuple[str, str, str, str, str]] = [
        ("https://ipinfo.io/json", "ipinfo", "ip", "country", "timezone"),
        (
            "http://ip-api.com/json/?fields=query,countryCode,timezone",
            "ip-api",
            "query",
            "countryCode",
            "timezone",
        ),
    ]
    last_exc: BaseException | None = None
    for url, svc, ip_key, country_key, tz_key in endpoints:
        try:
            resp = await request_ctx.get(url, timeout=15_000)
            if resp.status >= 400:
                continue
            data = await resp.json()
            exit_ip = data.get(ip_key, "?")
            country = data.get(country_key, "?")
            timezone = data.get(tz_key) or _DEFAULT_TIMEZONE
            source = f"{svc}: {exit_ip}/{country}"
            Actor.log.info(
                f"  Proxy exit IP: {exit_ip} | country={country} | "
                f"timezone={timezone} ({svc})"
            )
            return timezone, source, exit_ip, country
        except BaseException as exc:
            last_exc = exc
            Actor.log.debug(f"  Proxy identity via {url} failed: {exc} — trying next …")

    Actor.log.warning(
        f"  Could not fetch proxy exit identity via Playwright ({last_exc}) — "
        f"using default timezone label {_DEFAULT_TIMEZONE}"
    )
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  CAMOUFOX BROWSER PLUGIN
# ══════════════════════════════════════════════════════════════════════════════

class CamoufoxPlugin(PlaywrightBrowserPlugin):
    """
    Crawlee BrowserPlugin that launches Camoufox (stealth Firefox) instead of
    standard Playwright Firefox.  All other PlaywrightBrowserPlugin behaviour
    (context creation, proxy injection, page lifecycle) is inherited unchanged.

    max_open_pages_per_browser is intentionally left at the Crawlee default (20).
    With max_concurrency=1, at most 1 page is ever open — so the same browser
    instance (and its shared Playwright context) is reused for every place,
    giving DataDome cookies + session state continuity across requests.

    browser_state is a mutable dict shared with handle_place so the handler
    knows when a new browser has been launched and can log the session details.
    Keys: needs_log (bool), vp (dict), session_tz, session_src, exit_ip, exit_country.

    proxy_url_getter is an optional async callable (() -> str | None) that
    returns the current session's proxy URL.  When provided, Camoufox is
    launched with that proxy plus geoip=True so fingerprint/geo match the
    exit IP.  Detailed IP/country/timezone logs are produced in handle_place
    via Playwright's APIRequestContext (same proxy as the page).
    """

    def __init__(
        self, *, browser_state: dict, proxy_url_getter: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._browser_state = browser_state
        self._proxy_url_getter = proxy_url_getter

    @override
    async def new_browser(self) -> PlaywrightBrowserController:
        if not self._playwright:
            raise RuntimeError("Playwright browser plugin is not initialized.")
        vp = random.choice(VIEWPORTS)
        is_headless = _os.environ.get("APIFY_IS_AT_HOME") == "1"

        proxy_url: str | None = None
        if self._proxy_url_getter is not None:
            proxy_url = await self._proxy_url_getter()

        launch_options: dict = {
            "os": "windows",
            "block_webrtc": True,
            "locale": "en-US",
            **self._browser_launch_options,
        }
        launch_options["headless"] = is_headless
        if proxy_url:
            try:
                launch_options["proxy"] = _apify_proxy_url_to_playwright_proxy(proxy_url)
            except ValueError as exc:
                Actor.log.warning(f"  Invalid proxy URL for Camoufox launch: {exc}")
            else:
                launch_options["geoip"] = True

            try:
                browser = await AsyncNewBrowser(self._playwright, **launch_options)
            except (NotInstalledGeoIPExtra, InvalidIP, InvalidProxy) as exc:
                Actor.log.warning(
                    f"  camoufox geoip/proxy setup failed ({type(exc).__name__}: {exc}) — "
                    "retrying launch without browser-level proxy/geoip "
                    "(Crawlee still applies proxy on the context)."
                )
                launch_options.pop("geoip", None)
                launch_options.pop("proxy", None)
                browser = await AsyncNewBrowser(self._playwright, **launch_options)
        else:
            browser = await AsyncNewBrowser(self._playwright, **launch_options)

        # handle_place fills session_tz / exit_* via Playwright after the page exists.
        self._browser_state.update(
            vp=vp,
            needs_log=True,
            session_tz=_DEFAULT_TIMEZONE,
            session_src="pending",
            exit_ip="?",
            exit_country="?",
        )
        return PlaywrightBrowserController(
            browser=browser,
            # Camoufox generates its own headers — disable Crawlee's generator.
            header_generator=None,
        )


def _find_browser_controller(
    page: Page,
    browser_pool: BrowserPool,
) -> PlaywrightBrowserController | None:
    """Return the BrowserController in the pool that owns *page*, or None.

    PlaywrightCrawlingContext has no .browser_controller attribute, so we match
    by comparing the Playwright Browser object behind each active controller to
    the browser that hosts the current page's context.
    """
    pw_browser = page.context.browser
    for controller in list(browser_pool._active_browsers):
        if (
            isinstance(controller, PlaywrightBrowserController)
            and controller._browser is pw_browser
        ):
            return controller
    return None
