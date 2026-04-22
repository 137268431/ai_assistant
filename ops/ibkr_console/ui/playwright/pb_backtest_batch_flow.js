const { chromium } = require('playwright');
const base = 'https://pb.lzw-glory.top';
const email = '137268431@qq.com';
const password = 'Asd@2750066';
const runName = `UI Batch Smoke ${Date.now()}`;
const variants = JSON.stringify([
  { label: 'tight-risk', strategy_params: { sl_atr_mult: 1.6, tp_atr_mult: 2.4 } },
  { label: 'wide-risk', strategy_params: { sl_atr_mult: 2.2, tp_atr_mult: 3.6 } },
], null, 2);

async function login(page) {
  await page.goto(`${base}/login.html`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const emailInput = page.locator('input[type="email"], input[name="identity"]');
  if (!(await emailInput.count())) return;
  await emailInput.first().fill(email);
  await page.locator('input[type="password"]').first().fill(password);
  await page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]').first().click();
  await page.waitForTimeout(2500);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
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
  await page.goto(`${base}/ibkr_backtests.html`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(2500);

  await page.locator('#runName').fill(runName);
  await page.locator('#symbolsText').fill('AAPL');
  await page.locator('#dateFrom').fill('2026-04-01');
  await page.locator('#dateTo').fill('2026-04-01');
  await page.locator('#variantsJson').fill(variants);
  await page.locator('#startButton').click();

  await page.waitForFunction((expected) => {
    return document.body.innerText.includes(expected) && document.body.innerText.includes('parameter batch');
  }, runName, { timeout: 45000 });
  await page.waitForTimeout(3000);

  const bodyAfterRun = await page.locator('body').innerText();
  const batchVisible = bodyAfterRun.includes(runName);
  const completed = bodyAfterRun.includes('parameter batch completed');
  const batchIdMatch = bodyAfterRun.match(/Batch ID:\s*([a-z0-9]+)/i);
  const batchId = batchIdMatch ? batchIdMatch[1] : '';

  await page.evaluate(() => {
    window.confirm = () => true;
  });

  let cleanupOk = false;
  const cleanupButtons = page.locator('button:has-text("清理当前 experiment")');
  if (await cleanupButtons.count()) {
    await cleanupButtons.first().click();
    await page.waitForTimeout(5000);
    const bodyAfterCleanup = await page.locator('body').innerText();
    cleanupOk = !bodyAfterCleanup.includes(runName);
  }

  console.log(JSON.stringify({
    runName,
    batchVisible,
    completed,
    batchId,
    cleanupOk,
    errors,
    body_sample: bodyAfterRun.slice(0, 2200),
  }, null, 2));

  await context.close();
  await browser.close();
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
