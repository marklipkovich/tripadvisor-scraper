browser_pool = BrowserPool(
            plugins=[CamoufoxPlugin(
                browser_state=browser_state,
                proxy_url_getter=_proxy_url_for_geoip,
            )],
            browser_inactive_threshold=timedelta(minutes=30),
            identify_inactive_browsers_interval=timedelta(minutes=30),
        )