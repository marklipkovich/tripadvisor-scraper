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

        # ── Fallback: use GraphQL responses if DOM returned nothing ───────
        if not reviews and graphql_responses:
            seen_ids: set[str] = set()
            all_keys: set[str] = set()
            for resp in graphql_responses:
                data = resp if isinstance(resp, list) else [resp]
                for item in data if isinstance(data, list) else [data]:
                    if isinstance(item, dict):
                        all_keys.update((item.get("data") or {}).keys())
                tips = parse_tips_from_graphql(data)
                if not tips:
                    tips = parse_qa_from_graphql(data)
                for t in tips:
                    rid = t.get("review_id") or ("qa_" + str(len(reviews)))
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        t["place_url"] = landed_url
                        t.setdefault("place_name", place_obj.get("name", "") if place_obj else "")
                        reviews.append(t)
                if tips:
                    Actor.log.info(f"  Extracted {len(tips)} tips/Q&A from GraphQL")
            if not reviews and all_keys:
                Actor.log.info(f"  GraphQL keys (no tips/Q&A): {sorted(all_keys)[:10]}")

        # Ensure all reviews have place_url and place_name
        for r in reviews:
            r.setdefault("place_url", landed_url)
            r.setdefault("place_name", place_obj.get("name", "") if place_obj else "")

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
