"""
Core place scraper.

scrape_place — navigates to a TripAdvisor place URL using a Crawlee-provided
               Playwright Page, extracts place metadata, and fetches all reviews
               via parallel GraphQL requests, pushing them to the Apify dataset.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Optional

from apify import Actor
from playwright.async_api import Page

from .browser import CaptchaBlockedError
from .graphql import (
    PARALLEL_REQUESTS,
    PUSH_BATCH_SIZE,
    REVIEWS_PER_PAGE,
    fetch_reviews_via_graphql,
)
from .parsers import EXTRACT_PAGE_SCRIPT, parse_place_from_jsonld, parse_review_from_graphql
from .utils import (
    _date_sort_key,
    _normalize_place,
    extract_location_id_from_url,
    normalize_place_url,
    with_retry,
)


async def scrape_place(
    page: Page,
    place_url: str,
    max_reviews: Optional[int],
    has_proxy: bool = False,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    rating_filters: Optional[list] = None,
    language_filter: Optional[str] = None,
    place_idx: int = 1,
    total_places: int = 1,
) -> tuple[Optional[dict], int]:
    """
    Scrape one TripAdvisor place using a Crawlee-provided Playwright Page.
    Browser lifecycle, proxy injection, and retries are handled by Crawlee.
    Proxy is passed by Crawlee at context creation time — no manual wiring needed.
    Returns (place_dict, total_reviews_pushed).
    """
    place_url = normalize_place_url(place_url)
    if not place_url:
        Actor.log.warning(f"  Invalid URL: {place_url}")
        return None, 0
    loc_id = extract_location_id_from_url(place_url) or ""

    graphql_responses: list[dict] = []

    async def on_response(response):
        try:
            url = response.url
            if "graphql" not in url.lower() or response.status != 200:
                return
            ct = response.headers.get("content-type") or ""
            if "json" not in ct.lower():
                return
            body = await response.json()
            if body:
                graphql_responses.append(body)
        except Exception:
            pass

    page.on("response", on_response)

    async def _block_resources(route):
        if route.request.resource_type in ("image", "font", "media"):
            await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", _block_resources)

    captcha_detected = asyncio.Event()

    def _on_frame(f):
        if "captcha-delivery.com" in (f.url or "").lower():
            captcha_detected.set()

    page.on("frameattached", _on_frame)
    page.on("framenavigated", _on_frame)

    # ── Phase 1: Navigate ────────────────────────────────────────────────────
    # Crawlee already navigated to place_url before calling this handler.
    # A second page.goto() to the same URL resets TripAdvisor's session/CSRF state,
    # causing the GraphQL API to return {"errors":[...]} instead of review data.
    # Skip re-navigation if Crawlee already landed on the correct page.
    nav_timeout = 90_000 if has_proxy else 45_000
    current_loc = extract_location_id_from_url(page.url) or ""
    target_loc  = extract_location_id_from_url(place_url) or ""
    already_on_page = bool(current_loc and target_loc and current_loc == target_loc)

    if already_on_page:
        Actor.log.info("  Page already loaded by Crawlee — waiting for full JS load …")
        try:
            # Crawlee may have stopped at domcontentloaded; wait for React/Next.js to hydrate
            # so the Reviews tab is in the DOM before we look for it.
            await page.wait_for_load_state("load", timeout=20_000)
        except Exception:
            pass  # proceed even if it times out
    else:
        Actor.log.info("  Navigating …")
        try:
            await with_retry(
                lambda: page.goto(place_url, wait_until="domcontentloaded", timeout=nav_timeout),
                label=f"goto {place_url[:60]}",
            )
        except Exception:
            await with_retry(
                lambda: page.goto(place_url, wait_until="load", timeout=nav_timeout),
                label=f"goto fallback {place_url[:50]}",
            )

    Actor.log.info("  Place loaded successfully")
    await Actor.set_status_message(f"Place {place_idx}/{total_places} — Waiting for Reviews tab …")
    await asyncio.sleep(random.uniform(1.0, 2.0))

    tab_locator = page.get_by_role("tab", name=re.compile(r"Reviews?|Overview", re.I)).or_(
        page.locator('a:has-text("Reviews"), a:has-text("Overview")')
    ).first
    captcha_seen = False
    captcha_was_resolved = False
    captcha_task = asyncio.create_task(captcha_detected.wait())
    page_task = asyncio.create_task(
        tab_locator.wait_for(state="visible", timeout=20_000 if has_proxy else 10_000)
    )
    done, pending = await asyncio.wait(
        [captcha_task, page_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    page_task_failed = False
    if page_task in done:
        exc = page_task.exception() if not page_task.cancelled() else None
        if exc is not None:
            page_task_failed = True

    if captcha_detected.is_set():
        captcha_seen = True
        Actor.log.info("  Captcha detected — checking if Camoufox resolves it …")
        await Actor.set_status_message(f"Place {place_idx}/{total_places} — Captcha detected, waiting …")
    elif page_task_failed:
        # Secondary captcha check: the frame may have attached before our
        # listener was registered, or after the tab wait already timed out.
        for frame in page.frames:
            if frame != page.main_frame and "captcha-delivery.com" in (frame.url or "").lower():
                captcha_seen = True
                break
        if not captcha_seen:
            # Diagnose what is actually on the page before raising.
            current_url = page.url
            page_title = ""
            try:
                page_title = await page.title()
            except Exception:
                pass
            Actor.log.warning(
                f"  Reviews tab not visible after timeout — "
                f"URL: {current_url[:80]} | title: {page_title[:60]}"
            )
            raise CaptchaBlockedError(
                f"Reviews tab not visible after {20_000 if has_proxy else 10_000}ms "
                f"(URL: {current_url[:60]})"
            )
        else:
            Actor.log.info("  Captcha detected (late frame) — checking if Camoufox resolves it …")
            await Actor.set_status_message(f"Place {place_idx}/{total_places} — Captcha detected, waiting …")
    else:
        Actor.log.info("  Page ready — continuing")
        await Actor.set_status_message(f"Place {place_idx}/{total_places} — Scraping reviews …")

    # Poll up to 15 s for Camoufox to auto-resolve DataDome captcha
    if captcha_seen:
        captcha_resolved = False
        for _ in range(15):
            await asyncio.sleep(1.0)
            try:
                still_here = await page.locator(
                    "iframe[src*='captcha-delivery.com']"
                ).first.is_visible(timeout=300)
            except Exception:
                still_here = False
            if not still_here:
                captcha_resolved = True
                break
        if captcha_resolved:
            Actor.log.info("  Captcha auto-resolved (Camoufox passed DataDome check) ✓")
            captcha_seen = False
            captcha_was_resolved = True
        else:
            Actor.log.warning("  Captcha not resolved after 15s — raising for Crawlee retry")
            # Crawlee catches this, rotates proxy/session, and retries the URL
            raise CaptchaBlockedError("DataDome captcha not bypassed with current proxy")

    if captcha_was_resolved:
        try:
            await tab_locator.wait_for(state="visible", timeout=15_000)
        except Exception:
            pass
    await asyncio.sleep(0.3)

    consent_selectors = [
        'button:has-text("Accept")', 'button:has-text("Accept All")',
        'button:has-text("I Accept")', 'button:has-text("Accept all")',
        '[data-testid="accept-cookies"]', '#onetrust-accept-btn-handler',
        'a:has-text("Accept")',
    ]
    for sel in consent_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=150):
                await btn.click()
                Actor.log.info(f"  Clicked consent: {sel[:40]}...")
                await asyncio.sleep(0.5)
                break
        except Exception:
            pass

    for tab_text in ["Reviews", "Review"]:
        try:
            tab = page.get_by_role("tab", name=tab_text).or_(page.locator(f'a:has-text("{tab_text}")'))
            if await tab.first.is_visible(timeout=1000):
                await tab.first.click()
                Actor.log.info(f"  Clicked '{tab_text}' tab")
                await asyncio.sleep(random.uniform(0.5, 1.0))
                break
        except Exception:
            pass

    await page.evaluate("window.scrollBy(0, 400)")
    await asyncio.sleep(random.uniform(0.5, 1.0))

    # ── Phase 2: Extract page data ───────────────────────────────────────────
    place_obj: Optional[dict] = None
    landed_url = page.url

    try:
        page_data = await page.evaluate(EXTRACT_PAGE_SCRIPT)
        if isinstance(page_data, dict):
            ld = page_data.get("place")
            if isinstance(ld, dict) and ld:
                place_obj = parse_place_from_jsonld(ld, landed_url)
                if ld.get("ratingDistribution"):
                    place_obj["ratingDistribution"] = ld["ratingDistribution"]
        title = await page.title()
        Actor.log.info(f"  Page loaded: {title[:80]}")
    except Exception as e:
        Actor.log.warning(f"  Page data extraction failed: {e}")

    Actor.log.info(f"  Captured {len(graphql_responses)} GraphQL response(s)")

    if graphql_responses:
        initial_reviews = parse_review_from_graphql(graphql_responses)
    else:
        initial_reviews = []

    # ── Phase 3: Direct GraphQL pagination ──────────────────────────────────
    total_pushed = 0
    oldest_date = ""
    reviews: list[dict] = list(initial_reviews)

    start_ts = (start_date.strip()[:10] if start_date and start_date.strip() else "") or ""
    end_ts = (end_date.strip()[:10] if end_date and end_date.strip() else "") or ""
    page_review_count = (
        place_obj.get("review_count") or place_obj.get("totalReviews") or 0
    ) if place_obj else 0

    async def _push_batch(batch: list[dict]) -> None:
        nonlocal total_pushed, oldest_date
        if page_review_count and total_pushed + len(batch) > page_review_count:
            batch = batch[: page_review_count - total_pushed]
        if not batch:
            return
        await Actor.push_data(batch)
        total_pushed += len(batch)
        for rev in batch:
            d = (rev.get("publishedDate") or rev.get("date") or "")[:10]
            if d and (not oldest_date or d < oldest_date):
                oldest_date = d
        _total = max_reviews or page_review_count
        total_str = f"{_total:,}" if _total else "?"
        await Actor.set_status_message(
            f"Place {place_idx}/{total_places} — {total_pushed:,}/{total_str} reviews"
        )
        Actor.log.info(
            f"  Pushed batch: {len(batch)} reviews | "
            f"Place {place_idx}/{total_places} | "
            f"{total_pushed:,}/{total_str} reviews"
        )

    if loc_id:
        reviews_offset = 0
        while True:
            if max_reviews and total_pushed + len(reviews) >= max_reviews:
                break
            batch_offsets = [
                reviews_offset + i * REVIEWS_PER_PAGE
                for i in range(PARALLEL_REQUESTS)
            ]
            tasks = [
                fetch_reviews_via_graphql(
                    page, loc_id, off, REVIEWS_PER_PAGE,
                    rating_filters=rating_filters,
                    language_filter=language_filter,
                )
                for off in batch_offsets
            ]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            got_any = False
            got_partial = False
            for i, extracted in enumerate(batch_results):
                if isinstance(extracted, Exception):
                    Actor.log.warning(f"  GraphQL fetch offset={batch_offsets[i]} failed: {extracted}")
                    continue
                if extracted:
                    got_any = True
                    for t in extracted:
                        review_date = (t.get("date") or t.get("publishedDate") or "")[:10]
                        if start_ts and (review_date or "9999") < start_ts:
                            continue
                        if end_ts and review_date and review_date > end_ts:
                            continue
                        reviews.append(t)
                    if len(extracted) < REVIEWS_PER_PAGE:
                        got_partial = True
                        Actor.log.debug(f"  Partial response at offset {batch_offsets[i]}: {len(extracted)} reviews")
            if not got_any or got_partial:
                if got_partial:
                    Actor.log.info(f"  Reached end of reviews at offset ~{reviews_offset} (last batch had partial)")
                break
            reviews_offset += PARALLEL_REQUESTS * REVIEWS_PER_PAGE
            # Capture the stop condition BEFORE pushing so batches are always
            # flushed even on the last iteration (fixes single 300-review push
            # when max_reviews < PARALLEL_REQUESTS * REVIEWS_PER_PAGE).
            stop_after_push = bool(max_reviews and reviews_offset >= max_reviews)

            reviews.sort(key=_date_sort_key, reverse=True)
            if max_reviews:
                reviews = reviews[: max_reviews - total_pushed]
            if page_review_count:
                reviews = reviews[: page_review_count - total_pushed]
            while len(reviews) >= PUSH_BATCH_SIZE:
                batch = reviews[:PUSH_BATCH_SIZE]
                reviews = reviews[PUSH_BATCH_SIZE:]
                await _push_batch(batch)
            await asyncio.sleep(random.uniform(0.8, 1.5))

            if stop_after_push:
                break

    if not place_obj:
        place_obj = {}
    if not place_obj.get("name") and reviews:
        place_obj["name"] = (reviews[0].get("placeInfo") or {}).get("name", "") or ""

    reviews.sort(key=_date_sort_key, reverse=True)
    if max_reviews:
        reviews = reviews[: max_reviews - total_pushed]
    if page_review_count:
        reviews = reviews[: page_review_count - total_pushed]
    if reviews:
        await _push_batch(reviews)

    if total_pushed == 0:
        if graphql_responses:
            Actor.log.info(
                "  No reviews matched the applied filters "
                "(date range, rating, or language)."
            )
        else:
            Actor.log.warning(
                "  No reviews captured. TripAdvisor may be blocking. "
                "Try enabling Apify Residential Proxy."
            )

    place_obj = _normalize_place(place_obj, url=landed_url, loc_id=loc_id)
    place_obj["scrapedReviews"] = total_pushed
    place_obj["oldestDate"] = oldest_date

    Actor.log.info(f"  Done: {total_pushed} reviews scraped")
    return place_obj, total_pushed
