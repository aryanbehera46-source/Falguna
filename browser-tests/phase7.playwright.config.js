const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'phase7-authenticated.spec.js',
  reporter: 'line',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8878', headless: true, trace: 'retain-on-failure' },
  webServer: {
    command: 'cd .. && PYTHONPATH=. python3 tests/phase7_browser_server.py',
    url: 'http://127.0.0.1:8878/portal/login',
    reuseExistingServer: false,
    timeout: 20000,
  },
});
