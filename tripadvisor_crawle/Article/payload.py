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