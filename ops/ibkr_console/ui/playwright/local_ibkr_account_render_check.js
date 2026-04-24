const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

class Record {
  constructor(data) {
    this.data = data;
  }

  get(key) {
    return this.data[key];
  }
}

global.$app = {
  findRecordsByFilter(collection) {
    if (collection === 'orders') {
      return [
        new Record({
          id: 'pb-entry-1',
          environment: 'live',
          symbol: 'AAPL',
          status: 'Submitted',
          quantity: 100,
          filled_qty: 0,
          role: 'entry',
          relation_status: 'active',
          signal_id: 'sig-1',
          trade_group_id: 'tg-1',
          entry_order_unique_id: 'coid-1',
          unique_id: 'coid-1',
          broker_order_id: '1001',
          order_id: '1001',
          updated: '2026-04-10T09:31:00Z',
          extra: '{}',
        }),
        new Record({
          id: 'pb-tp-1',
          environment: 'live',
          symbol: 'AAPL',
          status: 'PreSubmitted',
          quantity: 100,
          filled_qty: 0,
          role: 'take_profit',
          relation_status: 'planned',
          signal_id: 'sig-1',
          trade_group_id: 'tg-1',
          entry_order_unique_id: 'coid-1',
          parent_order_unique_id: 'coid-1',
          unique_id: 'coid-1-tp',
          broker_order_id: '1002',
          order_id: '1002',
          updated: '2026-04-10T09:31:01Z',
          extra: '{}',
        }),
        new Record({
          id: 'pb-stop-shadow',
          environment: 'live',
          symbol: 'MSFT',
          status: 'Submitted',
          quantity: 50,
          filled_qty: 0,
          role: 'stop_loss',
          relation_status: 'planned',
          signal_id: 'sig-2',
          trade_group_id: 'tg-2',
          entry_order_unique_id: 'coid-2',
          parent_order_unique_id: 'coid-2',
          unique_id: 'coid-2-sl',
          broker_order_id: '2002',
          order_id: '2002',
          updated: '2026-04-10T09:35:00Z',
          extra: '{}',
        }),
      ];
    }

    if (collection === 'ibkr_signals') {
      return [
        new Record({
          signal_id: 'sig-1',
          symbol: 'AAPL',
          status: 'confirmed',
          note: 'ok',
          updated: '2026-04-10T09:31:30Z',
          extra: '{}',
        }),
        new Record({
          signal_id: 'sig-2',
          symbol: 'MSFT',
          status: 'pending',
          note: 'waiting',
          updated: '2026-04-10T09:35:30Z',
          extra: '{}',
        }),
      ];
    }

    return [];
  },
};

const repoRoot = path.resolve(__dirname, '..', '..', '..', '..');

function enrichAccountSnapshot(payload) {
  return payload;
}

const mockSnapshot = enrichAccountSnapshot(
  {
    ok: true,
    fetched_at: '2026-04-10T09:45:00Z',
    status: 'online',
    session_authenticated: true,
    gateway_running: true,
    service_running: true,
    account_id: 'U1234567',
    summary: {
      account_code: 'U1234567',
      account_type: 'INDIVIDUAL',
      net_liquidation: 120000,
      available_funds: 50000,
      buying_power: 100000,
      excess_liquidity: 75000,
      equity_with_loan: 120000,
      gross_position_value: 25000,
      total_cash_value: 95000,
      initial_margin: 8000,
      maintenance_margin: 5000,
      sma: 50000,
      day_trades_remaining: 3,
      leverage: 0.2,
    },
    positions: [
      {
        symbol: 'AAPL',
        conid: 265598,
        quantity: 100,
        direction: 'long',
        avg_cost: 180,
        avg_price: 180,
        market_price: 182,
        market_value: 18200,
        unrealized_pnl: 200,
        realized_pnl: 0,
        account: 'U1234567',
        currency: 'USD',
        asset_class: 'STK',
        raw: {},
      },
    ],
    orders: [],
    live_open_orders: [
      {
        order_id: '1001',
        symbol: 'AAPL',
        side: 'BUY',
        status: 'Submitted',
        status_key: 'SUBMITTED',
        total_quantity: 100,
        filled_quantity: 0,
        remaining_quantity: 100,
        order_type: 'LMT',
        price: 180,
        client_order_id: 'coid-1',
        can_cancel: true,
        can_modify: true,
        is_open: true,
        submitted_time: '2026-04-10T09:31:00Z',
        submitted_time_ms: 1775813460000,
        account: 'U1234567',
        currency: 'USD',
        asset_class: 'STK',
        listing_exchange: 'NASDAQ',
        raw: { orderId: '1001' },
      },
      {
        order_id: '1002',
        parent_id: '1001',
        symbol: 'AAPL',
        side: 'SELL',
        status: 'PreSubmitted',
        status_key: 'SUBMITTED',
        total_quantity: 100,
        filled_quantity: 0,
        remaining_quantity: 100,
        order_type: 'LMT',
        price: 186,
        client_order_id: 'coid-1-tp',
        can_cancel: true,
        can_modify: true,
        is_open: true,
        submitted_time: '2026-04-10T09:31:01Z',
        submitted_time_ms: 1775813461000,
        account: 'U1234567',
        currency: 'USD',
        asset_class: 'STK',
        listing_exchange: 'NASDAQ',
        raw: { orderId: '1002' },
      },
      {
        order_id: '3001',
        symbol: 'TSLA',
        side: 'BUY',
        status: 'Submitted',
        status_key: 'SUBMITTED',
        total_quantity: 10,
        filled_quantity: 0,
        remaining_quantity: 10,
        order_type: 'LMT',
        price: 170,
        client_order_id: '',
        can_cancel: true,
        can_modify: false,
        is_open: true,
        submitted_time: '2026-04-10T09:40:00Z',
        submitted_time_ms: 1775814000000,
        recovery_source: 'status_recovered',
        seed_sources: ['pb'],
        account: 'U1234567',
        currency: 'USD',
        asset_class: 'STK',
        listing_exchange: 'NASDAQ',
        raw: { orderId: '3001' },
      },
    ],
    counts: {},
    live_order_coverage: {
      coverage_state: 'recovered',
      bulk_open_count: 2,
      recovered_from_status_count: 1,
      unresolved_seed_count: 0,
      unresolved_order_ids: [],
    },
  },
  'live',
);

const htmlPath = path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'ibkr_account.html');
const commonBasePath = path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'assets', 'js', 'shared', 'base.js');
const commonUiPath = path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'assets', 'js', 'shared', 'ui.js');
let html = fs.readFileSync(htmlPath, 'utf8');
let common = `${fs.readFileSync(commonBasePath, 'utf8')}\n${fs.readFileSync(commonUiPath, 'utf8')}`;
common = common.replace(
  /function getStoredEnvironment\(\) \{[\s\S]*?\n\}/,
  "function getStoredEnvironment() {\n  return window.__mock_environment || '';\n}",
);
common = common.replace(
  /function setStoredEnvironment\(environment\) \{[\s\S]*?\n\}/,
  "function setStoredEnvironment(environment) {\n  window.__mock_environment = String(environment || '');\n}",
);
common = common.replace(
  /function getToken\(\) \{[\s\S]*?\n\}/,
  "function getToken() {\n  return 'mock-token';\n}",
);
common = common.replace(
  /function clearToken\(\) \{[\s\S]*?\n\}/,
  "function clearToken() {}\n",
);
const injected = `<script>${common}</script><script>
window.__MOCK_SNAPSHOT__ = ${JSON.stringify(mockSnapshot)};
window.buildPageUrl = function(path, params = {}, options = {}) {
  const search = new URLSearchParams();
  const env = options && options.environment ? String(options.environment) : 'live';
  if (env) search.set('environment', env);
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  });
  const query = search.toString();
  return query ? \`\${path}?\${query}\` : path;
};
window.fetchWithRetry = async function() {
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(window.__MOCK_SNAPSHOT__),
  };
};
window.getToken = function() { return 'mock-token'; };
window.showToast = function() {};
window.handleAuthError = function() {};
</script>`;

html = html.replace('<script src="common.js"></script>', injected);
html = html.replace(/<link href="https:\/\/fonts\.googleapis\.com[^"]+" rel="stylesheet">/, '');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
  const pageErrors = [];
  const consoleMessages = [];

  page.on('pageerror', (err) => pageErrors.push(err.message));
  page.on('console', (msg) => consoleMessages.push(`${msg.type()}: ${msg.text()}`));

  await page.setContent(html, { waitUntil: 'load' });
  try {
    await page.waitForFunction(() => {
      const text = document.getElementById('ordersMeta')?.textContent || '';
      return text.includes('live · groups');
    }, { timeout: 15000 });
  } catch (error) {
    const debug = await page.evaluate(() => ({
      refreshInfo: document.getElementById('refreshInfo')?.textContent || '',
      blocker: document.getElementById('blocker')?.innerText || '',
      ordersMeta: document.getElementById('ordersMeta')?.textContent || '',
      ordersArea: document.getElementById('ordersArea')?.innerText || '',
      bodyText: document.body?.innerText?.slice(0, 1200) || '',
    }));
    console.error(JSON.stringify({ pageErrors, consoleMessages, debug }, null, 2));
    throw error;
  }

  const result = await page.evaluate(() => ({
    ordersMeta: document.getElementById('ordersMeta')?.textContent || '',
    summaryText: document.getElementById('ordersSummary')?.innerText || '',
    areaText: document.getElementById('ordersArea')?.innerText || '',
    sectionTitles: Array.from(document.querySelectorAll('.orders-section-card .section-title')).map((el) => el.textContent.trim()),
    orderRowCount: document.querySelectorAll('.order-row').length,
  }));

  const screenshot = '/tmp/local_ibkr_account_render_check.png';
  await page.screenshot({ path: screenshot, fullPage: false });
  await browser.close();

  const passed = result.ordersMeta.includes('3 live')
    && result.ordersMeta.includes('groups 2')
    && result.areaText.includes('Broker Only Live Chains')
    && result.areaText.includes('Matched Live Chains')
    && result.areaText.includes('PB Shadow Chains')
    && result.areaText.includes('missing_client_order_id')
    && result.orderRowCount >= 4
    && pageErrors.length === 0;

  const output = {
    passed,
    ...result,
    pageErrors,
    consoleMessages,
    screenshot,
  };

  console.log(JSON.stringify(output, null, 2));
  process.exit(passed ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
