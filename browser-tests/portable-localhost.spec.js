const { test, expect } = require('@playwright/test');

test('portable localhost verification runs without a fixed browser executable', async ({ page }) => {
  await page.goto('/fixture.html');
  await expect(page.locator('[data-falguna-browser-boundary]')).toHaveText('localhost-only');
});
