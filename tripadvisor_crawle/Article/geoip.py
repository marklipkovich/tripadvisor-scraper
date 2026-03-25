proxy_url: str | None = None
if self._proxy_url_getter is not None:
    proxy_url = await self._proxy_url_getter()

launch_options: dict = {
    "os": "windows",
    "block_webrtc": True,
    "locale": "en-US",
    **self._browser_launch_options,
}
launch_options["headless"] = is_headless
if proxy_url:
    try:
        launch_options["proxy"] = _apify_proxy_url_to_playwright_proxy(proxy_url)
    except ValueError as exc:
        Actor.log.warning(f"  Invalid proxy URL for Camoufox launch: {exc}")
    else:
        launch_options["geoip"] = True

    try:
        browser = await AsyncNewBrowser(self._playwright, **launch_options)
    except (NotInstalledGeoIPExtra, InvalidIP, InvalidProxy) as exc:
        Actor.log.warning(
            f"  camoufox geoip/proxy setup failed ({type(exc).__name__}: {exc}) — "
            "retrying launch without browser-level proxy/geoip "
            "(Crawlee still applies proxy on the context)."
        )
        launch_options.pop("geoip", None)
        launch_options.pop("proxy", None)
        browser = await AsyncNewBrowser(self._playwright, **launch_options)
else:
    browser = await AsyncNewBrowser(self._playwright, **launch_options)