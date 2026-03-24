# Create a list of common scale factors
SCALE_FACTORS = [1, 1.25, 1.5, 2]

launch_options: dict = {
    "os": chosen_os,
    "block_webrtc": True,


    # IMPROVEMENT: Randomize scale factor and hardware noise
    "device_scale_factor": random.choice([1, 1.25, 1.5, 2]),
    "webgl": True,  # Enables hardware-based rendering

    # Camoufox's "humanize" is good, but adding these
    # specific hardware masks is better:
    "java_script_enabled": True,

    # We'll leave locale as en-US for now as you noted it keeps GQL in English,
    # but we can make it more complex: "en-US,en;q=0.9"
    "locale": "en-US,en;q=0.9",

    "humanize": True,
    **self._browser_launch_options,
}