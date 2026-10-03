const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'phase9-frontier.spec.js',
  reporter: 'line',
  workers: 1,
  use: { headless: true, trace: 'retain-on-failure' },
  webServer: {
    command: 'cd .. && PYTHONPATH=. python3 tests/phase9_browser_server.py',
    url: 'http://127.0.0.1:8883/',
    reuseExistingServer: false,
    timeout: 20000,
  },
});
