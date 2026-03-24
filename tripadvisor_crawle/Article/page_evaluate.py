result = await page.evaluate(
                """
                async (args) => {
                    const resp = await fetch(args.url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'Content-Type': 'application/json',
                            'Accept': '*/*',
                            'Origin': 'https://www.tripadvisor.com',
                            'Referer': window.location.href,
                        },
                        body: JSON.stringify(args.payload),
                    });
                    if (!resp.ok) return null;
                    return await resp.json();
                }
                """,
                {"url": url, "payload": payload},
            )