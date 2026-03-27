"""
TripAdvisor GraphQL review fetcher.

fetch_reviews_via_graphql  — executes a GraphQL request from within the browser
                             page (using page.evaluate) and returns parsed reviews.
Constants: PARALLEL_REQUESTS, REVIEWS_PER_PAGE, PUSH_BATCH_SIZE, REVIEWS_QUERY_ID.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from apify import Actor
from playwright.async_api import Page

from .parsers import parse_review_from_graphql


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
