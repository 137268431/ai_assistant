const { chromium, devices } = require('playwright');

const DEFAULT_EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const DEFAULT_PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const DEFAULT_BASE = process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const NAV_TIMEOUT_MS = Number(process.env.PB_SMOKE_NAV_TIMEOUT_MS || 15000);
const SETTLE_MS = Number(process.env.PB_SMOKE_SETTLE_MS || 1800);
const DEFAULT_TARGETS = [
  `${DEFAULT_BASE}/ibkr_runtime.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_system.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_data_quality.html?environment=live`,
];

function shouldIgnoreRequestFailure(req) {
  const errorText = req.failure()?.errorText || '';
  const url = req.url() || '';
  if (url.includes('fonts.gstatic.com')) return true;
  return errorText === 'net::ERR_ABORTED';
}

function parseArgs(argv) {
  const opts = {
    mobile: false,
    mobileOnly: false,
    desktopOnly: false,
    headless: true,
    targets: [],
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--mobile') {
      opts.mobile = true;
    } else if (arg === '--mobile-only') {
      opts.mobile = true;
      opts.mobileOnly = true;
    } else if (arg === '--desktop-only') {
      opts.desktopOnly = true;
    } else if (arg === '--headed') {
      opts.headless = false;
    } else if (arg.startsWith('--target=')) {
      opts.targets.push(arg.slice('--target='.length));
    } else if (arg === '--target' && argv[i + 1]) {
      opts.targets.push(argv[i + 1]);
      i += 1;
    }
  }
  if (!opts.targets.length) opts.targets = DEFAULT_TARGETS.slice();
  return opts;
}

async function login(page, targetUrl) {
  const target = new URL(targetUrl);
  const redirectPath = `${target.pathname}${target.search}`;
  await page.goto(`${DEFAULT_BASE}/login.html?from=${encodeURIComponent(redirectPath)}`, {
    waitUntil: 'domcontentloaded',
    timeout: NAV_TIMEOUT_MS,
  });
  if (page.url().includes('/ibkr_') || page.url().includes('/index.html')) return;

  const email = page.locator('input[type="email"], input[name="identity"]');
  const password = page.locator('input[type="password"]');
  if (!(await email.count())) return;

  await email.first().fill(DEFAULT_EMAIL);
  await password.first().fill(DEFAULT_PASSWORD);
  const loginButton = page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]');
  await loginButton.first().click();
  try {
    await page.waitForURL((url) => !url.pathname.endsWith('/login.html'), {
      timeout: NAV_TIMEOUT_MS,
    });
  } catch (_) {
    await page.waitForTimeout(SETTLE_MS);
  }
  await page.waitForTimeout(SETTLE_MS);
}

async function waitForDashboardReady(page, url) {
  const timeout = Math.max(NAV_TIMEOUT_MS, SETTLE_MS * 4);

  if (url.includes('/index.html')) {
    try {
      await page.waitForFunction(() => {
        const readyStates = {
          overview: document.getElementById('homeOverview')?.dataset.ready || '',
          targets: document.getElementById('homeTargets')?.dataset.ready || '',
          market: document.getElementById('homeMarket')?.dataset.ready || '',
          activity: document.getElementById('homeActivity')?.dataset.ready || '',
        };
        const overviewCards = document.querySelectorAll('#homeOverview .home-stat-card').length;
        const quickLinks = document.querySelectorAll('#homeQuickLinks .home-quick-link').length;
        const secondaryPanels = document.querySelectorAll('#homeSecondary > section').length;
        const layout = document.body?.dataset?.homeLayout || '';
        const isMobile = window.innerWidth <= 920;
        const quickTop = document.getElementById('homeQuickLinks')?.getBoundingClientRect().top ?? 0;
        const targetsTop = document.getElementById('homeTargets')?.getBoundingClientRect().top ?? 0;

        return (
          readyStates.overview === 'ready' &&
          ['ready', 'empty'].includes(readyStates.targets) &&
          ['ready', 'empty'].includes(readyStates.market) &&
          ['ready', 'empty'].includes(readyStates.activity) &&
          overviewCards >= 6 &&
          quickLinks >= 6 &&
          secondaryPanels >= 2 &&
          Boolean(layout) &&
          (!isMobile || quickTop <= targetsTop)
        );
      }, { timeout });
      return;
    } catch (_) {
      // Fall back to the generic settle wait below.
    }
  }

  if (url.includes('/ibkr_runtime.html')) {
    try {
      await page.waitForFunction(() => {
        const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
        const configText = document.getElementById('configDetail')?.innerText || '';
        return refreshInfo && !refreshInfo.includes('加载中') && configText && !configText.includes('加载中');
      }, { timeout });
      return;
    } catch (_) {
      // Fall back to the generic settle wait below.
    }
  }

  if (url.includes('/ibkr_system.html')) {
    try {
      await page.waitForFunction(() => {
        const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
        const configText = document.getElementById('configArea')?.innerText || '';
        return refreshInfo && !refreshInfo.includes('加载中') && configText && !configText.includes('加载中');
      }, { timeout });
      return;
    } catch (_) {
      // Fall back to the generic settle wait below.
    }
  }

  if (url.includes('/ibkr_data_quality.html')) {
    try {
      await page.waitForFunction(() => {
        const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
        const summaryInfo = document.getElementById('summaryInfo')?.textContent || '';
        const summaryCards = document.querySelectorAll('#summaryGrid .summary-card').length;
        const qualityRows = document.querySelectorAll('#qualityTable tr').length;
        return (
          refreshInfo &&
          !refreshInfo.includes('等待加载') &&
          !refreshInfo.includes('加载失败') &&
          summaryInfo &&
          !summaryInfo.includes('待加载') &&
          summaryCards > 0 &&
          qualityRows > 0
        );
      }, { timeout });
      return;
    } catch (_) {
      // Fall back to the generic settle wait below.
    }
  }

  if (url.includes('/ibkr_account.html')) {
    try {
      await page.waitForFunction(() => {
        const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
        const statCards = document.querySelectorAll('.summary-card, .stat-card, .metric-card').length;
        return (
          refreshInfo &&
          !refreshInfo.includes('加载中') &&
          !refreshInfo.includes('等待') &&
          statCards > 0
        );
      }, { timeout });
      return;
    } catch (_) {
      // Fall back to the generic settle wait below.
    }
  }

  await page.waitForTimeout(SETTLE_MS);
}

async function inspectPage(browser, url, mobile) {
  const context = mobile
    ? await browser.newContext({ ...devices['iPhone 12'] })
    : await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  let captureErrors = false;
  page.setDefaultNavigationTimeout(NAV_TIMEOUT_MS);
  page.setDefaultTimeout(NAV_TIMEOUT_MS);

  page.on('pageerror', (err) => {
    if (captureErrors) errors.push(`pageerror:${err.message}`);
  });
  page.on('console', (msg) => {
    if (captureErrors && ['error', 'warning'].includes(msg.type())) {
      errors.push(`console:${msg.type()}:${msg.text()}`);
    }
  });
  page.on('response', (resp) => {
    if (captureErrors && resp.status() >= 400) {
      errors.push(`response:${resp.status()}:${resp.url()}`);
    }
  });
  page.on('requestfailed', (req) => {
    if (shouldIgnoreRequestFailure(req)) return;
    if (captureErrors) {
      errors.push(`requestfailed:${req.failure()?.errorText || 'unknown'}:${req.url()}`);
    }
  });

  try {
    await login(page, url);
  } catch (err) {
    errors.push(`login:${err.message}`);
  }

  try {
    captureErrors = true;
    if (page.url() !== url) {
      await page.goto(url, {
        waitUntil: 'domcontentloaded',
        timeout: NAV_TIMEOUT_MS,
      });
    }
    await waitForDashboardReady(page, url);
  } catch (err) {
    errors.push(`goto:${err.message}`);
  }

  const title = await page.title();
  const navTexts = await page.locator('#nav .nav-item, #navContainer .nav-item').allTextContents().catch(() => []);
  const bridgeTexts = await page.locator('.page-bridge .page-bridge-label').allTextContents().catch(() => []);
  const bodyText = await page.locator('body').textContent().catch(() => '');
  const opsCards = await page.locator('.ops-card').count().catch(() => 0);
  const metricCards = await page.locator('.metric-card').count().catch(() => 0);
  const summaryCards = await page.locator('.summary-card').count().catch(() => 0);
  const qualityRows = await page.locator('#qualityTable tr').count().catch(() => 0);
  const blockerTitle = await page.locator('#primaryBlockerTitle').innerText().catch(() => '');
  const cronCards = await page.locator('#configArea .cron-card, #configDetail .cron-card').count().catch(() => 0);
  const refreshInfo = await page.locator('#refreshInfo').innerText().catch(() => '');
  const summaryInfo = await page.locator('#summaryInfo').innerText().catch(() => '');
  const homeLayout = await page.locator('body').getAttribute('data-home-layout').catch(() => '');
  const homeOverviewReady = await page.locator('#homeOverview').getAttribute('data-ready').catch(() => '');
  const homeTargetsReady = await page.locator('#homeTargets').getAttribute('data-ready').catch(() => '');
  const homeMarketReady = await page.locator('#homeMarket').getAttribute('data-ready').catch(() => '');
  const homeActivityReady = await page.locator('#homeActivity').getAttribute('data-ready').catch(() => '');
  const homeOverviewCards = await page.locator('#homeOverview .home-stat-card').count().catch(() => 0);
  const homeQuickLinks = await page.locator('#homeQuickLinks .home-quick-link').count().catch(() => 0);
  const homeTargetCards = await page.locator('#todayTargetsList .home-target-card').count().catch(() => 0);
  const homeMarketCards = await page.locator('#homeMarket .home-market-card').count().catch(() => 0);
  const homeActivityItems = await page.locator('#homeActivity .home-activity-item').count().catch(() => 0);
  const homePanelOrder = await page.evaluate(() => {
    const ids = ['homeOverview', 'homeQuickLinks', 'homeTargets', 'homeSecondary'];
    return ids
      .map((id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        return { id, top: rect.top, left: rect.left };
      })
      .filter(Boolean)
      .sort((a, b) => (a.top - b.top) || (a.left - b.left))
      .map((item) => item.id);
  }).catch(() => []);

  await context.close();
  return {
    url,
    device: mobile ? 'iPhone 12' : 'desktop',
    title,
    nav_count: navTexts.length,
    nav_texts: navTexts,
    bridge_count: bridgeTexts.length,
    bridge_texts: bridgeTexts,
    ops_cards: opsCards,
    metric_cards: metricCards,
    summary_cards: summaryCards,
    quality_rows: qualityRows,
    blocker_title: blockerTitle,
    cron_cards: cronCards,
    has_cron_summary: bodyText.includes('PB Cron 摘要'),
    has_compute_cron_key: bodyText.includes('pb_cron_ibkr_compute_runtime_enabled'),
    refresh_info: refreshInfo,
    summary_info: summaryInfo,
    has_gateway_running: /Gateway\s+ACTIVE|Gateway\s+RUNNING|IBKR 服务/.test(bodyText),
    has_challenge_hint: bodyText.includes('Challenge/Response') || bodyText.includes('Response Code'),
    home_layout: homeLayout,
    home_overview_ready: homeOverviewReady,
    home_targets_ready: homeTargetsReady,
    home_market_ready: homeMarketReady,
    home_activity_ready: homeActivityReady,
    home_overview_cards: homeOverviewCards,
    home_quick_links: homeQuickLinks,
    home_target_cards: homeTargetCards,
    home_market_cards: homeMarketCards,
    home_activity_items: homeActivityItems,
    home_panel_order: homePanelOrder,
    errors,
  };
}

(async () => {
  const opts = parseArgs(process.argv);
  const browser = await chromium.launch({ headless: opts.headless });
  const results = [];
  for (const target of opts.targets) {
    if (!opts.mobileOnly) {
      results.push(await inspectPage(browser, target, false));
    }
    if (opts.mobile && !opts.desktopOnly) {
      results.push(await inspectPage(browser, target, true));
    }
  }
  await browser.close();
  console.log(JSON.stringify(results, null, 2));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
