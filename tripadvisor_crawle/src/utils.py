"""
General utilities: retry helper, URL normalisation, place/output helpers.

with_retry            — async exponential-backoff retry wrapper.
normalize_place_url   — cleans and validates a TripAdvisor place URL.
extract_location_id_from_url — extracts the numeric location ID from a URL.
_normalize_place      — returns a place dict with a complete, fixed set of fields.
_build_places_md      — renders a list of place dicts to a Markdown document.
_date_sort_key        — sort key for review dicts by publishedDate / date.
"""

from __future__ import annotations

import asyncio
import random
import re
from typing import Optional
from urllib.parse import urlparse

from apify import Actor


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
#  OUTPUT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_place(raw: dict, url: str = "", loc_id: str = "") -> dict:
    """Return a place dict with a fixed, complete set of fields (no missing keys)."""
    return {
        "id":                raw.get("id") or loc_id or "",
        "url":               raw.get("url") or url or "",
        "name":              raw.get("name") or "",
        "placeType":         raw.get("placeType") or "",
        "rating":            raw.get("rating") or None,
        "totalReviews":      raw.get("review_count") or raw.get("totalReviews") or 0,
        "scrapedReviews":    raw.get("scrapedReviews") or raw.get("reviewCount") or 0,
        "address":           raw.get("address") or "",
        "city":              raw.get("city") or "",
        "region":            raw.get("region") or "",
        "country":           raw.get("country") or "",
        "priceRange":        raw.get("priceRange") or "",
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
        place_type = p.get("placeType") or ""
        rating     = p.get("rating") or ""
        total      = p.get("totalReviews") or 0
        scraped    = p.get("scrapedReviews") or 0
        address    = p.get("address") or ""
        city       = p.get("city") or ""
        region     = p.get("region") or ""
        country    = p.get("country") or ""
        price      = p.get("priceRange") or ""
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
#  REVIEW SORT KEY
# ══════════════════════════════════════════════════════════════════════════════

def _date_sort_key(r: dict) -> str:
    return str(r.get("publishedDate") or r.get("date") or "")
