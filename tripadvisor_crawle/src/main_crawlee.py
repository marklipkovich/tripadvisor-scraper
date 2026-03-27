"""
TripAdvisor Reviews Scraper — Crawlee Edition
═════════════════════════════════════════════════════════════════════════════

Architecture: PlaywrightCrawler + CamoufoxPlugin (shared browser+context).

  What Crawlee provides:
    • Browser lifecycle management (launch / close via BrowserPool)
    • Request queue with deduplication
    • Sequential processing (max_concurrency=1)
    • Automatic URL-level retries (max_request_retries)
    • Proxy injection at context creation time via CrawleeProxyConfiguration

  Shared browser behaviour:
    With max_concurrency=1 only one page is open at a time.  Crawlee's
    BrowserPool reuses the same Camoufox instance (and its Playwright
    context) for all places, so DataDome cookies earned on place 1 carry
    forward to place 2, 3 … making each successive place less likely to
    trigger a challenge.

  When a place is blocked (CaptchaBlockedError):
    1. The handler closes the current browser via browser_controller.close().
    2. The exception is re-raised so Crawlee schedules a retry.
    3. BrowserPool detects the closed browser and calls CamoufoxPlugin.new_browser()
       to launch a fresh Camoufox instance.
    4. CrawleeProxyConfiguration supplies a new session ID (derived from
       retry_count) so the fresh context gets a different residential IP.

  What we still manage manually (unchanged from main.py):
    • Captcha detection & 15-second polling window
    • GraphQL parallel fetches (40 × asyncio.gather)
    • Review parsing & dataset push batching
    • All data extraction logic

Module layout
─────────────
  browser.py   — CamoufoxPlugin, proxy utilities, VIEWPORTS
  parsers.py   — JSON-LD / GraphQL parsing, EXTRACT_PAGE_SCRIPT
  graphql.py   — fetch_reviews_via_graphql, pagination constants
  utils.py     — with_retry, URL helpers, _normalize_place, _build_places_md
  scraper.py   — scrape_place (core per-place logic)
  main_crawlee.py (this file) — Actor entry point, Crawlee wiring
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import Any, Optional

from apify import Actor
from crawlee import ConcurrencySettings, Request
from crawlee.browsers import BrowserPool
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.events import Event
from crawlee.proxy_configuration import ProxyConfiguration as CrawleeProxyConfiguration

from .browser import (
    _DEFAULT_TIMEZONE,
    _fetch_proxy_exit_identity_via_playwright,
    _find_browser_controller,
    CamoufoxPlugin,
    CaptchaBlockedError,
    VIEWPORTS,
)
from .scraper import scrape_place
from .utils import _build_places_md, normalize_place_url


# ══════════════════════════════════════════════════════════════════════════════
#  ACTOR ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    async with Actor:
        async def on_aborting() -> None:
            await asyncio.sleep(1)
            await Actor.exit()

        Actor.on(Event.ABORTING, on_aborting)

        actor_input = await Actor.get_input() or {}

        raw_urls        = actor_input.get("startUrls") or actor_input.get("start_urls") or []
        max_reviews     = actor_input.get("maxReviewsPerPlace")
        start_date: str = (actor_input.get("startDate") or "").strip()[:10]
        end_date: str   = (actor_input.get("endDate") or "").strip()[:10]
        rating_filters  = [str(r) for r in (actor_input.get("reviewRatings") or [])]
        language_filter = (actor_input.get("language") or "").strip()
        proxy_input     = actor_input.get("proxyConfiguration")

        # ── Input validation ─────────────────────────────────────────────────
        if not raw_urls:
            fail_msg = "Input field 'startUrls' is empty — add at least one TripAdvisor place URL."
            Actor.log.error(fail_msg)
            await Actor.fail(status_message=fail_msg)
            return

        invalid_urls = []
        for entry in raw_urls:
            url = entry.get("url") if isinstance(entry, dict) else entry
            if url and not normalize_place_url(str(url)):
                invalid_urls.append(str(url))
        if invalid_urls:
            fail_msg = (
                "Invalid URL(s) detected — all Place URLs must be TripAdvisor place pages. "
                f"Invalid: {', '.join(invalid_urls[:3])}"
                + (" …" if len(invalid_urls) > 3 else "")
            )
            Actor.log.error(fail_msg)
            await Actor.fail(status_message=fail_msg)
            return

        if start_date and end_date and start_date > end_date:
            fail_msg = (
                f"Start Date ({start_date}) must be on or before End Date ({end_date}). "
                "Please correct the date range in the input."
            )
            Actor.log.error(fail_msg)
            await Actor.fail(status_message=fail_msg)
            return

        # ── Logging ──────────────────────────────────────────────────────────
        Actor.log.info(f"Places to scrape: {len(raw_urls)}")
        Actor.log.info(f"Max reviews/place: {max_reviews or 'unlimited'}")
        if start_date:
            Actor.log.info(f"Start date filter: {start_date}")
        if end_date:
            Actor.log.info(f"End date filter: {end_date}")
        if rating_filters:
            Actor.log.info(f"Rating filter: {', '.join(rating_filters)} stars")
        if language_filter:
            Actor.log.info(f"Language filter: {language_filter}")

        # ── Proxy setup ───────────────────────────────────────────────────────
        # We create Crawlee's own ProxyConfiguration backed by Apify's proxy resolver.
        # Crawlee passes the proxy URL to each browser context at creation time, which
        # Firefox/Camoufox supports via Playwright's browser.new_context(proxy=...).
        apify_proxy_config = None
        crawlee_proxy_config = None
        # Async callable (() -> str | None) passed to CamoufoxPlugin for geoip.
        # Set below once we know a valid proxy configuration exists.
        _proxy_url_for_geoip: Any = None
        proxy_groups = (proxy_input or {}).get("apifyProxyGroups") or []
        is_residential = any("RESIDENTIAL" in (g or "").upper() for g in proxy_groups)
        proxy_country = ((proxy_input or {}).get("apifyProxyCountry") or "").strip().upper()

        # _session_rotation[0] is the global rotation counter (incremented on each
        # browser rotation after a block).  Both _get_proxy_url and CamoufoxPlugin
        # read this so they always use the same session ID.
        _session_rotation = [0]

        if proxy_input:
            apify_proxy_config = await Actor.create_proxy_configuration(
                actor_proxy_input=proxy_input
            )
            if apify_proxy_config is not None:
                Actor.log.info("Proxy configuration loaded.")

                _apify_proxy = apify_proxy_config  # capture non-None ref for closure

                async def _get_proxy_url(session_id: Optional[str] = None, request: Any = None) -> Optional[str]:
                    # Use the global rotation counter so all requests on the same
                    # browser share one session ID; it advances only on block/rotation.
                    sid = f"run_s{_session_rotation[0] + 1}"
                    info = await _apify_proxy.new_proxy_info(session_id=sid)
                    if info:
                        return f"{info.scheme}://{info.username}:{info.password}@{info.hostname}:{info.port}"
                    return None

                # Share the same getter with CamoufoxPlugin so geoip probes through
                # the correct residential session (same URL Crawlee uses for contexts).
                _proxy_url_for_geoip = _get_proxy_url

                crawlee_proxy_config = CrawleeProxyConfiguration(new_url_function=_get_proxy_url)
            else:
                Actor.log.warning(
                    "Proxy configuration could not be initialised (no valid credentials locally). "
                    "Proxy will not be used — this will be blocked on Apify Cloud without Residential Proxy."
                )
        else:
            Actor.log.warning(
                "No proxy configured. On Apify Cloud, datacenter IPs are blocked by DataDome "
                "regardless of browser fingerprint — enable Residential Proxy in the input."
            )

        if apify_proxy_config is not None:
            Actor.log.info(
                "Browser geoip: Camoufox geoip=True (browser-level proxy) matches "
                "timezone/geolocation to the residential exit IP; "
                "logs use Playwright requests through the same proxy."
            )
        else:
            Actor.log.info(f"No proxy — browser timezone: {_DEFAULT_TIMEZONE} (default)")

        # Without residential proxy datacenter IPs are always blocked by DataDome —
        # no point retrying with the same IP, fail immediately after first attempt.
        max_retries = 3 if is_residential else 0
        total_attempts = max_retries + 1  # residential: 4 | no proxy: 1

        await Actor.set_status_message(f"Starting — {len(raw_urls)} place(s) to process …")

        # ── Normalise place URLs ──────────────────────────────────────────────
        place_urls: list[str] = []
        for entry in raw_urls:
            url = entry.get("url") if isinstance(entry, dict) else entry
            if url and str(url).strip():
                norm = normalize_place_url(str(url))
                if norm:
                    place_urls.append(norm)

        all_places: list[dict] = []
        total_reviews_counter = [0]  # list so nested function can mutate
        seq_counter = [0]            # increments once per unique URL (retries reuse same number)
        url_seq: dict[str, int] = {} # url → sequential processing-order number

        # Shared state written by CamoufoxPlugin.new_browser() and handle_place.
        # session_* / exit_* come from Playwright APIRequestContext (ipinfo/ip-api)
        # once per browser session, then re-logged from cache for later places.
        browser_state: dict = {
            "needs_log": False,
            "vp": VIEWPORTS[0],
            "session_tz": _DEFAULT_TIMEZONE,
            "session_src": "startup",
            "session_id": "run_s1",
            "exit_ip": "?",
            "exit_country": "?",
        }

        # ── Crawlee PlaywrightCrawler with CamoufoxPlugin ─────────────────────
        # proxy_configuration is Crawlee's own ProxyConfiguration backed by Apify's
        # resolver. Crawlee calls new_url_function() per-request and injects the proxy
        # URL at browser.new_context() time — Firefox/Camoufox supports this fully.
        # With max_concurrency=1 (sequential), a single Camoufox browser+context is
        # reused for all places: DataDome cookies and session state accumulate across
        # requests, reducing the chance of a block on each successive place.
        #
        # IMPORTANT: browser_inactive_threshold and identify_inactive_browsers_interval
        # are set large (30 min) to prevent BrowserPool from moving the active browser
        # to the inactive list mid-scrape.  Crawlee measures idle_time from when the
        # page was *opened* (not closed), so a 45-second scrape exceeds the 10-second
        # default threshold and causes a new browser to be launched for every place.
        browser_pool = BrowserPool(
            plugins=[CamoufoxPlugin(
                browser_state=browser_state,
                proxy_url_getter=_proxy_url_for_geoip,
            )],
            browser_inactive_threshold=timedelta(minutes=30),
            identify_inactive_browsers_interval=timedelta(minutes=30),
        )
        crawler = PlaywrightCrawler(
            browser_pool=browser_pool,
            proxy_configuration=crawlee_proxy_config,
            max_request_retries=max_retries,
            concurrency_settings=ConcurrencySettings(max_concurrency=1, desired_concurrency=1),
            # Don't let Crawlee intercept bot-protection responses — we handle it ourselves.
            retry_on_blocked=False,
            configure_logging=False,
            # Ignore HTTP error codes so Camoufox can handle DataDome / captcha responses.
            ignore_http_error_status_codes=[403, 429, 503],
            request_handler_timeout=timedelta(seconds=1200),
            # Residential proxy adds latency; 120 s prevents wasting a retry on slow first load.
            navigation_timeout=timedelta(seconds=120),
        )

        @crawler.router.default_handler
        async def handle_place(context: PlaywrightCrawlingContext) -> None:
            place_url   = context.request.url
            retry_count = context.request.retry_count  # 0 on first attempt
            attempt     = retry_count + 1

            if place_url not in url_seq:
                seq_counter[0] += 1
                url_seq[place_url] = seq_counter[0]
            place_seq    = url_seq[place_url]
            total_places = len(place_urls)

            # ── Session log (emitted for every place and every retry) ───────
            session_id = f"run_s{_session_rotation[0] + 1}"
            vp = browser_state["vp"]

            if crawlee_proxy_config is not None and (
                browser_state["needs_log"] or browser_state["exit_ip"] == "?"
            ):
                fetched = await _fetch_proxy_exit_identity_via_playwright(context.page)
                if fetched:
                    tz, src, ip, cty = fetched
                    browser_state.update(
                        session_tz=tz,
                        session_src=src,
                        exit_ip=ip,
                        exit_country=cty,
                    )

            session_tz = browser_state["session_tz"]
            session_src = browser_state["session_src"]
            exit_ip = browser_state["exit_ip"]
            exit_country = browser_state["exit_country"]

            if browser_state["needs_log"]:
                browser_state.update(needs_log=False, session_id=session_id)
                Actor.log.info(
                    f"  Launching browser: os=windows | tz={session_tz} [geoip+{session_src}] | "
                    f"{vp['width']}×{vp['height']} | session={session_id}"
                )
            else:
                session_id = browser_state["session_id"]
                if exit_ip != "?":
                    svc = session_src.split(":")[0] if ":" in session_src else session_src
                    Actor.log.info(
                        f"  Proxy exit IP: {exit_ip} | country={exit_country} | "
                        f"timezone={session_tz} ({svc})"
                    )
                Actor.log.info(
                    f"  Browser session: os=windows | tz={session_tz} [geoip+{session_src}] | "
                    f"{vp['width']}×{vp['height']} | session={session_id}"
                )

            proxy_groups_str = ", ".join(proxy_groups) if proxy_groups else "NONE"
            attempt_str = (
                f" (attempt {attempt}/{total_attempts})" if total_attempts > 1 else ""
            )

            Actor.log.info(f"[{place_seq}/{total_places}] Processing: {place_url[:70]}...")
            Actor.log.info(f"  proxy={proxy_groups_str}{attempt_str}")

            if retry_count > 0:
                # Exponential backoff: 3 s, 6 s, 12 s … with ±1 s jitter
                backoff = 3.0 * (2 ** (retry_count - 1)) + random.uniform(0.5, 1.5)
                Actor.log.info(
                    f"  Backoff {backoff:.1f}s before retry attempt {attempt}/{total_attempts} …"
                )
                await asyncio.sleep(backoff)
                await Actor.set_status_message(
                    f"Place {place_seq}/{total_places} — Retrying (attempt {attempt}/{total_attempts}) …"
                )
            else:
                await Actor.set_status_message(f"Place {place_seq}/{total_places} — Loading …")

            try:
                place_obj, pushed = await scrape_place(
                    context.page, place_url, max_reviews,
                    has_proxy=crawlee_proxy_config is not None,
                    start_date=start_date or None,
                    end_date=end_date or None,
                    rating_filters=rating_filters or None,
                    language_filter=language_filter or None,
                    place_idx=place_seq,
                    total_places=total_places,
                )
                if place_obj:
                    all_places.append(place_obj)
                total_reviews_counter[0] += pushed

                delay = 2.0 + random.uniform(0.0, 1.0)
                Actor.log.info(f"  Inter-place delay: {delay:.1f}s …")
                await asyncio.sleep(delay)

            except CaptchaBlockedError:
                if retry_count < max_retries:
                    Actor.log.warning(
                        f"  Blocked on place {place_seq} "
                        f"(attempt {attempt}/{total_attempts}) — rotating proxy + browser …"
                    )
                    # Advance the session counter BEFORE retiring so _get_proxy_url
                    # supplies the new session ID when BrowserPool relaunches the browser.
                    _session_rotation[0] += 1
                    # Retire the current browser: moves it from _active_browsers to
                    # _inactive_browsers so the next new_page() call (on retry) creates
                    # a fresh Camoufox instance via CamoufoxPlugin.new_browser().
                    # _close_inactive_browsers() will close the process once the page
                    # is cleaned up by Crawlee — safer than close(force=True) which
                    # would leave a dead browser in the active pool and crash the retry.
                    #
                    # PlaywrightCrawlingContext has no .browser_controller attribute, so
                    # we locate the controller by matching the Playwright Browser object.
                    _ctrl = _find_browser_controller(context.page, browser_pool)
                    if _ctrl is not None:
                        browser_pool._retire_browser(_ctrl)
                    else:
                        Actor.log.warning(
                            "  Could not locate browser controller for retirement — "
                            "browser will be reused (may affect next place)"
                        )
                else:
                    Actor.log.error(
                        f"  Place {place_seq}/{total_places} blocked by DataDome "
                        f"after {total_attempts} attempt(s) — skipping."
                    )
                    await Actor.set_status_message(
                        f"Place {place_seq}/{total_places} — CAPTCHA FAILED, skipping"
                    )
                raise  # Crawlee handles retry scheduling or marks as failed

        requests_list = [Request.from_url(url) for url in place_urls]
        await crawler.run(requests_list)

        # ── Save Places.json / Places.md ─────────────────────────────────────
        if all_places:
            await Actor.set_value("Places.json", all_places)
            await Actor.set_value(
                "Places.md", _build_places_md(all_places), content_type="text/markdown"
            )
            Actor.log.info(
                f"  Saved {len(all_places)} place(s) to key-value store "
                "(keys: 'Places.json', 'Places.md')"
            )

        if total_reviews_counter[0] == 0 and place_urls:
            if all_places:
                filter_msg = (
                    f"Finished — {len(all_places)} place(s) scraped, "
                    "0 reviews matched the applied filters "
                    "(date range, rating, or language). "
                    "Try adjusting or removing your filters."
                )
                Actor.log.info(filter_msg)
                await Actor.exit(status_message=filter_msg)
            else:
                fail_msg = (
                    f"Failed — 0 reviews scraped for {len(place_urls)} place(s). "
                    "All requests were blocked or timed out. "
                    "Check your proxy configuration and retry."
                )
                Actor.log.error(fail_msg)
                await Actor.fail(status_message=fail_msg)
            return

        final_msg = (
            f"Finished — {len(all_places)} place(s) and "
            f"{total_reviews_counter[0]} review(s) pushed to dataset."
        )
        await Actor.set_status_message(final_msg)
        Actor.log.info(final_msg)


if __name__ == "__main__":
    asyncio.run(main())
