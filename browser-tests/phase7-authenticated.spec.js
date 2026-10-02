const { test, expect } = require('@playwright/test');

const users = {
  customer: ['customer@example.test', 'correct-horse-battery-customer'],
  partner: ['partner@example.test', 'correct-horse-battery-partner'],
};

async function login(page, role) {
  await page.goto('/portal/login');
  await page.getByLabel('Email').fill(users[role][0]);
  await page.getByLabel('Password').fill(users[role][1]);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test('customer authenticated journey is isolated and responsive', async ({ page }) => {
      const errors = [];
      page.on('console', m => { if (['warning', 'error'].includes(m.type())) errors.push(m.text()); });
      page.on('pageerror', e => errors.push(e.message));
      await login(page, 'customer');
      for (const route of ['/app', '/app/projects', '/app/requests', '/app/billing', '/app/support', '/app/account', '/pay']) {
        await page.goto(route);
        await expect(page.locator('main')).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      }
      await page.goto('/app/billing');
      await page.getByRole('link', { name: 'Browser milestone' }).click();
      await expect(page.getByRole('heading', { name: /Invoice/ })).toBeVisible();
      await page.goto('/app/requests');
      await page.getByLabel('What do you need?').fill('Please assess a synthetic inventory workflow for our test location.');
      await page.getByRole('button', { name: 'Record request' }).click();
      await expect(page.getByText(/No contract or assignment was created/)).toBeVisible();
      await page.goto('/partners/app');
      await expect(page).toHaveURL(/\/app$/);
      expect(errors).toEqual([]);
    });

    test('partner and public verification journeys are isolated and responsive', async ({ page }) => {
      const errors = [];
      page.on('console', m => { if (['warning', 'error'].includes(m.type())) errors.push(m.text()); });
      page.on('pageerror', e => errors.push(e.message));
      await login(page, 'partner');
      for (const route of ['/partners/app', '/partners/app/leads', '/partners/app/leads/new', '/partners/app/commissions', '/partners/app/opportunities', '/partners/app/account']) {
        await page.goto(route);
        await expect(page.locator('main')).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      }
      await page.goto('/partners/app/leads');
      await expect(page.getByText('Browser Lead')).toBeVisible();
      await page.goto('/partners/app/opportunities');
      await expect(page.getByText('Bilingual retail workflow implementation')).toBeVisible();
      await expect(page.getByText(/may not collect customer money/)).toBeVisible();
      await page.getByLabel('Capability statement').fill('I can deliver this synthetic scope using the reviewed workflow capability.');
      await page.getByRole('button', { name: 'Express interest' }).click();
      await expect(page.getByText(/not assigned yet/)).toBeVisible();
      await page.goto('/app');
      await expect(page).toHaveURL(/\/partners\/app$/);
      await page.goto('/partners');
      await page.getByLabel('Partner ID').fill('unknown-partner');
      await page.getByRole('button', { name: 'Verify partner' }).click();
      await expect(page.getByText('Unknown partner ID')).toBeVisible();
      await page.goto('/pay');
      await expect(page.getByRole('heading', { name: /Pay and verify/ })).toBeVisible();
      expect(errors).toEqual([]);
    });
  });
}
