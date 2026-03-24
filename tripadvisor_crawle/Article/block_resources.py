async def _block_resources(route):
    if route.request.resource_type in ("image", "font", "media"):
        await route.abort()
    else:
        await route.continue_()


await page.route("**/*", _block_resources)