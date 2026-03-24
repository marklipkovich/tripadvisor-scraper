PARALLEL_REQUESTS = 50
reviews_per_page = 10
reviews_offset = 0

while True:
    # Offsets for this batch: 0, 10, 20, …, 390 when PARALLEL_REQUESTS == 40
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

    # ... process batch_results (parse JSON, dedupe, append to reviews) ...

    if not got_any or got_partial:
        break
    reviews_offset += PARALLEL_REQUESTS * reviews_per_page
    await asyncio.sleep(random.uniform(0.8, 1.5))