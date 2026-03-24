# With Camoufox + residential proxy, DataDome often passes the fingerprint check
# and removes the captcha automatically within a few seconds.
# Poll up to 15 s for Camoufox to auto-resolve DataDome captcha
# Captcha self-resolve check

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
            # Crawlee catches this, rotates proxy/session, and retries the URL
            raise CaptchaBlockedError("DataDome captcha not bypassed with current proxy")





