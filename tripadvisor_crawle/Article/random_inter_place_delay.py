delay = 2.0 + random.uniform(0.0, 1.0)
Actor.log.info(f"  Inter-place delay: {delay:.1f}s …")
await asyncio.sleep(delay)