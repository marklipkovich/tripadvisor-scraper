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
  • Chrome fingerprint rotation — randomises UA, viewport, Sec-Ch-Ua
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
from patchright.async_api import async_playwright, Page


# ══════════════════════════════════════════════════════════════════════════════
#  CHROME FINGERPRINT ROTATION
# ══════════════════════════════════════════════════════════════════════════════

CHROME_VERSIONS: list[tuple[str, str]] = [
    ("131", '"Chromium";v="131","Google Chrome";v="131","Not-A.Brand";v="24"'),
    ("133", '"Chromium";v="133","Google Chrome";v="133","Not-A.Brand";v="24"'),
    ("136", '"Chromium";v="136","Google Chrome";v="136","Not-A.Brand";v="24"'),
    ("142", '"Chromium";v="142","Google Chrome";v="142","Not-A.Brand";v="24"'),
]

OS_PROFILES: list[tuple[str, str]] = [
    ("Windows NT 10.0; Win64; x64", '"Windows"'),
    ("Windows NT 10.0; Win64; x64", '"Windows"'),
    ("Macintosh; Intel Mac OS X 10_15_7", '"macOS"'),
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]


def random_fingerprint() -> dict:
    """Return a randomised but internally-consistent browser fingerprint."""
    ver, sec_ch = random.choice(CHROME_VERSIONS)
    ua_os, platform = random.choice(OS_PROFILES)
    return {
        "user_agent": (
            f"Mozilla/5.0 ({ua_os}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{ver}.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": sec_ch,
        "sec_ch_ua_platform": platform,
        "viewport": random.choice(VIEWPORTS),
        "chrome_version": ver,
    }


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
    raw = (item.get("date") or "").strip()
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


def _log_graphql_keys(resp: Any, label: str) -> None:
    """Log top-level keys from GraphQL response for debugging parser."""
    try:
        data = resp if isinstance(resp, list) else [resp]
        for i, item in enumerate(data):
            if isinstance(item, dict):
                inner = item.get("data") or {}
                if isinstance(inner, dict) and inner:
                    keys = sorted(inner.keys())
                    Actor.log.info(f"  [{label}] response[{i}] data keys: {keys}")
    except Exception:
        pass


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
        "_type": "place",
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

async def make_context(playwright, proxy_url: Optional[str] = None):
    """Launch Playwright Chromium with randomised fingerprint."""
    fp = random_fingerprint()
    Actor.log.info(
        f"  Fingerprint: Chrome/{fp['chrome_version']} | "
        f"{fp['viewport']['width']}×{fp['viewport']['height']}"
    )

    browser = await playwright.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--disable-gpu",
            f"--user-agent={fp['user_agent']}",
        ],
    )

    ctx_kwargs: dict = dict(
        viewport=fp["viewport"],
        user_agent=fp["user_agent"],
        locale="en-US",
        color_scheme="light",
        bypass_csp=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": fp["sec_ch_ua"],
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": fp["sec_ch_ua_platform"],
        },
    )
    if proxy_url:
        ctx_kwargs["proxy"] = {"server": proxy_url}

    context = await browser.new_context(**ctx_kwargs)

    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        window.chrome = { runtime: {} };
    """)

    return browser, context


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
        place_obj = {"_type": "place", "url": url, "name": str(place)}

    if not place_obj:
        place_obj = {"_type": "place", "url": url, "name": "", "review_count": 0}

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

TIPS_QUERY_ID = "13fbbde7cccdbabc"
# Full hotel reviews API (from devtools/cURL.txt, Response.txt)
REVIEWS_QUERY_ID = "ef1a9f94012220d3"  # ReviewsProxy_getReviewListPageForLocation


async def _get_user_id_from_page(page: Page) -> Optional[str]:
    """Extract userId from page cookies (OptanonConsent consentId) if present."""
    try:
        cookies = await page.context.cookies()
        for c in cookies:
            if c.get("name") == "OptanonConsent" and c.get("value"):
                # consentId=9F1729085B4241C14CB39B92E93A7D4F
                m = re.search(r"consentId=([A-F0-9]+)", c["value"], re.I)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


async def fetch_tips_via_graphql(
    page: Page, location_id: str, offset: int = 0, limit: int = 10
) -> Optional[list]:
    """
    Fetch location tips via GraphQL from within the browser session.
    Matches cURL: locationId, offset, limit, language, useTaql, optional userId.
    """
    variables: dict = {
        "locationId": int(location_id),
        "offset": offset,
        "limit": limit,
        "language": "en",
        "useTaql": True,
    }
    user_id = await _get_user_id_from_page(page)
    if user_id:
        variables["userId"] = user_id
    payload = [
        {"variables": variables, "extensions": {"preRegisteredQueryId": TIPS_QUERY_ID}}
    ]
    url = "https://www.tripadvisor.com/data/graphql/ids"
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
        Actor.log.warning(f"  GraphQL tips fetch failed: {e}")
        return None


async def fetch_reviews_via_graphql(
    page: Page, location_id: str, offset: int = 0, limit: int = 10
) -> Optional[list]:
    """
    Fetch full hotel reviews via ReviewsProxy_getReviewListPageForLocation.
    Query ef1a9f94012220d3 (from devtools/cURL.txt).
    """
    variables = {
        "locationId": int(location_id),
        "filters": [{"axis": "LANGUAGE", "selections": ["en"]}],
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
        Actor.log.warning(f"  GraphQL reviews fetch failed: {e}")
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
                name = (
                    user.get("displayName")
                    or user.get("name")
                    or user.get("username")
                    or ""
                ) if isinstance(user, dict) else ""
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
                results.append({
                    "_type": "review",
                    "source": "review",
                    "review_id": rid,
                    "title": (r.get("title") or "").strip(),
                    "text": (text or "").strip() if isinstance(text, str) else str(text).strip(),
                    "rating": float(rating) if rating is not None else None,
                    "date": str(date_val)[:50] if date_val else "",
                    "trip_type": (r.get("tripType") or (r.get("tripInfo") or {}).get("tripType") or ""),
                    "reviewer_name": name,
                "helpful_votes": int(r.get("helpfulVotes") or r.get("helpful_votes") or 0),
                "management_response": (
                    (r.get("mgmtResponse") or {}).get("text")
                    or r.get("managementResponse")
                    or r.get("management_response")
                    or ""
                ).strip()[:2000] or "",
                })
        # Fallback: recursively search for review-like objects in the response
        if not results:
            _extract_reviews_from_obj(inner, results)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  PAGINATION: build URL with offset
# ══════════════════════════════════════════════════════════════════════════════

def build_pagination_url(base_url: str, offset: int) -> str:
    """
    TripAdvisor uses -Reviews-or{offset}- (e.g. or5, or10, or15).
    First page has no offset; or5 = page 2, or10 = page 3, etc.
    """
    if offset <= 0:
        return base_url
    if "-Reviews-" in base_url:
        return re.sub(r"-Reviews-", f"-Reviews-or{offset}-", base_url)
    return base_url


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: SCRAPE ONE PLACE
# ══════════════════════════════════════════════════════════════════════════════

REVIEWS_PER_PAGE = 10


async def scrape_place(
    playwright,
    place_url: str,
    max_reviews: Optional[int],
    proxy_url: Optional[str],
) -> tuple[Optional[dict], list[dict]]:
    """
    Scrape a single TripAdvisor place (hotel, restaurant, attraction).
    Returns (place_dict, list_of_review_dicts).
    """
    place_url = normalize_place_url(place_url)
    if not place_url:
        Actor.log.warning(f"  Invalid URL: {place_url}")
        return None, []

    Actor.log.info(f"  Place URL: {place_url}")
    await Actor.set_status_message(f"Loading {place_url} …")

    browser, context = await make_context(playwright, proxy_url)
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

    try:
        # ── Phase 1: Navigate and extract first page ───────────────────────
        Actor.log.info(f"  Navigating to {place_url}")
        try:
            await with_retry(
                lambda: page.goto(place_url, wait_until="networkidle", timeout=60_000),
                label=f"goto {place_url[:60]}",
            )
        except Exception:
            await with_retry(
                lambda: page.goto(place_url, wait_until="load", timeout=45_000),
                label=f"goto fallback {place_url[:50]}",
            )

        await asyncio.sleep(random.uniform(3.0, 5.0))

        # ── Wait for captcha to be resolved (if present) ───────────────────
        try:
            captcha = page.get_by_text("Verification Required")
            if await captcha.is_visible(timeout=2000):
                Actor.log.info("  Captcha detected — waiting up to 90s for manual solve (headed mode)")
                await captcha.wait_for(state="hidden", timeout=90_000)
                await asyncio.sleep(2.0)
        except Exception:
            pass

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
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    Actor.log.info(f"  Clicked consent: {sel[:40]}...")
                    await asyncio.sleep(1.0)
                    break
            except Exception:
                pass

        # ── Click "Reviews" tab first (triggers main reviews load, per cURL_reviews.txt) ─
        for tab_text in ["Reviews", "Review"]:
            try:
                tab = page.get_by_role("tab", name=tab_text).or_(page.locator(f'a:has-text("{tab_text}")'))
                if await tab.first.is_visible(timeout=2000):
                    await tab.first.click()
                    Actor.log.info(f"  Clicked '{tab_text}' tab")
                    await asyncio.sleep(random.uniform(2.0, 3.0))
                    break
            except Exception:
                pass

        # ── Click "Traveler tips" tab to trigger CommunityUGC__locationTips ─
        for tab_text in ["Traveler tips", "Traveller tips", "Tips"]:
            try:
                tab = page.get_by_role("tab", name=tab_text).or_(page.locator(f'a:has-text("{tab_text}")'))
                if await tab.first.is_visible(timeout=2000):
                    await tab.first.click()
                    Actor.log.info(f"  Clicked '{tab_text}' tab")
                    await asyncio.sleep(random.uniform(2.0, 3.0))
                    break
            except Exception:
                pass

        # ── Scroll to trigger lazy-loaded reviews/tips ─────────────────────
        for _ in range(8):
            await page.evaluate("window.scrollBy(0, 500)")
            await asyncio.sleep(random.uniform(1.2, 2.0))
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(2.5, 4.0))
        await page.evaluate("window.scrollBy(0, -2000)")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        landed_url = page.url
        landed_title = await page.title()
        Actor.log.info(f"  Landed on: {landed_url[:80]}...")
        Actor.log.info(f"  Page title: {landed_title}")

        if "tripadvisor.com" not in landed_url:
            Actor.log.warning(f"  Redirected away from TripAdvisor — skipping")
            return None, []

        Actor.log.info(f"  Captured {len(graphql_responses)} GraphQL response(s)")

        place_obj, reviews = await extract_page_data(page, landed_url)

        # ── Direct GraphQL fetch for main reviews (paginated: offset 0, 10, 20, …) ─
        loc_id = extract_location_id_from_url(landed_url)
        if loc_id:
            reviews_per_page = 10
            reviews_offset = 0
            while True:
                if max_reviews and len(reviews) >= max_reviews:
                    break
                reviews_resp = await fetch_reviews_via_graphql(
                    page, loc_id, offset=reviews_offset, limit=reviews_per_page
                )
                if not reviews_resp:
                    break
                main_reviews = parse_reviews_from_graphql(
                    reviews_resp if isinstance(reviews_resp, list) else [reviews_resp]
                )
                if not main_reviews:
                    # Debug: log response keys so we can fix the parser
                    _log_graphql_keys(reviews_resp, "reviews")
                    break
                graphql_responses.append(reviews_resp)
                Actor.log.info(
                    f"  Fetched reviews via GraphQL (offset={reviews_offset}): {len(main_reviews)} items"
                )
                reviews_offset += reviews_per_page
                if len(main_reviews) < reviews_per_page:
                    break
                if max_reviews and reviews_offset >= max_reviews:
                    break
                await asyncio.sleep(random.uniform(0.5, 1.0))

        # ── Direct GraphQL fetch for tips (paginated: offset 0, 20, 40, …) ─
        loc_id = extract_location_id_from_url(landed_url)
        if loc_id:
            tips_per_page = 20
            tips_offset = 0
            while True:
                tips_resp = await fetch_tips_via_graphql(
                    page, loc_id, offset=tips_offset, limit=tips_per_page
                )
                if not tips_resp:
                    break
                tips = parse_tips_from_graphql(
                    tips_resp if isinstance(tips_resp, list) else [tips_resp]
                )
                if not tips:
                    break
                graphql_responses.append(tips_resp)
                Actor.log.info(
                    f"  Fetched tips via GraphQL (offset={tips_offset}): {len(tips)} items"
                )
                tips_offset += tips_per_page
                if len(tips) < tips_per_page:
                    break
                if max_reviews and tips_offset >= max_reviews:
                    break
                await asyncio.sleep(random.uniform(0.5, 1.0))

        # ── Parse GraphQL responses (reviews + tips + Q&A) ───────────────────
        if graphql_responses:
            seen_ids: set[str] = set()
            all_keys: set[str] = set()
            for resp in graphql_responses:
                data = resp if isinstance(resp, list) else [resp]
                for item in data if isinstance(data, list) else [data]:
                    if isinstance(item, dict):
                        all_keys.update((item.get("data") or {}).keys())
                # Try main reviews first, then tips, then Q&A
                extracted = parse_reviews_from_graphql(data)
                if not extracted:
                    extracted = parse_tips_from_graphql(data)
                if not extracted:
                    extracted = parse_qa_from_graphql(data)
                for t in extracted:
                    rid = t.get("review_id") or ("qa_" + str(len(reviews)))
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        t["place_url"] = landed_url
                        t.setdefault("place_name", place_obj.get("name", "") if place_obj else "")
                        reviews.append(t)
                if extracted:
                    Actor.log.info(f"  Extracted {len(extracted)} reviews/tips/Q&A from GraphQL")
            if not reviews and all_keys:
                Actor.log.info(f"  GraphQL keys (no reviews/tips/Q&A): {sorted(all_keys)[:15]}")

        # Ensure all items have place_url, place_name, and source
        for r in reviews:
            r.setdefault("place_url", landed_url)
            r.setdefault("place_name", place_obj.get("name", "") if place_obj else "")
            r.setdefault("source", "review")

        if not place_obj:
            place_obj = {"_type": "place", "url": landed_url, "name": "", "review_count": 0}

        total_reviews = place_obj.get("review_count") or 0
        if not total_reviews and reviews:
            total_reviews = len(reviews)

        Actor.log.info(
            f"  Place: {place_obj.get('name', '')!r} | "
            f"Rating: {place_obj.get('rating', '')} | "
            f"Page 1: {len(reviews)} reviews"
        )
        if not reviews and not graphql_responses:
            Actor.log.warning(
                "  No reviews or GraphQL data captured. TripAdvisor may be blocking automated access. "
                "Try enabling Apify Residential Proxy in the input."
            )

        # ── Phase 2: Pagination ───────────────────────────────────────────
        pages_to_fetch = max(1, (total_reviews + REVIEWS_PER_PAGE - 1) // REVIEWS_PER_PAGE)
        if max_reviews and reviews:
            pages_to_fetch = min(
                pages_to_fetch,
                (max_reviews + REVIEWS_PER_PAGE - 1) // REVIEWS_PER_PAGE,
            )

        for page_num in range(2, pages_to_fetch + 1):
            if max_reviews and len(reviews) >= max_reviews:
                Actor.log.info(f"  Reached maxReviewsPerPlace={max_reviews} — stopping.")
                break

            offset = REVIEWS_PER_PAGE * (page_num - 1)
            paginated_url = build_pagination_url(place_url, offset)

            await Actor.set_status_message(
                f"{place_obj.get('name', '')[:30]}: page {page_num}/{pages_to_fetch} …"
            )

            await asyncio.sleep(random.uniform(0.3, 0.8))

            try:
                await with_retry(
                    lambda: page.goto(paginated_url, wait_until="load", timeout=30_000),
                    label=f"page {page_num}",
                )
            except Exception as exc:
                Actor.log.warning(f"  Page {page_num} failed: {exc} — stopping pagination")
                break

            await asyncio.sleep(random.uniform(0.5, 1.0))

            _, page_reviews = await extract_page_data(page, landed_url)
            if not page_reviews:
                Actor.log.info(f"  Page {page_num}: no reviews — done.")
                break

            for r in page_reviews:
                r["place_url"] = landed_url
                r.setdefault("place_name", place_obj.get("name", ""))
                reviews.append(r)

            Actor.log.info(
                f"  Page {page_num}/{pages_to_fetch}: +{len(page_reviews)} reviews "
                f"(total: {len(reviews)})"
            )

        # Sort by date (newest first)
        reviews.sort(key=_date_sort_key, reverse=True)

        if max_reviews:
            reviews = reviews[:max_reviews]

        Actor.log.info(f"  Done: {len(reviews)} reviews scraped")
        return place_obj, reviews

    except Exception as exc:
        Actor.log.warning(f"  Unexpected error: {exc}")
        return None, []

    finally:
        await page.close()
        await context.close()
        await browser.close()


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
        proxy_input = actor_input.get("proxyConfiguration")

        INTER_PLACE_DELAY = 2.0

        if not raw_urls:
            Actor.log.warning("Input field 'startUrls' is empty — nothing to scrape.")
            await Actor.set_status_message("No URLs provided. Add place URLs to the input.")
            return

        Actor.log.info(f"Places to scrape: {len(raw_urls)}")
        Actor.log.info(f"Max reviews/place: {max_reviews or 'unlimited'}")
        Actor.log.info(f"Inter-place delay: {INTER_PLACE_DELAY}s (+ 0–1s jitter)")

        proxy_configuration = None
        if proxy_input:
            proxy_configuration = await Actor.create_proxy_configuration(
                actor_proxy_input=proxy_input
            )
            Actor.log.info("Proxy configuration loaded.")
        else:
            Actor.log.info(
                "No proxy configured — running without proxy. "
                "Consider enabling Apify Residential Proxies for high-volume runs."
            )

        await Actor.set_status_message(
            f"Starting — {len(raw_urls)} place(s) to process …"
        )

        total_places = 0
        total_reviews = 0

        async with async_playwright() as pw:
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

                proxy_url: Optional[str] = None
                if proxy_configuration:
                    loc_id = extract_location_id_from_url(place_url) or f"place_{idx}"
                    proxy_url = await proxy_configuration.new_url(
                        session_id=re.sub(r"[^\w]", "_", loc_id)
                    )

                place_obj, reviews = await scrape_place(
                    pw, place_url, max_reviews, proxy_url
                )

                if place_obj:
                    await Actor.push_data(place_obj)
                    total_places += 1

                for review in reviews:
                    await Actor.push_data(review)
                total_reviews += len(reviews)

                Actor.log.info(
                    f"  Pushed: {len(reviews)} reviews "
                    f"(totals: {total_places} places, {total_reviews} reviews)"
                )

                if idx < len(raw_urls):
                    jitter = random.uniform(0.0, 1.0)
                    delay = INTER_PLACE_DELAY + jitter
                    Actor.log.info(f"  Inter-place delay: {delay:.1f}s …")
                    await asyncio.sleep(delay)

        final_msg = (
            f"Finished — "
            f"{total_places} place(s) and "
            f"{total_reviews} review(s) pushed to dataset."
        )
        await Actor.set_status_message(final_msg)
        Actor.log.info(final_msg)


if __name__ == "__main__":
    asyncio.run(main())
