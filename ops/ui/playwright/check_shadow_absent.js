const { chromium } = require('playwright');

const consoleBase = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const pbBase = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const email = process.env.PB_EMAIL || '137268431@qq.com';
const password = process.env.PB_PASSWORD || 'Asd@2750066';
const targets = [
  consoleBase + '/ibkr_runtime.html?environment=live',
  consoleBase + '/ibkr_system.html?environment=live',
];

(async () => {
  const authResp = await fetch(pbBase + '/api/collections/_superusers/auth-with-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identity: email, password }),
  });
  const authJson = await authResp.json();
  if (!authResp.ok || !authJson.token) {
    throw new Error('auth_failed:' + JSON.stringify(authJson));
  }

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.addInitScript((token) => {
    localStorage.setItem('pb_token', token);
  }, authJson.token);
  const page = await context.newPage();

  for (const url of targets) {
    await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
    const title = await page.title();
    const body = await page.locator('body').innerText();
    const hasWriteShadow = body.includes('Write SHADOW') || body.includes('WRITE MODE') || body.includes('Write Mode');
    const navTexts = await page.locator('#nav .nav-item').allTextContents().catch(() => []);
    console.log(JSON.stringify({ url, title, hasWriteShadow, navTexts }, null, 2));
  }

  await browser.close();
})();
