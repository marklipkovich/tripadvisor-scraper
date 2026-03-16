"""
Trustpilot Reviews Scraper — Apify Actor
══════════════════════════════════════════════════════════════════════════════

Strategy (100% JSON — zero CSS selectors / DOM parsing):

  Phase 1 · Page 1 navigation
    • patchright stealth Chromium loads /review/{domain}
    • Clears Cloudflare's JS challenge automatically (real browser fingerprint)
    • Reads  window.__NEXT_DATA__  injected by Trustpilot's Next.js frontend
      → business profile, page-1 reviews, total page count, buildId

  Phase 2 · Pagination via browser-internal fetch
    • All pages 2..N are fetched by calling fetch() INSIDE the browser:
        page.evaluate("fetch('/_next/data/{buildId}/review/{domain}.json?page=N')")
    • Requests run within the already-authenticated browser session
      → Cloudflare cf_clearance cookies are automatically included
      → ~50 ms per page vs ~3 s for a full navigation + DOM render
    • Pure JSON response — no selectors, no scraping breakage

Anti-blocking stack:
  • patchright         — patched Chromium that passes Cloudflare bot-management
  • Chrome version rotation   — randomises UA across Chrome 122–126 + Win/Mac
  • Viewport randomisation    — 1366×768 / 1440×900 / 1920×1080
  • One proxy session per domain — Cloudflare cf_clearance is IP-tied; rotating
    mid-session would invalidate it.  New domain → new proxy session.
  • Exponential backoff retry — 3 attempts with 2s → 4s → 8s delays
  • Random inter-page delay  — 200–800 ms  (between JSON fetches)
  • Random inter-domain delay — configurable (default 2 s + 0–1 s jitter)

Limitation:
  • Trustpilot's _next/data endpoint returns empty for page 11+ (10-page / 200-review
    cap on the frontend API). To get more reviews, use Trustpilot's official API or
    a scraper that uses alternative endpoints.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Optional
from urllib.parse import urlparse

from apify import Actor
from patchright.async_api import async_playwright, Page


# ══════════════════════════════════════════════════════════════════════════════
#  CHROME FINGERPRINT ROTATION
# ══════════════════════════════════════════════════════════════════════════════

# (version_string, matching Sec-Ch-Ua header) — use current Chrome versions
# to avoid looking like an outdated/automated browser (2025)
CHROME_VERSIONS: list[tuple[str, str]] = [
    ("131", '"Chromium";v="131","Google Chrome";v="131","Not-A.Brand";v="24"'),
    ("133", '"Chromium";v="133","Google Chrome";v="133","Not-A.Brand";v="24"'),
    ("136", '"Chromium";v="136","Google Chrome";v="136","Not-A.Brand";v="24"'),
    ("142", '"Chromium";v="142","Google Chrome";v="142","Not-A.Brand";v="24"'),
]

# (ua_os_string, Sec-Ch-Ua-Platform)  — Windows weighted 2× (more common)
OS_PROFILES: list[tuple[str, str]] = [
    ("Windows NT 10.0; Win64; x64",        '"Windows"'),
    ("Windows NT 10.0; Win64; x64",        '"Windows"'),
    ("Macintosh; Intel Mac OS X 10_15_7",  '"macOS"'),
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
        "sec_ch_ua":          sec_ch,
        "sec_ch_ua_platform": platform,
        "viewport":           random.choice(VIEWPORTS),
        "chrome_version":     ver,
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
    """
    Call coro_factory() up to max_retries times.
    Delays follow exponential backoff: 2s → 4s → 8s + small random jitter.
    """
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


def normalize_domain(raw: str) -> str:
    """
    Accept any of these formats and return a bare domain:
      "amazon.com"
      "www.amazon.com"
      "https://www.trustpilot.com/review/amazon.com"
      "https://amazon.com"
    """
    raw = raw.strip()
    if "trustpilot.com/review/" in raw:
        return raw.split("trustpilot.com/review/")[1].strip("/").lower()
    if raw.startswith("http"):
        return urlparse(raw).netloc.removeprefix("www.").lower().strip("/")
    return raw.removeprefix("www.").lower().strip("/")


def parse_business(page_props: dict, domain: str) -> dict:
    """Extract the business profile fields from Next.js pageProps."""
    bu_raw = page_props.get("businessUnit")
    bu     = bu_raw if isinstance(bu_raw, dict) else {}

    # numberOfReviews can be a dict of star breakdowns OR a plain integer
    nr_raw = bu.get("numberOfReviews")
    nr     = nr_raw if isinstance(nr_raw, dict) else {}
    total_reviews = (
        nr.get("total") or nr.get("totalCount")
        if nr else (nr_raw if isinstance(nr_raw, int) else None)
    )

    cats_raw = bu.get("categories")
    cats     = cats_raw if isinstance(cats_raw, list) else []

    loc_raw = bu.get("location")
    loc     = loc_raw if isinstance(loc_raw, dict) else {}

    return {
        "_type":          "business",
        "domain":         domain,
        "trustpilot_url": f"https://www.trustpilot.com/review/{domain}",
        "business_name":  bu.get("displayName") or bu.get("name") or "",
        "website": (
            bu.get("websiteUrl") or dig(bu, "website", "url", default="")
        ),
        "trust_score":    bu.get("trustScore"),
        "stars":          bu.get("stars"),
        "total_reviews":  total_reviews,
        "reviews_5star":  nr.get("fiveStars")  or nr.get("5"),
        "reviews_4star":  nr.get("fourStars")  or nr.get("4"),
        "reviews_3star":  nr.get("threeStars") or nr.get("3"),
        "reviews_2star":  nr.get("twoStars")   or nr.get("2"),
        "reviews_1star":  nr.get("oneStar")    or nr.get("1"),
        "categories": ", ".join(
            c.get("categoryEnglishName", "") for c in cats
            if isinstance(c, dict)
        ),
        "country":    loc.get("country") or "",
        "city":       loc.get("city") or "",
        "is_claimed": bu.get("isClaimed") or bu.get("isClaimedProfile"),
        "about":      bu.get("aboutUs") or bu.get("description") or "",
    }


def parse_review(review: dict, business_name: str, domain: str) -> dict:
    """
    Extract all available fields from a single Trustpilot review object.
    Uses multiple fallback keys because field names have changed across
    Trustpilot's Next.js deployments.
    """
    consumer = review.get("consumer")
    consumer = consumer if isinstance(consumer, dict) else {}

    dates = review.get("dates")
    dates = dates if isinstance(dates, dict) else {}

    # Trustpilot renamed companyReply → reply in recent versions
    reply = review.get("reply") or review.get("companyReply")
    reply = reply if isinstance(reply, dict) else {}

    # Verification: try reviewVerification dict, fall back to top-level fields
    verification = review.get("reviewVerification")
    verification = verification if isinstance(verification, dict) else {}

    # source field: either a string directly or nested in verification
    source = (
        review.get("source")
        or verification.get("reviewSourceName")
        or ""
    )
    if isinstance(source, dict):
        source = source.get("name") or source.get("type") or str(source)

    return {
        "_type":         "review",
        # Business reference
        "domain":         domain,
        "business_name":  business_name,
        # Review core
        "review_id":  review.get("id") or "",
        "stars":      review.get("rating") or review.get("stars"),  # rating is current key
        "title":      review.get("title") or "",
        "text":       review.get("text") or review.get("body") or "",
        "language":   review.get("language") or "",
        # Dates
        "published_at": (
            dates.get("publishedDate") or review.get("createdAt") or ""
        ),
        "experience_date": (
            dates.get("experiencedDate") or review.get("experiencedAt") or ""
        ),
        # Reviewer — fields not visible in the UI
        "reviewer_id":            consumer.get("id") or "",
        "reviewer_name":          consumer.get("displayName") or "",
        "reviewer_country":       consumer.get("countryCode") or "",
        "reviewer_total_reviews": consumer.get("numberOfReviews"),
        # Verification — hidden metadata
        "is_verified": (
            verification.get("isVerified") or review.get("isVerified") or False
        ),
        "verification_level": verification.get("verificationLevel") or "",
        "source": source,
        # Engagement
        "likes": review.get("likes") or 0,
        # Company reply
        "company_reply":      reply.get("message") or reply.get("text") or "",
        "company_reply_date": reply.get("publishedDate") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STEALTH BROWSER FACTORY
# ══════════════════════════════════════════════════════════════════════════════

async def make_context(playwright, proxy_url: Optional[str] = None):
    """
    Launch a patchright Chromium instance with a randomised fingerprint.
    patchright patches binary-level automation flags that Cloudflare checks —
    no channel='chrome' needed (and it wouldn't work inside Docker anyway).
    """
    fp = random_fingerprint()
    Actor.log.info(
        f"  Fingerprint: Chrome/{fp['chrome_version']} | "
        f"{fp['viewport']['width']}×{fp['viewport']['height']} | "
        f"Platform: {fp['sec_ch_ua_platform']}"
    )

    browser = await playwright.chromium.launch(
        headless=True,
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
            "Accept-Language":    "en-US,en;q=0.9",
            "Accept-Encoding":    "gzip, deflate, br",
            "Sec-Ch-Ua":          fp["sec_ch_ua"],
            "Sec-Ch-Ua-Mobile":   "?0",
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
#  PAGINATION: fetch JSON from inside the authenticated browser session
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_page_json(
    page: Page, build_id: str, domain: str, page_num: int
) -> Optional[dict]:
    """
    Call Trustpilot's Next.js data endpoint from WITHIN the browser context.
    Because the request runs inside the Cloudflare-cleared session,
    cf_clearance and session cookies are included automatically — no extra
    auth headers needed.

    Endpoint: /_next/data/{buildId}/review/{domain}.json?page=N
    Returns the pageProps dict, or None on any error.
    """
    url         = (
        f"https://www.trustpilot.com/_next/data/{build_id}"
        f"/review/{domain}.json?page={page_num}"
    )
    safe_url    = url.replace("'", "\\'")
    safe_domain = domain.replace("'", "\\'")

    result = await page.evaluate(f"""
        async () => {{
            try {{
                const resp = await fetch('{safe_url}', {{
                    method:      'GET',
                    credentials: 'include',
                    headers: {{
                        'Accept':        'application/json',
                        'Referer':       'https://www.trustpilot.com/review/{safe_domain}',
                        'X-Nextjs-Data': '1',
                    }},
                }});
                if (!resp.ok) {{
                    return {{ __status: resp.status, __error: resp.statusText }};
                }}
                return await resp.json();
            }} catch (e) {{
                return {{ __error: String(e) }};
            }}
        }}
    """)

    if not result:
        Actor.log.warning(f"    Page {page_num}: empty response from /_next/data/")
        return None

    if "__error" in result:
        Actor.log.warning(
            f"    Page {page_num}: fetch error "
            f"(HTTP {result.get('__status', '?')}) — {result['__error']}"
        )
        return None

    # /_next/data/ wraps page props at the top level (no outer "props" key)
    return result.get("pageProps") or result


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: SCRAPE ONE BUSINESS DOMAIN
# ══════════════════════════════════════════════════════════════════════════════

async def scrape_domain(
    playwright,
    domain: str,
    max_reviews: Optional[int],
    proxy_url: Optional[str],
) -> tuple[Optional[dict], list[dict]]:
    """
    Scrape a single Trustpilot business.
    Returns (business_dict, list_of_review_dicts).
    Never raises — a failed domain returns (None, []).
    """
    domain = normalize_domain(domain)
    tp_url = f"https://www.trustpilot.com/review/{domain}"

    Actor.log.info(f"  Domain: {domain}")
    await Actor.set_status_message(f"Loading {domain} …")

    browser, context = await make_context(playwright, proxy_url)
    page = await context.new_page()

    try:
        # ── Phase 1: Navigate and extract __NEXT_DATA__ ───────────────────
        Actor.log.info(f"  Navigating to {tp_url}")
        await with_retry(
            lambda: page. goto(
                tp_url, wait_until="domcontentloaded", timeout=30_000
            ),
            label=f"goto {domain}",
        )

        # Let Cloudflare JS challenge complete if it was triggered
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Log actual landing URL and title — helps diagnose blocks/redirects
        landed_url = page.url
        landed_title = await page.title()
        Actor.log.info(f"  Landed on: {landed_url}")
        Actor.log.info(f"  Page title: {landed_title}")

        # Trustpilot may redirect (e.g. amazon.com → www.amazon.com).
        # The /_next/data/ API uses the domain from the actual URL.
        if "trustpilot.com/review/" in landed_url:
            effective_domain = landed_url.split("trustpilot.com/review/")[1].strip("/").split("?")[0]
        else:
            effective_domain = domain
        if effective_domain != domain:
            Actor.log.info(f"  Using effective domain for API: {effective_domain}")

        # Read __NEXT_DATA__ directly from the <script id="__NEXT_DATA__"> tag.
        # Avoids the browser named-property trap where window.__NEXT_DATA__
        # resolves to the DOM element itself instead of its parsed content.
        next_data = await page.evaluate("""
            () => {
                try {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (!el) return null;
                    return JSON.parse(el.textContent);
                } catch (e) {
                    return null;
                }
            }
        """)

        if not next_data:
            await Actor.set_status_message(f"⚠️ Blocked for {domain} — try later")
            if "challenges.cloudflare" in landed_url or "just a moment" in landed_title.lower():
                Actor.log.warning(
                    f"  Cloudflare block detected. Try again in a few hours or "
                    "enable Residential Proxies in the input."
                )
            else:
                Actor.log.warning(
                    f"  No __NEXT_DATA__ found — business may not exist on "
                    "Trustpilot or page failed to load. Check the domain."
                )
            return None, []

        if not isinstance(next_data, dict):
            Actor.log.warning(
                f"  __NEXT_DATA__ has unexpected type {type(next_data).__name__} — skipping"
            )
            return None, []

        build_id = next_data.get("buildId")
        if not build_id:
            Actor.log.warning(f"  buildId missing in __NEXT_DATA__ for {domain}")
            return None, []

        Actor.log.info(f"  buildId: {build_id}")

        page_props = dig(next_data, "props", "pageProps") or {}
        if not page_props:
            Actor.log.warning(f"  Empty pageProps for {domain} — cannot extract data")
            return None, []

        # Business profile
        business = parse_business(page_props, domain)
        Actor.log.info(
            f"  Business: {business['business_name']!r} | "
            f"Score: {business['trust_score']} ★{business['stars']} | "
            f"Total reviews: {business['total_reviews']}"
        )

        # Page-1 reviews
        reviews_raw    = page_props.get("reviews")
        raw_reviews_p1 = reviews_raw if isinstance(reviews_raw, list) else []

        pagination_raw = page_props.get("pagination")
        if isinstance(pagination_raw, dict):
            total_pages = (
                pagination_raw.get("totalPages")
                or pagination_raw.get("pageCount")
                or 1
            )
        elif isinstance(pagination_raw, int):
            total_pages = pagination_raw
        else:
            # pagination absent — calculate from total review count
            per_page         = len(raw_reviews_p1) or 20
            total_rev_count  = business.get("total_reviews") or 0
            total_pages      = max(1, -(-total_rev_count // per_page))  # ceiling div

        # Cap total_pages to honour max_reviews limit
        if max_reviews and raw_reviews_p1:
            per_page    = len(raw_reviews_p1)
            total_pages = min(total_pages, -(-max_reviews // per_page))  # ceiling div

        Actor.log.info(
            f"  Pagination: page 1 of {total_pages} "
            f"({len(raw_reviews_p1)} reviews on page 1)"
        )

        reviews: list[dict] = [
            parse_review(r, business["business_name"], domain)
            for r in raw_reviews_p1
        ]

        # ── Phase 2: Remaining pages via parallel batch fetch ──────────────
        # Fetch up to 4 pages at a time (async) — much faster than sequential
        BATCH_SIZE = 4
        for batch_start in range(2, total_pages + 1, BATCH_SIZE):
            if max_reviews and len(reviews) >= max_reviews:
                Actor.log.info(f"  Reached maxReviewsPerBusiness={max_reviews} — stopping.")
                break

            batch_end = min(batch_start + BATCH_SIZE, total_pages + 1)
            page_nums = list(range(batch_start, batch_end))
            await Actor.set_status_message(
                f"{domain}: pages {batch_start}-{batch_end - 1}/{total_pages} …"
            )

            async def fetch_page(pn: int):
                return await with_retry(
                    lambda: fetch_page_json(page, build_id, effective_domain, pn),
                    label=f"{domain} page {pn}",
                )

            results = await asyncio.gather(*[fetch_page(pn) for pn in page_nums])

            for pn, page_props_n in zip(page_nums, results):
                if not page_props_n:
                    Actor.log.warning(f"  Page {pn}: no data — stopping pagination")
                    break
                raw_page: list = page_props_n.get("reviews") or []
                if not raw_page:
                    Actor.log.info(
                        f"  Page {pn}: empty reviews list — done. "
                        f"(Trustpilot limits _next/data to ~10 pages / 200 reviews)"
                    )
                    break
                for r in raw_page:
                    reviews.append(parse_review(r, business["business_name"], domain))
                Actor.log.info(
                    f"  Page {pn}/{total_pages}: +{len(raw_page)} reviews "
                    f"(total: {len(reviews)})"
                )
            else:
                # No break — add short delay before next batch
                if batch_end <= total_pages:
                    await asyncio.sleep(random.uniform(0.15, 0.4))
                continue
            break

        if max_reviews:
            reviews = reviews[:max_reviews]

        Actor.log.info(f"  Done: {len(reviews)} reviews scraped for {domain}")
        return business, reviews

    except Exception as exc:
        Actor.log.warning(f"  Unexpected error for {domain}: {exc}")
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
        actor_input = await Actor.get_input() or {}

        # ── Parse & validate input ────────────────────────────────────────
        raw_domains: list          = actor_input.get("domains") or []
        max_reviews: Optional[int] = actor_input.get("maxReviewsPerBusiness")
        proxy_input                = actor_input.get("proxyConfig")

        # Fixed inter-domain delay — 2s + 0–1s jitter (no user input)
        INTER_DOMAIN_DELAY = 2.0

        if not raw_domains:
            Actor.log.warning("Input field 'domains' is empty — nothing to scrape.")
            await Actor.set_status_message("No domains provided. Add domains to the input.")
            return

        Actor.log.info(f"Domains to scrape: {len(raw_domains)}")
        Actor.log.info(f"Max reviews/domain: {max_reviews or 'unlimited'}")
        Actor.log.info(f"Inter-domain delay: {INTER_DOMAIN_DELAY}s (+ 0–1s jitter)")

        # ── Proxy configuration ───────────────────────────────────────────
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
            f"Starting — {len(raw_domains)} domain(s) to process …"
        )

        total_businesses = 0
        total_reviews    = 0

        async with async_playwright() as pw:
            for idx, domain_entry in enumerate(raw_domains, 1):

                # Accept plain strings or {"domain": "..."} / {"url": "..."} dicts
                raw = (
                    domain_entry
                    if isinstance(domain_entry, str)
                    else (
                        domain_entry.get("domain")
                        or domain_entry.get("url")
                        or ""
                    )
                )
                if not raw.strip():
                    Actor.log.warning(f"Skipping empty entry at index {idx}.")
                    continue

                domain = normalize_domain(raw)
                Actor.log.info(f"[{idx}/{len(raw_domains)}] Processing: {domain}")

                # One proxy session per domain — same IP for all pages of this domain.
                # Cloudflare cf_clearance is tied to the originating IP, so we must
                # NOT rotate the proxy mid-session or the cookie becomes invalid.
                proxy_url: Optional[str] = None
                if proxy_configuration:
                    session_id = re.sub(r"[^\w]", "_", domain)
                    proxy_url  = await proxy_configuration.new_url(
                        session_id=session_id
                    )
                    Actor.log.info(f"  Proxy session: {session_id}")

                business, reviews = await scrape_domain(
                    pw, domain, max_reviews, proxy_url
                )

                if business:
                    await Actor.push_data(business)
                    total_businesses += 1

                for review in reviews:
                    await Actor.push_data(review)
                total_reviews += len(reviews)

                pushed_biz = "1 business" if business else "0 businesses"
                Actor.log.info(
                    f"  Pushed: {pushed_biz} + {len(reviews)} reviews "
                    f"(totals so far: {total_businesses} businesses, "
                    f"{total_reviews} reviews)"
                )

                # Inter-domain delay — allows Cloudflare session to settle
                if idx < len(raw_domains):
                    jitter = random.uniform(0.0, 1.0)
                    delay = INTER_DOMAIN_DELAY + jitter
                    Actor.log.info(
                        f"  Inter-domain delay: {INTER_DOMAIN_DELAY}s "
                        f"(+ {jitter:.1f}s jitter) — waiting {delay:.1f}s …"
                    )
                    await asyncio.sleep(delay)

        final_msg = (
            f"Finished — "
            f"{total_businesses} business profile(s) and "
            f"{total_reviews} review(s) pushed to dataset."
        )
        await Actor.set_status_message(final_msg)
        Actor.log.info(final_msg)


if __name__ == "__main__":
    asyncio.run(main())