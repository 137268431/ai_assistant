const { chromium } = require('playwright');
const consoleBase = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const email = '137268431@qq.com';
const password = 'Asd@2750066';
const runName = 'UI Batch Smoke 1775261917932';

async function login(page) {
  await page.goto(`${consoleBase}/login.html`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const emailInput = page.locator('input[type="email"], input[name="identity"]');
  if (!(await emailInput.count())) return;
  await emailInput.first().fill(email);
  await page.locator('input[type="password"]').first().fill(password);
  await page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]').first().click();
  await page.waitForTimeout(2500);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => { if (['error', 'warning'].includes(msg.type())) errors.push(`console:${msg.type()}:${msg.text()}`); });
  page.on('response', (resp) => { if (resp.status() >= 400) errors.push(`response:${resp.status()}:${resp.url()}`); });
  await login(page);
  await page.goto(`${consoleBase}/ibkr_backtests.html`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(3000);
  await page.evaluate(() => { window.confirm = () => true; });
  await page.locator('button:has-text("清理 Experiment")').first().click();
  await page.waitForTimeout(5000);
  const body = await page.locator('body').innerText();
  console.log(JSON.stringify({removed: !body.includes(runName), errors, body_sample: body.slice(0, 1800)}, null, 2));
  await browser.close();
})().catch((err) => { console.error(err); process.exit(1); });
