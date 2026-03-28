---
slug: tripadvisor-reviews-scraper-python-crawlee-camoufox
title: How to scrape TripAdvisor reviews with Python, Crawlee, and Camoufox
description: Scrape TripAdvisor reviews with Python, Crawlee, and Camoufox. Bypass DataDome using stealth Firefox, GeoIP matching, and parallel GraphQL fetching.
authors:
  - name: Mark Lipkovich
    title: Apify community developer specializing in high-fidelity data extraction for ML/AI training, automation, and data analysis. Published scrapers on the Apify Store include this Actor and a YouTube Transcript Scraper, with more extraction tools in development.
tags: [community]
---

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
- **Python packages** — [Crawlee for Python](https://crawlee.dev/python) with **Playwright**, the [Apify SDK for Python](https://docs.apify.com/sdk/python), **httpx**, and **typing-extensions** (Camoufox is installed in [§3 Move to Camoufox](#3-move-to-camoufox)).

Install in a virtual environment:

```bash
pip install "apify~=3.3.0" "crawlee[playwright]~=1.5.0" "httpx~=0.28.1" "typing-extensions~=4.15.0"
```

- **Node.js** — required for the [Apify CLI](https://docs.apify.com/cli) (local `apify run`, `apify push`, `apify create`).
- **Apify CLI** — install globally:

```bash
npm install -g apify-cli
```

- **Apify account** — if you use cloud features later ([sign up](https://apify.com/)), run `apify login` so the CLI can push and talk to the platform.
- **Browser DevTools** — **Chrome** or **Edge** (F12 → **Network**). You will inspect **Fetch/XHR** and copy requests as **cURL** before writing scraping code.
- **TripAdvisor in the browser** — a normal place URL on `tripadvisor.com` to reproduce the network calls (hotel, restaurant, or attraction).
- **Apify Cloud runs** — [Apify Proxy](https://docs.apify.com/platform/proxy) with a **residential** group for anything beyond quick local experiments; DataDome typically blocks **datacenter** IPs.

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

I treated `response.json` as the ground truth: the envelope is a list of GraphQL results, each with `data`, and the reviews live under `data.ReviewsProxy_getReviewListPageForLocation[0].reviews`. I implemented that path in `parse_review_from_graphql`—walking the same keys you see above, then mapping each review object into the Actor’s output shape (absolute review URL from `reviewDetailPageWrapper`, place fields from `location`, owner reply from `mgmtResponse`, and so on). The extra branches and `.get(...)` fallbacks in the code below aren’t guesswork; they’re there because TripAdvisor sometimes exposes reviews under sibling keys or alternate operations, so the parser still works when the payload isn’t identical to this one capture.

```python
def parse_review_from_graphql(data: list) -> list[dict]:
    """Extract reviews from GraphQL response list."""
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
        # ... fallbacks for other TripAdvisor operation shapes ...
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
                # ...
                user = r.get("user") or r.get("userProfile") or r.get("author") or {}
                # ...
                detail = r.get("reviewDetailPageWrapper") or {}
                route = (detail.get("reviewDetailPageRoute") or {}) if isinstance(detail, dict) else {}
                review_url = (
                    "https://www.tripadvisor.com" + str(route["url"])
                    if isinstance(route, dict) and route.get("url") else ""
                )
                # ...
                loc = r.get("location") or {}
                # ...
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
                # ...
                results.append({
                    "id": rid,
                    "url": review_url,
                    "title": (r.get("title") or "").strip(),
                    "text": (text or "").strip() if isinstance(text, str) else str(text).strip(),
                    # ... placeName, placeUrl, reviewerName, ownerResponse, user.contributions, etc.
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

In the Actor, I replicate this from within the browser using `page.evaluate()`. The Python call wrapping the JavaScript `fetch()`(abbreviated code):

```python
from apify import Actor
from playwright.async_api import Page

async def fetch_reviews_via_graphql(page: Page) -> list[dict]: 
    payload = [
        {"variables": variables, "extensions": {"preRegisteredQueryId": REVIEWS_QUERY_ID}}
    ]
    url = "https://www.tripadvisor.com/data/graphql/ids"

    max_gql_retries = 3    
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
           
        except Exception as exc:            
            if attempt < max_gql_retries:
                delay = 1.5 * (2 ** (attempt - 1))
                Actor.log.warning(
                    f"  GraphQL reviews fetch failed: {exc!s:.100}. "
                    f"Retrying in {delay:.1f}s (attempt {attempt}/{max_gql_retries}) …"
                )
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

The request runs in JavaScript inside the browser context. Clean, structured JSON comes back directly.

## 2. Run code locally

If you are starting from scratch, `apify create actor-name` will create a new subdirectory for the new project, containing all the necessary files. (You can add `-t template_id` to start from a template — see [Creating Actors](https://docs.apify.com/academy/getting-started/creating-actors) in the Apify Academy.)

For local runs, you often want a **visible** browser window while debugging. The snippet below is a **generic illustration**

```python
# Standalone experiment: visible window — not the Actor entrypoint.
from playwright.async_api import async_playwright
from camoufox import AsyncNewBrowser

async def run():
    async with async_playwright() as pw:
        browser = await AsyncNewBrowser(
            pw,
            headless=False,  # watch navigation, captchas, tab clicks locally
            os="windows",
            block_webrtc=True,
            locale="en-US",
        )
        # … open a context/page and scrape …
        await browser.close()
```
With `headless=False`, the window stays open so you can follow navigation, captchas, and clicks. The Actor itself runs headless in the cloud with the same plugin stack.

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

Each request returns exactly 10 reviews. To scrape 500 reviews efficiently, I run batches of 50 concurrent requests using `asyncio.gather()`(abbreviated code):

```python
import asyncio
from apify import Actor

PARALLEL_REQUESTS = 50
REVIEWS_PER_PAGE = 10
PUSH_BATCH_SIZE = 300

async def _push_batch(batch: list[dict]) -> None:

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
```

I tested 40, 50, 60, and 100 parallel requests. Beyond 50, runtime didn't improve and blocking probability increased. 50 is the practical sweet spot for this endpoint.

## 3. Move to Camoufox

My first implementation used standard Playwright with Chromium and Patchright (a stealth-patched Chromium fork). On the first local run, I got this:

![DataDome captcha slider appearing with Chromium headless browser](./tripadvisor-reviews-scraper-python-crawlee-camoufox-images/captcha-tripadvisor-chromium.png)

DataDome detected the headless Chromium fingerprint. I could solve it locally with a quick slider automation (simplified code):

```python
# Find slider, drag to the right
from playwright.async_api import Page
page: Page

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

Install it with the `geoip` extra, then download the browser binary:

```bash
pip install "camoufox[geoip]~=0.4.11"
python -m camoufox fetch  # downloads the browser binary (~200 MB)
```

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

That standalone setup confirms Camoufox bypasses DataDome. Using it inside Crawlee is the real goal — and it takes more than just swapping `browser_type`. Crawlee manages browsers through a `BrowserPool`, and while the official [guide on avoiding getting blocked](https://crawlee.dev/python/docs/guides/avoid-blocking) covers fingerprinting, headers, and proxy use, none of it replaces the browser engine itself. The fix: subclass `PlaywrightBrowserPlugin` and override `new_browser()` to launch Camoufox instead (abbreviated code):

```python
from crawlee.browsers import BrowserPool, PlaywrightBrowserController, PlaywrightBrowserPlugin
from camoufox import AsyncNewBrowser
from typing_extensions import override
from apify import Actor
import os as _os
import random

class CamoufoxPlugin(PlaywrightBrowserPlugin):
    """
    Crawlee BrowserPlugin that launches Camoufox (stealth Firefox) instead of
    standard Playwright Firefox.  All other PlaywrightBrowserPlugin behaviour
    (context creation, proxy injection, page lifecycle) is inherited unchanged.  
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
            # I need the exit IP before I can set geoip=, so probing is sequential.
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
```

The `PlaywrightCrawler` setup (abbreviated code):

```python
from datetime import timedelta
from crawlee import ConcurrencySettings
from crawlee.browsers import BrowserPool, PlaywrightBrowserPlugin
from crawlee.crawlers import PlaywrightCrawler
        
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
            # Don't let Crawlee intercept bot-protection responses — I handle it myself.
            retry_on_blocked=False,
            configure_logging=False,
            # Ignore HTTP error codes so Camoufox can handle DataDome / captcha responses.
            ignore_http_error_status_codes=[403, 429, 503],
            request_handler_timeout=timedelta(seconds=1200),
            # Residential proxy adds latency; 120 s prevents wasting a retry on slow first load.
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

**Places** (Key-Value Store, `Places.json`):

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

I only need the page shell, cookies/session, and a DOM to interact with so the in-page GraphQL fetch calls work. `domcontentloaded` fires as soon as the HTML is parsed and the DOM is ready — JavaScript may still be running and resources still loading, but the structure is there. I can navigate, click the Reviews tab, and run GraphQL fetches without waiting for the network to go quiet (abbreviated code):

```python
# domcontentloaded instead of networkidle
from playwright.async_api import Page
page: Page
from apify import Actor

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
from playwright.async_api import Page

async def on_response(response):
    page: Page
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

`window.scrollBy(0, 400)` moves the page 400 pixels down (0 horizontal) as a single, light human-like nudge (abbreviated code):

```python
import random
import asyncio
from playwright.async_api import Page

async def scrape_place(page: Page, ...) ->:
    page: Page
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
from apify import Actor
import asyncio
class CaptchaBlockedError(Exception):
"""Raised when DataDome captcha cannot be bypassed with the current proxy."""
async def scrape_place(page: Page,...) ->:    
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

The fix is to match the browser's timezone, geolocation, and locale to the proxy's actual exit IP before the browser starts.
Camoufox's `geoip= parameter` accepts a plain IP address string. Pass it the exit IP and Camoufox does a local MaxMind database lookup to configure the browser accordingly — no network request at launch time, no browser-level proxy required. Crawlee continues to inject the proxy at context creation time, as normal.
Camoufox also accepts `geoip=probe_ip`, which lets it probe the exit IP internally. I avoid that here because it requires routing the proxy through the browser at the launch level, which sends all of Firefox's own internal traffic through the residential IP and adds an extra network probe I can't control. Passing the IP string directly is cleaner.
The approach is straightforward: probe the exit IP yourself with httpx before the browser opens, then hand the result to Camoufox:

```python
import httpx
from apify import Actor
from crawlee.browsers import PlaywrightBrowserPlugin, PlaywrightBrowserController

from camoufox import AsyncNewBrowser

async def _probe_proxy_timezone(proxy_url: str) -> tuple[str, str, str, str]:
    """Return (IANA_timezone, source_label, exit_ip, country) via the proxy."""
    endpoints = [
        ("https://ipinfo.io/json",                                    "ipinfo", "ip",    "country",     "timezone"),
        ("http://ip-api.com/json/?fields=query,countryCode,timezone", "ip-api", "query", "countryCode", "timezone"),
    ]
    for url, svc, ip_key, country_key, tz_key in endpoints:
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=15.0) as client:
                data = (await client.get(url)).json()
            exit_ip  = data.get(ip_key,      "?")
            country  = data.get(country_key, "?")
            timezone = data.get(tz_key) or "Europe/London"
            Actor.log.info(f"Proxy exit IP: {exit_ip} | country={country} | timezone={timezone} ({svc})")
            return timezone, f"{svc}: {exit_ip}/{country}", exit_ip, country
        except Exception:
            continue
    return "Europe/London", "probe-failed: default", "?", "?"


class CamoufoxPlugin(PlaywrightBrowserPlugin):

    async def new_browser(self) -> PlaywrightBrowserController:
        proxy_url = await self._proxy_url_getter()  # same session ID Crawlee uses

        launch_options = {"os": "windows", "block_webrtc": True, "locale": "en-US"}

        if proxy_url:
            # Probe the exit IP before the browser opens.
            _, _, probe_ip, _ = await _probe_proxy_timezone(proxy_url)

            if probe_ip != "?":
                # Pass the IP string — Camoufox does a local MaxMind lookup only.
                # No browser-level proxy needed; Crawlee sets it on the context.
                launch_options["geoip"] = probe_ip

        browser = await AsyncNewBrowser(self._playwright, **launch_options)
        return PlaywrightBrowserController(browser=browser, header_generator=None)
```

After adding `geoip=probe_ip` at browser launch, the timezone and locale align to the residential exit IP automatically. The "Captcha detected" message no longer appears in the logs.

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

The fix (abbreviated code):

```python
from datetime import timedelta
from crawlee.browsers import BrowserPool

...
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
from apify import Actor
import asyncio
import random

if retry_count > 0:
    # Exponential backoff: 3s, 6s, 12s … with ±1s jitter
    backoff = 3.0 * (2 ** (retry_count - 1)) + random.uniform(0.5, 1.5)
    Actor.log.info(
        f"  Backoff {backoff:.1f}s before retry attempt {attempt}/{total_attempts} …"
    )
    await asyncio.sleep(backoff)
```

### Random delays throughout

Between GraphQL pagination rounds, after page load, after tab click, after scroll (abbreviated code):

```python
import asyncio
import random

await asyncio.sleep(random.uniform(1.0, 2.0))   # after initial page load
await asyncio.sleep(random.uniform(0.5, 1.0))    # after Reviews tab click
await asyncio.sleep(random.uniform(0.5, 1.0))    # after scroll
await asyncio.sleep(random.uniform(0.8, 1.5))    # between GraphQL pagination rounds
```

Between places — after a place finishes successfully, sleep 2–3s before the next one so traffic doesn't look like a tight loop:

```python
from apify import Actor
import asyncio
import random

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

**Use `geoip=probe_ip` at browser launch.** Camoufox needs the proxy's exit IP before the browser starts, so it can align timezone, geolocation, and locale to the residential IP. Rather than letting Camoufox probe the IP internally (which requires routing the proxy through the browser at launch), probe it yourself first with a lightweight httpx call, then pass the result as a plain IP string: `launch_options["geoip"] = probe_ip`

You can find the full Actor code in the [GitHub repository](https://github.com/marklipkovich/tripadvisor-scraper) and the deployed version on the [Apify Store](https://apify.com/marklp/tripadvisor-reviews-scraper). Questions or improvements? Join the [Discord](https://discord.com/invite/jyEM2PRvMU) — 11,000+ developers working through exactly these kinds of problems.
