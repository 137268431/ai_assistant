const { chromium, devices } = require('playwright');
const consoleBase = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const email = '137268431@qq.com';
const password = 'Asd@2750066';
const target = `${consoleBase}/ibkr_backtests.html`;

async function login(page) {
  await page.goto(`${consoleBase}/login.html`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const emailInput = page.locator('input[type="email"], input[name="identity"]');
  if (!(await emailInput.count())) return;
  await emailInput.first().fill(email);
  await page.locator('input[type="password"]').first().fill(password);
  await page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]').first().click();
  await page.waitForTimeout(2500);
}

async function inspect(browser, mobile) {
  const context = mobile ? await browser.newContext({ ...devices['iPhone 12'] }) : await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) errors.push(`console:${msg.type()}:${msg.text()}`);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) errors.push(`response:${resp.status()}:${resp.url()}`);
  });

  await login(page);
  await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(3500);

  const title = await page.title();
  const nav = await page.locator('#nav .nav-item').allTextContents().catch(() => []);
  const body = await page.locator('body').innerText().catch(() => '');
  const metricCards = await page.locator('.metric-card').count().catch(() => 0);
  const tableRows = await page.locator('table tbody tr').count().catch(() => 0);
  const notes = await page.locator('.empty-state,.toast,.error,.status-note').allTextContents().catch(() => []);

  console.log(JSON.stringify({
    device: mobile ? 'mobile' : 'desktop',
    title,
    nav_count: nav.length,
    nav,
    metric_cards: metricCards,
    table_rows: tableRows,
    notes: notes.slice(0, 12),
    body_sample: body.slice(0, 1200),
    errors,
  }, null, 2));

  await context.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  await inspect(browser, false);
  await inspect(browser, true);
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
