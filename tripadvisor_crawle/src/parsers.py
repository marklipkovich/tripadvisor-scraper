"""
HTML/JSON-LD and GraphQL response parsers.

parse_place_from_jsonld  — extracts place metadata from a schema.org JSON-LD blob.
parse_review_from_graphql — extracts reviews from TripAdvisor GraphQL response list.
EXTRACT_PAGE_SCRIPT      — client-side JS injected via page.evaluate() to pull
                           JSON-LD, __NEXT_DATA__, rating distribution, and DOM reviews.
"""

from __future__ import annotations

from typing import Any


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
        "url": url,
        "name": ld.get("name") or "",
        "placeType": ld.get("@type") or "LodgingBusiness",
        "rating": rating_value,
        "review_count": review_count,
        "address": street,
        "city": locality,
        "region": region,
        "country": country,
        "priceRange": ld.get("priceRange") or "",
        "image": ld.get("image") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE EXTRACTION SCRIPT
# ══════════════════════════════════════════════════════════════════════════════

EXTRACT_PAGE_SCRIPT = r"""
() => {
    const result = { place: null, reviews: [] };

    // 1. JSON-LD (schema.org)
    // Restaurants/attractions often wrap their data in an @graph array instead of
    // a flat top-level object — flatten those before checking @type.
    const _PLACE_TYPES = ['LodgingBusiness','Restaurant','FoodEstablishment',
                          'TouristAttraction','LocalBusiness','Product','Service'];
    function _typeMatch(t) {
        if (!t) return false;
        const types = Array.isArray(t) ? t : [t];
        return types.some(v => _PLACE_TYPES.some(pt => String(v).includes(pt)));
    }
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of ldScripts) {
        try {
            const d = JSON.parse(s.textContent);
            const top = Array.isArray(d) ? d : [d];
            // Flatten: push top-level items AND any nested @graph entries
            const items = [];
            for (const t of top) {
                items.push(t);
                if (Array.isArray(t['@graph'])) items.push(...t['@graph']);
            }
            for (const item of items) {
                if (_typeMatch(item['@type'])) {
                    result.place = item;
                    break;
                }
            }
        } catch(e) {}
        if (result.place) break;
    }

    // 2. __NEXT_DATA__ place info (if JSON-LD empty or missing name)
    if (!result.place || !result.place.name) {
        try {
            const nd = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
            function findPlace(obj, depth) {
                if (!obj || typeof obj !== 'object' || depth > 6) return null;
                if (obj.name && obj.locationId && (obj.accommodationCategory ||
                    obj.restaurantCuisine !== undefined || obj.subtype || obj.productType)) return obj;
                for (const k of Object.keys(obj).slice(0, 30)) {
                    const r = findPlace(obj[k], depth + 1);
                    if (r) return r;
                }
                return null;
            }
            const pl = findPlace(nd, 0);
            if (pl && !result.place) {
                // Extract as many fields as possible from __NEXT_DATA__ so
                // parse_place_from_jsonld() can build a complete place object.
                const rs = pl.reviewSummary || {};
                const addr = pl.address || {};
                result.place = {
                    name: pl.name || '',
                    locationId: pl.locationId,
                    aggregateRating: {
                        ratingValue: rs.rating || rs.ratingValue || '',
                        reviewCount: rs.count || rs.reviewCount || pl.reviewCount || 0,
                    },
                    address: {
                        streetAddress:  addr.street  || addr.streetAddress  || '',
                        addressLocality: addr.city   || addr.addressLocality || '',
                        addressRegion:  addr.state   || addr.addressRegion   || '',
                        addressCountry: addr.country || addr.addressCountry  || '',
                    },
                    priceRange: pl.priceRange || pl.priceLevel || pl.priceLevelStr || '',
                    image: pl.image || '',
                };
            }
        } catch(e) {}
    }

    // 3a. Rating distribution — search __NEXT_DATA__ for several known shapes
    try {
        const ndEl = document.getElementById('__NEXT_DATA__');
        if (ndEl) {
            function _toRatingDist(obj) {
                if (!obj || typeof obj !== 'object') return null;
                if (['1','2','3','4','5'].every(k => typeof obj[k] === 'number')) {
                    return { excellent: obj['5'], good: obj['4'], average: obj['3'], poor: obj['2'], terrible: obj['1'] };
                }
                if (typeof obj['EXCELLENT'] === 'number') {
                    return { excellent: obj['EXCELLENT'], good: obj['VERY_GOOD']||obj['GOOD']||0, average: obj['AVERAGE']||0, poor: obj['POOR']||0, terrible: obj['TERRIBLE']||0 };
                }
                if (Array.isArray(obj) && obj.length >= 5 && typeof obj[0].count === 'number') {
                    const m = {};
                    obj.forEach(e => { m[String(e.ratingValue||e.rating||e.value)] = e.count; });
                    if (['1','2','3','4','5'].every(k => typeof m[k] === 'number')) {
                        return { excellent: m['5'], good: m['4'], average: m['3'], poor: m['2'], terrible: m['1'] };
                    }
                }
                return null;
            }
            function _findRatingCounts(obj, depth) {
                if (!obj || typeof obj !== 'object' || depth > 10) return null;
                const d = _toRatingDist(obj);
                if (d) return d;
                const keys = Object.keys(obj);
                const prio = ['ratingCounts','reviewRatingCounts','distribution','ratingDistribution','subRatings','histogram'];
                for (const k of prio) {
                    if (obj[k]) { const r = _findRatingCounts(obj[k], depth + 1); if (r) return r; }
                }
                for (const k of keys.slice(0, 60)) {
                    if (prio.includes(k)) continue;
                    const r = _findRatingCounts(obj[k], depth + 1);
                    if (r) return r;
                }
                return null;
            }
            const nd = JSON.parse(ndEl.textContent);
            const dist = _findRatingCounts(nd, 0);
            if (dist) {
                if (!result.place) result.place = {};
                result.place.ratingDistribution = dist;
            }
        }
    } catch (_) {}

    // 3. DOM reviews (data-reviewid, data-test-target, data-automation)
    let reviewBlocks = document.querySelectorAll('[data-reviewid]');
    if (reviewBlocks.length === 0) {
        reviewBlocks = document.querySelectorAll('[data-automation="reviewCard"]');
    }
    if (reviewBlocks.length === 0) {
        reviewBlocks = document.querySelectorAll('[data-test-target="HR_CC_CARD"]');
    }
    for (const block of reviewBlocks) {
        try {
            const rid = block.getAttribute('data-reviewid') || '';
            const titleEl = block.querySelector('[data-test-target="review-title"] span, .noQuotes, .title');
            const textEl = block.querySelector('[data-test-target="review-body"] span, .reviewText span, .review-container .entry span');
            const ratingEl = block.querySelector('[class*="ui_bubble_rating"]');
            const ratingMatch = ratingEl ? (ratingEl.className.match(/bubble_(\d+)/) || []) : [];
            const dateEl = block.querySelector('[data-test-target="review-date"], .ratingDate');
            result.reviews.push({
                review_id: rid,
                title: titleEl ? titleEl.innerText.trim() : '',
                text: textEl ? textEl.innerText.trim() : '',
                rating: ratingMatch[1] ? parseInt(ratingMatch[1]) / 10 : null,
                date: dateEl ? (dateEl.getAttribute('title') || dateEl.innerText.trim()) : '',
            });
        } catch(e) {}
    }
    return result;
}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  GRAPHQL REVIEW PARSING
# ══════════════════════════════════════════════════════════════════════════════

def dig(obj: Any, *keys) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list):
            try:
                obj = obj[k]
            except (IndexError, TypeError):
                return None
        else:
            return None
    return obj


def _safe_avatar_url(user: dict) -> str:
    avatar = user.get("avatar") or user.get("userAvatar") or {}
    if isinstance(avatar, str):
        return avatar
    if isinstance(avatar, dict):
        photo = avatar.get("photoSizeDynamic") or avatar.get("photo") or {}
        if isinstance(photo, dict):
            tpl = photo.get("urlTemplate") or ""
            if tpl:
                return tpl.replace("{width}", "100").replace("{height}", "100")
        return avatar.get("url") or avatar.get("smallUrl") or ""
    return ""


def _extract_reviews_from_obj(obj: Any, results: list, depth: int = 0) -> None:
    if depth > 6 or not isinstance(obj, (dict, list)):
        return
    if isinstance(obj, list):
        for item in obj:
            _extract_reviews_from_obj(item, results, depth + 1)
        return
    if obj.get("text") and obj.get("rating") and obj.get("publishedDate"):
        results.append(obj)
        return
    for v in obj.values():
        _extract_reviews_from_obj(v, results, depth + 1)


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
        if reviews_data is None:
            reviews_data = (
                inner.get("LocationReviews__getReviews")
                or inner.get("SocialData_getSocialObjects")
                or inner.get("SocialData")
                or inner.get("LocationReviews")
                or inner.get("reviews")
                or inner.get("reviewList")
            )
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
                if not text and not r.get("title"):
                    continue
                user = r.get("user") or r.get("userProfile") or r.get("author") or {}
                if not isinstance(user, dict):
                    user = {}
                name = user.get("displayName") or user.get("name") or user.get("username") or ""
                rating = r.get("rating")
                if rating is None and isinstance(r.get("tripInfo"), dict):
                    rating = r.get("tripInfo", {}).get("rating")
                date_val = (
                    r.get("publishedDate") or r.get("createdAt")
                    or r.get("date") or r.get("submittedDateTime") or ""
                )
                rid = str(r.get("id") or r.get("reviewId") or r.get("objectId") or len(results))
                loc = r.get("location") or {}
                if not isinstance(loc, dict):
                    loc = {}
                detail = r.get("reviewDetailPageWrapper") or {}
                route = (detail.get("reviewDetailPageRoute") or {}) if isinstance(detail, dict) else {}
                review_url = (
                    "https://www.tripadvisor.com" + str(route["url"])
                    if isinstance(route, dict) and route.get("url") else ""
                )
                trip_info = r.get("tripInfo") or {}
                stay_date = trip_info.get("stayDate") or "" if isinstance(trip_info, dict) else ""
                if stay_date and len(stay_date) >= 7:
                    travel_date = stay_date[:7]
                else:
                    travel_date = str(date_val)[:7] if date_val else ""
                trip_type = (trip_info.get("tripType") or trip_info.get("type") or "") if isinstance(trip_info, dict) else ""
                contrib = user.get("contributionCounts") or {} if isinstance(user, dict) else {}
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
                addl = r.get("additionalRatings") or []
                subratings = []
                if isinstance(addl, list):
                    for a in addl:
                        if isinstance(a, dict) and a.get("ratingLabelLocalizedString"):
                            subratings.append({
                                "name": a.get("ratingLabelLocalizedString"),
                                "value": int(a.get("rating") or 0),
                            })
                photos_raw = r.get("photos") or []
                photos_list = []
                if isinstance(photos_raw, list):
                    for p in photos_raw:
                        ph = p.get("photo") if isinstance(p, dict) else p
                        if isinstance(ph, dict):
                            dyn = ph.get("photoSizeDynamic") or {}
                            url_tpl = dyn.get("urlTemplate") or ""
                            if url_tpl:
                                photos_list.append({
                                    "id": str(ph.get("id") or ""),
                                    "image": url_tpl.replace("{width}", "640").replace("{height}", "480"),
                                })
                place_name = loc.get("name") or "" if isinstance(loc, dict) else ""
                place_web_url = (
                    "https://www.tripadvisor.com" + str(loc.get("url") or "")
                ) if isinstance(loc, dict) and loc.get("url") else ""
                place_info = {
                    "id": str(loc.get("locationId") or r.get("locationId") or ""),
                    "name": place_name,
                    "webUrl": place_web_url,
                } if isinstance(loc, dict) else {}
                results.append({
                    "id": rid,
                    "url": review_url,
                    "title": (r.get("title") or "").strip(),
                    "text": (text or "").strip() if isinstance(text, str) else str(text).strip(),
                    "rating": int(rating) if rating is not None else None,
                    "lang": r.get("language") or "en",
                    "originalLanguage": r.get("originalLanguage") or r.get("language") or "en",
                    "publishedDate": str(date_val)[:50] if date_val else "",
                    "travelDate": travel_date,
                    "tripType": trip_type,
                    "helpfulVotes": int(r.get("helpfulVotes") or r.get("helpful_votes") or 0),
                    "reviewerName": name,
                    "placeName": place_name,
                    "placeUrl": place_web_url,
                    "publishedPlatform": r.get("publishPlatform"),
                    "locationId": str(loc.get("locationId") or r.get("locationId") or ""),
                    "subratings": subratings,
                    "ownerResponse": owner_resp,
                    "photos": photos_list,
                    "user": {
                        "userId": user.get("id") or "",
                        "displayName": name,
                        "username": user.get("username") or "",
                        "avatar": _safe_avatar_url(user),
                        "contributions": contrib,
                    } if isinstance(user, dict) else {},
                    "placeInfo": place_info,
                    "date": str(date_val)[:50] if date_val else "",
                })
        if not results:
            _extract_reviews_from_obj(inner, results)
    return results
