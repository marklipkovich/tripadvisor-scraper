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
        reviews_list = reviews_data.get("reviews") or reviews_data.get("reviewList") or reviews_data.get(
            "socialObjects") or []
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