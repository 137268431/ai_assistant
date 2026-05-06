const { chromium } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const ENVIRONMENT = process.env.IBKR_ENVIRONMENT || 'live';
const NAV_TIMEOUT = 60000;
const SETTLE_MS = 2500;

async function auth() {
  const resp = await fetch(`${PB_BASE}/api/collections/_superusers/auth-with-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identity: EMAIL, password: PASSWORD }),
  });
  const json = await resp.json();
  if (!resp.ok || !json.token) throw new Error(`auth_failed: ${JSON.stringify(json)}`);
  return json.token;
}

(async () => {
  const token = await auth();
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.addInitScript((t) => localStorage.setItem('pb_token', t), token);
  const page = await context.newPage();

  const apiResponses = {};
  const pageErrors = [];

  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('response', async (resp) => {
    const url = resp.url();
    if (url.includes('/api/custom/ibkr/account_snapshot')) {
      try {
        const body = await resp.json();
        apiResponses.account_snapshot = body;
      } catch (_) {}
    }
  });

  const accountUrl = `${CONSOLE_BASE}/ibkr_account.html?environment=${ENVIRONMENT}`;
  await page.goto(accountUrl, { waitUntil: 'networkidle', timeout: NAV_TIMEOUT });

  // Wait for snapshot data to load (refreshInfo stops saying 加载中)
  try {
    await page.waitForFunction(() => {
      const refreshInfo = document.getElementById('refreshInfo')?.textContent || '';
      const summaryCards = document.querySelectorAll('.summary-card').length;
      return refreshInfo && !refreshInfo.includes('加载中') && !refreshInfo.includes('刷新中') && summaryCards > 0;
    }, { timeout: 30000 });
  } catch (_) {
    await page.waitForTimeout(SETTLE_MS * 2);
  }
  await page.waitForTimeout(SETTLE_MS);

  // --- collect orders section state ---
  const ordersMeta = await page.locator('#ordersMeta').textContent().catch(() => '');
  const ordersAreaText = await page.locator('#ordersArea').innerText().catch(() => '');
  const ordersSummaryText = await page.locator('#ordersSummary').innerText().catch(() => '');
  const tableCount = await page.locator('.orders-live-table').count().catch(() => 0);
  const chainRowCount = await page.locator('.order-chain-row').count().catch(() => 0);
  const legRowCount = await page.locator('.order-leg-row').count().catch(() => 0);

  // parse broker open count from ordersSummary cards
  const brokerOpenMatch = ordersSummaryText.match(/LIVE OPEN[\s\S]*?(\d+)/i)
    || ordersSummaryText.match(/BROKER OPEN[\s\S]*?(\d+)/i)
    || ordersSummaryText.match(/Open Orders[\s\S]*?(\d+)/i);
  const brokerOpenCount = brokerOpenMatch ? parseInt(brokerOpenMatch[1], 10) : null;

  const hasBrokerOrders = !ordersAreaText.includes('当前没有 broker 已确认仍在挂着的实时订单')
    && !ordersAreaText.includes('当前无法确认完整实时挂单列表')
    && ordersAreaText.trim().length > 0;

  const hasPbOnlyFallback = ordersAreaText.includes('PB Shadow');

  // --- collect snapshot API result ---
  const snapshotOrders = Array.isArray(apiResponses.account_snapshot?.live_open_orders)
    ? apiResponses.account_snapshot.live_open_orders
    : null;
  const snapshotOrderCount = snapshotOrders !== null ? snapshotOrders.length : null;
  const snapshotOpenOrderCount = apiResponses.account_snapshot?.counts?.open_orders ?? null;
  const coverageState = apiResponses.account_snapshot?.live_order_coverage?.coverage_state ?? null;

  // screenshot for evidence
  const screenshotPath = '/tmp/ibkr_account_orders_check.png';
  await page.screenshot({ path: screenshotPath, fullPage: false });

  await context.close();
  await browser.close();

  const renderedOpenOrders = snapshotOrderCount === null || snapshotOrderCount === 0 || legRowCount >= snapshotOrderCount;
  const passed = snapshotOrderCount !== null
    ? (snapshotOpenOrderCount === null || snapshotOpenOrderCount === snapshotOrderCount)
      && coverageState !== 'degraded'
      && renderedOpenOrders
    : (hasBrokerOrders && !hasPbOnlyFallback);

  const result = {
    passed,
    environment: ENVIRONMENT,
    orders_meta: ordersMeta.trim(),
    broker_open_count: brokerOpenCount,
    snapshot_order_count: snapshotOrderCount,
    snapshot_open_order_count: snapshotOpenOrderCount,
    coverage_state: coverageState,
    table_count: tableCount,
    chain_row_count: chainRowCount,
    leg_row_count: legRowCount,
    has_broker_orders: hasBrokerOrders,
    has_pb_only_fallback: hasPbOnlyFallback,
    api_snapshot_ok: apiResponses.account_snapshot?.ok ?? null,
    page_errors: pageErrors,
    screenshot: screenshotPath,
    diagnosis: passed
      ? 'PASS: live_open_orders and counts.open_orders are aligned.'
      : coverageState === 'degraded'
        ? 'FAIL: live_open_orders coverage is degraded — unresolved seed orders remain.'
        : snapshotOrderCount === 0
          ? 'FAIL: account_snapshot returned live_open_orders:[] — no broker-confirmed live orders are visible right now.'
          : 'FAIL: live_open_orders and rendered state are inconsistent.',
  };

  console.log(JSON.stringify(result, null, 2));
  process.exit(passed ? 0 : 1);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
