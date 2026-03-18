"""
TripAdvisor Reviews Scraper — Apify Actor
══════════════════════════════════════════════════════════════════════════════

Strategy (JSON-first, DOM fallback):

  Phase 1 · Initial page load
    • Playwright loads place URL (Hotel_Review, Restaurant_Review, etc.)
    • Intercepts GraphQL/XHR responses for review JSON when available
    • Falls back to parsing embedded JSON (__NEXT_DATA__, JSON-LD) or DOM

  Phase 2 · Pagination
    • TripAdvisor paginates via URL: -Reviews-or{offset}- (e.g. or10, or20)
    • Typically 5–10 reviews per page; no hard 200-review cap like Trustpilot
    • Fetches each page; can scale to thousands of reviews per place

Anti-blocking stack:
  • Camoufox — stealthy Firefox fork; spoofs WebGL/canvas/audio/fonts, passes CreepJS
  • Viewport randomisation — 1366×768 / 1440×900 / 1920×1080
  • One proxy session per place — consistent IP for session
  • Exponential backoff retry — 3 attempts with 2s → 4s → 8s delays
  • Random inter-page delay — 300–800 ms between pages
  • Random inter-place delay — 2s + 0–1s jitter between places
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Optional
from urllib.parse import urlparse

from apify import Actor
from crawlee.events import Event
from playwright.async_api import async_playwright, Page
from camoufox import AsyncNewBrowser


class CaptchaBlockedError(Exception):
    """Raised when DataDome captcha cannot be bypassed with the current proxy."""


# ══════════════════════════════════════════════════════════════════════════════
#  VIEWPORT RANDOMISATION
# ══════════════════════════════════════════════════════════════════════════════

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]


def random_fingerprint() -> dict:
    """Return a randomised viewport. Camoufox handles UA/fingerprint internally."""
    return {
        "viewport": random.choice(VIEWPORTS),
        "user_agent": "",  # populated lazily from browser after first navigation
    }


def _build_places_md(places: list[dict]) -> str:
    """Render a list of place dicts to a human-readable Markdown document."""
    lines = ["# TripAdvisor Places\n"]
    for i, p in enumerate(places, 1):
        name = p.get("name") or p.get("title") or "Unknown"
        url = p.get("url") or ""
        rating = p.get("rating") or p.get("overallRating") or ""
        review_count = p.get("reviewCount") or 0
        address = p.get("address") or p.get("location") or ""
        price = p.get("priceLevel") or p.get("price") or ""
        error = p.get("error")
        lines.append(f"## {i}. {name}\n")
        if url:
            lines.append(f"- **URL**: {url}")
        if address:
            lines.append(f"- **Address**: {address}")
        if rating:
            lines.append(f"- **Rating**: {rating} / 5")
        if price:
            lines.append(f"- **Price level**: {price}")
        lines.append(f"- **Reviews scraped**: {review_count:,}")
        if error:
            lines.append(f"- **Error**: {error}")
        lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  RETRY HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def with_retry(
    coro_factory,
    max_retries: int = 3,
    base_delay: float = 2.0,
    label: str = "",
) -> Any:
    """Call coro_factory() up to max_retries times with exponential backoff."""
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            if attempt == max_retries:
                Actor.log.warning(
                    f"{label} — failed after {max_retries} attempts: {exc}"
                )
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0)
            Actor.log.warning(
                f"{label} — attempt {attempt}/{max_retries} failed: {exc}. "
                f"Retrying in {delay:.1f}s …"
            )
            await asyncio.sleep(delay)


# ══════════════════════════════════════════════════════════════════════════════
#  DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _date_sort_key(item: dict) -> tuple:
    """Return (year, month) for sorting; newest first. Empty/unparseable -> (0, 0)."""
    raw = (item.get("date") or item.get("publishedDate") or "").strip()
    if not raw:
        return (0, 0)
    # ISO: "2026-01-24T15:06:21.384Z" or "2025-12-21"
    m = re.match(r"(\d{4})-(\d{1,2})", raw)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    # "12/2025" or "1/2025" (month/year)
    m = re.match(r"(\d{1,2})/(\d{4})", raw)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    # "March 2026", "Mar 2026", "24 March 2026", "March 24, 2026"
    m = re.search(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\s*,?\s*(\d{4})",
        raw,
        re.I,
    )
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower(), 0)
        if month:
            return (int(m.group(2)), month)
    # "2026" only
    m = re.search(r"\b(20\d{2})\b", raw)
    if m:
        return (int(m.group(1)), 12)  # assume Dec if only year
    return (0, 0)



def _safe_avatar_url(user: Any) -> str:
    """Safely extract avatar URL from user.avatar.data.photoSizeDynamic.urlTemplate."""
    av = (user.get("avatar") or {}) if isinstance(user, dict) else {}
    data = (av.get("data") or {}) if isinstance(av, dict) else {}
    psd = (data.get("photoSizeDynamic") or {}) if isinstance(data, dict) else {}
    url = (psd.get("urlTemplate") or "") if isinstance(psd, dict) else ""
    return url.replace("{width}", "100").replace("{height}", "100") if url else ""


def dig(obj: Any, *keys, default: Any = None) -> Any:
    """Safely traverse a nested dict/list without raising KeyError/IndexError."""
    for key in keys:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int):
            obj = obj[key] if 0 <= key < len(obj) else None
        else:
            return default
    return obj if obj is not None else default


def normalize_place_url(raw: str) -> str:
    """
    Accept any of these formats and return a full TripAdvisor place URL:
      "https://www.tripadvisor.com/Hotel_Review-g190327-d264936-Reviews-..."
      "Hotel_Review-g190327-d264936-Reviews-1926_Hotel_Spa-Sliema_Island_of_Malta.html"
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http"):
        parsed = urlparse(raw)
        if "tripadvisor.com" in parsed.netloc:
            return raw.split("?")[0]
        return ""
    if raw.startswith("/"):
        return f"https://www.tripadvisor.com{raw}".split("?")[0]
    if "tripadvisor.com" in raw or "Hotel_Review" in raw or "Restaurant_Review" in raw or "Attraction_Review" in raw:
        return f"https://www.tripadvisor.com/{raw}".strip("/").split("?")[0]
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


def parse_review_from_dom(review_el: dict) -> dict:
    """Parse a review from DOM-extracted structure."""
    return {
        "_type": "review",
        "source": "review",
        "review_id": review_el.get("review_id") or "",
        "title": review_el.get("title") or "",
        "text": review_el.get("text") or "",
        "rating": review_el.get("rating"),
        "date": review_el.get("date") or "",
        "trip_type": review_el.get("trip_type") or "",
        "reviewer_name": review_el.get("reviewer_name") or "",
        "helpful_votes": review_el.get("helpful_votes") or 0,
        "management_response": review_el.get("management_response") or "",
    }


def parse_qa_from_graphql(data: list) -> list[dict]:
    """Extract Q&A from QuestionsAndAnswers_getQuestionsByLocations."""
    results: list[dict] = []
    if not isinstance(data, list):
        return results
    for item in data:
        if not isinstance(item, dict):
            continue
        inner = item.get("data") or {}
        qa_data = inner.get("QuestionsAndAnswers_getQuestionsByLocations")
        if qa_data is None:
            continue
        blocks = qa_data if isinstance(qa_data, list) else [qa_data]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            questions = block.get("questions") or []
            if not isinstance(questions, list):
                continue
            for q in questions:
                if not isinstance(q, dict):
                    continue
                content = q.get("content") or q.get("text") or ""
                if not content:
                    continue
                answers = q.get("answers") or q.get("posts") or []
                if not isinstance(answers, list):
                    answers = []
                ans_text = " | ".join(
                    str(a.get("content") or a.get("text") or a.get("answer") or "")[:500]
                    for a in answers if isinstance(a, dict)
                )
                results.append({
                    "_type": "review",
                    "source": "qa",
                    "review_id": str(q.get("id") or len(results)),
                    "title": content[:200],
                    "text": ans_text or content,
                    "rating": None,
                    "date": q.get("submittedDateTime") or "",
                    "trip_type": "Q&A",
                    "reviewer_name": "",
                    "helpful_votes": q.get("postCount") or 0,
                    "management_response": "",
                })
    return results


def parse_tips_from_graphql(data: list) -> list[dict]:
    """
    Extract location tips from GraphQL ids response.
    CommunityUGC__locationTips returns short tips (no rating/title).
    """
    reviews: list[dict] = []
    if not isinstance(data, list):
        return reviews
    for item in data:
        if not isinstance(item, dict):
            continue
        inner = item.get("data") or {}
        if not isinstance(inner, dict):
            continue
        tips_list = inner.get("CommunityUGC__locationTips")
        if not isinstance(tips_list, list):
            continue
        for tip_block in tips_list:
            if not isinstance(tip_block, dict):
                continue
            tips = tip_block.get("locationTips") or []
            for t in tips:
                if not isinstance(t, dict):
                    continue
                user = t.get("userProfile") or {}
                if isinstance(user, dict):
                    name = user.get("displayName") or ""
                else:
                    name = ""
                stay = t.get("stayOrVisitYearMonth") or {}
                date_str = ""
                if isinstance(stay, dict):
                    y, m = stay.get("year"), stay.get("month")
                    if y and m:
                        date_str = f"{m}/{y}"
                reviews.append({
                    "_type": "review",
                    "source": "tip",
                    "review_id": str(t.get("reviewId") or t.get("id") or ""),
                    "title": "",
                    "text": (t.get("body") or "").strip(),
                    "rating": None,
                    "date": t.get("createdAt") or date_str,
                    "trip_type": "",
                    "reviewer_name": name,
                    "helpful_votes": 0,
                    "management_response": "",
                })
    return reviews


# ══════════════════════════════════════════════════════════════════════════════
#  BROWSER CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

async def make_browser(playwright):
    """Launch Camoufox (stealth Firefox) once. Reuse for all places."""
    fp = random_fingerprint()
    Actor.log.info(
        f"  Browser: Camoufox (Firefox) | "
        f"{fp['viewport']['width']}×{fp['viewport']['height']}"
    )
    browser = await AsyncNewBrowser(
        playwright,
        headless=True,
        os="windows",
        block_webrtc=True,
    )
    return browser, fp


def _proxy_info_to_playwright(proxy_info) -> dict:
    """Convert Apify ProxyInfo to Playwright proxy dict (server, username, password)."""
    return {
        "server": f"{proxy_info.scheme}://{proxy_info.hostname}:{proxy_info.port}",
        "username": proxy_info.username or "",
        "password": proxy_info.password or "",
    }



async def make_context(browser, fp: dict, proxy_setting: Optional[dict] = None):
    """Create a new Playwright context. Camoufox handles all fingerprinting internally."""
    ctx_kwargs: dict = dict(
        viewport=fp["viewport"],
        locale="en-US",
    )
    if proxy_setting:
        ctx_kwargs["proxy"] = proxy_setting

    context = await browser.new_context(**ctx_kwargs)
    return context


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE EXTRACTION (DOM + embedded JSON)
# ══════════════════════════════════════════════════════════════════════════════

EXTRACT_PAGE_SCRIPT = """
() => {
    const result = { place: null, reviews: [] };

    // 1. JSON-LD (schema.org)
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of ldScripts) {
        try {
            const data = JSON.parse(s.textContent);
            const arr = Array.isArray(data) ? data : [data];
            for (const item of arr) {
                if (item['@type'] === 'LodgingBusiness' || item['@type'] === 'Restaurant' || item['@type'] === 'TouristAttraction') {
                    result.place = item;
                    break;
                }
            }
            if (result.place) break;
        } catch (_) {}
    }

    // 2. __NEXT_DATA__ (Next.js)
    if (!result.place) {
        const nextEl = document.getElementById('__NEXT_DATA__');
        if (nextEl) {
            try {
                const next = JSON.parse(nextEl.textContent);
                const props = next?.props?.pageProps || {};
                if (props.bulkData) result.place = props.bulkData;
                if (props.reviews) result.reviews = props.reviews;
            } catch (_) {}
        }
    }

    // 3. DOM reviews (data-reviewid, data-test-target, data-automation)
    let reviewBlocks = document.querySelectorAll('[data-reviewid]');
    if (reviewBlocks.length === 0) {
        reviewBlocks = document.querySelectorAll('[data-automation="reviewCard"]');
    }
    if (reviewBlocks.length === 0) {
        reviewBlocks = document.querySelectorAll('.review-container, .reviewSelector');
    }

    const seen = new Set();
    let idx = 0;
    for (const block of reviewBlocks) {
        const rid = block.getAttribute('data-reviewid') || ('dom_' + (idx++));
        if (seen.has(rid)) continue;
        seen.add(rid);

        const titleEl = block.querySelector('[data-test-target="review-title"] a span, [data-test-target="review-title"] span, [data-automation="reviewTitle"]');
        const textEl = block.querySelector('[data-automation="reviewText"] span, [data-test-target="review-text"] span, .review-text span, [data-automation="reviewText"]');
        const ratingEl = block.querySelector('[data-test-target="review-rating"] span, [data-automation="reviewRating"] span, .ui_bubble_rating');
        const dateEl = block.querySelector('[data-test-target="review-date"], .ratingDate');
        const nameEl = block.querySelector('.member_info .name, [data-automation="reviewerName"], .info_text div:first-child');
        const helpfulEl = block.querySelector('.helpful_count, [data-automation="helpfulVoteCount"]');
        const managementEl = block.querySelector('.mgrRsp, .managementResponse');

        let rating = null;
        if (ratingEl) {
            const cls = (ratingEl.className || '') + ' ' + (ratingEl.getAttribute('class') || '');
            const m = cls.match(/bubble_rating[^\\s]*([0-9]+)/) || cls.match(/([0-9]+)/);
            if (m) rating = parseInt(m[1], 10) / 10;
        }

        let text = '';
        if (textEl) {
            const spans = textEl.querySelectorAll ? textEl.querySelectorAll('span') : [textEl];
            for (const sp of (spans.length ? spans : [textEl])) text += (sp.textContent || '').trim();
            if (!text) text = (textEl.textContent || '').trim();
        }

        result.reviews.push({
            review_id: rid,
            title: titleEl ? (titleEl.textContent || '').trim() : '',
            text: text || (textEl ? (textEl.textContent || '').trim() : ''),
            rating: rating,
            date: dateEl ? (dateEl.textContent || '').trim() : '',
            trip_type: '',
            reviewer_name: nameEl ? (nameEl.textContent || '').trim() : '',
            helpful_votes: helpfulEl ? parseInt(helpfulEl.textContent, 10) || 0 : 0,
            management_response: managementEl ? (managementEl.textContent || '').trim() : ''
        });
    }

    return result;
}
"""


async def extract_page_data(page: Page, url: str) -> tuple[Optional[dict], list[dict]]:
    """
    Extract place info and reviews from the current page.
    Returns (place_dict, list_of_reviews).
    """
    try:
        data = await page.evaluate(EXTRACT_PAGE_SCRIPT)
    except Exception as exc:
        Actor.log.warning(f"  Page evaluate failed: {exc}")
        return None, []

    place = data.get("place")
    reviews_raw = data.get("reviews") or []

    place_obj: Optional[dict] = None
    if isinstance(place, dict):
        place_obj = parse_place_from_jsonld(place, url)
    elif place:
        place_obj = {"url": url, "name": str(place)}

    if not place_obj:
        place_obj = {"url": url, "name": "", "review_count": 0}

    reviews: list[dict] = []
    for r in reviews_raw:
        if isinstance(r, dict):
            rev = parse_review_from_dom(r)
            rev["place_url"] = url
            place_obj and rev.setdefault("place_name", place_obj.get("name", ""))
            reviews.append(rev)

    return place_obj, reviews


# ══════════════════════════════════════════════════════════════════════════════
#  DIRECT GRAPHQL: fetch tips via API (from cURL capture)
# ══════════════════════════════════════════════════════════════════════════════

# Reviews GraphQL query ID (from devtools/cURL.txt + Response.txt)
REVIEWS_QUERY_ID = "ef1a9f94012220d3"  # ReviewsProxy_getReviewListPageForLocation




async def fetch_reviews_via_graphql(
    page: Page, location_id: str, offset: int = 0, limit: int = 10,
    max_retries: int = 3,
) -> Optional[list]:
    """
    Fetch full hotel reviews via ReviewsProxy_getReviewListPageForLocation.
    Query ef1a9f94012220d3 (from devtools/cURL.txt).
    Retries up to max_retries times on transient network errors.
    """
    variables = {
        "locationId": int(location_id),
        "filters": [],  # No language filter = all languages (was ["en"] only, missing ~10–15% of reviews)
        "limit": limit,
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
    for attempt in range(1, max_retries + 1):
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
            return result
        except Exception as e:
            if attempt < max_retries:
                wait = 1.5 * attempt  # 1.5 s, 3.0 s
                Actor.log.warning(
                    f"  GraphQL fetch offset={offset} attempt {attempt}/{max_retries} failed"
                    f" ({e}) — retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
            else:
                Actor.log.warning(
                    f"  GraphQL reviews fetch failed after {max_retries} attempts"
                    f" (offset={offset}): {e}"
                )
                return None


def _extract_reviews_from_obj(obj: Any, results: list[dict]) -> None:
    """Recursively find review-like objects (have text/body + user/author)."""
    if not obj or len(results) > 5000:
        return
    if isinstance(obj, dict):
        text = obj.get("text") or obj.get("body") or obj.get("review") or ""
        if isinstance(text, dict):
            text = text.get("text") or text.get("body") or ""
        has_content = (text and len(str(text).strip()) > 20) or obj.get("title")
        user = obj.get("user") or obj.get("userProfile") or obj.get("author") or {}
        if has_content and (user or obj.get("rating") is not None or obj.get("objectId")):
            name = (
                (user.get("displayName") or user.get("name") or user.get("username") or "")
                if isinstance(user, dict) else ""
            )
            date_val = (
                obj.get("publishedDate") or obj.get("createdAt") or obj.get("date")
                or obj.get("submittedDateTime") or ""
            )
            rid = str(obj.get("id") or obj.get("reviewId") or obj.get("objectId") or len(results))
            results.append({
                "_type": "review",
                "source": "review",
                "review_id": rid,
                "title": (obj.get("title") or "").strip(),
                "text": (str(text) or "").strip()[:10000],
                "rating": float(obj["rating"]) if isinstance(obj.get("rating"), (int, float)) else None,
                "date": str(date_val)[:50] if date_val else "",
                "trip_type": (obj.get("tripType") or (obj.get("tripInfo") or {}).get("tripType") or ""),
                "reviewer_name": name,
                "helpful_votes": int(obj.get("helpfulVotes") or obj.get("helpful_votes") or 0),
                "management_response": (obj.get("managementResponse") or obj.get("management_response") or "").strip()[:2000] or "",
            })
            return  # Don't recurse into this object's children (already extracted)
        for v in obj.values():
            _extract_reviews_from_obj(v, results)
    elif isinstance(obj, list):
        for v in obj:
            _extract_reviews_from_obj(v, results)


def parse_reviews_from_graphql(data: list) -> list[dict]:
    """
    Extract main hotel reviews from GraphQL response.
    Tries known keys first, then recursively searches for review-like objects.
    """
    results: list[dict] = []
    if not isinstance(data, list):
        return results
    for item in data:
        if not isinstance(item, dict):
            continue
        inner = item.get("data") or {}
        if not isinstance(inner, dict):
            continue
        # Skip tips — CommunityUGC__locationTips are tips, not full reviews
        if "CommunityUGC__locationTips" in inner:
            continue
        # Try known keys (order matters)
        # ReviewsProxy_getReviewListPageForLocation (from devtools/Response.txt)
        reviews_proxy = inner.get("ReviewsProxy_getReviewListPageForLocation")
        if isinstance(reviews_proxy, list) and reviews_proxy:
            first = reviews_proxy[0]
            if isinstance(first, dict):
                reviews_data = first.get("reviews")
            else:
                reviews_data = None
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
                    r.get("text")
                    or r.get("body")
                    or r.get("review")
                    or dig(r, "snippets", 0, "text")
                    or ""
                )
                if not text and not r.get("title"):
                    continue
                user = r.get("user") or r.get("userProfile") or r.get("author") or {}
                if not isinstance(user, dict):
                    user = {}
                name = (
                    user.get("displayName")
                    or user.get("name")
                    or user.get("username")
                    or ""
                )
                rating = r.get("rating")
                if rating is None and isinstance(r.get("tripInfo"), dict):
                    rating = r.get("tripInfo", {}).get("rating")
                date_val = (
                    r.get("publishedDate")
                    or r.get("createdAt")
                    or r.get("date")
                    or r.get("submittedDateTime")
                    or ""
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
                place_info = {
                    "id": str(loc.get("locationId") or r.get("locationId") or ""),
                    "name": loc.get("name") or "",
                    "webUrl": "https://www.tripadvisor.com" + str(loc.get("url") or ""),
                } if isinstance(loc, dict) else {}
                results.append({
                    "id": rid,
                    "url": review_url,
                    "title": (r.get("title") or "").strip(),
                    "lang": r.get("language") or "en",
                    "originalLanguage": r.get("originalLanguage") or r.get("language") or "en",
                    "locationId": str(loc.get("locationId") or r.get("locationId") or ""),
                    "publishedDate": str(date_val)[:50] if date_val else "",
                    "publishedPlatform": r.get("publishPlatform"),
                    "rating": int(rating) if rating is not None else None,
                    "helpfulVotes": int(r.get("helpfulVotes") or r.get("helpful_votes") or 0),
                    "travelDate": travel_date,
                    "text": (text or "").strip() if isinstance(text, str) else str(text).strip(),
                    "user": {
                        "userId": user.get("id") or "",
                        "displayName": name,
                        "username": user.get("username") or "",
                        "avatar": _safe_avatar_url(user),
                        "contributions": contrib,
                    } if isinstance(user, dict) else {},
                    "ownerResponse": owner_resp,
                    "subratings": subratings,
                    "photos": photos_list,
                    "placeInfo": place_info,
                    "date": str(date_val)[:50] if date_val else "",
                })
        # Fallback: recursively search for review-like objects in the response
        if not results:
            _extract_reviews_from_obj(inner, results)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: SCRAPE ONE PLACE
# ══════════════════════════════════════════════════════════════════════════════

# Parallel GraphQL requests per batch (40 × 10 reviews = 400 per batch ≈ 10K reviews/min).
PARALLEL_REQUESTS = 40

# Batch size for pushing reviews to dataset (line ~830)
PUSH_BATCH_SIZE = 50


async def scrape_place(
    browser,
    fingerprint: dict,
    place_url: str,
    max_reviews: Optional[int],
    proxy_setting: Optional[dict],
    shared_context=None,
    start_date: Optional[str] = None,
    place_idx: int = 1,
    total_places: int = 1,
) -> tuple[Optional[dict], int]:
    """
    Scrape a single TripAdvisor place (hotel, restaurant, attraction).
    Pushes reviews to dataset in batches as they are scraped.
    Returns (place_dict, total_reviews_pushed).
    """
    place_url = normalize_place_url(place_url)
    if not place_url:
        Actor.log.warning(f"  Invalid URL: {place_url}")
        return None, 0

    if shared_context is not None:
        context = shared_context
    else:
        context = await make_context(browser, fingerprint, proxy_setting)
    page = await context.new_page()

    # Collect GraphQL responses (CommunityUGC__locationTips, etc.)
    graphql_responses: list[dict] = []

    async def on_response(response):
        try:
            url = response.url
            # Match graphql endpoints (ids or other)
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

    # Block images/fonts/media — saves 2–5 s per page; GraphQL + DOM + captcha iframe unaffected.
    # Safe with Camoufox: DataDome fingerprint check is browser-level, not network-level.
    async def _block_resources(route):
        if route.request.resource_type in ("image", "font", "media"):
            await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", _block_resources)

    # ── Captcha listener: triggers when captcha iframe appears ─
    captcha_detected = asyncio.Event()

    def _on_frame(f):
        if "captcha-delivery.com" in (f.url or "").lower():
            captcha_detected.set()

    page.on("frameattached", _on_frame)
    page.on("framenavigated", _on_frame)

    try:
        # ── Phase 1: Navigate and extract first page ───────────────────────
        # Longer timeout when using proxy — residential proxies add latency
        nav_timeout = 90_000 if proxy_setting else 45_000
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

        # Human-like pause — DataDome behavioral analysis
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Wait for EITHER captcha OR page ready (whichever first) — no fixed timeout
        tab_locator = page.get_by_role("tab", name=re.compile(r"Reviews?|Overview", re.I)).or_(
            page.locator('a:has-text("Reviews"), a:has-text("Overview")')
        ).first
        captcha_seen = False
        captcha_was_resolved = False  # captcha appeared but Camoufox auto-resolved it
        captcha_task = asyncio.create_task(captcha_detected.wait())
        page_task = asyncio.create_task(
            tab_locator.wait_for(state="visible", timeout=60_000 if proxy_setting else 30_000)
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
        if captcha_detected.is_set():
            captcha_seen = True
            Actor.log.info("  Captcha detected — checking if Camoufox resolves it …")
        else:
            Actor.log.info("  Page ready — continuing")

        # Check for existing captcha (iframe may have loaded before listener)
        if not captcha_seen:
            for frame in page.frames:
                if frame != page.main_frame and "captcha-delivery.com" in (frame.url or "").lower():
                    captcha_seen = True
                    break

        # Poll every second for up to 8s — gives Camoufox time to pass DataDome check.
        # Some proxy IPs need slightly longer; exit early as soon as the iframe disappears.
        if captcha_seen:
            captcha_resolved = False
            for _ in range(8):
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
                Actor.log.warning("  Captcha not resolved after 8s — signalling for proxy rotation retry")
                raise CaptchaBlockedError("DataDome captcha not bypassed with current proxy")

        # ── Page ready ───────────────────────────────────────────────────────
        # When captcha appeared, page_task was cancelled before tab visibility was
        # confirmed. Re-wait for it now that Camoufox has passed the check.
        if captcha_was_resolved:
            try:
                await tab_locator.wait_for(state="visible", timeout=15_000)
            except Exception:
                pass  # Tab click will handle any remaining delay
        # If no captcha: tab was already confirmed visible by the race above.
        await asyncio.sleep(0.3)

        # ── Consent / cookie banner ───────────────────────────────────────
        consent_selectors = [
            'button:has-text("Accept")',
            'button:has-text("Accept All")',
            'button:has-text("I Accept")',
            'button:has-text("Accept all")',
            '[data-testid="accept-cookies"]',
            '#onetrust-accept-btn-handler',
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

        # ── Click "Reviews" tab first (triggers main reviews load, per cURL_reviews.txt) ─
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

        # ── Minimal scroll (GraphQL fetches reviews directly; scroll was for lazy-load) ─
        await page.evaluate("window.scrollBy(0, 400)")
        await asyncio.sleep(random.uniform(0.5, 1.0))

        landed_url = page.url
        landed_title = await page.title()
        Actor.log.info(f"  Page loaded: {landed_title}")

        if "tripadvisor.com" not in landed_url:
            Actor.log.warning(f"  Redirected away from TripAdvisor — skipping")
            return None, 0

        Actor.log.info(f"  Captured {len(graphql_responses)} GraphQL response(s)")

        place_obj, _ = await extract_page_data(page, landed_url)
        reviews: list[dict] = []
        seen_ids: set[str] = set()
        total_pushed = 0
        oldest_date = ""
        start_ts = (start_date.strip()[:10] if start_date and start_date.strip() else "") or ""
        # Cap at TripAdvisor's reported count to avoid API returning extra/padded items
        page_review_count = (
            place_obj.get("review_count") or place_obj.get("reviewCount") or 0
        ) if place_obj else 0

        async def _push_batch(batch: list[dict]) -> None:
            nonlocal total_pushed, oldest_date
            if page_review_count and total_pushed + len(batch) > page_review_count:
                batch = batch[: page_review_count - total_pushed]
            if not batch:
                return
            for item in batch:
                await Actor.push_data(item)
            total_pushed += len(batch)
            if batch:
                batch_dates = [
                    (r.get("date") or r.get("publishedDate") or "")[:10]
                    for r in batch
                    if (r.get("date") or r.get("publishedDate") or "")
                ]
                batch_oldest = min(batch_dates) if batch_dates else ""
                if batch_oldest and (not oldest_date or batch_oldest < oldest_date):
                    oldest_date = batch_oldest
            cap = (min(max_reviews, page_review_count) if max_reviews and page_review_count
                   else (max_reviews or page_review_count or 0))
            cap_str = f"/{cap:,}" if cap else ""
            Actor.log.info(
                f"  Pushed batch: {len(batch)} reviews | "
                f"Place {place_idx}/{total_places} | {total_pushed:,}{cap_str} reviews"
            )
            await Actor.set_status_message(
                f"Place {place_idx}/{total_places} | {total_pushed:,}{cap_str} reviews"
            )

        # ── Direct GraphQL fetch for reviews (parallel batches of PARALLEL_REQUESTS) ─
        loc_id = extract_location_id_from_url(landed_url)
        if loc_id:
            reviews_per_page = 10
            reviews_offset = 0
            while True:
                if max_reviews and total_pushed + len(reviews) >= max_reviews:
                    break
                if page_review_count and total_pushed + len(reviews) >= page_review_count:
                    break
                # Fetch PARALLEL_REQUESTS offsets in parallel
                batch_offsets = [
                    reviews_offset + i * reviews_per_page
                    for i in range(PARALLEL_REQUESTS)
                ]
                if max_reviews:
                    batch_offsets = [o for o in batch_offsets if o < max_reviews]
                if not batch_offsets:
                    break
                tasks = [
                    fetch_reviews_via_graphql(page, loc_id, offset=o, limit=reviews_per_page)
                    for o in batch_offsets
                ]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                got_any = False
                got_partial = False
                for i, resp in enumerate(batch_results):
                    if isinstance(resp, Exception):
                        Actor.log.warning(f"  GraphQL fetch offset={batch_offsets[i]} failed: {resp}")
                        continue
                    if not resp:
                        continue
                    extracted = parse_reviews_from_graphql(
                        resp if isinstance(resp, list) else [resp]
                    )
                    if extracted:
                        got_any = True
                    for t in extracted:
                        rid = str(t.get("id") or t.get("review_id") or "")
                        if rid and rid not in seen_ids:
                            seen_ids.add(rid)
                            t["place_url"] = landed_url
                            place_name = (place_obj.get("name", "") if place_obj else "") or (t.get("placeInfo") or {}).get("name", "")
                            t["name"] = place_name
                            if start_ts and ((t.get("date") or t.get("publishedDate") or "")[:10] or "9999") < start_ts:
                                continue
                            reviews.append(t)
                    if len(extracted) < reviews_per_page:
                        got_partial = True
                        Actor.log.debug(f"  Partial response at offset {batch_offsets[i]}: {len(extracted)} reviews")
                    # Don't break inner loop — process remaining responses in this batch
                if not got_any or got_partial:
                    if got_partial:
                        Actor.log.info(f"  Reached end of reviews at offset ~{reviews_offset} (last batch had partial)")
                    break
                reviews_offset += PARALLEL_REQUESTS * reviews_per_page
                if max_reviews and reviews_offset >= max_reviews:
                    break

                # Sort and push full batches immediately
                reviews.sort(key=_date_sort_key, reverse=True)
                if max_reviews:
                    reviews = reviews[: max_reviews - total_pushed]
                if page_review_count:
                    reviews = reviews[: page_review_count - total_pushed]
                while len(reviews) >= PUSH_BATCH_SIZE:
                    batch = reviews[:PUSH_BATCH_SIZE]
                    reviews = reviews[PUSH_BATCH_SIZE:]
                    await _push_batch(batch)
                await asyncio.sleep(random.uniform(0.8, 1.5))  # Human-like pacing (crawlerbros pattern)

        if not place_obj:
            place_obj = {"url": landed_url, "name": "", "review_count": 0}
        if not place_obj.get("name") and reviews:
            place_obj["name"] = (reviews[0].get("placeInfo") or {}).get("name", "") or ""

        # Sort and push remaining reviews
        reviews.sort(key=_date_sort_key, reverse=True)
        if max_reviews:
            reviews = reviews[: max_reviews - total_pushed]
        if page_review_count:
            reviews = reviews[: page_review_count - total_pushed]
        if reviews:
            await _push_batch(reviews)

        if total_pushed == 0:
            Actor.log.warning(
                "  No reviews captured. TripAdvisor may be blocking. "
                "Try enabling Apify Residential Proxy."
            )

        if place_obj:
            place_obj["id"] = place_obj.get("id") or loc_id or ""
            place_obj["reviewCount"] = total_pushed
            place_obj["oldestDate"] = oldest_date
            place_obj["error"] = None

        Actor.log.info(f"  Done: {total_pushed} reviews scraped")
        return place_obj, total_pushed

    except CaptchaBlockedError:
        raise  # propagate to caller for proxy rotation retry

    except Exception as exc:
        Actor.log.warning(f"  Unexpected error: {exc}")
        err_place = {"url": place_url, "name": "", "reviewCount": 0, "oldestDate": "", "error": str(exc)}
        return err_place, 0

    finally:
        await page.close()
        if shared_context is None:
            await context.close()


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

        raw_urls = actor_input.get("startUrls") or actor_input.get("start_urls") or []
        max_reviews: Optional[int] = actor_input.get("maxReviewsPerPlace")
        start_date: Optional[str] = actor_input.get("startDate") or ""
        proxy_input = actor_input.get("proxyConfiguration")

        INTER_PLACE_DELAY = 2.0

        if not raw_urls:
            Actor.log.warning("Input field 'startUrls' is empty — nothing to scrape.")
            await Actor.set_status_message("No URLs provided. Add place URLs to the input.")
            return

        Actor.log.info(f"Places to scrape: {len(raw_urls)}")
        Actor.log.info(f"Max reviews/place: {max_reviews or 'unlimited'}")
        if start_date:
            Actor.log.info(f"Start date filter: {start_date}")
        Actor.log.info(f"Inter-place delay: {INTER_PLACE_DELAY}s (+ 0–1s jitter)")

        proxy_configuration = None
        if proxy_input:
            proxy_configuration = await Actor.create_proxy_configuration(
                actor_proxy_input=proxy_input
            )
            Actor.log.info("Proxy configuration loaded.")
        else:
            Actor.log.warning(
                "No proxy configured. On Apify Cloud, datacenter IPs are blocked by DataDome "
                "regardless of browser fingerprint — enable Residential Proxy in the input."
            )

        await Actor.set_status_message(
            f"Starting — {len(raw_urls)} place(s) to process …"
        )

        proxy_groups = (proxy_input or {}).get("apifyProxyGroups") or []
        is_residential = any("RESIDENTIAL" in (g or "").upper() for g in proxy_groups)
        # Retry (proxy rotation) only helps with residential proxies.
        # Datacenter IPs are always blocked by DataDome — fail fast instead of wasting time.
        MAX_CAPTCHA_RETRIES = 3 if is_residential else 1

        total_places = 0
        total_reviews = 0
        all_places: list[dict] = []

        async with async_playwright() as pw:
            browser, fingerprint = await make_browser(pw)
            shared_ctx = None
            try:
                for idx, entry in enumerate(raw_urls, 1):
                    url = (
                        entry.get("url") if isinstance(entry, dict) else entry
                    )
                    if not url or not str(url).strip():
                        Actor.log.warning(f"Skipping empty entry at index {idx}.")
                        continue

                    place_url = normalize_place_url(str(url))
                    if not place_url:
                        Actor.log.warning(f"Invalid URL at index {idx}: {url}")
                        continue

                    Actor.log.info(f"[{idx}/{len(raw_urls)}] Processing: {place_url[:70]}...")

                    place_obj, pushed = None, 0
                    loc_id_for_session = extract_location_id_from_url(place_url) or f"place_{idx}"

                    for captcha_attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
                        proxy_setting: Optional[dict] = None
                        if proxy_configuration:
                            session_id = (
                                re.sub(r"[^\w]", "_", loc_id_for_session)[:45]
                                + (f"_r{captcha_attempt}" if captcha_attempt > 1 else "")
                            )
                            proxy_info = await proxy_configuration.new_proxy_info(
                                session_id=session_id
                            )
                            if proxy_info:
                                proxy_setting = _proxy_info_to_playwright(proxy_info)
                                proxy_type_str = ", ".join(proxy_groups) if proxy_groups else "proxy"
                                attempt_str = (
                                    f" (attempt {captcha_attempt}/{MAX_CAPTCHA_RETRIES})"
                                    if MAX_CAPTCHA_RETRIES > 1 else ""
                                )
                                Actor.log.info(f"  proxy={proxy_type_str}, session={session_id}{attempt_str}")

                        # Reuse one context when no proxy — keeps one window, avoids new window per place
                        if shared_ctx is None and not proxy_setting:
                            shared_ctx = await make_context(browser, fingerprint, None)

                        try:
                            place_obj, pushed = await scrape_place(
                                browser, fingerprint, place_url, max_reviews, proxy_setting,
                                shared_context=shared_ctx if not proxy_setting else None,
                                start_date=start_date or None,
                                place_idx=idx,
                                total_places=len(raw_urls),
                            )
                            break  # success — exit retry loop

                        except CaptchaBlockedError:
                            can_retry = is_residential and captcha_attempt < MAX_CAPTCHA_RETRIES
                            if can_retry:
                                backoff = 3 * (2 ** (captcha_attempt - 1)) + random.uniform(0.5, 1.5)
                                Actor.log.warning(
                                    f"  Captcha blocked on attempt {captcha_attempt}/{MAX_CAPTCHA_RETRIES}"
                                    f" — rotating proxy, retry in {backoff:.1f}s …"
                                )
                                await asyncio.sleep(backoff)
                            else:
                                if is_residential:
                                    fail_msg = (
                                        f"Blocked by DataDome after {captcha_attempt} attempt(s) "
                                        "with Residential Proxy — IP pool may be temporarily flagged. "
                                        "Please try again later."
                                    )
                                else:
                                    fail_msg = (
                                        "Blocked by DataDome captcha — Residential Proxy is required. "
                                        "Select RESIDENTIAL under Proxy configuration in the Actor input."
                                    )
                                Actor.log.error(f"  {fail_msg}")
                                await Actor.fail(status_message=fail_msg)
                                return

                    if place_obj:
                        all_places.append(place_obj)
                        total_places += 1

                    total_reviews += pushed

                    if idx < len(raw_urls):
                        jitter = random.uniform(0.0, 1.0)
                        delay = INTER_PLACE_DELAY + jitter
                        Actor.log.info(f"  Inter-place delay: {delay:.1f}s …")
                        await asyncio.sleep(delay)
            finally:
                if shared_ctx:
                    await shared_ctx.close()
                await browser.close()

        if all_places:
            await Actor.set_value("places", all_places)
            await Actor.set_value(
                "places_md", _build_places_md(all_places), content_type="text/markdown"
            )
            Actor.log.info(
                f"  Saved {len(all_places)} place(s) to key-value store "
                "(keys: 'places' JSON, 'places_md' Markdown)"
            )

        final_msg = (
            f"Finished — "
            f"{total_places} place(s) and "
            f"{total_reviews} review(s) pushed to dataset."
        )
        await Actor.set_status_message(final_msg)
        Actor.log.info(final_msg)


if __name__ == "__main__":
    asyncio.run(main())
