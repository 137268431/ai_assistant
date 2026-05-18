const { chromium, devices } = require('playwright');

const CONSOLE_BASE = (process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top').replace(/\/+$/, '');
const PB_BASE = (process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top').replace(/\/+$/, '');
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const ENVIRONMENT = process.env.IBKR_MARKET_DATA_MODE || 'live';

const ANALYTICS_BRIDGE_SOURCES = [
  '/ibkr_indicators.html',
  '/ibkr_chart.html',
];

function attachErrors(page, origin) {
  const errors = [];
  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) errors.push(`console:${msg.type()}:${msg.text()}`);
  });
  page.on('response', (resp) => {
    const url = resp.url();
    if (resp.status() < 400) return;
    if (url.endsWith('/favicon.ico')) return;
    if (origin && !url.startsWith(origin) && !url.startsWith(PB_BASE)) return;
    errors.push(`response:${resp.status()}:${url}`);
  });
  page.on('requestfailed', (req) => {
    const url = req.url();
    const failureText = req.failure()?.errorText || 'unknown';
    if (url.endsWith('/favicon.ico')) return;
    if (failureText.includes('net::ERR_ABORTED')) return;
    if (origin && !url.startsWith(origin) && !url.startsWith(PB_BASE)) return;
    errors.push(`requestfailed:${failureText}:${url}`);
  });
  return errors;
}

function pageUrl(path) {
  const url = new URL(path, `${CONSOLE_BASE}/`);
  url.searchParams.set('environment', ENVIRONMENT);
  url.searchParams.set('ts', String(Date.now()));
  return url.toString();
}

async function fetchToken() {
  const resp = await fetch(`${PB_BASE}/api/collections/_superusers/auth-with-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identity: EMAIL, password: PASSWORD }),
  });
  const json = await resp.json().catch(() => ({}));
  if (!resp.ok || !json?.token) throw new Error(`auth_failed:${resp.status}:${JSON.stringify(json)}`);
  return json.token;
}

async function createAuthedContext(browser, token, mobile = false) {
  const context = mobile
    ? await browser.newContext({ ...devices['iPhone 12'], ignoreHTTPSErrors: true })
    : await browser.newContext({ viewport: { width: 1440, height: 960 }, ignoreHTTPSErrors: true });
  await context.addInitScript((savedToken) => {
    localStorage.setItem('pb_token', savedToken);
  }, token);
  return context;
}

async function waitForStatsReady(page, timeout = 35000) {
  await page.waitForFunction(() => {
    const overlayVisible = Array.from(document.querySelectorAll('.page-loading-overlay')).some((node) => {
      const style = window.getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return (
        style.display !== 'none' &&
        style.visibility !== 'hidden' &&
        style.opacity !== '0' &&
        !node.classList.contains('is-hidden') &&
        rect.width > 1 &&
        rect.height > 1
      );
    });
    const textOf = (id) => String(document.getElementById(id)?.textContent || '').trim();
    const tradesText = String(document.getElementById('tradesTable')?.textContent || '').trim();
    return (
      !overlayVisible &&
      document.querySelectorAll('.stat-card').length >= 8 &&
      document.querySelectorAll('canvas').length >= 4 &&
      textOf('totalOrders') !== '' &&
      textOf('totalOrders') !== '-' &&
      textOf('totalSignals') !== '' &&
      textOf('totalSignals') !== '-' &&
      tradesText !== '' &&
      !tradesText.includes('加载中')
    );
  }, null, { timeout });
}

async function checkStats(browser, token, mobile) {
  const context = await createAuthedContext(browser, token, mobile);
  const page = await context.newPage();
  const errors = attachErrors(page, CONSOLE_BASE);
  await page.goto(pageUrl('/ibkr_stats.html'), { waitUntil: 'domcontentloaded', timeout: 60000 });
  await waitForStatsReady(page).catch((error) => errors.push(`stats_ready:${error.message}`));
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
  result.ok = Boolean(
    result.errors.length === 0 &&
    result.stat_cards >= 8 &&
    result.chart_count >= 4 &&
    result.total_orders &&
    result.total_orders !== '-' &&
    result.total_signals &&
    result.total_signals !== '-'
  );
  await context.close();
  return result;
}

async function checkBridge(browser, token, path) {
  const context = await createAuthedContext(browser, token, false);
  const page = await context.newPage();
  const errors = attachErrors(page, CONSOLE_BASE);
  await page.goto(pageUrl(path), { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => document.querySelectorAll('.page-bridge-link').length > 0, null, { timeout: 25000 })
    .catch((error) => errors.push(`bridge_ready:${error.message}`));
  const bridgeLinks = await page.locator('.page-bridge-link').evaluateAll((nodes) => nodes.map((node) => ({
    text: String(node.textContent || '').replace(/\s+/g, ' ').trim(),
    href: node.getAttribute('href') || '',
    active: node.classList.contains('active'),
  }))).catch(() => []);
  const statsLink = bridgeLinks.find((item) => item.text.includes('统计') && item.href.includes('/ibkr_stats.html'));
  if (!statsLink) errors.push('missing_stats_bridge_link');
  if (statsLink) {
    await page.locator('.page-bridge-link', { hasText: '统计' }).first().click();
    await page.waitForURL(/\/ibkr_stats\.html/, { timeout: 25000 }).catch((error) => errors.push(`stats_navigation:${error.message}`));
    await waitForStatsReady(page).catch((error) => errors.push(`stats_ready_after_bridge:${error.message}`));
  }
  const result = {
    test: `bridge:${path}`,
    url: page.url(),
    title: await page.title(),
    bridge_links: bridgeLinks,
    ok: Boolean(statsLink && page.url().includes('/ibkr_stats.html') && errors.length === 0),
    errors,
  };
  await context.close();
  return result;
}

async function checkStatsActiveBridge(browser, token) {
  const context = await createAuthedContext(browser, token, false);
  const page = await context.newPage();
  const errors = attachErrors(page, CONSOLE_BASE);
  await page.goto(pageUrl('/ibkr_stats.html'), { waitUntil: 'domcontentloaded', timeout: 60000 });
  await waitForStatsReady(page).catch((error) => errors.push(`stats_ready:${error.message}`));
  const activeText = await page.locator('.page-bridge-link.active').first().innerText().catch(() => '');
  const activeHref = await page.locator('.page-bridge-link.active').first().getAttribute('href').catch(() => '');
  if (!activeText.includes('统计') || !String(activeHref || '').includes('/ibkr_stats.html')) {
    errors.push(`stats_active_bridge_mismatch:${activeText}:${activeHref}`);
  }
  const result = {
    test: 'bridge:/ibkr_stats.html:active',
    url: page.url(),
    active_text: activeText,
    active_href: activeHref,
    ok: errors.length === 0,
    errors,
  };
  await context.close();
  return result;
}

(async () => {
  const token = await fetchToken();
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    results.push(await checkStats(browser, token, false));
    results.push(await checkStats(browser, token, true));
    for (const source of ANALYTICS_BRIDGE_SOURCES) {
      results.push(await checkBridge(browser, token, source));
    }
    results.push(await checkStatsActiveBridge(browser, token));
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(results, null, 2));
  if (results.some((item) => !item.ok)) process.exitCode = 1;
})().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error?.message || String(error), checked_at: new Date().toISOString() }, null, 2));
  process.exit(1);
});
