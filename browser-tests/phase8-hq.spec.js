const { test, expect } = require('@playwright/test');

for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test('private HQ ecosystem workflow is authenticated, usable and responsive', async ({ page }) => {
      const errors = [];
      page.on('console', m => { if (['warning', 'error'].includes(m.type())) errors.push(m.text()); });
      page.on('pageerror', e => errors.push(e.message));
      await page.goto('http://127.0.0.1:8881/login');
      await page.getByLabel('Email').fill('owner@example.test');
      await page.getByLabel('Password').fill('correct-horse-battery-owner');
      await page.getByRole('button', { name: 'Sign in' }).click();
      await expect(page.getByText('Welcome, Synthetic Owner')).toBeVisible();
      await page.goto('http://127.0.0.1:8882/');
      if (viewport.width < 820) await page.getByRole('button', { name: 'Open navigation' }).click();
      await page.getByRole('button', { name: 'Ecosystem Review' }).click();
      await expect(page.getByRole('heading', { name: 'Digital Business Ecosystem' })).toBeVisible();
      await expect(page.getByText('Need a bilingual ordering workflow')).toBeVisible();
      await expect(page.getByText('Synthetic Specialist')).toBeVisible();
      await expect(page.getByText('Available for the reviewed synthetic scope')).toBeVisible();
      await expect(page.getByText('UNAUTHORIZED_PAYMENT_INSTRUCTION')).toBeVisible();
      await expect(page.getByText(/STARTUP_SMB · MARKET_RESEARCH/)).toBeVisible();
      await expect(page.getByText(/PRODUCTIZED_SERVICE · repeated workflow setup/)).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      expect(errors).toEqual([]);
    });

    test('route decision is human-reasoned and CSRF-backed', async ({ page }) => {
      await page.goto('http://127.0.0.1:8881/login');
      await page.getByLabel('Email').fill('owner@example.test');
      await page.getByLabel('Password').fill('correct-horse-battery-owner');
      await page.getByRole('button', { name: 'Sign in' }).click();
      await page.goto('http://127.0.0.1:8882/');
      if (viewport.width < 820) await page.getByRole('button', { name: 'Open navigation' }).click();
      await page.getByRole('button', { name: 'Ecosystem Review' }).click();
      const decide = page.getByRole('button', { name: 'Record decision' });
      await expect(decide.first()).toBeVisible();
      page.once('dialog', dialog => dialog.accept('Owner reviewed the synthetic intake'));
      await decide.first().click();
      await expect(page.getByText('Owner reviewed the synthetic intake')).toBeVisible();
    });
  });
}
