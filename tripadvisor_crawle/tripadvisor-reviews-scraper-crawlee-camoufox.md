---
slug: tripadvisor-reviews-scraper-python-crawlee-camoufox
title: How to scrape TripAdvisor reviews with Python, Crawlee, and Camoufox
description: Scrape TripAdvisor reviews with Python, Crawlee, and Camoufox. Bypass DataDome using stealth Firefox, GeoIP matching, and parallel GraphQL fetching.
authors:
  - name: Mark
    title: Apify community developer specializing in high-fidelity data extraction for ML/AI training, automation, and data analysis. Published scrapers on the Apify Store include this Actor and a YouTube Transcript Scraper, with more extraction tools in development.
tags: [community]
---

:::note
One of our community members wrote this guide as a contribution to the Crawlee Blog. If you'd like to contribute articles like these, please reach out to us on [Apify's Discord channel](https://discord.com/invite/jyEM2PRvMU).
:::

This article walks through how I built an [Apify Actor](https://apify.com/store) that scrapes [TripAdvisor](https://www.tripadvisor.com/) for reviews and place metadata, the problems I hit ([DataDome](https://datadome.co/), sessions, proxies), and how I improved performance and solved blocking using [Crawlee](https://crawlee.dev/python), [Camoufox](https://camoufox.com/), and TripAdvisor’s GraphQL API.

**In this article:**

- [Prerequisites](#prerequisites)
- [What the Actor does](#what-the-actor-does)
- [1. DOM inspection with DevTools](#1-dom-inspection-with-devtools)
- [2. Run code locally](#2-run-code-locally)
- [3. Move to Camoufox](#3-move-to-camoufox)
- [4. Move to Apify Cloud](#4-move-to-apify-cloud)
- [5. Output samples](#5-output-samples)
- [6. Performance optimisation](#6-performance-optimisation)
- [7. Anti-blocking measures](#7-anti-blocking-measures)
- [Conclusion](#conclusion)

## Prerequisites

Before you follow along, you should have:

- **Python** — 3.10+ recommended (the reference Actor uses **3.12** in its Docker image). Comfortable with **`async`/`await`** helps.
- **Python packages** — [Crawlee for Python](https://crawlee.dev/python) with **Playwright**, the [Apify SDK for Python](https://docs.apify.com/sdk/python), **Camoufox** (with the **`geoip`** extra for proxy-aligned fingerprinting), plus **httpx** and **typing-extensions**. Pin roughly like this:

```text
apify ~= 3.3.0
camoufox[geoip] ~= 0.4.11
crawlee[playwright] ~= 1.5.0
httpx ~= 0.28.1
typing-extensions ~= 4.15.0
```

Install in a virtual environment:

```bash
pip install "apify~=3.3.0" "camoufox[geoip]~=0.4.11" "crawlee[playwright]~=1.5.0" "httpx~=0.28.1" "typing-extensions~=4.15.0"
python -m camoufox fetch
```

- **Browser DevTools** — **Chrome** or **Edge** (F12 → **Network**). You will inspect **Fetch/XHR** and copy requests as **cURL** before writing scraping code.
- **TripAdvisor in the browser** — a normal place URL on `tripadvisor.com` to reproduce the network calls (hotel, restaurant, or attraction).
- **Apify Cloud runs** — an [Apify account](https://apify.com/) and [Apify Proxy](https://docs.apify.com/platform/proxy) with a **residential** group for anything beyond quick local experiments; DataDome typically blocks **datacenter** IPs.
- **Optional** — [Apify CLI](https://docs.apify.com/cli) (`apify run`, `apify push`) if you mirror the Actor layout.

After hitting walls with standard headless Chromium approaches, I ended up with a solution built around Crawlee, a custom Camoufox browser plugin, and TripAdvisor's internal [GraphQL](https://graphql.org) API. The rest of the article walks through what I built, what broke, and what I’d do differently next time.

## What the Actor does

An Apify Actor that accepts one or more TripAdvisor place URLs (hotels, restaurants, attractions) and produces two outputs:

- **Places dataset** — metadata: name, rating, address, total review count, price range, image URL
- **Reviews dataset** — individual reviews: title, text, rating, date, traveler type, reviewer name, helpful votes, management response

The Actor uses Crawlee's [`PlaywrightCrawler`](https://crawlee.dev/python/docs/guides/playwright-crawler) with a custom `CamoufoxPlugin` to launch Camoufox (a fingerprint-evasion Firefox fork) instead of standard Playwright browsers. Reviews are fetched directly from TripAdvisor's internal [GraphQL](https://graphql.org) API using 50 parallel `asyncio.gather()` calls per pagination batch — fast, structured, and not dependent on DOM layout.

### How it works — step by step

1. **Load the place page** — the browser opens the TripAdvisor place URL (`page.goto()`). During page load, TripAdvisor makes its own GraphQL calls (place info, first reviews, etc.). A `page.on("response", ...)` listener captures those JSON responses automatically.
2. **Wait for the page and handle captcha** — the Actor waits for the Reviews section to appear. If [DataDome](https://datadome.co/) shows a captcha, it polls until the check self-resolves (Camoufox's fingerprint passes most of the time) or raises a block error.
3. **Click the Reviews tab** — a tab click switches the page into review-listing mode.
4. **Fetch reviews via the GraphQL API** — reads the numeric place ID from the URL (e.g. `d264936` → `264936`), then fires **50 parallel requests** using JavaScript inside the browser (`page.evaluate()`) at different offsets (0, 10, 20, …, 490). Each request returns 10 reviews in JSON. Running inside the browser context means cookies — including DataDome session cookies — are sent automatically with no manual auth.
5. **Parse** — each JSON response is parsed into review objects (text, rating, date, reviewer, trip type, helpful votes, management response, etc.).
6. **Push to dataset** — review objects are pushed to the Apify dataset in batches.
7. **Repeat** — the offset advances and the next batch fires until the desired total is reached or the API returns fewer than 10 reviews (end of available reviews for that place).

**Why fetch from the GraphQL API instead of parsing HTML:**

| Advantage | Detail |
|-----------|--------|
| **Clean data** | JSON is structured; no fragile CSS selectors needed |
| **Performance** | 50 concurrent requests per batch vs. scrolling and parsing DOM page by page |
| **Lower load** | Only review JSON is fetched — no need to load or scroll full HTML pages |
| **Stability** | Less dependent on DOM structure; TripAdvisor can restyle the page without breaking the API |
| **Scalability** | No hard cap from HTML pagination; can reach thousands of reviews per place |


## 1. DOM inspection with DevTools

Before touching a keyboard, I spent 20 minutes in DevTools on a TripAdvisor hotel page.

Here's exactly what to do:

1. Open a TripAdvisor place page — for example: [The Waterfront Hotel, Sliema](https://www.tripadvisor.com/Hotel_Review-g190327-d264939-Reviews-The_Waterfront_Hotel-Sliema_Island_of_Malta.html)
2. Open DevTools (F12) → **Network** tab → select **Fetch/XHR**
3. Type `graphql` in the filter box — this shows only calls to `https://www.tripadvisor.com/data/graphql/ids`
4. Check **Preserve log** and **Disable cache**
5. Scroll down to the reviews section on the page
6. Click the **Size** column to sort descending — the reviews response is usually the largest

![DevTools Network tab showing TripAdvisor GraphQL requests filtered by "graphql"](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/devtools-tripadvisor-network-requests.png)

7. Click any large GraphQL request in the list. In the **Response** tab you'll see review text matching what's on screen — "Very nice hotel…" — confirming this is the right endpoint.

![DevTools Response tab showing TripAdvisor GraphQL review JSON data](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/devtools-tripadvisor-network-response.png)

8. Open the **Payload**, **Response**, and **Preview** sub-tabs to see the JSON and copy the contents into text files for your notes
9. Right-click the request → **Copy → Copy as cURL (bash)**. The cURL command includes the URL, headers, and **Payload** (JSON body).

With `Payload.txt`, `Response.txt`, `cURL.txt`, and `Headers.txt` saved from DevTools, I have everything needed to build the code.

### Payload

TripAdvisor uses a `preRegisteredQueryId` pattern — one endpoint that handles multiple operation types based on an opaque ID. After inspecting several requests, the one that returns reviews is:

**Query ID:** `ef1a9f94012220d3`

:::note
There are several query IDs on this endpoint. `ef1a9f94012220d3` is the reviews query.
:::

The payload captured from DevTools:

```json
[
  {
    "variables": {
      "locationId": 264936,
      "filters": [
        { "axis": "LANGUAGE", "selections": ["en"] }
      ],
      "limit": 10,
      "offset": 10,
      "sortType": null,
      "sortBy": "SERVER_DETERMINED",
      "language": "en",
      "doMachineTranslation": true,
      "photosPerReviewLimit": 3
    },
    "extensions": {
      "preRegisteredQueryId": "ef1a9f94012220d3"
    }
  }
]
```

Key fields:
- `locationId` — the numeric place ID extracted from the URL (e.g. `d264936` → `264936`)
- `limit: 10` — always 10 reviews per request
- `offset` — pagination cursor (0, 10, 20, …)
- `sortBy: "SERVER_DETERMINED"` — TripAdvisor's default sort order

The Python function that builds this payload:

```python
async def fetch_reviews_via_graphql(
    page: Page, location_id: str, offset: int = 0, limit: int = 10
) -> list[dict]:
    
    """Fetch one page of reviews via TripAdvisor GraphQL API from within the browser page."""
    
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
    # ... the payload is then passed to page.evaluate() — see § cURL command and endpoint
```

### Response structure

The response key is `ReviewsProxy_getReviewListPageForLocation`:

- **Response key:** `ReviewsProxy_getReviewListPageForLocation` (not `CommunityUGC__locationTips` or Q&A keys)
- **Structure:** `data.ReviewsProxy_getReviewListPageForLocation[0]` → `reviews[]`, `totalCount`
- **Review fields:** `id`, `title`, `text`, `rating`, `publishedDate`, `userProfile.displayName`, `tripInfo.tripType`, `helpfulVotes`, `mgmtResponse.text` (can be `null`)

A sample of the raw GraphQL response (from `response.json`), trimmed of internal GraphQL metadata:

```json
[
  {
    "data": {
      "ReviewsProxy_getReviewListPageForLocation": [
        {
          "preferredReviewIds": [],
          "totalCount": 1004,
          "reviews": [
            {
              "id": 1044599990,
              "status": "PUBLISHED",
              "reviewDetailPageWrapper": {
                "reviewDetailPageRoute": {
                  "url": "/ShowUserReviews-g190327-d264936-r1044599990-1926_Le_Soleil_Hotel_Spa-Sliema_Island_of_Malta.html"
                }
              },
              "location": {
                "locationId": 264936,
                "name": "1926 Le Soleil Hotel & Spa",
                "url": "/Hotel_Review-g190327-d264936-Reviews-1926_Le_Soleil_Hotel_Spa-Sliema_Island_of_Malta.html",
                "placeType": "ACCOMMODATION",
                "accommodationCategory": "HOTEL"
              },
              "createdDate": "2026-01-02",
              "publishedDate": "2026-01-08",
              "userProfile": {
                "id": "19EFB5A940FE5D400677CA9E15FF6C87",
                "displayName": "Valentina",
                "username": "Valentinamatisse",
                "contributionCounts": {
                  "sumAllUgc": 6
                }
              },
              "rating": 5,
              "title": "Pleasant experience and professional staff",
              "language": "en",
              "originalLanguage": "it",
              "text": "Very nice hotel with a lot of potential but the services offered have reserved us some surprises. The location is very nice, the deluxe rooms are quite large and the breakfast is good...",
              "helpfulVotes": 0,
              "mgmtResponse": {
                "id": 1048923007,
                "text": "Dear Valentina,\n\nThank you for taking the time to share your detailed feedback regarding your recent stay at 1926 Le Soleil Hotel & Spa. We are delighted to learn that you appreciated our beautiful location...",
                "language": "en",
                "publishedDate": "2026-02-08",
                "connectionToSubject": "Owner",
                "userProfile": {
                  "displayName": "Guest Care Team"
                }
              }
            ...
```

The response parsing first locates the review list — TripAdvisor occasionally changes which sub-key it appears under — then extracts each field with fallbacks for alternative key names:

```python
# inner = response[0]["data"]  —  top-level dict from the GraphQL response
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
if reviews_data is not None:
    if isinstance(reviews_data, dict):
        reviews_list = (
            reviews_data.get("reviews")
            or reviews_data.get("reviewList")
            or reviews_data.get("socialObjects")
            or []
        )
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
            "review_id": rid,
            "title": (r.get("title") or "").strip(),
            "text": (text or "").strip() if isinstance(text, str) else str(text).strip(),
            "rating": float(rating) if rating is not None else None,
            "date": str(date_val)[:50] if date_val else "",
            "trip_type": (r.get("tripType") or (r.get("tripInfo") or {}).get("tripType") or ""),
            "reviewer_name": name,
            "helpful_votes": int(r.get("helpfulVotes") or r.get("helpful_votes") or 0),
            "management_response": (r.get("mgmtResponse") or {}).get("text"),
        })
```

### cURL command and endpoint

Right-clicking the request and choosing **Copy → Copy as cURL** gives the full picture — endpoint, all headers, and payload in one command. The key details confirmed:

```bash
curl 'https://www.tripadvisor.com/data/graphql/ids' \
  -H 'accept: */*' \
  -H 'accept-language: en-US,en;q=0.9' \
  -H 'cache-control: no-cache' \
  -H 'content-type: application/json' \
  -H 'origin: https://www.tripadvisor.com' \
  -H 'pragma: no-cache' \
  -H 'referer: https://www.tripadvisor.com/Hotel_Review-g190327-d264936-Reviews-1926_Le_Soleil_Hotel_Spa-Sliema_Island_of_Malta.html' \
  --data-raw '[{"variables":{...},"extensions":{"preRegisteredQueryId":"ef1a9f94012220d3"}}]'
```

In the Actor, I replicate this from within the browser using `page.evaluate()`. The Python call wrapping the JavaScript `fetch()`:

```python
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
except Exception as e:
    Actor.log.error(f"GraphQL fetch failed: {e}")
    result = None
```

### Request headers

The required headers captured from DevTools (`headers.txt`):

```
:authority: www.tripadvisor.com
:method: POST
:path: /data/graphql/ids
content-type: application/json
origin: https://www.tripadvisor.com
referer: https://www.tripadvisor.com/Hotel_Review-g190327-d264936-Reviews-1926_Le_Soleil_Hotel_Spa-Sliema_Island_of_Malta.html
```

In the `page.evaluate()` call, `credentials: 'include'` ensures the browser's session cookies are sent automatically, and `Referer: window.location.href` mirrors the current page URL — matching what the DevTools headers show.

This is the core technique. TripAdvisor's GraphQL API validates session cookies and CSRF state, so calling it directly from Python with `httpx` or `requests` doesn't work reliably. Instead, I run a `fetch()` call from inside the Playwright page using `page.evaluate()`. This inherits all the browser's cookies — including any DataDome approval cookies — with no manual authentication needed.

The request runs in JavaScript inside the browser context. Clean, structured JSON comes back directly. This pattern works for any site where the API relies on session cookies that are difficult to replicate externally.

## 2. Run code locally

For local runs, temporarily set `headless=False` to watch the browser:

```python
browser = await AsyncNewBrowser(
    headless=False,
    ...
)
```

With `headless=False`, the browser window stays visible during the run, which helps with debugging — you can watch pages load, see captchas appear, and follow each navigation step. Switch it back to `headless=True` when testing is done.

For local runs, use `INPUT.json` at `storage/key_value_stores/default/INPUT.json`:

```json
{
  "startUrls": [
    { "url": "https://www.tripadvisor.com/Hotel_Review-g190327-d264936-Reviews-1926_Le_Soleil_Hotel_Spa-Sliema_Island_of_Malta.html" },
    { "url": "https://www.tripadvisor.com/Hotel_Review-g190327-d264939-Reviews-The_Waterfront_Hotel-Sliema_Island_of_Malta.html" }
  ],
  "maxReviewsPerPlace": 300,
  "proxyConfiguration": { "useApifyProxy": false }
}
```

Run with:

```bash
apify run
```


### Parallel GraphQL pagination

Each request returns exactly 10 reviews. To scrape 500 reviews efficiently, I run batches of 50 concurrent requests using `asyncio.gather()`:

```python
PARALLEL_REQUESTS = 50
reviews_per_page = 10
reviews_offset = 0

while True:
    # Offsets for this batch: 0, 10, 20, …, 490 when PARALLEL_REQUESTS == 50
    batch_offsets = [
        reviews_offset + i * reviews_per_page
        for i in range(PARALLEL_REQUESTS)
    ]
    if max_reviews:
        batch_offsets = [o for o in batch_offsets if o < max_reviews]
    if not batch_offsets:
        break

    # One async coroutine per offset (each calls page.evaluate → fetch in the browser)
    tasks = [
        fetch_reviews_via_graphql(
            page, loc_id, offset=o, limit=reviews_per_page,
            rating_filters=rating_filters,
            language_filter=language_filter,
        )
        for o in batch_offsets
    ]

    # Run all tasks concurrently
    batch_results = await asyncio.gather(*tasks, return_exceptions=True)

    if not got_any or got_partial:
        break
    reviews_offset += PARALLEL_REQUESTS * reviews_per_page
    await asyncio.sleep(random.uniform(0.8, 1.5))
```

I tested 40, 50, 60, and 100 parallel requests. Beyond 50, runtime didn't improve and blocking probability increased. 50 is the practical sweet spot for this endpoint.

## 3. Move to Camoufox

My first implementation used standard Playwright with Chromium and Patchright (a stealth-patched Chromium fork). On the first local run, I got this:

![DataDome captcha slider appearing with Chromium headless browser](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/captcha-tripadvisor-chromium.png)

DataDome detected the headless Chromium fingerprint. I could solve it locally with a quick slider automation (simplified code):

```python
# Find slider, drag to the right
slider = page.locator('[data-testid="slider"]')
box = await slider.bounding_box()
await page.mouse.move(box['x'], box['y'] + box['height'] / 2)
await page.mouse.down()
await page.mouse.move(box['x'] + 300, box['y'] + box['height'] / 2, steps=20)
await page.mouse.up()
```

But that doesn't scale to Apify Cloud. Even with every stealth patch applied, headless Chromium consistently failed DataDome's fingerprint checks.

[Camoufox](https://camoufox.com/) is a Firefox fork built specifically for fingerprint evasion. Unlike stealth patches applied on top of Chromium, it modifies Firefox at the binary level. It spoofs:

- WebGL vendor and renderer
- Canvas fingerprint
- AudioContext
- Fonts
- Timezone and locale
- Navigator properties (platform, plugins, hardware concurrency)

The basic Camoufox setup:

```python
from playwright.async_api import async_playwright
from camoufox import AsyncNewBrowser

async def run():
    async with async_playwright() as pw:
        browser = await AsyncNewBrowser(
            pw,
            headless=True,          # False locally; True on Apify Cloud
            os="windows",
            block_webrtc=True,
            locale="en-US",
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        # … scrape …
        await context.close()
        await browser.close()
```

After switching to Camoufox, the captcha stopped appearing on most requests. When it did appear, Camoufox's fingerprint was convincing enough that DataDome cleared it automatically within a few seconds.

That standalone setup confirms Camoufox bypasses DataDome. Using it inside Crawlee is the real goal — and it takes more than just swapping `browser_type`. Crawlee manages browsers through a `BrowserPool`, and while the official [guide on avoiding getting blocked](https://crawlee.dev/python/docs/guides/avoid-blocking) covers fingerprinting, headers, and proxy use, none of it replaces the browser engine itself. The fix: subclass `PlaywrightBrowserPlugin` and override `new_browser()` to launch Camoufox instead:

```python
from crawlee.browsers import BrowserPool, PlaywrightBrowserController, PlaywrightBrowserPlugin
from camoufox import AsyncNewBrowser
from typing_extensions import override

class CamoufoxPlugin(PlaywrightBrowserPlugin):
    """
    Crawlee BrowserPlugin that launches Camoufox (stealth Firefox) instead of
    standard Playwright Firefox. All other PlaywrightBrowserPlugin behaviour
    (context creation, proxy injection, page lifecycle) is inherited unchanged.
    """

    def __init__(self, *, browser_state: dict, proxy_url_getter=None, **kwargs):
        super().__init__(**kwargs)
        self._browser_state = browser_state
        self._proxy_url_getter = proxy_url_getter

    @override
    async def new_browser(self) -> PlaywrightBrowserController:
        is_headless = os.environ.get("APIFY_IS_AT_HOME") == "1"

        launch_options = {
            "os": "windows",
            "block_webrtc": True,
            "locale": "en-US",
            "headless": is_headless,
        }

        proxy_url = await self._proxy_url_getter() if self._proxy_url_getter else None
        if proxy_url:
            try:
                launch_options["proxy"] = _apify_proxy_url_to_playwright_proxy(proxy_url)
                launch_options["geoip"] = True
                browser = await AsyncNewBrowser(self._playwright, **launch_options)
            except (NotInstalledGeoIPExtra, InvalidIP, InvalidProxy):
                launch_options.pop("geoip", None)
                launch_options.pop("proxy", None)
                browser = await AsyncNewBrowser(self._playwright, **launch_options)
        else:
            browser = await AsyncNewBrowser(self._playwright, **launch_options)

        return PlaywrightBrowserController(
            browser=browser,
            header_generator=None,  # Camoufox generates its own headers
        )
```

The `PlaywrightCrawler` setup:

```python
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
    retry_on_blocked=False,
    ignore_http_error_status_codes=[403, 429, 503],
    request_handler_timeout=timedelta(seconds=1200),
    navigation_timeout=timedelta(seconds=120),
)
```

Key settings:
- `max_concurrency=1` — one place at a time; one browser reused for all places (crucial for DataDome cookie accumulation — see anti-blocking section below)
- `retry_on_blocked=False` — I handle DataDome blocks myself via `CaptchaBlockedError`
- `ignore_http_error_status_codes=[403, 429, 503]` — let Camoufox handle these instead of Crawlee aborting immediately

The `geoip=True` option and proxy-at-launch pattern in `CamoufoxPlugin` are explained in detail in [§7 Anti-blocking measures](#7-anti-blocking-measures).

## 4. Move to Apify Cloud

Deploy with:

```bash
apify push
```

The Actor accepts these input fields:

| Field | Type | Description |
|---|---|---|
| `startUrls` | array | TripAdvisor place URLs (hotels, restaurants, attractions) |
| `maxReviewsPerPlace` | integer | Maximum reviews per place (omit for unlimited) |
| `startDate` | string | Filter: only reviews on or after this date (YYYY-MM-DD) |
| `endDate` | string | Filter: only reviews on or before this date (YYYY-MM-DD) |
| `reviewRatings` | array | Filter by star rating: [1], [5], [3,4,5], etc. |
| `language` | string | Filter by review language code (e.g. `en`, `de`, `fr`) |
| `proxyConfiguration` | object | [Apify Proxy](https://docs.apify.com/platform/proxy) settings — use Residential Proxy for reliable bypassing |

:::note
Without residential proxy, DataDome blocks datacenter IPs regardless of browser fingerprint. Camoufox handles fingerprint detection, but [residential proxy](https://docs.apify.com/platform/proxy/residential-proxy) is required on Apify Cloud for consistent results.
:::

## 5. Output samples

After a successful run, the Actor produces two datasets. Field names in the parsing code use snake_case; the output JSON uses camelCase — the Actor normalises them during output construction.

**Places** (Key-Value Store, `places.json`):

```json
[
  {
    "id": "264939",
    "url": "https://www.tripadvisor.com/Hotel_Review-g190327-d264939-Reviews-The_Waterfront_Hotel-Sliema_Island_of_Malta.html",
    "name": "The Waterfront Hotel",
    "placeType": "LodgingBusiness",
    "rating": "4.4",
    "totalReviews": 3876,
    "scrapedReviews": 600,
    "address": "Triq Ix - Xatt",
    "city": "Sliema",
    "region": "",
    "country": "MT",
    "priceRange": "$ (Based on Average Nightly Rates for a Standard Room from our Partners)",
    "image": "https://dynamic-media-cdn.tripadvisor.com/media/photo-o/2a/52/68/5d/the-waterfront-hotel.jpg?w=500&h=-1&s=1",
    "ratingDistribution": null,
    "oldestDate": "2025-08-17",
    "error": null
  }
]
```

**Reviews** (Dataset):

```json
[
  {
    "placeName": "The Waterfront Hotel",
    "rating": 5,
    "title": "Enjoyable 4 night stay at the Waterfront hotel Sliema, Malta",
    "text": "The hotel was fairly modern and our room was very clean and the king size bed was comfortable. The staff were very helpful and friendly, the breakfasts were very good with lots of options from yogurts to omelettes.",
    "publishedDate": "2026-03-24",
    "travelDate": "2026-03",
    "tripType": "NONE",
    "lang": "en",
    "reviewerName": "Stephen O",
    "helpfulVotes": 0,
    "placeUrl": "https://www.tripadvisor.com/Hotel_Review-g190327-d264939-Reviews-The_Waterfront_Hotel-Sliema_Island_of_Malta.html",
    "managementResponse": null
  }
]
```

## 6. Performance optimisation

After I had the basic version working, I profiled it and found 22–66 seconds of unnecessary waiting per place. Here's what I changed and why.

### `domcontentloaded` instead of `networkidle`

After navigating to a URL, many Playwright setups wait until the page seems "settled" — often using `networkidle`, which means no network activity for about 500ms. On TripAdvisor, ads and analytics keep opening connections indefinitely, so reaching that idle state can easily add 10–20 seconds per place with little benefit for my use case.

I only need the page shell, cookies/session, and a DOM to interact with so the in-page GraphQL fetch calls work. `domcontentloaded` fires as soon as the HTML is parsed and the DOM is ready — JavaScript may still be running and resources still loading, but the structure is there. I can navigate, click the Reviews tab, and run GraphQL fetches without waiting for the network to go quiet.

```python
# domcontentloaded instead of networkidle
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
```

**Saves: 5–12s per place.**

### Block images, fonts, and media

`page.route("**/*", ...)` intercepts every outgoing request before it's sent. For `image`, `font`, and `media` resource types it calls `route.abort()` — the request is cancelled immediately. For everything else (HTML, scripts, XHR/fetch calls for GraphQL, the captcha document iframe) it calls `route.continue_()`.

```python
async def _block_resources(route):
    if route.request.resource_type in ("image", "font", "media"):
        await route.abort()
    else:
        await route.continue_()

await page.route("**/*", _block_resources)
```

DataDome's bot detection is based on browser fingerprint properties (TLS, WebGL, canvas, `navigator.*`) — all browser-level checks that run regardless of whether images loaded. The captcha iframe is resource type `document`, not `image`, so it still loads normally and the check still runs.

**Saves: 2–5s per place.**

### Minimal scroll

`window.scrollBy(0, 400)` moves the page 400 pixels down (0 horizontal) as a single, light human-like nudge:

```python
await page.evaluate("window.scrollBy(0, 400)")
await asyncio.sleep(random.uniform(0.5, 1.0))
```

With a DOM-based approach you'd depend on lazy-loading: scroll repeatedly (`window.scrollBy(0, 500)`, many times), with delays of 1–2 seconds between each step so lazy scripts and requests can fire. Since I use GraphQL with offsets, that long scroll became unnecessary for data collection — I don't need the DOM to load every review card. Only the small nudge remained as a behavioural signal.

**Saves: 15–25s per place.**

### Captcha self-resolve check

Camoufox is a stealth browser designed to look convincingly like a real user to bot-detection systems. When a DataDome captcha iframe appears, I don't immediately rotate to a new proxy session or start a fresh browser. Instead, I wait — polling once per second for up to 15 seconds to check whether the iframe is still visible:

:::note
Rotating too eagerly means paying the full cost of a cold session (new IP, zero cookies, fresh fingerprint) every time. Waiting a few seconds for Camoufox to pass DataDome's check naturally is almost always cheaper.
:::

```python
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
        raise CaptchaBlockedError("DataDome captcha not bypassed with current proxy")
```

If the iframe disappears, I treat it as auto-resolved and continue immediately — no wasted retry, no cold new session. If it's still there after 15 seconds, I raise `CaptchaBlockedError` and let Crawlee schedule a retry with a new proxy session.

**Saves: 0–24s per place when captcha auto-resolves — which it does most of the time with Camoufox + residential proxy.**

### Summary

| Optimization | Saves |
|---|---|
| `domcontentloaded` navigation | 5–12s |
| Block images/fonts/media | 2–5s |
| Minimal scroll | 15–25s |
| Captcha self-resolve | 0–24s |
| **Total** | **~22–66s per place** |

## 7. Anti-blocking measures

### Match browser timezone and locale to the proxy's exit IP (GeoIP)

Every browser fingerprint includes timezone, locale, and geolocation. For DataDome, these must match what the proxy's exit IP actually looks like — country, timezone, and language. If they don't, DataDome sees a mismatch and triggers a challenge even when the rest of the fingerprint is clean.

I hit this exact case before implementing GeoIP:

```
Proxy: IP=179.214.41.73, country=BR, timezone=America/Sao_Paulo
Browser: locale=en-US, timezone=(not set)
```

The proxy was exiting in Brazil, but the browser had no timezone set and reported `en-US` locale. The logs showed "Captcha detected" consistently:

![DataDome captcha triggered by a timezone/locale mismatch between proxy and browser](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/captcha-detected-without-geoip.png)

The fix is to pass the proxy to Camoufox at **browser launch** (not at context creation), with `geoip=True`. Camoufox then makes a quick request through the proxy tunnel to discover the exit IP's country and timezone, and wires the browser's timezone, geolocation, and locale to match before the browser fully starts.

There is an important architectural constraint here: Camoufox has to look up "which country is this IP?" before the browser fully starts. If you only set the proxy on `new_context()` — which is how Crawlee normally injects proxies — Camoufox's early lookup goes out through the server's own internet connection instead of the proxy tunnel. It resolves the wrong IP, wires the browser to the wrong timezone, and the mismatch gets flagged. The proxy must be set at browser launch for GeoIP to work correctly.

```python
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
            "retrying without browser-level proxy/geoip "
            "(Crawlee still applies proxy on the context)."
        )
        launch_options.pop("geoip", None)
        launch_options.pop("proxy", None)
        browser = await AsyncNewBrowser(self._playwright, **launch_options)
else:
    browser = await AsyncNewBrowser(self._playwright, **launch_options)
```

After adding `geoip=True` at browser launch, the timezone and locale align to the residential exit IP automatically. The "Captcha detected" message no longer appears in the logs.

### Keep the same browser and proxy for all places

First: use [Residential Proxy](https://docs.apify.com/platform/proxy/residential-proxy). Datacenter IPs are blocked by DataDome regardless of browser fingerprint — residential is the baseline for anything beyond local testing.

The standard scraping advice is to rotate the proxy and browser session when moving to a new place. For DataDome, that's the wrong call — and it took a bug to make me realise it.

Here's why rotation hurts. When a browser passes DataDome's JavaScript challenge, DataDome writes a session cookie (`datadome=xxxxx`) into that browser. That cookie says: *"this browser passed our check."*

- **Place 1** — fresh browser, fresh proxy IP. DataDome runs its full challenge and lets it through. Cookie is set.
- **Place 2 (with rotation)** — the entire browser is thrown away. New browser, new proxy IP, zero cookies, zero history. DataDome sees a completely unknown fingerprint on an IP it has no history with, and runs the full challenge again from scratch.

The combination of "new IP + no cookies" is far more suspicious than "same IP + returning visitor cookie." Reusing one browser for all places means the `datadome` cookie from Place 1 carries forward to Place 2 — the same user browsing multiple places, which is natural behaviour.

To verify this, I logged the proxy IP and browser session for each place. That's when I found a subtle Crawlee bug that was silently defeating the strategy.

Crawlee's `BrowserPool` has a default `browser_inactive_threshold` of 10 seconds. Crawlee measures idle time from when the page was *opened*, not *closed*. Each place takes 45+ seconds to scrape. After just 10 seconds, `_identify_inactive_browsers` (runs every 20 seconds by default) moved the active browser to the inactive list, and the next place triggered a brand-new browser with a fresh proxy IP — without any obvious error in the logs.

The logs showed the proxy IP changing mid-run from `73.244.6.162` to `96.191.113.237`, immediately followed by a block on Place 3:

![Log showing proxy IP changing from 73.244.6.162 to 96.191.113.237, followed by a DataDome block on place 3](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/datadome-block-after-proxy-rotation.png)

```
2026-03-23T07:33:02.900Z [apify] WARN  Blocked on place 3 (attempt 1/4) — rotating proxy + browser
```

The fix:

```python
browser_pool = BrowserPool(
    plugins=[CamoufoxPlugin(
        browser_state=browser_state,
        proxy_url_getter=_proxy_url_for_geoip,
    )],
    browser_inactive_threshold=timedelta(minutes=30),
    identify_inactive_browsers_interval=timedelta(minutes=30),
)
```

After this change, the IP no longer rotates between places, the `datadome` cookie carries forward, and the blocking stops. With both fixes in place — GeoIP alignment and browser reuse — the logs show the same proxy IP throughout the run, no "Captcha detected", and no blocks:

```
Proxy exit IP: 139.216.177.28 | country=AU | timezone=Australia/Melbourne (ipinfo)
Launching browser: os=windows | tz=Australia/Melbourne [geoip+ipinfo: 139.216.177.28/AU]
```

![Apify Actor log showing the same proxy IP and Camoufox browser session reused across all TripAdvisor places, with no captcha or blocking](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/same-proxy-browser-geoip.png)

### Exponential backoff on retries

After a DataDome block, wait longer before each retry — reduces pressure on the protection system and gives the session time to cool down. With up to 3 retries, the delays are 3s → 6s → 12s (plus ±1s jitter):

```python
if retry_count > 0:
    # Exponential backoff: 3s, 6s, 12s … with ±1s jitter
    backoff = 3.0 * (2 ** (retry_count - 1)) + random.uniform(0.5, 1.5)
    Actor.log.info(
        f"  Backoff {backoff:.1f}s before retry attempt {attempt}/{total_attempts} …"
    )
    await asyncio.sleep(backoff)
```

### Random delays throughout

Between GraphQL pagination rounds, after page load, after tab click, after scroll:

```python
await asyncio.sleep(random.uniform(1.0, 2.0))   # after initial page load
await asyncio.sleep(random.uniform(0.5, 1.0))    # after Reviews tab click
await asyncio.sleep(random.uniform(0.5, 1.0))    # after scroll
await asyncio.sleep(random.uniform(0.8, 1.5))    # between GraphQL pagination rounds
```

Between places — after a place finishes successfully, sleep 2–3s before the next one so traffic doesn't look like a tight loop:

```python
delay = 2.0 + random.uniform(0.0, 1.0)
Actor.log.info(f"  Inter-place delay: {delay:.1f}s …")
await asyncio.sleep(delay)
```

### Don't use `humanize=True`

Camoufox has a `humanize=True` parameter that injects realistic mouse movement curves and keystroke timing. I tried it, expecting it to help — it made things worse.

On Apify Cloud, `headless=True` always. No actual browser events are happening. The `humanize` timing signatures appear without the corresponding real interactions, creating a pattern that DataDome is specifically designed to detect: a bot imitating human behavior without the underlying human input. `humanize=False` gave measurably better results on headless Cloud runs.

## Conclusion

A few things I learned — and would do differently next time:

**Start in DevTools, not in code.** Twenty minutes with the Network tab revealed the exact GraphQL endpoint, query ID, payload shape, and response structure. That upfront investment made everything else fast. Without it, I'd have spent days fighting fragile DOM selectors.

**Skip Chromium entirely — start with Camoufox.** I wasted time trying to get Chromium past DataDome with various stealth patches. None of them worked reliably. Camoufox was the right tool from the start; I just didn't know it yet. If you're dealing with DataDome or similar fingerprint-based bot protection, go straight to Camoufox.

**`humanize=True` made things worse, not better.** I tried it expecting it to help, since "more human-like" sounds like the right direction. In headless mode on Apify Cloud, the timing signatures of `humanize` appear without any corresponding real browser input. That mismatch is detectable. Leave it off on headless runs.

**`page.evaluate()` for authenticated API calls is a pattern worth knowing.** Any time a site's API relies on session cookies that are painful to replicate externally, running `fetch()` from inside the browser page context is the cleanest solution. The browser handles auth; you handle the data.

**Keep the same proxy and browser session for all places — unless you're actually blocked.** Every scraping guide says "rotate your proxy." For DataDome, rotating on every place is counterproductive. A browser that's passed the challenge carries approval cookies forward to the next place. Rotating resets all that accumulated trust and forces the full challenge to run again on a cold IP. The `browser_inactive_threshold` bug was silently defeating this strategy for me — the browser was being replaced every 45 seconds without any obvious error in the logs.

**Use `geoip=True` and pass the proxy at browser launch, not context creation.** Camoufox needs the proxy's exit IP before the browser starts, so it can align timezone, geolocation, and locale to the residential IP. If you only set the proxy on `new_context()` — which is Crawlee's default — Camoufox's early IP lookup goes through the server's own network, producing a timezone/locale mismatch that DataDome flags immediately.

You can find the full Actor code in the [GitHub repository](#) and the deployed version on the [Apify Store](#). Questions or improvements? Join the [Crawlee Discord](https://discord.com/invite/jyEM2PRvMU) — 11,000+ developers working through exactly these kinds of problems.
