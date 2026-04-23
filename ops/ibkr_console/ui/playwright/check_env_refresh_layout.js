const fs = require('fs');
const { chromium, devices, request } = require('playwright');
const { waitForHomeOverviewReady, collectHomeOverviewIssues } = require('./home_overview_checks');

const PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const PAGE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const TIMEOUT_MS = Number(process.env.PB_SMOKE_NAV_TIMEOUT_MS || 20000);
const ARTIFACT_DIR = process.env.PB_SMOKE_ARTIFACT_DIR || '/tmp/ai_assistant_pb_smoke';

const TARGETS = [
  {
    name: 'index',
    url: `${PAGE_BASE}/index.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '',
  },
  {
    name: 'signals',
    url: `${PAGE_BASE}/ibkr_signals.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '#refreshBtn.page-refresh-trigger.is-compact',
  },
  {
    name: 'orders',
    url: `${PAGE_BASE}/orders.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '#refreshBtn.page-refresh-trigger.is-compact',
  },
  {
    name: 'reverse',
    url: `${PAGE_BASE}/ibkr_reverse_signals.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '#refreshBtn.page-refresh-trigger.is-compact',
  },
  {
    name: 'order_details',
    url: `${PAGE_BASE}/ibkr_order_details.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '#refreshBtn.page-refresh-trigger.is-compact',
  },
  {
    name: 'indicators',
    url: `${PAGE_BASE}/ibkr_indicators.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '#refreshBtn.page-refresh-trigger.is-compact',
  },
  {
    name: 'stats',
    url: `${PAGE_BASE}/ibkr_stats.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '.refresh-btn.page-refresh-trigger',
  },
  {
    name: 'monitor',
    url: `${PAGE_BASE}/ibkr_monitor.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '#refreshBtn.page-refresh-trigger',
  },
  {
    name: 'backtests',
    url: `${PAGE_BASE}/ibkr_backtests.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '.btn.page-refresh-trigger',
  },
  {
    name: 'chart',
    url: `${PAGE_BASE}/ibkr_chart.html?environment=live`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#contextBar .env-switcher-select',
    refreshSelector: '.toolbar-btn.page-refresh-trigger',
  },
  {
    name: 'config',
    url: `${PAGE_BASE}/ibkr_config.html?environment=global`,
    expectHeaderBadge: false,
    contextSwitcherSelector: '#configEnvironmentBar .env-switcher-select',
    refreshSelector: '#configRefreshBtn.refresh-btn.page-refresh-trigger',
  },
];

const MOBILE_TARGETS = new Set(['index', 'signals']);

async function fetchToken() {
  const api = await request.newContext({
    baseURL: PB_BASE,
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: { 'Content-Type': 'application/json' },
  });

  try {
    const response = await api.post('/api/collections/_superusers/auth-with-password', {
      data: {
        identity: EMAIL,
        password: PASSWORD,
      },
      timeout: TIMEOUT_MS,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload?.token) {
      throw new Error(`auth_failed:${response.status()}:${JSON.stringify(payload)}`);
    }
    return payload.token;
  } finally {
    await api.dispose();
  }
}

async function createContext(browser, token, mobile) {
  const context = mobile
    ? await browser.newContext({ ...devices['iPhone 12'], ignoreHTTPSErrors: true })
    : await browser.newContext({ viewport: { width: 1440, height: 960 }, ignoreHTTPSErrors: true });
  await context.addInitScript((savedToken) => {
    localStorage.setItem('pb_token', savedToken);
  }, token);
  return context;
}

async function waitForReady(page, targetName) {
  switch (targetName) {
    case 'index':
      await waitForHomeOverviewReady(page, TIMEOUT_MS);
      return;
    case 'signals':
      await page.waitForFunction(() => !document.querySelector('#signalsContainer .loading'), { timeout: TIMEOUT_MS });
      return;
    case 'orders':
      await page.waitForFunction(() => !document.querySelector('#ordersContainer .loading'), { timeout: TIMEOUT_MS });
      return;
    case 'reverse':
      await page.waitForSelector('#refreshBtn.page-refresh-trigger.is-compact', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#contextBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    case 'order_details':
      await page.waitForFunction(() => !document.querySelector('#detailsContainer .loading'), { timeout: TIMEOUT_MS });
      return;
    case 'indicators':
      await page.waitForSelector('#refreshBtn.page-refresh-trigger.is-compact', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#contextBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    case 'stats':
      await page.waitForSelector('.refresh-btn.page-refresh-trigger', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#contextBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    case 'monitor':
      await page.waitForSelector('#refreshBtn.page-refresh-trigger', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#contextBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    case 'backtests':
      await page.waitForSelector('.btn.page-refresh-trigger', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#contextBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    case 'chart':
      await page.waitForSelector('.toolbar-btn.page-refresh-trigger', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#contextBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    case 'config':
      await page.waitForSelector('#configRefreshBtn.refresh-btn.page-refresh-trigger', { timeout: TIMEOUT_MS });
      await page.waitForSelector('#configEnvironmentBar .env-switcher-select', { timeout: TIMEOUT_MS });
      return;
    default:
      await page.waitForTimeout(1200);
  }
}

function sanitizeFileStem(name, device) {
  return `${name}_${device}`.replace(/[^a-zA-Z0-9_-]+/g, '_');
}

async function inspectTarget(browser, token, target, mobile = false) {
  const context = await createContext(browser, token, mobile);
  const page = await context.newPage();
  const device = mobile ? 'mobile' : 'desktop';
  const errors = [];

  page.on('pageerror', (err) => {
    errors.push(`pageerror:${err.message}`);
  });
  try {
    await page.goto(target.url, { waitUntil: 'domcontentloaded', timeout: TIMEOUT_MS });
    await waitForReady(page, target.name);
  } catch (err) {
    errors.push(`goto:${err.message}`);
  }

  const checks = await page.evaluate((config) => {
    const allSwitchers = Array.from(document.querySelectorAll('.env-switcher-select'));
    const contextSwitcher = config.contextSwitcherSelector
      ? document.querySelector(config.contextSwitcherSelector)
      : null;
    const header = document.getElementById('environmentHeader');
    const refreshNode = config.refreshSelector ? document.querySelector(config.refreshSelector) : null;

    return {
      switcherCount: allSwitchers.length,
      headerSelectCount: header ? header.querySelectorAll('.env-switcher-select').length : 0,
      headerBadgeCount: header ? header.querySelectorAll('.env-badge').length : 0,
      contextSwitcherCount: contextSwitcher ? 1 : 0,
      refreshSelectorMatched: Boolean(refreshNode),
      refreshText: refreshNode ? String(refreshNode.textContent || '').trim() : '',
    };
  }, {
    contextSwitcherSelector: target.contextSwitcherSelector,
    refreshSelector: target.refreshSelector,
  }).catch((err) => {
    errors.push(`eval:${err.message}`);
    return null;
  });

  if (checks) {
    if (checks.switcherCount !== 1) {
      errors.push(`switcher_count:${checks.switcherCount}`);
    }
    if (checks.headerSelectCount !== 0) {
      errors.push(`header_select_count:${checks.headerSelectCount}`);
    }
    if (checks.contextSwitcherCount !== 1) {
      errors.push(`context_switcher_count:${checks.contextSwitcherCount}`);
    }
    if (target.expectHeaderBadge && checks.headerBadgeCount < 1) {
      errors.push(`header_badge_count:${checks.headerBadgeCount}`);
    }
    if (target.refreshSelector && !checks.refreshSelectorMatched) {
      errors.push(`missing_refresh_selector:${target.refreshSelector}`);
    }
  }

  if (target.name === 'index') {
    const homeIssues = await collectHomeOverviewIssues(page, mobile).catch((err) => {
      errors.push(`home_eval:${err.message}`);
      return [];
    });
    homeIssues.forEach((issue) => {
      errors.push(`home_issue:${issue}`);
    });
  }

  let screenshot = '';
  if (errors.length) {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    screenshot = `${ARTIFACT_DIR}/${sanitizeFileStem(target.name, device)}.png`;
    await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {
      screenshot = '';
    });
  }

  await context.close();
  return {
    target: target.name,
    device,
    url: target.url,
    checks,
    errors,
    screenshot,
  };
}

(async () => {
  const token = await fetchToken();
  const browser = await chromium.launch({ headless: true });
  const results = [];

  for (const target of TARGETS) {
    results.push(await inspectTarget(browser, token, target, false));
    if (MOBILE_TARGETS.has(target.name)) {
      results.push(await inspectTarget(browser, token, target, true));
    }
  }

  await browser.close();

  const failing = results.filter((item) => item.errors.length);
  console.log(JSON.stringify(results, null, 2));
  if (failing.length) {
    process.exit(1);
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
