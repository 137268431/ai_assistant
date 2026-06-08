const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');
const htmlPath = path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'orders.html');
const sharedFiles = [
  'runtime-config.js',
  'data-center.js',
  'base.js',
  'ui-toast-nav.js',
  'ui-time-indicator.js',
  'ui-page.js',
  'ui-bridges.js',
  'ui-legacy.js',
  'ui.js',
].map((file) => path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'assets', 'js', 'shared', file));

const mockSnapshot = {
  ok: true,
  fetched_at: '2026-04-10T14:45:00Z',
  counts: {
    open_orders: 3,
  },
  live_order_coverage: {
    coverage_state: 'complete',
  },
  order_reconciliation: {
    broker_open_orders: 3,
    broker_matched_orders: 2,
    broker_only_open_orders: 1,
    pb_shadow_groups: 0,
  },
  live_open_orders: [],
  live_order_groups: [
    {
      symbol: 'CCJ',
      trade_group_id: 'tg-live-ccj',
      signal_id: 'sig-ccj',
      match_state: 'matched',
      latest_order_status: 'Submitted',
      live_order_count: 2,
      matched_live_orders: 2,
      broker_only_live_orders: 0,
      total_quantity: 94,
      remaining_quantity: 94,
      orders: [
        {
          order_id: '11252',
          client_order_id: 'tp_BATS_CCJ_short_20260410_0946',
          symbol: 'CCJ',
          side: 'BUY',
          role: 'take_profit',
          status: 'Submitted',
          match_state: 'matched',
          order_type: 'LMT',
          total_quantity: 47,
          filled_quantity: 0,
          remaining_quantity: 47,
          price: 103.17,
          trigger_price: 0,
          parent_id: '11251',
        },
        {
          order_id: '11253',
          client_order_id: 'sl_BATS_CCJ_short_20260410_0946',
          symbol: 'CCJ',
          side: 'BUY',
          role: 'stop_loss',
          status: 'PreSubmitted',
          match_state: 'matched',
          order_type: 'STP',
          total_quantity: 47,
          filled_quantity: 0,
          remaining_quantity: 47,
          price: 0,
          trigger_price: 106.2,
          parent_id: '11251',
        },
      ],
    },
    {
      symbol: 'MSFT',
      group_key: 'broker-only-msft',
      match_state: 'broker_only',
      latest_order_status: 'Submitted',
      live_order_count: 1,
      matched_live_orders: 0,
      broker_only_live_orders: 1,
      total_quantity: 10,
      remaining_quantity: 10,
      orders: [
        {
          order_id: '21201',
          client_order_id: '',
          symbol: 'MSFT',
          side: 'SELL',
          role: 'entry',
          status: 'Submitted',
          match_state: 'broker_only',
          order_type: 'LMT',
          total_quantity: 10,
          filled_quantity: 0,
          remaining_quantity: 10,
          price: 455.25,
          trigger_price: 0,
        },
      ],
    },
  ],
};

let html = fs.readFileSync(htmlPath, 'utf8');
const sharedBundle = sharedFiles.map((filePath) => fs.readFileSync(filePath, 'utf8')).join('\n');
const safeSharedBundle = sharedBundle.replace(/<\/script/gi, '<\\/script');
const injected = `<script>${safeSharedBundle}</script><script>
window.__MOCK_SNAPSHOT__ = ${JSON.stringify(mockSnapshot)};
window.__mock_environment = 'live';
window.getStoredEnvironment = function() { return window.__mock_environment || 'live'; };
window.setStoredEnvironment = function(environment) { window.__mock_environment = String(environment || 'live'); };
window.getToken = function() { return 'mock-token'; };
window.clearToken = function() {};
window.createPocketBaseClient = function() {
  return {
    authStore: { save() {}, clear() {} },
    collection() {
      return {
        getList: async () => ({ items: [], totalItems: 0 }),
      };
    },
  };
};
window.cachedPageJson = async function(pathname) {
  if (String(pathname || '').includes('/api/custom/ibkr/account_snapshot')) return window.__MOCK_SNAPSHOT__;
  return {};
};
window.fetchWithRetry = async function() {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(window.__MOCK_SNAPSHOT__),
    json: async () => window.__MOCK_SNAPSHOT__,
  };
};
window.cachedApiFetch = async function(collection) {
  if (collection === 'config') return { items: [] };
  return { items: [], totalItems: 0 };
};
window.apiFetch = window.cachedApiFetch;
window.fetchCollectionFullListCached = async function() { return []; };
window.showToast = function() {};
window.handleAuthError = function() {};
</script>`;

html = html.replace(/<script src="common\.js(?:\?[^"]*)?"><\/script>/, injected);
html = html.replace(/<link href="https:\/\/fonts\.googleapis\.com[^"]+" rel="stylesheet">/, '');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const pageErrors = [];
  const consoleMessages = [];

  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => consoleMessages.push(`${msg.type()}: ${msg.text()}`));

  await page.route('**/*', async (route) => {
    if (route.request().resourceType() === 'document') {
      await route.fulfill({ status: 200, contentType: 'text/html', body: html });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'text/plain', body: '' });
  });

  await page.goto('http://local.orders.test/orders.html?environment=live', { waitUntil: 'load' });
  try {
    await page.waitForFunction(() => document.querySelectorAll('.broker-live-card').length === 2, { timeout: 15000 });
  } catch (error) {
    const debug = await page.evaluate(() => ({
      brokerLivePanel: document.getElementById('brokerLivePanel')?.innerText || '',
      ordersContainer: document.getElementById('ordersContainer')?.innerText || '',
      bodyText: document.body?.innerText?.slice(0, 1200) || '',
    }));
    console.error(JSON.stringify({ pageErrors, consoleMessages, debug }, null, 2));
    throw error;
  }

  const before = await page.evaluate(() => {
    const firstCard = document.querySelector('.broker-live-card');
    return {
      cardCount: document.querySelectorAll('.broker-live-card').length,
      hiddenOrderPanels: document.querySelectorAll('.broker-live-orders[hidden]').length,
      firstOrdersHidden: firstCard?.querySelector('.broker-live-orders')?.hasAttribute('hidden') || false,
      firstCardText: firstCard?.innerText || '',
      metricText: document.querySelector('.broker-live-metrics')?.innerText || '',
      toggleAria: firstCard?.querySelector('.broker-live-toggle')?.getAttribute('aria-expanded') || '',
    };
  });

  await page.locator('.broker-live-card').first().locator('.broker-live-toggle').click();

  const expanded = await page.evaluate(() => {
    const firstCard = document.querySelector('.broker-live-card');
    return {
      hiddenOrderPanels: document.querySelectorAll('.broker-live-orders[hidden]').length,
      firstOrdersHidden: firstCard?.querySelector('.broker-live-orders')?.hasAttribute('hidden') || false,
      firstCardText: firstCard?.innerText || '',
      toggleAria: firstCard?.querySelector('.broker-live-toggle')?.getAttribute('aria-expanded') || '',
    };
  });

  await page.locator('.broker-live-card').first().locator('.broker-live-toggle').click();

  const collapsedAgain = await page.evaluate(() => {
    const firstCard = document.querySelector('.broker-live-card');
    return {
      firstOrdersHidden: firstCard?.querySelector('.broker-live-orders')?.hasAttribute('hidden') || false,
      toggleAria: firstCard?.querySelector('.broker-live-toggle')?.getAttribute('aria-expanded') || '',
    };
  });

  await browser.close();

  const passed = before.cardCount === 2
    && before.hiddenOrderPanels === 2
    && before.firstOrdersHidden
    && before.toggleAria === 'false'
    && before.firstCardText.includes('matched=2')
    && !before.firstCardText.includes('broker=11252')
    && before.metricText.includes('live 3')
    && before.metricText.includes('groups 2')
    && expanded.hiddenOrderPanels === 1
    && !expanded.firstOrdersHidden
    && expanded.toggleAria === 'true'
    && expanded.firstCardText.includes('broker=11252')
    && expanded.firstCardText.includes('price / trigger')
    && collapsedAgain.firstOrdersHidden
    && collapsedAgain.toggleAria === 'false'
    && pageErrors.length === 0;

  const output = {
    passed,
    before,
    expanded,
    collapsedAgain,
    pageErrors,
    consoleMessages,
  };

  console.log(JSON.stringify(output, null, 2));
  process.exit(passed ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
