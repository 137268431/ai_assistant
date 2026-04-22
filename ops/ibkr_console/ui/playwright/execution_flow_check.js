const { chromium } = require('playwright');

const BASE = process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';

async function auth() {
  const resp = await fetch(`${BASE}/api/collections/_superusers/auth-with-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identity: EMAIL, password: PASSWORD }),
  });
  const json = await resp.json();
  if (!resp.ok || !json.token) throw new Error(`auth_failed:${JSON.stringify(json)}`);
  return json.token;
}

async function pbList(token, collection, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  });
  const resp = await fetch(`${BASE}/api/collections/${collection}/records?${query.toString()}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const json = await resp.json();
  if (!resp.ok) throw new Error(`${collection}_query_failed:${JSON.stringify(json)}`);
  return Array.isArray(json.items) ? json.items : [];
}

function deriveDate(item) {
  if (!item) return '';
  if (item.us_time) return String(item.us_time).slice(0, 10);
  if (item.created) return String(item.created).slice(0, 10);
  if (item.bar_time_ms) return new Date(Number(item.bar_time_ms)).toISOString().slice(0, 10);
  return '';
}

(async () => {
  const token = await auth();
  const latestOrderWithTradeGroup = (await pbList(token, 'orders', {
    perPage: 1,
    sort: '-bar_time_ms,-created',
    filter: 'environment = "live" && signal_id != "" && trade_group_id != ""',
  }).catch(() => []))[0] || null;
  const latestOrderFallback = latestOrderWithTradeGroup || (await pbList(token, 'orders', {
    perPage: 1,
    sort: '-bar_time_ms,-created',
    filter: 'environment = "live" && signal_id != ""',
  }).catch(() => []))[0] || null;
  const latestOrder = latestOrderFallback;

  if (!latestOrder) {
    console.log(JSON.stringify({ skipped: true, reason: 'no_live_orders_with_signal' }, null, 2));
    return;
  }

  const signalId = String(latestOrder.signal_id || '').trim();
  const tradeGroupId = String(latestOrder.trade_group_id || '').trim();
  const orderId = String(latestOrder.order_id || latestOrder.id || '').trim();
  const date = deriveDate(latestOrder);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.addInitScript((savedToken) => localStorage.setItem('pb_token', savedToken), token);
  const page = await context.newPage();
  const errors = [];

  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) errors.push(`console:${msg.type()}:${msg.text()}`);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 400) errors.push(`response:${resp.status()}:${resp.url()}`);
  });

  const signalsUrl = new URL('/ibkr_signals.html', BASE);
  signalsUrl.searchParams.set('environment', 'live');
  if (date) signalsUrl.searchParams.set('date', date);
  if (signalId) signalsUrl.searchParams.set('signal_id', signalId);

  const result = {
    seed: {
      signal_id: signalId,
      trade_group_id: tradeGroupId,
      order_id: orderId,
      date,
      symbol: latestOrder.symbol || '',
    },
    steps: [],
    errors,
  };

  try {
    await page.goto(signalsUrl.toString(), { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForTimeout(1800);
    result.steps.push({
      step: 'signals_open',
      url: page.url(),
      title: await page.title(),
      bridge_texts: await page.locator('.page-bridge .page-bridge-label').allTextContents().catch(() => []),
      order_button_count: await page.locator('button:not([disabled])').filter({ hasText: /^📋 订单/ }).count().catch(() => 0),
    });

    await page.locator('button:not([disabled])').filter({ hasText: /^📋 订单/ }).first().click();
    await page.waitForURL(/\/ibkr_orders\.html/, { timeout: 15000 });
    await page.waitForTimeout(1500);
    result.steps.push({
      step: 'orders_open',
      url: page.url(),
      title: await page.title(),
      bridge_texts: await page.locator('.page-bridge .page-bridge-label').allTextContents().catch(() => []),
      trade_group_button_count: await page.locator('button:not([disabled])').filter({ hasText: '交易组明细' }).count().catch(() => 0),
    });

    await page.locator('button:not([disabled])').filter({ hasText: '交易组明细' }).first().click();
    await page.waitForURL(/\/ibkr_order_details\.html/, { timeout: 15000 });
    await page.waitForTimeout(1500);
    const orderDetailsUrl = page.url();
    result.steps.push({
      step: 'order_details_open',
      url: orderDetailsUrl,
      title: await page.title(),
      bridge_texts: await page.locator('.page-bridge .page-bridge-label').allTextContents().catch(() => []),
      contains_signal_id: signalId ? orderDetailsUrl.includes(encodeURIComponent(signalId)) || orderDetailsUrl.includes(signalId) : false,
      contains_trade_group_id: tradeGroupId ? orderDetailsUrl.includes(encodeURIComponent(tradeGroupId)) || orderDetailsUrl.includes(tradeGroupId) : false,
      contains_order_id: orderId ? orderDetailsUrl.includes(encodeURIComponent(orderId)) || orderDetailsUrl.includes(orderId) : false,
      body_has_expected_text: await page.locator('body').innerText().then((text) => /订单明细|交易组|order_id|trade_group_id/.test(text)).catch(() => false),
    });
  } finally {
    await context.close();
    await browser.close();
  }

  console.log(JSON.stringify(result, null, 2));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
