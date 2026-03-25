from playwright.async_api import async_playwright
from camoufox import AsyncNewBrowser

async def run():
    async with async_playwright() as pw:
        browser = await AsyncNewBrowser(
            pw,
            headless=True,          # False locally; True on Apify Cloud
            os="windows",
            block_webrtc=True,
            locale="en-US",
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        await page.goto("https://example.com", wait_until="domcontentloaded")
        # … scrape …
        await context.close()
        await browser.close()

# asyncio.run(run())