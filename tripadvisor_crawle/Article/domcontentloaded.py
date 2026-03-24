#domcontentloaded instead of networkidle

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