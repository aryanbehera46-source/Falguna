const { test, expect } = require('@playwright/test');

test('portable localhost verification runs without a fixed browser executable', async ({ page }) => {
  let externalBlocked = false;
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (!['127.0.0.1', 'localhost', '::1'].includes(url.hostname)) {
      externalBlocked = true;
      await route.abort('blockedbyclient');
      return;
    }
    await route.continue();
  });
  await page.goto('/fixture.html');
  await expect(page.locator('[data-falguna-browser-boundary]')).toHaveText('localhost-only');
  const result = await page.evaluate(async () => {
    try {
      await fetch('https://example.invalid/falguna-harmless-probe');
      return 'unexpected-success';
    } catch (error) {
      return 'blocked';
    }
  });
  expect(result).toBe('blocked');
  expect(externalBlocked).toBe(true);
  console.log('FALGUNA_NETWORK_BOUNDARY external_blocked=true localhost_ok=true');
});
