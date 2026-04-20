const fs = require('fs');
const { chromium, devices, request } = require('playwright');

const DEFAULT_EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const DEFAULT_PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const DEFAULT_BASE = process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const NAV_TIMEOUT_MS = Number(process.env.PB_SMOKE_NAV_TIMEOUT_MS || 20000);
const SETTLE_MS = Number(process.env.PB_SMOKE_SETTLE_MS || 2200);
const AUTH_TIMEOUT_MS = Number(process.env.PB_SMOKE_AUTH_TIMEOUT_MS || 15000);
const PAGE_MAX_SPREAD_PX = Number(process.env.PB_SMOKE_PANEL_SPREAD_MAX || 24);
const BRIDGE_MAX_SPREAD_PX = Number(process.env.PB_SMOKE_BRIDGE_SPREAD_MAX || 12);
const PANEL_ROW_HEIGHT_MAX_PX = Number(process.env.PB_SMOKE_PANEL_HEIGHT_MAX || 1400);
const DEFAULT_TARGETS = [
  `${DEFAULT_BASE}/index.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_system.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_runtime.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_config.html?environment=global`,
  `${DEFAULT_BASE}/ibkr_monitor.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_warmup.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_data_quality.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_history_rebuild.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_screener.html?environment=live&tab=screener&view=current`,
  `${DEFAULT_BASE}/ibkr_signals.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_reverse_signals.html?environment=live`,
  `${DEFAULT_BASE}/orders.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_order_details.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_account.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_indicators.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_chart.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_stats.html?environment=live`,
  `${DEFAULT_BASE}/ibkr_backtests.html?environment=live`,
];
const ARTIFACT_DIR = process.env.PB_SMOKE_ARTIFACT_DIR || '/tmp/ai_assistant_pb_smoke';

function shouldIgnoreRequestFailure(req) {
  const errorText = req.failure()?.errorText || '';
  const url = req.url() || '';
  if (url.includes('fonts.gstatic.com') || url.includes('fonts.googleapis.com')) return true;
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

async function fetchToken() {
  const api = await request.newContext({
    baseURL: DEFAULT_BASE,
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: { 'Content-Type': 'application/json' },
  });
  try {
    let lastError = null;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        const resp = await api.post('/api/collections/_superusers/auth-with-password', {
          data: {
            identity: DEFAULT_EMAIL,
            password: DEFAULT_PASSWORD,
          },
          timeout: AUTH_TIMEOUT_MS,
        });
        const payload = await resp.json().catch(() => ({}));
        if (!resp.ok || !payload?.token) {
          lastError = new Error(`auth_failed:${resp.status()}:${JSON.stringify(payload)}`);
        } else {
          return payload.token;
        }
      } catch (err) {
        lastError = err;
      }
      await new Promise((resolve) => setTimeout(resolve, attempt * 500));
    }
    throw lastError || new Error('auth_failed:unknown');
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

async function waitForPageReady(page, url) {
  const timeout = Math.max(NAV_TIMEOUT_MS, SETTLE_MS * 4);
  const path = new URL(url).pathname;

  const waiters = {
    '/index.html': () => page.waitForFunction(() => {
      const readyStates = {
        overview: document.getElementById('homeOverview')?.dataset.ready || '',
        targets: document.getElementById('homeTargets')?.dataset.ready || '',
        market: document.getElementById('homeMarket')?.dataset.ready || '',
        activity: document.getElementById('homeActivity')?.dataset.ready || '',
      };
      return (
        readyStates.overview === 'ready' &&
        ['ready', 'empty'].includes(readyStates.targets) &&
        ['ready', 'empty'].includes(readyStates.market) &&
        ['ready', 'empty'].includes(readyStates.activity) &&
        document.querySelectorAll('#homeOverview .home-stat-card').length >= 6 &&
        document.querySelectorAll('#homeQuickLinks .home-quick-link').length >= 6
      );
    }, { timeout }),
    '/ibkr_runtime.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      const configText = document.getElementById('configDetail')?.innerText || '';
      return refreshInfo && !refreshInfo.includes('加载中') && configText && !configText.includes('加载中');
    }, { timeout }),
    '/ibkr_system.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      const configText = document.getElementById('configArea')?.innerText || '';
      return refreshInfo && !refreshInfo.includes('加载中') && configText && !configText.includes('加载中');
    }, { timeout }),
    '/ibkr_data_quality.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      const summaryInfo = document.getElementById('summaryInfo')?.textContent || '';
      return (
        refreshInfo &&
        !refreshInfo.includes('等待加载') &&
        !refreshInfo.includes('加载失败') &&
        summaryInfo &&
        !summaryInfo.includes('待加载') &&
        document.querySelectorAll('#summaryGrid .summary-card').length > 0
      );
    }, { timeout }),
    '/ibkr_account.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      return (
        refreshInfo &&
        !refreshInfo.includes('加载中') &&
        !refreshInfo.includes('等待') &&
        document.querySelectorAll('.summary-card, .stat-card, .metric-card').length > 0
      );
    }, { timeout }),
    '/ibkr_signals.html': () => page.waitForFunction(() => !document.querySelector('#signalsContainer .loading'), { timeout }),
    '/ibkr_reverse_signals.html': () => page.waitForFunction(() => !document.querySelector('#signalsContainer .loading'), { timeout }),
    '/orders.html': () => page.waitForFunction(() => !document.querySelector('#ordersContainer .loading'), { timeout }),
    '/ibkr_order_details.html': () => page.waitForFunction(() => !document.querySelector('#detailsContainer .loading'), { timeout }),
    '/ibkr_config.html': () => page.waitForFunction(() => !/LOADING/i.test(document.getElementById('configContainer')?.innerText || ''), { timeout }),
  };

  if (waiters[path]) {
    try {
      await waiters[path]();
      return;
    } catch (_) {
      // Fall back below.
    }
  }

  await page.waitForTimeout(SETTLE_MS);
}

function sanitizeFileStem(url, device) {
  const parsed = new URL(url);
  const suffix = parsed.search ? `_${parsed.search.replace(/[^a-zA-Z0-9]+/g, '_')}` : '';
  return `${parsed.pathname.replace(/[^a-zA-Z0-9]+/g, '_')}_${device}${suffix}`.replace(/^_+|_+$/g, '');
}

async function collectLayoutMetrics(page) {
  return page.evaluate((panelRowHeightMaxPx) => {
    const groupRowSpread = (selector) => {
      const nodes = Array.from(document.querySelectorAll(selector)).filter((node) => {
        const rect = node.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1;
      });
      const rows = new Map();
      nodes.forEach((node) => {
        const rect = node.getBoundingClientRect();
        const key = Math.round(rect.top / 6) * 6;
        const row = rows.get(key) || [];
        row.push(Math.round(rect.height));
        rows.set(key, row);
      });
      return Array.from(rows.entries()).map(([top, heights]) => ({
        top,
        count: heights.length,
        min: Math.min(...heights),
        max: Math.max(...heights),
        spread: Math.max(...heights) - Math.min(...heights),
      }));
    };

    const scrollIssues = [];
    const selectors = ['.panel-body', '.data-table-wrap', '.section-scroll-body', '.card-scroll-body'];
    selectors.forEach((selector) => {
      document.querySelectorAll(selector).forEach((node, index) => {
        const style = window.getComputedStyle(node);
        const overflowY = style.overflowY || '';
        const gap = node.scrollHeight - node.clientHeight;
        if (gap > 24 && !['auto', 'scroll', 'overlay'].includes(overflowY)) {
          scrollIssues.push({ selector, index, gap, overflowY });
        }
      });
    });

    const doc = document.documentElement;
    const body = document.body;
    const scrollWidth = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0);
    const bridgeRows = groupRowSpread('.page-bridge-link, .domain-tab');
    const panelRows = groupRowSpread([
      '.panel-grid > .panel',
      '.table-grid > .panel',
      '.dual-grid > .panel',
      '.home-primary-grid > .home-panel',
      '.home-secondary-grid > .home-panel',
      '.workspace > .panel',
      '.workspace > .rail-shell'
    ].join(', '));
    const tallPanelRows = panelRows.filter((row) => row.count > 1 && row.max > panelRowHeightMaxPx);

    return {
      viewport_width: window.innerWidth,
      scroll_width: scrollWidth,
      horizontal_overflow: scrollWidth > window.innerWidth + 4,
      context_count: document.querySelectorAll('.page-context-bar').length,
      bridge_count: document.querySelectorAll('.page-bridge-link, .domain-tab').length,
      bridge_rows: bridgeRows,
      panel_rows: panelRows,
      tall_panel_rows: tallPanelRows,
      bridge_row_spread_max: bridgeRows.reduce((max, row) => Math.max(max, row.spread), 0),
      panel_row_spread_max: panelRows.reduce((max, row) => Math.max(max, row.spread), 0),
      scroll_issues: scrollIssues.slice(0, 12),
    };
  }, PANEL_ROW_HEIGHT_MAX_PX);
}

async function collectPageExpectationIssues(page, url, mobile) {
  const path = new URL(url).pathname;
  if (path === '/index.html' || path === '/') {
    return page.evaluate((isMobileViewport) => {
      const issues = [];
      const tip = document.getElementById('todayTargetsTimeTip');
      const badge = document.querySelector('.home-panel-tip-badge');
      const tipCard = document.querySelector('.home-panel-tip');

      const tipText = String(tip?.textContent || '').trim();
      const badgeText = String(badge?.textContent || '').trim();
      const tipStyle = tipCard ? window.getComputedStyle(tipCard) : null;

      if (!tip) issues.push('missing_targets_time_tip');
      if (!badge) issues.push('missing_targets_tip_badge');
      if (!tipCard) issues.push('missing_targets_tip_card');
      if (tip && !tipText.includes('美东交易日')) issues.push('targets_time_tip_missing_market_date_copy');
      if (tip && !tipText.includes('ET')) issues.push('targets_time_tip_missing_et_copy');
      if (tip && !tipText.includes('当前标的榜')) issues.push('targets_time_tip_missing_jump_hint');
      if (badge && badgeText !== 'Tips') issues.push(`targets_tip_badge_text:${badgeText || 'empty'}`);
      if (tipCard && tipStyle?.display !== 'flex') issues.push(`targets_tip_display:${tipStyle?.display || 'missing'}`);
      if (tipCard && isMobileViewport && tipStyle?.flexDirection !== 'column') {
        issues.push(`targets_tip_mobile_direction:${tipStyle?.flexDirection || 'missing'}`);
      }

      return issues;
    }, mobile);
  }

  if (path === '/ibkr_screener.html') {
    return page.evaluate((isMobileViewport) => {
      const issues = [];
      const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 1 && rect.height > 1;
      };

      const activeTab = document.querySelector('.tab-panel.active');
      if (!activeTab) {
        issues.push('missing_active_tab_panel');
        return issues;
      }

      const isScreenerTab = activeTab.id === 'screenerTab';
      const currentViewPanel = document.getElementById('currentViewPanel');
      const activeMobileLists = Array.from(activeTab.querySelectorAll('.mobile-card-list')).filter(visible);
      const visibleDesktopTables = Array.from(activeTab.querySelectorAll('.desktop-table-wrap')).filter(visible);
      const rulesPanels = Array.from(document.querySelectorAll('#rulesBoard .rules-panel'));
      const toggleButton = document.getElementById('toggleCurrentAdvancedFiltersBtn');

      if (isMobileViewport) {
        if (!activeMobileLists.length) issues.push('missing_visible_mobile_card_list');
        if (visibleDesktopTables.length) issues.push(`desktop_table_visible:${visibleDesktopTables.length}`);
        if (isScreenerTab && currentViewPanel?.classList.contains('active')) {
          const currentCards = document.getElementById('currentTargetsCards');
          if (!visible(currentCards)) issues.push('current_targets_mobile_cards_hidden');
          if (toggleButton && !visible(toggleButton)) issues.push('current_filter_toggle_hidden');
        }
        if (isScreenerTab && rulesPanels.length && !rulesPanels.some((panel) => panel.dataset.expanded === 'false')) {
          issues.push('rules_not_collapsed_by_default');
        }
      } else {
        if (activeMobileLists.length) issues.push(`mobile_card_list_visible_on_desktop:${activeMobileLists.length}`);
        if (!visibleDesktopTables.length) issues.push('desktop_table_missing_on_desktop');
      }

      return issues;
    }, mobile);
  }

  return [];
}

async function inspectPage(browser, token, url, mobile) {
  const context = await createContext(browser, token, mobile);
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
    if (!captureErrors || shouldIgnoreRequestFailure(req)) return;
    errors.push(`requestfailed:${req.failure()?.errorText || 'unknown'}:${req.url()}`);
  });

  try {
    captureErrors = true;
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
    await waitForPageReady(page, url);
  } catch (err) {
    errors.push(`goto:${err.message}`);
  }

  const finalUrl = page.url();
  const title = await page.title().catch(() => '');
  const navTexts = await page.locator('#nav .nav-item, #navContainer .nav-item').allTextContents().catch(() => []);
  const bridgeTexts = await page.locator('.page-bridge .page-bridge-label, .domain-tab .domain-tab-label').allTextContents().catch(() => []);
  const layout = await collectLayoutMetrics(page).catch(() => ({
    horizontal_overflow: true,
    context_count: 0,
    bridge_count: 0,
    bridge_row_spread_max: 999,
    panel_row_spread_max: 999,
    bridge_rows: [],
    panel_rows: [],
    tall_panel_rows: [],
    scroll_issues: [{ selector: 'layout_eval_failed', index: 0, gap: 0, overflowY: 'error' }],
  }));
  const pageExpectationIssues = await collectPageExpectationIssues(page, finalUrl, mobile).catch(() => ['page_expectation_eval_failed']);

  const layoutIssues = [];
  if (/\/login\.html/.test(finalUrl)) layoutIssues.push('redirected_to_login');
  if (!navTexts.length) layoutIssues.push('missing_nav');
  if (!layout.context_count) layoutIssues.push('missing_context_bar');
  if (!layout.bridge_count) layoutIssues.push('missing_bridge');
  if (layout.horizontal_overflow) layoutIssues.push('horizontal_overflow');
  if (layout.bridge_row_spread_max > BRIDGE_MAX_SPREAD_PX) layoutIssues.push(`bridge_spread:${layout.bridge_row_spread_max}`);
  if (layout.panel_row_spread_max > PAGE_MAX_SPREAD_PX) layoutIssues.push(`panel_spread:${layout.panel_row_spread_max}`);
  if (layout.tall_panel_rows.length) layoutIssues.push(`tall_panel_rows:${layout.tall_panel_rows.map((row) => row.max).join(',')}`);
  if (layout.scroll_issues.length) layoutIssues.push(`uncontained_scroll:${layout.scroll_issues.length}`);

  let screenshot = '';
  if (errors.length || layoutIssues.length || pageExpectationIssues.length) {
    screenshot = `${ARTIFACT_DIR}/${sanitizeFileStem(url, mobile ? 'mobile' : 'desktop')}.png`;
    await page.screenshot({
      path: screenshot,
      fullPage: true,
    }).catch(() => {
      screenshot = '';
    });
  }

  await context.close();
  return {
    url,
    final_url: finalUrl,
    device: mobile ? 'iPhone 12' : 'desktop',
    title,
    nav_count: navTexts.length,
    bridge_count: layout.bridge_count,
    bridge_texts: bridgeTexts,
    layout,
    errors,
    layout_issues: layoutIssues,
    page_expectation_issues: pageExpectationIssues,
    screenshot,
  };
}

(async () => {
  const opts = parseArgs(process.argv);
  fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
  const token = await fetchToken();
  const browser = await chromium.launch({ headless: opts.headless });
  const results = [];
  for (const target of opts.targets) {
    if (!opts.mobileOnly) {
      results.push(await inspectPage(browser, token, target, false));
    }
    if (opts.mobile && !opts.desktopOnly) {
      results.push(await inspectPage(browser, token, target, true));
    }
  }
  await browser.close();

  const failing = results.filter((item) => item.errors.length || item.layout_issues.length || item.page_expectation_issues.length);
  console.log(JSON.stringify(results, null, 2));
  if (failing.length) {
    process.exit(1);
  }
})();
