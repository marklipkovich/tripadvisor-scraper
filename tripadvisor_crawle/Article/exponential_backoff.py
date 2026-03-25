if retry_count > 0:
    # Exponential backoff: 3 s, 6 s, 12 s … with ±1 s jitter
    backoff = 3.0 * (2 ** (retry_count - 1)) + random.uniform(0.5, 1.5)
    Actor.log.info(
        f"  Backoff {backoff:.1f}s before retry attempt {attempt}/{total_attempts} …"
    )
    await asyncio.sleep(backoff)