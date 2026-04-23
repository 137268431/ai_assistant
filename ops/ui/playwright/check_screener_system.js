const { chromium, devices } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';

function shouldIgnoreRequestFailure(req) {
  const errorText = req.failure()?.errorText || '';
  const url = req.url() || '';
  if (url.includes('fonts.gstatic.com')) return true;
  return errorText === 'net::ERR_ABORTED';
}

async function login(page, targetUrl) {
  const target = new URL(targetUrl);
  const redirectPath = `${target.pathname}${target.search}`;
  await page.goto(`${CONSOLE_BASE}/login.html?from=${encodeURIComponent(redirectPath)}`, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });
  if (page.url().includes('/ibkr_') || page.url().includes('/index.html')) return;

  const email = page.locator('input[type="email"], input[name="identity"]');
  const password = page.locator('input[type="password"]');
  if (!(await email.count())) return;

  await email.first().fill(EMAIL);
  await password.first().fill(PASSWORD);
  await page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]').first().click();
  await page.waitForTimeout(1800);
}

async function gotoStable(page, url) {
  try {
    await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: 20000,
    });
  } catch (err) {
    if (!String(err && err.message || '').includes('ERR_ABORTED')) throw err;
    await page.waitForTimeout(800);
    await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: 20000,
    });
  }
}

async function waitForCurrentTargetsView(page, deviceName) {
  const isMobile = /iphone|android|mobile/i.test(String(deviceName || ''));
  if (isMobile) {
    await page.waitForSelector('#currentViewPanel.active #currentTargetsCards', { timeout: 20000 });
    await page.waitForFunction(() => {
      const cards = document.getElementById('currentTargetsCards');
      const text = cards?.textContent || '';
      return cards && text && !text.includes('加载中');
    }, { timeout: 20000 });
    return;
  }

  await page.waitForSelector('#currentViewPanel.active #currentTargetsTable', { timeout: 20000 });
}

async function waitForUniverseView(page, deviceName) {
  const isMobile = /iphone|android|mobile/i.test(String(deviceName || ''));
  if (isMobile) {
    await page.waitForSelector('#universeViewPanel.active #screenerCards', { timeout: 20000 });
    await page.waitForFunction(() => {
      const cards = document.getElementById('screenerCards');
      const text = cards?.textContent || '';
      return cards && text && !text.includes('加载中');
    }, { timeout: 20000 });
    return;
  }

  await page.waitForSelector('#universeViewPanel.active #screenerTable', { timeout: 20000 });
}

async function waitForTargetsTab(page, deviceName) {
  const isMobile = /iphone|android|mobile/i.test(String(deviceName || ''));
  if (isMobile) {
    await page.waitForSelector('#targetsTab.active #dailyTargetsCards', { timeout: 20000 });
    await page.waitForFunction(() => {
      const cards = document.getElementById('dailyTargetsCards');
      const text = cards?.textContent || '';
      return cards && text && !text.includes('加载中');
    }, { timeout: 20000 });
    return;
  }

  await page.waitForSelector('#targetsTab.active #dailyTargetsTable', { timeout: 20000 });
}

async function waitForWatchlistTab(page, deviceName) {
  const isMobile = /iphone|android|mobile/i.test(String(deviceName || ''));
  if (isMobile) {
    await page.waitForSelector('#watchlistTab.active #watchlistCards', { timeout: 20000 });
    await page.waitForFunction(() => {
      const cards = document.getElementById('watchlistCards');
      const text = cards?.textContent || '';
      return cards && text && !text.includes('加载中');
    }, { timeout: 20000 });
    return;
  }

  await page.waitForSelector('#watchlistTab.active #watchlistTable', { timeout: 20000 });
}

async function collect(deviceName, deviceConfig) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext(deviceConfig || {});
  const page = await context.newPage();
  const result = { device: deviceName, errors: [] };

  page.on('pageerror', (err) => result.errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) {
      result.errors.push(`console:${msg.type()}:${msg.text()}`);
    }
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) {
      result.errors.push(`response:${resp.status()}:${resp.url()}`);
    }
  });
  page.on('requestfailed', (req) => {
    if (shouldIgnoreRequestFailure(req)) return;
    result.errors.push(`requestfailed:${req.failure()?.errorText || 'unknown'}:${req.url()}`);
  });

  try {
    await login(page, `${CONSOLE_BASE}/ibkr_screener.html?environment=live`);

    await gotoStable(page, `${CONSOLE_BASE}/ibkr_screener.html?environment=live`);
    await page.waitForSelector('#pageBridge .domain-tab', { timeout: 20000 });
    await page.waitForSelector('#summaryGrid .summary-card', { timeout: 20000 });
    await page.waitForSelector('#screenerViewTabs .subview-tab', { timeout: 20000 });
    await waitForCurrentTargetsView(page, deviceName);
    await page.waitForFunction(() => {
      const text = document.getElementById('currentTargetsMeta')?.textContent || '';
      return text && !text.includes('等待加载') && !text.includes('正在加载');
    }, { timeout: 20000 });
    result.nav_texts = await page.locator('#nav .nav-item').allTextContents();
    result.screener_tabs = await page.locator('#pageBridge .domain-tab-label').allTextContents();
    result.screener_view_tabs = await page.locator('#screenerViewTabs .subview-tab-label').allTextContents();
    result.screener_url = page.url();
    result.current_targets_meta = await page.locator('#currentTargetsMeta').innerText().catch(() => '');

    await page.locator('#screenerViewTabs .subview-tab[data-view="universe"]').click();
    await waitForUniverseView(page, deviceName);
    result.universe_view_active = await page.locator('#universeViewPanel.active').count().catch(() => 0);

    await page.locator('#pageBridge .domain-tab[data-tab="targets"]').click();
    await waitForTargetsTab(page, deviceName);
    await page.waitForFunction(() => {
      const text = document.getElementById('dailyTargetListMeta')?.textContent || '';
      return text && !text.includes('尚未加载');
    }, { timeout: 20000 });
    result.targets_tab_url = page.url();
    result.targets_list_meta = await page.locator('#dailyTargetListMeta').innerText().catch(() => '');

    await page.locator('#pageBridge .domain-tab[data-tab="watchlist"]').click();
    await waitForWatchlistTab(page, deviceName);
    result.watchlist_tab_url = page.url();
    result.watchlist_meta = await page.locator('#listMeta').innerText().catch(() => '');

    await gotoStable(page, `${CONSOLE_BASE}/ibkr_watchlist.html?environment=live`);
    await page.waitForURL(/ibkr_screener\.html/, { timeout: 20000 });
    await waitForWatchlistTab(page, deviceName);
    result.watchlist_redirect_url = page.url();

    await gotoStable(page, `${CONSOLE_BASE}/ibkr_targets.html?environment=live&date=2026-04-07`);
    await page.waitForURL(/ibkr_screener\.html/, { timeout: 20000 });
    await waitForTargetsTab(page, deviceName);
    await page.waitForFunction(() => {
      const text = document.getElementById('dailyTargetListMeta')?.textContent || '';
      return text && !text.includes('尚未加载');
    }, { timeout: 20000 });
    result.targets_redirect_url = page.url();

    await gotoStable(page, `${CONSOLE_BASE}/ibkr_signals.html?environment=live`);
    await page.waitForFunction(() => !document.querySelector('#signalsContainer .loading'), { timeout: 20000 });
    await page.waitForSelector('#latencySummary .pipeline-strip-shell', { timeout: 20000 });
    result.signals_pipeline_metric_count = await page.locator('#latencySummary .pipeline-metric-card').count().catch(() => 0);
    result.signals_pipeline_actions = await page.locator('#latencySummary .pipeline-link').allTextContents().catch(() => []);

    result.system_bridges = {};
    const bridgeTargets = {
      runtime: `${CONSOLE_BASE}/ibkr_runtime.html?environment=live`,
      account: `${CONSOLE_BASE}/ibkr_account.html?environment=live`,
      quality: `${CONSOLE_BASE}/ibkr_data_quality.html?environment=live`,
    };

    for (const [key, url] of Object.entries(bridgeTargets)) {
      await gotoStable(page, url);
      await page.waitForSelector('#pageBridge .page-bridge-label', { timeout: 20000 });
      result.system_bridges[key] = await page.locator('#pageBridge .page-bridge-label').allTextContents();
    }
  } catch (err) {
    result.errors.push(`fatal:${err.message}`);
  }

  await context.close();
  await browser.close();
  return result;
}

(async () => {
  const results = [];
  results.push(await collect('desktop', {}));
  results.push(await collect('iPhone 12', devices['iPhone 12']));
  console.log(JSON.stringify(results, null, 2));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
