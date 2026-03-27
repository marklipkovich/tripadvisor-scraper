## What does TripAdvisor Reviews Scraper (Crawlee Edition) do?

**TripAdvisor Reviews Scraper (Crawlee Edition)** extracts **public review data** from **[TripAdvisor](https://www.tripadvisor.com/)** place pages (hotels, restaurants, attractions). The easiest way to try it: add one or more `https://www.tripadvisor.com/` **place URLs**, turn on **[Apify Proxy](https://docs.apify.com/platform/proxy)** with a **residential** group, and start a run from the Apify Console.

The Actor does **not** log into TripAdvisor, bypass paywalls, or access private account data. It collects what is visible on public listing pages and what TripAdvisor serves to the browser for those listings.

**Input in one sentence:** you provide **start URLs**, optional **filters** (max reviews per place, date range, star ratings, language), and **proxy configuration** — the Actor returns structured reviews plus a **places** summary in the key-value store.

---

## Why use this Actor to scrape TripAdvisor?

- **Structured data, not fragile HTML** — Reviews are loaded via TripAdvisor’s **GraphQL** layer (`/data/graphql/ids`) from inside a real browser session, so field names stay stable when the page layout changes.
- **Built for tough bot protection** — The stack uses **[Crawlee](https://crawlee.dev/python)** **`PlaywrightCrawler`** with **[Camoufox](https://camoufox.com/)** (stealth-oriented Firefox) instead of default Chromium, aligned with how **DataDome-class** protection fingerprints browsers.
- **Faster runs** — **Parallel GraphQL pagination** (many requests per batch via `asyncio.gather`), **`domcontentloaded`** navigation where appropriate, and **blocking of images, fonts, and media** cut load time and compute use compared to full page loads.
- **Apify platform, not just a script** — The same Actor run gets **[scheduling](https://docs.apify.com/platform/schedules)**, **[API](https://docs.apify.com/api/v2)**, **[webhooks](https://docs.apify.com/platform/webhooks)**, **[integrations](https://apify.com/integrations)**, dataset export, and **proxy rotation** managed for you.

**Typical uses:** reputation monitoring, competitor benchmarking, sentiment and NLP pipelines, market research, and training datasets (using only data you may lawfully process).

---

## What can this Actor do?

- Scrape **multiple places** in one run from a list of TripAdvisor **place URLs**.
- Filter by **maximum reviews per place**, **publish date range**, **star rating**, and **review language**.
- Write **one row per review** to the **default dataset**, with rich fields (see table below).
- Save **place-level metadata** to the key-value store as **`Places.json`** and a readable **`Places.md`** summary (see **Output**).
- On **Apify Cloud**, expect reliable operation only with **residential** proxy IPs; datacenter IPs are often blocked regardless of browser hardening.

---

## What data can you extract from TripAdvisor?

| Data point | Description |
|------------|-------------|
| **Place** | Place name, link to TripAdvisor place URL |
| **Review text** | Title and body of the review |
| **Star rating** | 1–5 rating for the review |
| **Dates** | Published date; travel date when available |
| **Trip type** | e.g. business, family (when provided) |
| **Language** | Detected or stated review language |
| **Reviewer** | Public display name (not private account data) |
| **Engagement** | Helpful vote counts when exposed |
| **Owner response** | Management reply object when present |
| **Sub-ratings** | Detailed category scores when present |
| **Links** | Review URL when available |

The Console **Output** tab uses the Actor’s **dataset views** (“Overview” and “Full Details”) so you can switch between a compact table and all fields.

---

## How does this Actor optimize run time and anti-blocking?

**Speed (lower compute, shorter runs)**

- **GraphQL-first** — After the place page establishes a valid session, the Actor requests review pages through TripAdvisor’s API instead of scrolling and parsing the DOM for every review.
- **Parallel fetches** — Review pages are requested in **parallel batches** (implemented with `asyncio.gather` and a fixed parallel width in code) so each wave brings back many small pages at once.
- **Lean navigation** — Page loads use **`domcontentloaded`** and avoid waiting for full idle networks where that would slow the crawl without helping GraphQL.
- **Resource blocking** — **Images, fonts, and media** are aborted at the route level so bandwidth and CPU go to API calls, not assets.

**Anti-blocking (session and fingerprint consistency)**

- **Camoufox** — Uses a **stealth-oriented Firefox** profile suited to strict fingerprinting, integrated through Crawlee’s **browser plugin** model.
- **`geoip` alignment** — With Apify Proxy, the browser launch path can align **timezone/locale-style signals** with the **proxy exit** (see logs at run time). Mismatched geo vs IP is a common block trigger.
- **Stable proxy + browser session** — The implementation favors **reusing the same browser and proxy** across places so **session trust** accumulates; the **browser pool** uses a **long inactive threshold** so Crawlee does not retire the browser mid-job and discard that session.
- **Residential proxy** — On Apify Cloud, **select residential proxy groups** in input; datacenter IPs are frequently challenged or blocked.

If TripAdvisor serves a **captcha** or hard block for the current session, the Actor surfaces that as a **block** condition so you can retry with policy changes (proxy, timing, or smaller batches) instead of silently returning empty data.

---

## How to scrape TripAdvisor reviews with this Actor

1. Open the Actor on **[Apify Console](https://console.apify.com/)** and select the **Input** tab.
2. Add **Place URLs** — each must start with `https://www.tripadvisor.com/` (hotel, restaurant, or attraction review pages).
3. Set **Proxy configuration** → enable **Apify Proxy** and choose a **RESIDENTIAL** group (strongly recommended on the cloud platform).
4. Optionally set **Max reviews per place**, **Start / end date**, **Review ratings**, and **Review language**.
5. Click **Start**. Monitor **Log** for geo/proxy and GraphQL messages.
6. Open **Dataset** for review rows; open **Storage → Key-value store** for **`Places.json`** and **`Places.md`**.

---

## How much does it cost to scrape TripAdvisor?

Cost depends on **Apify pricing** (subscription or pay-as-you-go), **compute units (CUs)** your run consumes, and **proxy traffic** (residential proxy is billed separately from raw compute).

**What drives CUs here**

- **Browser time** — Each place needs a **real browser context** to satisfy anti-bot checks before GraphQL can run.
- **Number of reviews** — More reviews mean **more GraphQL round-trips** (even though they are parallelized).
- **Parallelism** — Batches reduce wall-clock time versus strictly serial fetching, which often **lowers total cost** for the same review count.

**Tips to control cost**

- Use **date**, **rating**, and **language** filters to avoid downloading reviews you will discard later.
- Set **Max reviews per place** during testing, then raise it for production pulls.
- Prefer **one stable residential session** over aggressive proxy rotation unless you are reacting to a block.

Apify’s **free tier** lets you explore small runs; for production volumes, plan for **residential proxy** + steady **CU** budget. See **[Apify pricing](https://apify.com/pricing)** for current plans.

---

## Input

TripAdvisor Reviews Scraper has the following input options. For field-level help, open the **Input** tab in Apify Console (tooltips come from the Actor **input schema**).

| Field | Type | Notes |
|-------|------|--------|
| **Place URLs** (`startUrls`) | List of URLs | Required. TripAdvisor.com place pages only. |
| **Max reviews per place** | Integer | `0` = no limit (subject to site availability). |
| **Start date** / **End date** | `YYYY-MM-DD` | Optional publish-date window. |
| **Review ratings** | Multi-select | Optional filter by 1–5 stars. |
| **Review language** | Select | Optional ISO-style language filter; empty = all. |
| **Proxy configuration** | Proxy object | Use **Apify Proxy** with **residential** group on cloud. |

### Optional: screenshots in this readme

**Plain GitHub `README.md` files often have no images** — that is normal. On **Apify**, the same markdown is also your **Actor’s Store page**; Apify’s own readme guide suggests **optional** screenshots (especially the **Input** form) because visitors often want to see the Console before signing up. You can ship a **text-only** readme; images are **not** required for the Actor to work.

If you **do** add images:

1. Save files in the repo (e.g. `docs/readme-input.png`) or host on a stable URL.
2. In `README.md`, use Markdown:

   `![Short description for accessibility](./docs/readme-input.png)`

   Or a full URL: `![Input form](https://raw.githubusercontent.com/USER/REPO/branch/docs/readme-input.png)`  
   (Use the **raw** file URL if the image is on GitHub and does not render otherwise.)

3. Good candidates: **Input** tab (place URLs + proxy), then optionally **Dataset** preview or **Places.json** in storage — only Apify Console context, not DevTools/blog art.

Apify Console also **auto-embeds** a **YouTube** link if you put the video URL alone on its own line (see Apify readme guide).

---

## Output

You can download data extracted by this Actor in **JSON**, **CSV**, **Excel**, **HTML**, and other formats from the **Dataset** and **Storage** tabs, or via the **[Apify API](https://docs.apify.com/api/v2)**.

Per **`output_schema.json`**:

- **Dataset (default)** — **one object per review** (see example below).
- **Key-value store — `Places.json`** — Place metadata (ratings distribution, counts, oldest review date, etc.).
- **Key-value store — `Places.md`** — Human-readable summary of scraped places.

### Example dataset item (simplified)

```json
{
  "placeName": "Example Hotel",
  "placeUrl": "https://www.tripadvisor.com/Hotel_Review-...",
  "title": "Great stay",
  "text": "We enjoyed the location and the staff...",
  "rating": 5,
  "publishedDate": "2025-01-15",
  "travelDate": "2024-12",
  "tripType": "Family",
  "lang": "en",
  "reviewerName": "Traveler123",
  "helpfulVotes": 3,
  "subratings": [],
  "ownerResponse": null,
  "url": "https://www.tripadvisor.com/ShowUserReviews-..."
}
```

Full field set matches the **Full Details** view in **`dataset_schema.json`** (`id`, `originalLanguage`, etc.).

---

## Tips and advanced notes

- **Always test with a small `maxReviewsPerPlace` and one URL** before large jobs.
- If you see **blocks or empty GraphQL**, confirm **residential proxy** and that URLs are **canonical TripAdvisor.com** place pages.
- **Do not rotate proxy on every place** unless you must — session stability usually improves success rate and can **reduce repeated challenges** (and cost).
- Integrate with **Make**, **Zapier**, **Slack**, or custom pipelines via **[Apify webhooks and API](https://docs.apify.com/platform/integrations)**.

---

## Other Actors

If you use **Apify** for more sources, browse the **[Apify Store](https://apify.com/store)** for other scrapers by the same author (for example **YouTube** transcript tooling). Link your Store profile here when published so users can discover related Actors.

---

## FAQ, disclaimers, and support

### Is it legal to scrape TripAdvisor?

This Actor only extracts **public** content shown to visitors. It does **not** target private inboxes, passwords, or hidden profile data. You are responsible for **compliance** with TripAdvisor’s **Terms of Use**, applicable **copyright**, and **data-protection** rules (e.g. **GDPR**) when you store or process reviews that may contain **personal data**. When in doubt, seek legal advice. Apify’s perspective on scraping legality is discussed in **[Apify’s web scraping legality blog post](https://blog.apify.com/is-web-scraping-legal/)**.

### Does TripAdvisor have an official API?

TripAdvisor offers **partner and commercial APIs** for eligible use cases. This Actor is **not** an official TripAdvisor product; it automates the **public website** like a user’s browser. Compare licensing and coverage against official APIs when building production products.

### Why am I getting blocked or captchas?

Common causes: **datacenter IP**, **proxy country/geo mismatch**, **too many places** on a cold session, or **rate spikes**. Prefer **residential** proxy, keep **one session** when possible, and reduce concurrency or volume when testing.

### Where can I get help?

- **[Apify Discord](https://discord.com/invite/jyEM2PRvMU)** — community and Apify staff.
- **Issues** — If your Actor listing exposes a GitHub or issue tracker, link it here for bug reports and feature requests.

---

## Resources

- [Apify SDK for Python](https://docs.apify.com/sdk/python)
- [Crawlee for Python](https://crawlee.dev/python)
- [PlaywrightCrawler guide](https://crawlee.dev/python/docs/guides/playwright-crawler)
- [Apify Proxy documentation](https://docs.apify.com/platform/proxy)
- [Actor input schema](https://docs.apify.com/platform/actors/development/input-schema)
- [Publishing to Apify Store](https://docs.apify.com/platform/actors/publishing)
