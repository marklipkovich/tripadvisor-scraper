# Find slider, drag to the right
slider = page.locator('[data-testid="slider"]')  # adjust selector
box = await slider.bounding_box()
await page.mouse.move(box['x'], box['y'] + box['height']/2)
await page.mouse.down()
await page.mouse.move(box['x'] + 300, box['y'] + box['height']/2, steps=20)
await page.mouse.up()
```