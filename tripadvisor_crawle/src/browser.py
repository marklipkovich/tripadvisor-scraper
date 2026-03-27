"""
Browser infrastructure: Camoufox plugin, proxy timezone probe, and viewport constants.

CamoufoxPlugin wraps Crawlee's PlaywrightBrowserPlugin to launch stealth Firefox
(Camoufox) instead of standard Playwright Firefox.  The proxy exit IP is probed
via httpx before launching so Camoufox's geoip= parameter receives the actual
IP string, which it looks up in the local MaxMind database to auto-configure
timezone, geolocation, and related fingerprint fields.
"""

from __future__ import annotations

import os as _os
import random
from typing import Any

import httpx  # type: ignore[import-untyped]

from apify import Actor
from camoufox import AsyncNewBrowser
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

# Maps Apify proxy country codes to plausible IANA timezones (for log display).
COUNTRY_TIMEZONES: dict[str, list[str]] = {
    "US": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
    "GB": ["Europe/London"],
    "CA": ["America/Toronto", "America/Vancouver", "America/Edmonton"],
    "AU": ["Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane"],
    "DE": ["Europe/Berlin"],
    "FR": ["Europe/Paris"],
    "NL": ["Europe/Amsterdam"],
    "IE": ["Europe/Dublin"],
    "IT": ["Europe/Rome"],
    "ES": ["Europe/Madrid"],
    "PL": ["Europe/Warsaw"],
    "SE": ["Europe/Stockholm"],
    "IN": ["Asia/Kolkata"],
    "JP": ["Asia/Tokyo"],
    "SG": ["Asia/Singapore"],
    "BR": ["America/Sao_Paulo", "America/Manaus"],
}

_DEFAULT_TIMEZONE = "Europe/London"


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY TIMEZONE PROBE
# ══════════════════════════════════════════════════════════════════════════════

async def _probe_proxy_timezone(proxy_url: str) -> tuple[str, str, str, str]:
    """
    Detect the proxy's exit IP and return (IANA_timezone, source_label, exit_ip, country).

    source_label is a short string for log display, e.g.:
        "ipinfo: 82.45.x.x/GB"
        "ip-api: 1.2.3.4/US"

    Tries two services in order so that a block on one doesn't fail the run:
      1. ipinfo.io  — HTTPS, proxy-friendly, free 50 k req/month
      2. ip-api.com — HTTP fallback, very permissive, widely reachable

    Falls back to _DEFAULT_TIMEZONE on any error.
    """
    _ENDPOINTS = [
        # (url, service_name, ip_key, country_key, tz_key)
        ("https://ipinfo.io/json",                                    "ipinfo",  "ip",    "country",     "timezone"),
        ("http://ip-api.com/json/?fields=query,countryCode,timezone", "ip-api",  "query", "countryCode", "timezone"),
    ]
    last_exc: Exception | None = None
    for url, svc, ip_key, country_key, tz_key in _ENDPOINTS:
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=15.0) as client:
                resp = await client.get(url)
                data = resp.json()
            exit_ip  = data.get(ip_key,      "?")
            country  = data.get(country_key, "?")
            timezone = data.get(tz_key)      or _DEFAULT_TIMEZONE
            source   = f"{svc}: {exit_ip}/{country}"
            Actor.log.info(f"  Proxy exit IP: {exit_ip} | country={country} | timezone={timezone} ({svc})")
            return timezone, source, exit_ip, country
        except Exception as exc:
            last_exc = exc
            Actor.log.debug(f"  Timezone probe via {url} failed: {exc} — trying next …")

    Actor.log.warning(
        f"  All proxy IP probes failed ({last_exc}) — "
        f"falling back to default timezone: {_DEFAULT_TIMEZONE}"
    )
    return _DEFAULT_TIMEZONE, "probe-failed: default", "?", "?"


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
    returns the current session's proxy URL.  When provided:
      • The URL is passed to Camoufox as geoip=<url> so the browser's timezone,
        geolocation, and related fingerprint fields are automatically set to
        match the proxy's exit IP — eliminating the mismatch that DataDome and
        similar bot-protection systems detect.
      • A lightweight IP-info probe (_probe_proxy_timezone) is run concurrently
        with the browser launch so we get the "Proxy exit IP" log line without
        adding extra round-trip latency.
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
            # Probe the proxy exit IP first — Camoufox's geoip parameter expects a
            # plain IP address string (e.g. "177.97.200.207"), NOT a proxy URL.
            # We need the exit IP before we can set geoip=, so probing is sequential.
            probe_tz, probe_src, probe_ip, probe_country = (
                await _probe_proxy_timezone(proxy_url)
            )
            if probe_ip != "?":
                # Pass the actual exit IP so Camoufox looks it up in the local
                # MaxMind database and auto-configures timezone, geolocation, etc.
                launch_options["geoip"] = probe_ip

            try:
                browser = await AsyncNewBrowser(self._playwright, **launch_options)
            except Exception as exc:
                exc_name = type(exc).__name__
                if exc_name in ("NotInstalledGeoIPExtra", "InvalidIP"):
                    Actor.log.warning(
                        f"  camoufox geoip unavailable ({exc_name}) — "
                        "launching without geoip (timezone will not match proxy)."
                    )
                    launch_options.pop("geoip", None)
                    browser = await AsyncNewBrowser(self._playwright, **launch_options)
                else:
                    raise
        else:
            probe_tz, probe_src, probe_ip, probe_country = (
                _DEFAULT_TIMEZONE, "no-proxy", "?", "?"
            )
            browser = await AsyncNewBrowser(self._playwright, **launch_options)

        # Cache probe results so handle_place can log them without a second call.
        self._browser_state.update(
            vp=vp, needs_log=True,
            session_tz=probe_tz, session_src=probe_src,
            exit_ip=probe_ip, exit_country=probe_country,
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
