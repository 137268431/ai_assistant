
const { chromium, devices } = require('playwright');
(async () => {
  const targets = [
    { name: 'desktop', opts: {} },
    { name: 'iPhone 12', opts: devices['iPhone 12'] },
  ];
  const results = [];
  for (const target of targets) {
    const browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ ignoreHTTPSErrors: true, ...target.opts });
    const page = await context.newPage();
    const errors = [];
    page.on('console', msg => { if (msg.type() === 'error') errors.push(`console:${msg.text()}`); });
    page.on('pageerror', err => errors.push(`page:${err.message}`));
    page.on('response', res => { if (res.status() >= 400) errors.push(`http:${res.status()} ${res.url()}`); });
    await page.goto('https://pb.lzw-glory.top/ibkr_system.html?environment=live', { waitUntil: 'networkidle', timeout: 90000 });
    results.push({
      device: target.name,
      title: await page.title(),
      errors,
      statusBar: await page.locator('#statusBar').innerText(),
      backtest: await page.locator('#backtestArea').innerText(),
    });
    await browser.close();
  }
  console.log(JSON.stringify(results, null, 2));
})();
