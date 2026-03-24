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
"""

from __future__ import annotations

import asyncio
import os as _os
import random
import re
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import urlparse

import httpx  # type: ignore[import-untyped]

from apify import Actor
from camoufox import AsyncNewBrowser
from crawlee import ConcurrencySettings, Request
from crawlee.browsers import BrowserPool, PlaywrightBrowserController, PlaywrightBrowserPlugin
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.events import Event
from crawlee.proxy_configuration import ProxyConfiguration as CrawleeProxyConfiguration
from playwright.async_api import Page
from typing_extensions import override


class CaptchaBlockedError(Exception):
    """Raised when DataDome captcha cannot be bypassed with the current proxy."""


# ══════════════════════════════════════════════════════════════════════════════
#  CRAWLEE: CAMOUFOX BROWSER PLUGIN
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


# ══════════════════════════════════════════════════════════════════════════════
#  VIEWPORT RANDOMISATION
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
#  OUTPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_place(raw: dict, url: str = "", loc_id: str = "") -> dict:
    """Return a place dict with a fixed, complete set of fields (no missing keys)."""
    return {
        "id":                raw.get("id") or loc_id or "",
        "url":               raw.get("url") or url or "",
        "name":              raw.get("name") or "",
        "place_type":        raw.get("place_type") or "",
        "rating":            raw.get("rating") or None,
        "totalReviews":      raw.get("review_count") or raw.get("totalReviews") or 0,
        "scrapedReviews":    raw.get("scrapedReviews") or raw.get("reviewCount") or 0,
        "address":           raw.get("address") or "",
        "city":              raw.get("city") or "",
        "region":            raw.get("region") or "",
        "country":           raw.get("country") or "",
        "price_range":       raw.get("price_range") or "",
        "image":             raw.get("image") or "",
        "ratingDistribution": raw.get("ratingDistribution") or None,
        "oldestDate":        raw.get("oldestDate") or "",
        "error":             raw.get("error"),
    }


def _build_places_md(places: list[dict]) -> str:
    """Render a list of place dicts to a human-readable Markdown document."""
    lines = ["# TripAdvisor Places\n"]
    for i, p in enumerate(places, 1):
        name       = p.get("name") or "Unknown"
        pid        = p.get("id") or ""
        url        = p.get("url") or ""
        place_type = p.get("place_type") or ""
        rating     = p.get("rating") or ""
        total      = p.get("totalReviews") or 0
        scraped    = p.get("scrapedReviews") or 0
        address    = p.get("address") or ""
        city       = p.get("city") or ""
        region     = p.get("region") or ""
        country    = p.get("country") or ""
        price      = p.get("price_range") or ""
        image      = p.get("image") or ""
        oldest     = p.get("oldestDate") or ""
        dist       = p.get("ratingDistribution") or {}
        error      = p.get("error")

        lines.append(f"## {i}. {name}\n")
        if pid:
            lines.append(f"- **ID**: {pid}")
        if url:
            lines.append(f"- **URL**: {url}")
        if place_type:
            lines.append(f"- **Type**: {place_type}")
        if rating:
            lines.append(f"- **Rating**: {rating} / 5")
        location_parts = ", ".join(filter(None, [address, city, region, country]))
        if location_parts:
            lines.append(f"- **Location**: {location_parts}")
        if price:
            lines.append(f"- **Price level**: {price}")
        if image:
            lines.append(f"- **Image**: {image}")
        if total:
            lines.append(f"- **Total reviews on TripAdvisor**: {total:,}")
        lines.append(f"- **Reviews scraped**: {scraped:,}")
        if oldest:
            lines.append(f"- **Oldest scraped review**: {oldest}")
        if dist:
            lines.append(
                f"- **Rating distribution**: "
                f"Excellent {dist.get('excellent', '?')}, "
                f"Very Good {dist.get('good', '?')}, "
                f"Average {dist.get('average', '?')}, "
                f"Poor {dist.get('poor', '?')}, "
                f"Terrible {dist.get('terrible', '?')}"
            )
        if error:
            lines.append(f"- **Error**: {error}")
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  RETRY HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def with_retry(coro_factory, max_retries: int = 3, base_delay: float = 2.0, label: str = ""):
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                Actor.log.warning(
                    f"{label} — attempt {attempt}/{max_retries} failed: {exc!s:.120}. "
                    f"Retrying in {delay:.1f}s …"
                )
                await asyncio.sleep(delay)
    raise last_exc


# ══════════════════════════════════════════════════════════════════════════════
#  URL UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

_TA_HOSTS = {"tripadvisor.com", "www.tripadvisor.com"}
_TA_PLACE_PATH_RE = re.compile(
    r"/(Hotel_Review|Restaurant_Review|Attraction_Review|AttractionProductReview|"
    r"VacationRentalReview|ShowUserReviews|geo\d+)-",
    re.I,
)


def normalize_place_url(url: str) -> str:
    """Return a cleaned TripAdvisor place URL, or '' if not a valid place URL."""
    try:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        if "tripadvisor." not in host:
            return ""
        if not _TA_PLACE_PATH_RE.search(parsed.path):
            return ""
        clean = f"https://www.tripadvisor.com{parsed.path}"
        clean = re.sub(r"-Reviews-or\d+-", "-Reviews-", clean)
        if not clean.endswith(".html"):
            clean = clean.rstrip("/") + ".html"
        return clean
    except Exception:
        return ""


def extract_location_id_from_url(url: str) -> Optional[str]:
    """Extract location ID (e.g. d264936) from TripAdvisor URL."""
    match = re.search(r"-d(\d+)-", url)
    return match.group(1) if match else None


# ══════════════════════════════════════════════════════════════════════════════
#  PLACE PARSING (JSON-LD, embedded data)
# ══════════════════════════════════════════════════════════════════════════════

def parse_place_from_jsonld(ld: dict, url: str) -> dict:
    """Extract place info from JSON-LD schema."""
    addr = ld.get("address") or {}
    if isinstance(addr, dict):
        street = addr.get("streetAddress") or ""
        locality = addr.get("addressLocality") or ""
        region = addr.get("addressRegion") or ""
        country = ""
        if isinstance(addr.get("addressCountry"), dict):
            country = addr.get("addressCountry", {}).get("name") or ""
        elif isinstance(addr.get("addressCountry"), str):
            country = addr.get("addressCountry") or ""
    else:
        street = locality = region = country = ""

    rating = ld.get("aggregateRating") or {}
    if isinstance(rating, dict):
        rating_value = rating.get("ratingValue") or ""
        review_count = rating.get("reviewCount") or 0
        try:
            review_count = int(str(review_count).replace(",", ""))
        except (ValueError, TypeError):
            review_count = 0
    else:
        rating_value = ""
        review_count = 0

    return {
        "url": url,
        "name": ld.get("name") or "",
        "place_type": ld.get("@type") or "LodgingBusiness",
        "rating": rating_value,
        "review_count": review_count,
        "address": street,
        "city": locality,
        "region": region,
        "country": country,
        "price_range": ld.get("priceRange") or "",
        "image": ld.get("image") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE EXTRACTION SCRIPT  (identical to main.py)
# ══════════════════════════════════════════════════════════════════════════════

EXTRACT_PAGE_SCRIPT = r"""
() => {
    const result = { place: null, reviews: [] };

    // 1. JSON-LD (schema.org)
    // Restaurants/attractions often wrap their data in an @graph array instead of
    // a flat top-level object — flatten those before checking @type.
    const _PLACE_TYPES = ['LodgingBusiness','Restaurant','FoodEstablishment',
                          'TouristAttraction','LocalBusiness','Product','Service'];
    function _typeMatch(t) {
        if (!t) return false;
        const types = Array.isArray(t) ? t : [t];
        return types.some(v => _PLACE_TYPES.some(pt => String(v).includes(pt)));
    }
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of ldScripts) {
        try {
            const d = JSON.parse(s.textContent);
            const top = Array.isArray(d) ? d : [d];
            // Flatten: push top-level items AND any nested @graph entries
            const items = [];
            for (const t of top) {
                items.push(t);
                if (Array.isArray(t['@graph'])) items.push(...t['@graph']);
            }
            for (const item of items) {
                if (_typeMatch(item['@type'])) {
                    result.place = item;
                    break;
                }
            }
        } catch(e) {}
        if (result.place) break;
    }

    // 2. __NEXT_DATA__ place info (if JSON-LD empty or missing name)
    if (!result.place || !result.place.name) {
        try {
            const nd = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
            function findPlace(obj, depth) {
                if (!obj || typeof obj !== 'object' || depth > 6) return null;
                if (obj.name && obj.locationId && (obj.accommodationCategory ||
                    obj.restaurantCuisine !== undefined || obj.subtype || obj.productType)) return obj;
                for (const k of Object.keys(obj).slice(0, 30)) {
                    const r = findPlace(obj[k], depth + 1);
                    if (r) return r;
                }
                return null;
            }
            const pl = findPlace(nd, 0);
            if (pl && !result.place) {
                // Extract as many fields as possible from __NEXT_DATA__ so
                // parse_place_from_jsonld() can build a complete place object.
                const rs = pl.reviewSummary || {};
                const addr = pl.address || {};
                result.place = {
                    name: pl.name || '',
                    locationId: pl.locationId,
                    aggregateRating: {
                        ratingValue: rs.rating || rs.ratingValue || '',
                        reviewCount: rs.count || rs.reviewCount || pl.reviewCount || 0,
                    },
                    address: {
                        streetAddress:  addr.street  || addr.streetAddress  || '',
                        addressLocality: addr.city   || addr.addressLocality || '',
                        addressRegion:  addr.state   || addr.addressRegion   || '',
                        addressCountry: addr.country || addr.addressCountry  || '',
                    },
                    priceRange: pl.priceRange || pl.priceLevel || pl.priceLevelStr || '',
                    image: pl.image || '',
                };
            }
        } catch(e) {}
    }

    // 3a. Rating distribution — search __NEXT_DATA__ for several known shapes
    try {
        const ndEl = document.getElementById('__NEXT_DATA__');
        if (ndEl) {
            function _toRatingDist(obj) {
                if (!obj || typeof obj !== 'object') return null;
                if (['1','2','3','4','5'].every(k => typeof obj[k] === 'number')) {
                    return { excellent: obj['5'], good: obj['4'], average: obj['3'], poor: obj['2'], terrible: obj['1'] };
                }
                if (typeof obj['EXCELLENT'] === 'number') {
                    return { excellent: obj['EXCELLENT'], good: obj['VERY_GOOD']||obj['GOOD']||0, average: obj['AVERAGE']||0, poor: obj['POOR']||0, terrible: obj['TERRIBLE']||0 };
                }
                if (Array.isArray(obj) && obj.length >= 5 && typeof obj[0].count === 'number') {
                    const m = {};
                    obj.forEach(e => { m[String(e.ratingValue||e.rating||e.value)] = e.count; });
                    if (['1','2','3','4','5'].every(k => typeof m[k] === 'number')) {
                        return { excellent: m['5'], good: m['4'], average: m['3'], poor: m['2'], terrible: m['1'] };
                    }
                }
                return null;
            }
            function _findRatingCounts(obj, depth) {
                if (!obj || typeof obj !== 'object' || depth > 10) return null;
                const d = _toRatingDist(obj);
                if (d) return d;
                const keys = Object.keys(obj);
                const prio = ['ratingCounts','reviewRatingCounts','distribution','ratingDistribution','subRatings','histogram'];
                for (const k of prio) {
                    if (obj[k]) { const r = _findRatingCounts(obj[k], depth + 1); if (r) return r; }
                }
                for (const k of keys.slice(0, 60)) {
                    if (prio.includes(k)) continue;
                    const r = _findRatingCounts(obj[k], depth + 1);
                    if (r) return r;
                }
                return null;
            }
            const nd = JSON.parse(ndEl.textContent);
            const dist = _findRatingCounts(nd, 0);
            if (dist) {
                if (!result.place) result.place = {};
                result.place.ratingDistribution = dist;
            }
        }
    } catch (_) {}

    // 3. DOM reviews (data-reviewid, data-test-target, data-automation)
    let reviewBlocks = document.querySelectorAll('[data-reviewid]');
    if (reviewBlocks.length === 0) {
        reviewBlocks = document.querySelectorAll('[data-automation="reviewCard"]');
    }
    if (reviewBlocks.length === 0) {
        reviewBlocks = document.querySelectorAll('[data-test-target="HR_CC_CARD"]');
    }
    for (const block of reviewBlocks) {
        try {
            const rid = block.getAttribute('data-reviewid') || '';
            const titleEl = block.querySelector('[data-test-target="review-title"] span, .noQuotes, .title');
            const textEl = block.querySelector('[data-test-target="review-body"] span, .reviewText span, .review-container .entry span');
            const ratingEl = block.querySelector('[class*="ui_bubble_rating"]');
            const ratingMatch = ratingEl ? (ratingEl.className.match(/bubble_(\d+)/) || []) : [];
            const dateEl = block.querySelector('[data-test-target="review-date"], .ratingDate');
            result.reviews.push({
                review_id: rid,
                title: titleEl ? titleEl.innerText.trim() : '',
                text: textEl ? textEl.innerText.trim() : '',
                rating: ratingMatch[1] ? parseInt(ratingMatch[1]) / 10 : null,
                date: dateEl ? (dateEl.getAttribute('title') || dateEl.innerText.trim()) : '',
            });
        } catch(e) {}
    }
    return result;
}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  GRAPHQL REVIEW PARSING  (identical to main.py)
# ══════════════════════════════════════════════════════════════════════════════

def dig(obj: Any, *keys) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list):
            try:
                obj = obj[k]
            except (IndexError, TypeError):
                return None
        else:
            return None
    return obj


def _safe_avatar_url(user: dict) -> str:
    avatar = user.get("avatar") or user.get("userAvatar") or {}
    if isinstance(avatar, str):
        return avatar
    if isinstance(avatar, dict):
        photo = avatar.get("photoSizeDynamic") or avatar.get("photo") or {}
        if isinstance(photo, dict):
            tpl = photo.get("urlTemplate") or ""
            if tpl:
                return tpl.replace("{width}", "100").replace("{height}", "100")
        return avatar.get("url") or avatar.get("smallUrl") or ""
    return ""


def _extract_reviews_from_obj(obj: Any, results: list, depth: int = 0) -> None:
    if depth > 6 or not isinstance(obj, (dict, list)):
        return
    if isinstance(obj, list):
        for item in obj:
            _extract_reviews_from_obj(item, results, depth + 1)
        return
    if obj.get("text") and obj.get("rating") and obj.get("publishedDate"):
        results.append(obj)
        return
    for v in obj.values():
        _extract_reviews_from_obj(v, results, depth + 1)


def parse_review_from_graphql(data: list) -> list[dict]:
    """Extract reviews from GraphQL response list (same as main.py)."""
    results: list[dict] = []
    if not isinstance(data, list):
        return results
    for item in data:
        if not isinstance(item, dict):
            continue
        inner = item.get("data") or {}
        if not isinstance(inner, dict):
            continue
        if "CommunityUGC__locationTips" in inner:
            continue
        reviews_proxy = inner.get("ReviewsProxy_getReviewListPageForLocation")
        if isinstance(reviews_proxy, list) and reviews_proxy:
            first = reviews_proxy[0]
            reviews_data = first.get("reviews") if isinstance(first, dict) else None
        else:
            reviews_data = None
        if reviews_data is None:
            reviews_data = (
                inner.get("LocationReviews__getReviews")
                or inner.get("SocialData_getSocialObjects")
                or inner.get("SocialData")
                or inner.get("LocationReviews")
                or inner.get("reviews")
                or inner.get("reviewList")
            )
        if reviews_data is not None:
            if isinstance(reviews_data, dict):
                reviews_list = reviews_data.get("reviews") or reviews_data.get("reviewList") or reviews_data.get("socialObjects") or []
            else:
                reviews_list = reviews_data if isinstance(reviews_data, list) else []
            for r in (reviews_list if isinstance(reviews_list, list) else []):
                if not isinstance(r, dict):
                    continue
                text = (
                    r.get("text") or r.get("body") or r.get("review")
                    or dig(r, "snippets", 0, "text") or ""
                )
                if not text and not r.get("title"):
                    continue
                user = r.get("user") or r.get("userProfile") or r.get("author") or {}
                if not isinstance(user, dict):
                    user = {}
                name = user.get("displayName") or user.get("name") or user.get("username") or ""
                rating = r.get("rating")
                if rating is None and isinstance(r.get("tripInfo"), dict):
                    rating = r.get("tripInfo", {}).get("rating")
                date_val = (
                    r.get("publishedDate") or r.get("createdAt")
                    or r.get("date") or r.get("submittedDateTime") or ""
                )
                rid = str(r.get("id") or r.get("reviewId") or r.get("objectId") or len(results))
                loc = r.get("location") or {}
                if not isinstance(loc, dict):
                    loc = {}
                detail = r.get("reviewDetailPageWrapper") or {}
                route = (detail.get("reviewDetailPageRoute") or {}) if isinstance(detail, dict) else {}
                review_url = (
                    "https://www.tripadvisor.com" + str(route["url"])
                    if isinstance(route, dict) and route.get("url") else ""
                )
                trip_info = r.get("tripInfo") or {}
                stay_date = trip_info.get("stayDate") or "" if isinstance(trip_info, dict) else ""
                if stay_date and len(stay_date) >= 7:
                    travel_date = stay_date[:7]
                else:
                    travel_date = str(date_val)[:7] if date_val else ""
                trip_type = (trip_info.get("tripType") or trip_info.get("type") or "") if isinstance(trip_info, dict) else ""
                contrib = user.get("contributionCounts") or {} if isinstance(user, dict) else {}
                mgmt = r.get("mgmtResponse") or {}
                owner_resp = None
                if isinstance(mgmt, dict) and mgmt.get("text"):
                    owner_resp = {
                        "id": str(mgmt.get("id") or ""),
                        "title": "Owner Response",
                        "text": (mgmt.get("text") or "").strip()[:2000],
                        "lang": mgmt.get("language") or "en",
                        "publishedDate": mgmt.get("publishedDate") or "",
                        "responder": (mgmt.get("userProfile") or {}).get("displayName") or "",
                    }
                addl = r.get("additionalRatings") or []
                subratings = []
                if isinstance(addl, list):
                    for a in addl:
                        if isinstance(a, dict) and a.get("ratingLabelLocalizedString"):
                            subratings.append({
                                "name": a.get("ratingLabelLocalizedString"),
                                "value": int(a.get("rating") or 0),
                            })
                photos_raw = r.get("photos") or []
                photos_list = []
                if isinstance(photos_raw, list):
                    for p in photos_raw:
                        ph = p.get("photo") if isinstance(p, dict) else p
                        if isinstance(ph, dict):
                            dyn = ph.get("photoSizeDynamic") or {}
                            url_tpl = dyn.get("urlTemplate") or ""
                            if url_tpl:
                                photos_list.append({
                                    "id": str(ph.get("id") or ""),
                                    "image": url_tpl.replace("{width}", "640").replace("{height}", "480"),
                                })
                place_name = loc.get("name") or "" if isinstance(loc, dict) else ""
                place_web_url = (
                    "https://www.tripadvisor.com" + str(loc.get("url") or "")
                ) if isinstance(loc, dict) and loc.get("url") else ""
                place_info = {
                    "id": str(loc.get("locationId") or r.get("locationId") or ""),
                    "name": place_name,
                    "webUrl": place_web_url,
                } if isinstance(loc, dict) else {}
                results.append({
                    "id": rid,
                    "url": review_url,
                    "title": (r.get("title") or "").strip(),
                    "text": (text or "").strip() if isinstance(text, str) else str(text).strip(),
                    "rating": int(rating) if rating is not None else None,
                    "lang": r.get("language") or "en",
                    "originalLanguage": r.get("originalLanguage") or r.get("language") or "en",
                    "publishedDate": str(date_val)[:50] if date_val else "",
                    "travelDate": travel_date,
                    "tripType": trip_type,
                    "helpfulVotes": int(r.get("helpfulVotes") or r.get("helpful_votes") or 0),
                    "reviewerName": name,
                    "placeName": place_name,
                    "placeUrl": place_web_url,
                    "publishedPlatform": r.get("publishPlatform"),
                    "locationId": str(loc.get("locationId") or r.get("locationId") or ""),
                    "subratings": subratings,
                    "ownerResponse": owner_resp,
                    "photos": photos_list,
                    "user": {
                        "userId": user.get("id") or "",
                        "displayName": name,
                        "username": user.get("username") or "",
                        "avatar": _safe_avatar_url(user),
                        "contributions": contrib,
                    } if isinstance(user, dict) else {},
                    "placeInfo": place_info,
                    "date": str(date_val)[:50] if date_val else "",
                })
        if not results:
            _extract_reviews_from_obj(inner, results)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  GRAPHQL DIRECT FETCH  (identical to main.py)
# ══════════════════════════════════════════════════════════════════════════════

PARALLEL_REQUESTS = 40
REVIEWS_PER_PAGE = 10
PUSH_BATCH_SIZE = 300


REVIEWS_QUERY_ID = "ef1a9f94012220d3"  # ReviewsProxy_getReviewListPageForLocation


async def fetch_reviews_via_graphql(
    page: Page,
    loc_id: str,
    offset: int,
    reviews_per_page: int = REVIEWS_PER_PAGE,
    rating_filters: Optional[list] = None,
    language_filter: Optional[str] = None,
) -> list[dict]:
    """Fetch one page of reviews via TripAdvisor GraphQL API from within the browser page."""
    gql_filters = []
    if rating_filters:
        gql_filters.append({"axis": "RATING", "selections": [str(r) for r in rating_filters]})
    if language_filter:
        gql_filters.append({"axis": "LANGUAGE", "selections": [language_filter]})

    variables = {
        "locationId": int(loc_id),
        "filters": gql_filters,
        "limit": reviews_per_page,
        "offset": offset,
        "sortType": None,
        "sortBy": "SERVER_DETERMINED",
        "language": "en",
        "doMachineTranslation": True,
        "photosPerReviewLimit": 3,
    }
    payload = [
        {"variables": variables, "extensions": {"preRegisteredQueryId": REVIEWS_QUERY_ID}}
    ]
    url = "https://www.tripadvisor.com/data/graphql/ids"

    max_gql_retries = 3
    last_exc = None
    for attempt in range(1, max_gql_retries + 1):
        try:
            result = await page.evaluate(
                """
                async (args) => {
                    const resp = await fetch(args.url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Content-Type': 'application/json',
                            'Accept': '*/*',
                            'Origin': 'https://www.tripadvisor.com',
                            'Referer': window.location.href,
                        },
                        body: JSON.stringify(args.payload),
                    });
                    if (!resp.ok) return null;
                    return await resp.json();
                }
                """,
                {"url": url, "payload": payload},
            )
            if isinstance(result, list):
                return parse_review_from_graphql(result)
            Actor.log.debug(f"  GraphQL response was not a list — got {type(result).__name__}: {str(result)[:200]}")
            return []
        except Exception as exc:
            last_exc = exc
            if attempt < max_gql_retries:
                delay = 1.5 * (2 ** (attempt - 1))
                Actor.log.warning(
                    f"  GraphQL reviews fetch failed: {exc!s:.100}. "
                    f"Retrying in {delay:.1f}s (attempt {attempt}/{max_gql_retries}) …"
                )
                await asyncio.sleep(delay)
    Actor.log.warning(f"  GraphQL reviews fetch failed after {max_gql_retries} attempts: {last_exc!s:.100}")
    return []


def _date_sort_key(r: dict) -> str:
    return str(r.get("publishedDate") or r.get("date") or "")


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: SCRAPE ONE PLACE  (adapted for Crawlee — page provided by framework)
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_place(
    page: Page,                        # ← Crawlee provides this (was browser + context)
    place_url: str,
    max_reviews: Optional[int],
    has_proxy: bool = False,           # ← for timeout sizing only
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


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY TIMEZONE PROBE  (logging only — timezone is NOT applied to the context)
# ══════════════════════════════════════════════════════════════════════════════

async def _probe_proxy_timezone(proxy_url: str) -> tuple[str, str]:
    """
    Detect the proxy's exit IP and return (IANA_timezone, source_label).

    source_label is a short string for log display, e.g.:
        "Europe/London [ipinfo: 82.45.x.x/GB]"
        "America/New_York [ip-api: 1.2.3.4/US]"
        "Europe/London [probe-failed: default]"

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

        # ── Input validation (same as main.py) ───────────────────────────────
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

        # ── Proxy setup ────────────────────────────────────────────────────────
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
                "Browser geoip: timezone, geolocation, and fingerprint will be "
                "auto-matched to the proxy exit IP via camoufox geoip"
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

        # Shared state written by CamoufoxPlugin.new_browser() so handle_place
        # knows when a fresh browser was launched and can emit the session log.
        # Fields prefixed with session_* / exit_* are the cached values from the
        # last proxy probe; they are re-logged for every place so the user can see
        # which session/IP is in use even when the browser is being reused.
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
            # new_browser() already ran the proxy probe and cached the results in
            # browser_state (probe + geoip browser launch ran concurrently there).
            # Here we just read the cache and log — no extra HTTP round-trip.
            session_id = f"run_s{_session_rotation[0] + 1}"
            vp          = browser_state["vp"]
            session_tz  = browser_state["session_tz"]
            session_src = browser_state["session_src"]
            exit_ip     = browser_state["exit_ip"]
            exit_country = browser_state["exit_country"]

            if browser_state["needs_log"]:
                # New browser — "Proxy exit IP:" was already logged by
                # _probe_proxy_timezone inside new_browser(); log "Launching browser:".
                browser_state.update(needs_log=False, session_id=session_id)
                Actor.log.info(
                    f"  Launching browser: os=windows | tz={session_tz} [geoip+{session_src}] | "
                    f"{vp['width']}×{vp['height']} | session={session_id}"
                )
            else:
                # Reused browser — re-log cached values for per-place visibility.
                session_id = browser_state["session_id"]
                if exit_ip != "?":
                    svc = session_src.split(":")[0]
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
