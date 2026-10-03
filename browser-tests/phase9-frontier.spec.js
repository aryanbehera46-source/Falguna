const { test, expect } = require('@playwright/test');

for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test('persistent objective creates, plans, checkpoints and survives reload', async ({ page }) => {
      const errors = [];
      page.on('console', m => { if (['warning', 'error'].includes(m.type())) errors.push(m.text()); });
      page.on('pageerror', e => errors.push(e.message));
      await page.goto('http://127.0.0.1:8883/#/frontier');
      await expect(page.getByRole('heading', { name: 'Persistent objectives' })).toBeVisible();
      await page.getByLabel('New objective').fill(`Synthetic Phase 9 objective ${viewport.width}`);
      await page.getByPlaceholder('Outcome, constraints and evidence expected').fill('Persist a safe local execution graph and continuity evidence.');
      await page.getByRole('button', { name: 'Create objective' }).click();
      await expect(page.getByRole('heading', { name: `Synthetic Phase 9 objective ${viewport.width}` })).toBeVisible();
      await page.getByRole('button', { name: 'Create execution graph' }).click();
      await expect(page.getByText('Inspect context and constraints')).toBeVisible();
      await expect(page.getByText('Produce checkpoint and finished deliverables')).toBeVisible();
      await page.getByRole('button', { name: 'Save continuity bundle' }).click();
      await expect(page.getByText('Continuity bundle v1 saved')).toBeVisible();
      await page.getByRole('button', { name: 'Pause' }).click();
      await expect(page.getByText('PAUSED', { exact: true })).toBeVisible();
      await page.reload();
      await expect(page.getByText('Inspect context and constraints')).toBeVisible();
      await expect(page.getByRole('button', { name: 'Resume' })).toBeVisible();
      await page.getByRole('button', { name: 'Resume' }).click();
      await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      expect(errors).toEqual([]);
    });

    test('hard safety boundaries stay visible', async ({ page }) => {
      await page.goto('http://127.0.0.1:8883/#/frontier');
      await expect(page.getByText(/Live external actions, production deployment, payments and live trading are disabled/)).toBeVisible();
      await expect(page.getByText(/commercial mutation disabled/i)).toBeVisible();
    });
  });
}
