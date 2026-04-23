const { chromium, devices } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';

function attachErrors(page) {
  const errors = [];
  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) errors.push(`console:${msg.type()}:${msg.text()}`);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) errors.push(`response:${resp.status()}:${resp.url()}`);
  });
  return errors;
}

async function login(page) {
  await page.goto(`${CONSOLE_BASE}/login.html`, { waitUntil: 'domcontentloaded' });
  const email = page.locator('input[type="email"], input[name="identity"]');
  if (!(await email.count())) return;
  await email.first().fill(EMAIL);
  await page.locator('input[type="password"]').first().fill(PASSWORD);
  await page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]').first().click();
  await page.waitForTimeout(1800);
}

async function checkStats(browser, mobile) {
  const context = mobile ? await browser.newContext({ ...devices['iPhone 12'] }) : await browser.newContext();
  const page = await context.newPage();
  const errors = attachErrors(page);
  await login(page);
  await page.goto(`${CONSOLE_BASE}/ibkr_stats.html?environment=live`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  const result = {
    test: mobile ? 'stats-mobile' : 'stats-desktop',
    url: page.url(),
    title: await page.title(),
    stat_cards: await page.locator('.stat-card').count(),
    chart_count: await page.locator('canvas').count(),
    bridge_texts: await page.locator('.page-bridge-link').allTextContents().catch(() => []),
    nav_texts: await page.locator('#nav .nav-item').allTextContents().catch(() => []),
    total_orders: await page.locator('#totalOrders').innerText().catch(() => 'ERR'),
    total_signals: await page.locator('#totalSignals').innerText().catch(() => 'ERR'),
    errors,
  };
  await context.close();
  return result;
}

async function checkBridge(browser, path) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = attachErrors(page);
  await login(page);
  await page.goto(`${CONSOLE_BASE}${path}?environment=live`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1800);
  await page.locator('.page-bridge-link', { hasText: '统计' }).first().click();
  await page.waitForTimeout(1800);
  const result = {
    test: `bridge:${path}`,
    url: page.url(),
    title: await page.title(),
    ok: page.url().includes('/ibkr_stats.html'),
    errors,
  };
  await context.close();
  return result;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const results = [];
  results.push(await checkStats(browser, false));
  results.push(await checkStats(browser, true));
  results.push(await checkBridge(browser, '/ibkr_runtime.html'));
  results.push(await checkBridge(browser, '/ibkr_account.html'));
  results.push(await checkBridge(browser, '/ibkr_system.html'));
  await browser.close();
  console.log(JSON.stringify(results, null, 2));
})();
