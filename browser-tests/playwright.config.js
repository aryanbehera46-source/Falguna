const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'portable-localhost.spec.js',
  reporter: 'line',
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:4173', headless: true },
  webServer: {
    command: 'python3 -m http.server 4173 --bind 127.0.0.1',
    cwd: __dirname,
    url: 'http://127.0.0.1:4173/fixture.html',
    reuseExistingServer: false,
    timeout: 15000,
  },
});
