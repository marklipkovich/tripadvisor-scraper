# Changelog — TripAdvisor Reviews Scraper (Crawlee Edition)

All notable changes to this Crawlee-based implementation are documented here.  
This actor is a parallel implementation of the raw-Playwright version (`tripadvisor/`) created for architectural comparison purposes.

---

## [1.0.0] — 2026-03-19

### Added
- **Initial Crawlee + Camoufox implementation** — ported from `tripadvisor/src/main.py` (raw Playwright version).
- **`CamoufoxPlugin`** — custom `PlaywrightBrowserPlugin` subclass that injects Camoufox (stealthy Firefox fork) into Crawlee's `BrowserPool`, replacing a plain Playwright browser launch.
- **`PlaywrightCrawler`** with `BrowserPool(plugins=[CamoufoxPlugin()])` — Crawlee now manages browser lifecycle (launch, close, page allocation) instead of manual `async_playwright()` context.
- **Crawlee request queue** — place URLs are enqueued via `crawler.run(place_urls)` instead of a manual `for` loop.
- **Crawlee concurrency control** — `ConcurrencySettings(max_concurrency=1)` replaces the manual semaphore used in the original.
- **Crawlee automatic retries** — `max_request_retries=MAX_CAPTCHA_RETRIES` replaces the manual captcha retry loop; raising `CaptchaBlockedError` triggers Crawlee's built-in retry mechanism with proxy rotation.
- **`retry_on_blocked=False`** — disabled Crawlee's default block detection to avoid conflicts with the custom DataDome / captcha handling logic.
- **`configure_logging=False`** — disabled Crawlee's default logging setup so that Apify SDK logging (`Actor.log`) remains in control.
- **`scrape_place()`** adapted to accept a Crawlee-provided `Page` object (`PlaywrightCrawlingContext.page`) instead of launching its own browser context.
- **Proxy handling** — Crawlee's `ProxyConfiguration` is wired via `proxy_configuration=` on `PlaywrightCrawler`; Apify's residential proxy sessions are resolved per-request inside the handler and applied through Crawlee's context-level proxy injection.
- Identical inputs, outputs, log messages, and status messages as the raw-Playwright version for a fair side-by-side comparison.
- `SyntaxWarning` fix: `EXTRACT_PAGE_SCRIPT` changed to a raw string (`r"""..."""`) so that JavaScript regex sequences such as `\d` are not mis-interpreted by Python's string parser.
- `ConcurrencySettings` import added (`from crawlee import ConcurrencySettings`) after `max_concurrency=` was found to be an invalid keyword argument for `PlaywrightCrawler.__init__`.
- Local `storage/key_value_stores/default/INPUT.json` added for local `apify run` / PyCharm testing.

### Changed (vs raw-Playwright `main.py`)
| Aspect | `main.py` (raw Playwright) | `main_crawlee.py` (Crawlee) |
|---|---|---|
| Browser launch | Manual `AsyncNewBrowser` inside `main()` | `CamoufoxPlugin` via `BrowserPool` |
| Request loop | `for place_url in place_urls` | `crawler.run(place_urls)` |
| Concurrency | `asyncio.Semaphore` | `ConcurrencySettings(max_concurrency=1)` |
| Captcha retries | Manual `for attempt in range(MAX_CAPTCHA_RETRIES)` | Raise `CaptchaBlockedError` → Crawlee retries |
| Proxy rotation | Manual session suffix (`_r2`, `_r3`) | Crawlee rotates session per retry automatically |
| Dataset push | `await Actor.push_data(batch)` | `await Actor.push_data(batch)` (unchanged) |
| KV store output | `Actor.set_value("Places.json", …)` | `Actor.set_value("Places.json", …)` (unchanged) |

### Fixed (vs initial implementation)

**Reviews returning empty (root cause: wrong GraphQL implementation)**
- `fetch_reviews_via_graphql` was copied from an older draft instead of from the working `main.py`. Three critical differences caused TripAdvisor's GraphQL API to return `{"errors":[...]}` for every request:
  1. **Missing `credentials: 'include'`** — without this flag the browser strips session cookies from the `fetch()` call; TripAdvisor rejects unauthenticated GraphQL requests.
  2. **Wrong query ID** — `a0d72c4bb0eb9898` was used instead of the correct `ef1a9f94012220d3` (`ReviewsProxy_getReviewListPageForLocation`).
  3. **Wrong variables and headers** — missing `sortBy`, `language`, `doMachineTranslation`, `Origin`, `Referer`, `Accept`.
- Fixed by replacing the function body with the exact implementation from `main.py`.

**Double-navigation breaking TripAdvisor session**
- Crawlee navigates to the place URL before calling the handler. `scrape_place()` was then calling `page.goto()` a second time, resetting TripAdvisor's session/CSRF state and causing all subsequent API calls to fail.
- Fixed by detecting whether the page is already on the correct URL and skipping re-navigation: `if extract_location_id_from_url(page.url) == extract_location_id_from_url(place_url)`.

**1-2 minute delay waiting for Reviews tab**
- Crawlee's `PlaywrightCrawler` stops navigation at `domcontentloaded`; TripAdvisor's React/Next.js has not yet hydrated the DOM, so the Reviews tab locator had to wait up to 30 seconds.
- Fixed by calling `await page.wait_for_load_state("load", timeout=20_000)` immediately after skipping re-navigation, ensuring the full JS bundle has run before the tab locator search begins.

**Proxy routing now works correctly on Apify Cloud**
- The initial approach used `page.context.set_http_credentials()` which only set Basic Auth credentials but did not route traffic through the proxy server. Replaced with a Crawlee `ProxyConfiguration` backed by a `new_url_function` that calls Apify's proxy resolver. Crawlee now injects the full proxy URL (server + credentials) at `browser.new_context()` time, which Firefox/Camoufox supports via Playwright's per-context proxy API.
- Removed the now-redundant `proxy_setting: Optional[dict]` parameter from `scrape_place()`.
- Session rotation on captcha retries is handled by Crawlee passing a new `session_id` to `new_url_function` on each retry, which Apify's proxy resolver maps to a different exit IP.

**Various startup / API errors fixed during development**
- `ValueError: desired_concurrency cannot be greater than max_concurrency` — added `desired_concurrency=1` alongside `max_concurrency=1`.
- `ValueError: Invalid request type: <class 'dict'>` — replaced plain dicts with `Request.from_url(url, user_data=...)`.
- `AttributeError: 'NoneType' has no attribute 'new_proxy_info'` — `Actor.create_proxy_configuration()` returns `None` locally without valid Apify credentials; added `None` guard before creating `CrawleeProxyConfiguration`.
- `HttpClientStatusCodeError (403)` — Crawlee raises on 4xx before calling the handler; fixed by adding `ignore_http_error_status_codes=[403, 429, 503]`.
- `TimeoutError: Request handler timed out after 60s` — Crawlee's default handler timeout is 60 s; raised to 20 min via `request_handler_timeout=timedelta(seconds=1200)`.
- `headless=False` ignored — Crawlee's `_browser_launch_options` spread was overriding the user's value; moved `headless` assignment after the spread, auto-detected via `APIFY_IS_AT_HOME`.
- `locale="en-US"` added to Camoufox launch options to match `main.py`'s `browser.new_context(locale="en-US")` and ensure consistent `Accept-Language` headers.

### Known Limitations
- Session ID naming differs from `main.py`; Crawlee generates its own session IDs rather than the explicit `264936_r2`/`264936_r3` suffix convention. The net effect (different IP per retry) is identical.
- Camoufox is launched fresh per Crawlee browser-pool slot; there is no persistent browser reuse across multiple place URLs (same as `main.py` behaviour since each place requires a new context).
- Crawlee's `retry_on_blocked` heuristics are disabled; all block/captcha detection relies on the custom 12-second polling loop inherited from `main.py`.
