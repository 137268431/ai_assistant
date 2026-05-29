const fs = require('fs');
const { chromium, devices, request } = require('playwright');
const { waitForHomeOverviewReady, collectHomeOverviewIssues } = require('./home_overview_checks');

const DEFAULT_EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const DEFAULT_PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const DEFAULT_CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const DEFAULT_PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const NAV_TIMEOUT_MS = Number(process.env.PB_SMOKE_NAV_TIMEOUT_MS || 20000);
const SETTLE_MS = Number(process.env.PB_SMOKE_SETTLE_MS || 2200);
const AUTH_TIMEOUT_MS = Number(process.env.PB_SMOKE_AUTH_TIMEOUT_MS || 15000);
const PAGE_MAX_SPREAD_PX = Number(process.env.PB_SMOKE_PANEL_SPREAD_MAX || 24);
const BRIDGE_MAX_SPREAD_PX = Number(process.env.PB_SMOKE_BRIDGE_SPREAD_MAX || 12);
const PANEL_ROW_HEIGHT_MAX_PX = Number(process.env.PB_SMOKE_PANEL_HEIGHT_MAX || 1400);
const DEFAULT_TARGETS = [
  `${DEFAULT_CONSOLE_BASE}/index.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_signals.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_reverse_signals.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/orders.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_order_details.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_lifecycle_flow.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_trade_review.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_account.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_screener.html?environment=live&tab=screener&view=current`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_system.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_runtime.html?environment=live`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_config.html?environment=global`,
];
const ARTIFACT_DIR = process.env.PB_SMOKE_ARTIFACT_DIR || '/tmp/ai_assistant_pb_smoke';
const HELP_TEXT = `Usage: node pb_smoke.js [options]

Options:
  --desktop-only           Run desktop checks only
  --mobile                 Run desktop + mobile checks
  --mobile-only            Run mobile checks only
  --headed                 Launch browser in headed mode
  --target <url>           Restrict checks to one or more explicit targets
  -h, --help               Show this help
`;

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
    help: false,
    targets: [],
    targetsExplicit: false,
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
      opts.targetsExplicit = true;
    } else if (arg === '--target' && argv[i + 1]) {
      opts.targets.push(argv[i + 1]);
      opts.targetsExplicit = true;
      i += 1;
    } else if (arg === '-h' || arg === '--help') {
      opts.help = true;
    }
  }
  if (!opts.targets.length) opts.targets = DEFAULT_TARGETS.slice();
  return opts;
}

function hasFailures(item) {
  return Boolean(
    (Array.isArray(item?.errors) && item.errors.length)
      || (Array.isArray(item?.layout_issues) && item.layout_issues.length)
      || (Array.isArray(item?.page_expectation_issues) && item.page_expectation_issues.length)
  );
}

function formatTargetLabel(item) {
  if (item?.device === 'scenario') {
    return item?.name || item?.title || 'scenario';
  }

  try {
    const parsed = new URL(item?.final_url || item?.url || '');
    const suffix = parsed.search || '';
    return `${item?.device || 'unknown'} ${parsed.pathname}${suffix}`;
  } catch (_) {
    return `${item?.device || 'unknown'} ${item?.url || item?.title || 'unknown'}`;
  }
}

function collectIssuePreview(item) {
  const entries = [
    ...(Array.isArray(item?.errors) ? item.errors : []),
    ...(Array.isArray(item?.layout_issues) ? item.layout_issues : []),
    ...(Array.isArray(item?.page_expectation_issues) ? item.page_expectation_issues : []),
  ].filter(Boolean);
  return entries.slice(0, 3).join(', ');
}

function printRunSummary(results) {
  const pageResults = results.filter((item) => item?.device !== 'scenario');
  const failing = results.filter(hasFailures);
  const statusText = failing.length ? 'FAILED' : 'OK';
  const lines = [
    `[pb-smoke] ${statusText} total=${results.length} pages=${pageResults.length} failed=${failing.length}`,
  ];

  failing.slice(0, 8).forEach((item) => {
    lines.push(`[pb-smoke] issue ${formatTargetLabel(item)} -> ${collectIssuePreview(item) || 'unknown_issue'}`);
  });

  const screenshots = failing.map((item) => item?.screenshot).filter(Boolean);
  if (screenshots.length) {
    lines.push(`[pb-smoke] screenshots ${screenshots.join(' ')}`);
  }
  lines.forEach((line) => console.error(line));
}

async function fetchToken() {
  const api = await request.newContext({
    baseURL: DEFAULT_PB_BASE,
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
    '/index.html': () => waitForHomeOverviewReady(page, timeout),
    '/ibkr_runtime.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      const configText = document.getElementById('configDetail')?.innerText || '';
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
      return refreshInfo && !refreshInfo.includes('加载中') && configText && !configText.includes('加载中') && !overlayVisible;
    }, { timeout }),
    '/ibkr_system.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      const configText = document.getElementById('configArea')?.innerText || '';
      return refreshInfo && !refreshInfo.includes('加载中') && configText && !configText.includes('加载中');
    }, { timeout }),
    '/ibkr_account.html': () => page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
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
      const hasLoadedCards = document.querySelectorAll('.summary-card, .stat-card, .metric-card').length > 0;
      const hasFailureState = refreshInfo.includes('刷新失败')
        || document.querySelectorAll('#positionsArea .empty-state, #ordersArea .empty-state').length > 0;
      return (
        refreshInfo &&
        !refreshInfo.includes('加载中') &&
        !refreshInfo.includes('等待') &&
        (hasLoadedCards || hasFailureState) &&
        !overlayVisible
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
    const topSections = Array.from(new Set([
      ...document.querySelectorAll('.page-top-section, .home-hero'),
      ...document.querySelectorAll([
        '.page-shell > .hero',
        '.page-shell > .page-header',
        '.content > .hero',
        '.content > .page-header',
        '.content-area > .page-header',
        '.workspace > .panel > .workspace-head',
      ].join(', ')),
    ]));
    const bridgeShells = Array.from(new Set(document.querySelectorAll('.page-bridge, .domain-tabs')));
    const loadingOverlays = Array.from(document.querySelectorAll('.page-loading-overlay'));
    const visibleLoadingOverlays = loadingOverlays.filter((node) => {
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
      top_section_count: topSections.length,
      bridge_shell_count: bridgeShells.length,
      bridge_count: document.querySelectorAll('.page-bridge-link, .domain-tab').length,
      loading_overlay_count: loadingOverlays.length,
      visible_loading_overlay_count: visibleLoadingOverlays.length,
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
    return collectHomeOverviewIssues(page, mobile);
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
      const toggleButton = document.getElementById('toggleCurrentAdvancedFiltersBtn');
      const isCurrentScreenerView = isScreenerTab && currentViewPanel?.classList.contains('active');
      const rerunDailyScanButton = document.getElementById('rerunDailyScanBtn');
      const manualDailyScanFeedback = document.getElementById('manualDailyScanFeedback');
      const manualDailyScanCluster = document.querySelector('.manual-daily-scan-cluster');

      if (isCurrentScreenerView) {
        if (!visible(rerunDailyScanButton)) issues.push('manual_daily_scan_button_hidden');
        if (!visible(manualDailyScanFeedback)) issues.push('manual_daily_scan_feedback_hidden');
        if (!visible(manualDailyScanCluster)) issues.push('manual_daily_scan_cluster_hidden');
      }

      if (isMobileViewport) {
        if (!activeMobileLists.length) issues.push('missing_visible_mobile_card_list');
        if (visibleDesktopTables.length) issues.push(`desktop_table_visible:${visibleDesktopTables.length}`);
        if (isCurrentScreenerView) {
          const currentCards = document.getElementById('currentTargetsCards');
          if (!visible(currentCards)) issues.push('current_targets_mobile_cards_hidden');
          if (toggleButton && !visible(toggleButton)) issues.push('current_filter_toggle_hidden');
        }
      } else {
        if (activeMobileLists.length) issues.push(`mobile_card_list_visible_on_desktop:${activeMobileLists.length}`);
        if (!visibleDesktopTables.length) issues.push('desktop_table_missing_on_desktop');
      }

      return issues;
    }, mobile);
  }

  const systemDomainExpectations = {
    '/ibkr_system.html': { systemBridge: '总览' },
    '/ibkr_runtime.html': { systemBridge: '控制台', runtimeSummary: true },
    '/ibkr_config.html': { systemBridge: '配置' },
  };
  const expectedSystemPage = systemDomainExpectations[path];
  if (expectedSystemPage) {
    return page.evaluate((expected) => {
      const issues = [];
      const navTexts = Array.from(document.querySelectorAll('#nav .nav-item, #navContainer .nav-item'))
        .map((node) => String(node.textContent || '').trim())
        .filter(Boolean);
      const bridgeLabels = Array.from(document.querySelectorAll('#pageBridge .page-bridge-label'))
        .map((node) => String(node.textContent || '').trim())
        .filter(Boolean);
      if (!navTexts.some((text) => text.includes('系统'))) issues.push('system_domain_missing_bottom_system');
      if (navTexts.some((text) => text.includes('运维'))) issues.push('system_domain_legacy_bottom_ops');
      if (!bridgeLabels.includes(expected.systemBridge)) issues.push(`missing_system_bridge:${expected.systemBridge}`);
      if (expected.runtimeSummary) {
        const summaryText = String(document.getElementById('serviceTopologyArea')?.textContent || '');
        const summaryLink = document.querySelector('#serviceTopologyArea .runtime-link-action');
        const serviceControlCount = document.querySelectorAll('#serviceControlGrid .service-control-card').length;
        if (!summaryText.trim()) issues.push('missing_runtime_summary_copy');
        if (summaryLink) issues.push('unexpected_runtime_summary_link');
        if (serviceControlCount < 3) issues.push(`runtime_service_control_count:${serviceControlCount}`);
      }
      return issues;
    }, expectedSystemPage);
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
  const path = new URL(finalUrl).pathname;
  const isHomePage = path === '/index.html' || path === '/';
  const allowWorkspacePanelSpread = new Set([
    '/index.html',
    '/ibkr_runtime.html',
  ]).has(path);
  const allowVisibleInitialOverlay = new Set([
    '/ibkr_runtime.html',
  ]).has(path);
  const allowMissingTopSection = new Set([
    '/ibkr_config.html',
  ]).has(path);
  const allowTwoBridgeShells = false;

  const layoutIssues = [];
  if (/\/login\.html/.test(finalUrl)) layoutIssues.push('redirected_to_login');
  if (!navTexts.length) layoutIssues.push('missing_nav');
  if (navTexts.length && navTexts.length !== 4) layoutIssues.push(`nav_count:${navTexts.length}`);
  if (navTexts.some((text) => String(text || '').includes('运维'))) layoutIssues.push('legacy_ops_bottom_nav');
  if (navTexts.length && !navTexts.some((text) => String(text || '').includes('系统'))) layoutIssues.push('missing_system_bottom_nav');
  if (!layout.context_count) layoutIssues.push('missing_context_bar');
  if (layout.context_count !== 1) layoutIssues.push(`context_bar_count:${layout.context_count}`);
  if (!isHomePage && !allowMissingTopSection && !layout.top_section_count) layoutIssues.push('missing_top_section');
  if (!isHomePage && !layout.bridge_count) layoutIssues.push('missing_bridge');
  if (!isHomePage && layout.bridge_shell_count !== 1 && !(allowTwoBridgeShells && layout.bridge_shell_count === 2)) {
    layoutIssues.push(`bridge_shell_count:${layout.bridge_shell_count}`);
  }
  if (!allowVisibleInitialOverlay && layout.visible_loading_overlay_count) {
    layoutIssues.push(`visible_loading_overlay_count:${layout.visible_loading_overlay_count}`);
  }
  if (layout.horizontal_overflow) layoutIssues.push('horizontal_overflow');
  if (layout.bridge_row_spread_max > BRIDGE_MAX_SPREAD_PX) layoutIssues.push(`bridge_spread:${layout.bridge_row_spread_max}`);
  if (!allowWorkspacePanelSpread && layout.panel_row_spread_max > PAGE_MAX_SPREAD_PX) {
    layoutIssues.push(`panel_spread:${layout.panel_row_spread_max}`);
  }
  if (!allowWorkspacePanelSpread && layout.tall_panel_rows.length) {
    layoutIssues.push(`tall_panel_rows:${layout.tall_panel_rows.map((row) => row.max).join(',')}`);
  }
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
  if (opts.help) {
    console.log(HELP_TEXT);
    return;
  }
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
  printRunSummary(results);
  console.log(JSON.stringify(results, null, 2));
  if (failing.length) {
    process.exit(1);
  }
})();
