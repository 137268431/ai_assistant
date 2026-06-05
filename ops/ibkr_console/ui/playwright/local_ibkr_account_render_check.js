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
      remaining_buying_power: 100000,
      remaining_buying_power_pct_net_liq: 83.3333333333,
      excess_liquidity: 75000,
      equity_with_loan: 120000,
      gross_position_value: 25000,
      total_cash_value: 95000,
      realized_pnl: -12.5,
      unrealized_pnl: 42.25,
      daily_pnl: 29.75,
      today_pnl: 29.75,
      daily_pnl_available: true,
      account_today_pnl: {
        ok: true,
        net: 29.75,
        currency: 'USD',
        source: 'broker_daily_pnl',
        raw_field: 'DailyPnL',
        realized: -12.5,
        unrealized: 42.25,
        message: '',
      },
      initial_margin: 8000,
      maintenance_margin: 5000,
      sma: 50000,
      day_trades_remaining: 3,
      leverage: 0.2,
    },
    buying_power_guard: {
      enabled: true,
      basis: 'buying_power',
      remaining: 100000,
      net_liquidation: 120000,
      remaining_after: 100000,
      requested_exposure: 0,
      remaining_pct_net_liq: 83.3333333333,
      remaining_after_pct_net_liq: 83.3333333333,
      warn_floor: 25000,
      block_floor: 12000,
      warn_usd: 25000,
      warn_pct_net_liq: 20,
      block_usd: 10000,
      block_pct_net_liq: 10,
      state: 'ok',
      reason: 'ok',
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
        relation: {
          status: 'system_managed',
          signal_id: 'sig-1',
          trade_group_id: 'tg-1',
          entry_order_unique_id: 'coid-1',
          last_order_status: 'Filled',
          order_updated: '2026-04-10T09:31:30Z',
          commission: 2.5,
          commission_currency: 'USD',
          commission_known: true,
          commission_fill_count: 1,
        },
        raw: {},
      },
      {
        symbol: 'ABNB',
        conid: 459530964,
        quantity: 0,
        direction: 'flat',
        avg_cost: 0,
        avg_price: 0,
        market_price: 134.4,
        market_value: 0,
        unrealized_pnl: 0,
        realized_pnl: -141.06,
        account: 'U1234567',
        currency: 'USD',
        asset_class: 'STK',
        relation: {
          status: 'flat_broker_position',
          reason: 'gateway_flat_position_record',
          signal_id: 'sig-flat',
          trade_group_id: 'tg-flat',
          entry_order_unique_id: 'coid-flat',
          last_order_status: 'Filled',
          order_updated: '2026-04-10T09:42:00Z',
          order_count: 2,
          entry_filled_qty: 10,
          exit_filled_qty: 10,
          commission: 1.25,
          commission_currency: 'USD',
          commission_known: true,
        },
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
        signal_id: 'sig-1',
        trade_group_id: 'tg-1',
        entry_order_unique_id: 'coid-1',
        match_state: 'matched',
        pb_context: {
          signal_id: 'sig-1',
          trade_group_id: 'tg-1',
          entry_order_unique_id: 'coid-1',
          match_state: 'matched',
          relation_status: 'active',
          pb_status: 'Submitted',
        },
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
        signal_id: 'sig-1',
        trade_group_id: 'tg-1',
        entry_order_unique_id: 'coid-1',
        match_state: 'matched',
        pb_context: {
          signal_id: 'sig-1',
          trade_group_id: 'tg-1',
          entry_order_unique_id: 'coid-1',
          match_state: 'matched',
          relation_status: 'planned',
          pb_status: 'PreSubmitted',
        },
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
        order_id: '1003',
        symbol: 'AAPL',
        side: 'BUY',
        status: 'Submitted',
        status_key: 'SUBMITTED',
        total_quantity: 30,
        filled_quantity: 0,
        remaining_quantity: 30,
        order_type: 'LMT',
        price: 180,
        client_order_id: 'coid-1-tactical',
        signal_id: 'sig-1',
        trade_group_id: 'tg-1-tactical',
        entry_order_unique_id: 'coid-1-tactical',
        match_state: 'matched',
        pb_context: {
          signal_id: 'sig-1',
          trade_group_id: 'tg-1-tactical',
          entry_order_unique_id: 'coid-1-tactical',
          match_state: 'matched',
          relation_status: 'active',
          pb_status: 'Submitted',
        },
        can_cancel: true,
        can_modify: true,
        is_open: true,
        submitted_time: '2026-04-10T09:31:02Z',
        submitted_time_ms: 1775813462000,
        account: 'U1234567',
        currency: 'USD',
        asset_class: 'STK',
        listing_exchange: 'NASDAQ',
        raw: { orderId: '1003' },
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
        match_state: 'broker_only',
        diagnostic_tags: ['missing_client_order_id'],
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
    pb_only_order_groups: [
      {
        symbol: 'MSFT',
        signal_id: 'sig-2',
        trade_group_id: 'tg-2',
        entry_order_unique_id: 'coid-2',
        latest_order_status: 'Submitted',
        latest_updated: '2026-04-10T09:35:00Z',
        order_count: 1,
        matched_broker_orders: 0,
        orders: [
          {
            role: 'stop_loss',
            status: 'Submitted',
            quantity: 50,
            filled_qty: 0,
            relation_status: 'planned',
            unique_id: 'coid-2-sl',
            broker_order_id: '2002',
            updated: '2026-04-10T09:35:00Z',
          },
        ],
      },
    ],
    counts: {
      open_positions: 1,
      system_managed_positions: 1,
      external_positions: 0,
      flat_positions: 1,
      open_orders: 4,
      cancelable_orders: 4,
      editable_orders: 3,
      outside_rth_orders: 0,
    },
    order_reconciliation: {
      broker_matched_groups: 2,
      broker_only_groups: 1,
      pb_shadow_groups: 1,
      cancelable_orders: 4,
      editable_orders: 3,
      missing_client_order_id_orders: 1,
      status_mismatch_orders: 0,
      quantity_mismatch_orders: 0,
      filled_qty_mismatch_orders: 0,
      coverage_state: 'recovered',
    },
    live_order_coverage: {
      coverage_state: 'recovered',
      bulk_open_count: 3,
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
const commonUiBundlePaths = [
  'ui-toast-nav.js',
  'ui-time-indicator.js',
  'ui-page.js',
  'ui-bridges.js',
  'ui-legacy.js',
].map((file) => path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'assets', 'js', 'shared', file));
let html = fs.readFileSync(htmlPath, 'utf8');
let common = [
  fs.readFileSync(commonBasePath, 'utf8'),
  fs.readFileSync(commonUiPath, 'utf8'),
  ...commonUiBundlePaths.map((filePath) => fs.readFileSync(filePath, 'utf8')),
].join('\n');
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
const safeCommon = common.replace(/<\/script/gi, '<\\/script');
const injected = `<script>${safeCommon}</script><script>
window.__MOCK_SNAPSHOT__ = ${JSON.stringify(mockSnapshot)};
window.getUsDate = function() { return '2026-04-10'; };
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
window.fetchWithRetry = async function(input) {
  const url = String(input || '');
  if (url.includes('/api/custom/ibkr/today-targets')) {
    const payload = {
      ok: true,
      market_date: '2026-04-10',
      summary: {
        total: 25,
        active_count: 24,
        candidate_count: 1,
        signaled_count: 3,
      },
      items: [],
    };
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify(payload),
      json: async () => payload,
    };
  }
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(window.__MOCK_SNAPSHOT__),
    json: async () => window.__MOCK_SNAPSHOT__,
  };
};
window.getToken = function() { return 'mock-token'; };
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
  await page.goto('http://local.ibkr-account.test/ibkr_account.html?environment=live&broker_mode=paper', { waitUntil: 'load' });
  try {
    await page.waitForFunction(() => {
      const text = document.getElementById('ordersMeta')?.textContent || '';
      return text.includes('IBKR live') && text.includes('signals') && text.includes('brackets');
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
    positionsMeta: document.getElementById('positionsMeta')?.textContent || '',
    accountSummaryText: document.getElementById('summaryGrid')?.innerText || '',
    pageTopText: document.getElementById('pageTopSection')?.innerText || '',
    contextText: document.getElementById('contextBar')?.innerText || '',
    positionsText: document.getElementById('positionsArea')?.innerText || '',
    openPositionCardText: document.querySelector('.position-grid .surface-card')?.innerText || '',
    flatSectionText: document.querySelector('.flat-position-section')?.innerText || '',
    summaryText: document.getElementById('ordersSummary')?.innerText || '',
    areaText: document.getElementById('ordersArea')?.innerText || '',
    sectionTitles: Array.from(document.querySelectorAll('.orders-section-card .section-title')).map((el) => el.textContent.trim()),
    tableCount: document.querySelectorAll('.orders-live-table').length,
    chainRowCount: document.querySelectorAll('.order-chain-row').length,
    legRowCount: document.querySelectorAll('.order-leg-row').length,
    diagnosticsCount: document.querySelectorAll('.order-diagnostics').length,
    flatCloseButtonCount: document.querySelectorAll('.flat-position-section .action-btn.close').length,
  }));

  const screenshot = '/tmp/local_ibkr_account_render_check.png';
  await page.screenshot({ path: screenshot, fullPage: false });
  await browser.close();

  const passed = result.ordersMeta.includes('4 IBKR live')
    && result.ordersMeta.includes('signals 2')
    && result.ordersMeta.includes('brackets 3')
    && result.areaText.includes('Broker Only Live Signal Groups')
    && result.areaText.includes('Matched Live Signal Groups')
    && result.areaText.includes('brackets=2:')
    && result.areaText.includes('tg-1')
    && result.areaText.includes('tg-1-tactical')
    && result.areaText.includes('Stale / Needs Repair Chains')
    && result.areaText.includes('missing_client_order_id')
    && result.accountSummaryText.includes('Remaining BP')
    && result.accountSummaryText.includes('+0.02% NetLiq')
    && result.pageTopText.includes('盘中标 24 active')
    && result.contextText.includes('盘中标')
    && result.contextText.includes('24 active')
    && result.positionsMeta.includes('1 open · 1 flat rows')
    && result.positionsText.includes('Unrealized %')
    && result.openPositionCardText.includes('Fees')
    && result.openPositionCardText.includes('$2.50')
    && result.flatSectionText.includes('今日已闭合 / FLAT')
    && result.flatSectionText.includes('ABNB')
    && result.flatSectionText.includes('不是当前 IBKR live open order')
    && result.flatSectionText.includes('Fees')
    && result.flatSectionText.includes('$1.25')
    && result.flatSectionText.includes('订单页')
    && result.flatCloseButtonCount === 0
    && result.tableCount >= 3
    && result.chainRowCount >= 3
    && result.legRowCount >= 4
    && result.diagnosticsCount >= 3
    && result.accountSummaryText.includes('Today P&L')
    && result.accountSummaryText.includes('IBKR Daily PnL')
    && result.accountSummaryText.includes('Realized')
    && result.accountSummaryText.includes('Floating')
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
