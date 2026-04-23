const { chromium, devices } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';

async function auth() {
  const resp = await fetch(`${PB_BASE}/api/collections/_superusers/auth-with-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identity: EMAIL, password: PASSWORD })
  });
  const json = await resp.json();
  if (!resp.ok || !json.token) throw new Error(`auth_failed:${JSON.stringify(json)}`);
  return json.token;
}

function deriveDate(item) {
  if (!item) return null;
  if (item.us_time) return String(item.us_time).split(' ')[0];
  if (item.bar_time_ms) return new Date(Number(item.bar_time_ms)).toISOString().slice(0, 10);
  if (item.created) return String(item.created).slice(0, 10);
  return null;
}

async function latestDate(token, collection, sort) {
  const params = new URLSearchParams({
    perPage: '1',
    sort,
    filter: 'environment = "live"',
  });
  const resp = await fetch(`${PB_BASE}/api/collections/${collection}/records?${params.toString()}`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  const json = await resp.json();
  if (!resp.ok) {
    const message = String(json?.message || '');
    if (resp.status === 404 && message.includes('Missing collection context.')) {
      return { date: null, unavailable: true, reason: 'missing_collection_context' };
    }
    throw new Error(`${collection}_query_failed:${JSON.stringify(json)}`);
  }
  const items = Array.isArray(json.items) ? json.items : [];
  const date = deriveDate(items[0] || null);
  return {
    date,
    unavailable: !date,
    reason: date ? '' : 'no_records',
  };
}

function buildTargetUrl(path, date) {
  const url = new URL(path, CONSOLE_BASE);
  url.searchParams.set('environment', 'live');
  if (date) url.searchParams.set('date', date);
  return url.toString();
}

async function inspect(browser, token, spec, mobile = false) {
  const context = mobile
    ? await browser.newContext({ ...devices['iPhone 12'] })
    : await browser.newContext({ viewport: { width: 1440, height: 960 } });
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

  const result = {
    name: spec.name,
    device: mobile ? 'iPhone 12' : 'desktop',
    url: spec.url,
  };

  try {
    await page.goto(spec.url, { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForTimeout(2500);

    const buttons = page.locator('button:not([disabled])').filter({ hasText: '图表页' });
    result.button_count = await buttons.count();
    if (!result.button_count) {
      result.ok = false;
      result.reason = 'no_enabled_chart_button';
      result.body_snippet = (await page.locator('body').innerText()).slice(0, 400);
      result.errors = errors;
      await context.close();
      return result;
    }

    await buttons.first().click();
    await page.waitForURL(/\/ibkr_chart\.html/, { timeout: 15000 });
    await page.waitForTimeout(1800);

    result.final_url = page.url();
    result.hero_title = await page.locator('#heroTitle').innerText().catch(() => '');
    result.chart_title = await page.locator('#chartPanelTitle').innerText().catch(() => '');
    result.tf_count = await page.locator('.tf-btn').count().catch(() => 0);
    result.rail_count = await page.locator('.rail-card').count().catch(() => 0);
    result.ok = /\/ibkr_chart\.html/.test(result.final_url) && result.tf_count === 6 && result.rail_count >= 1;
  } catch (error) {
    result.ok = false;
    result.reason = error.message;
  }

  result.errors = errors;
  await context.close();
  return result;
}

(async () => {
  const token = await auth();
  const dates = {
    signals: await latestDate(token, 'ibkr_signals', '-bar_time_ms,-created'),
    reverse: await latestDate(token, 'reverse_signals', '-created'),
    orders: await latestDate(token, 'orders', '-bar_time_ms,-created'),
  };

  const specs = [
    { name: 'signals_to_chart', url: buildTargetUrl('/ibkr_signals.html', dates.signals.date) },
    dates.reverse.unavailable
      ? { name: 'reverse_to_chart', skipped: true, reason: dates.reverse.reason }
      : { name: 'reverse_to_chart', url: buildTargetUrl('/ibkr_reverse_signals.html', dates.reverse.date) },
    dates.orders.unavailable
      ? { name: 'orders_to_chart', skipped: true, reason: dates.orders.reason }
      : { name: 'orders_to_chart', url: buildTargetUrl('/ibkr_orders.html', dates.orders.date) },
  ];

  const browser = await chromium.launch({ headless: true });
  const results = [];
  for (const spec of specs) {
    if (spec.skipped) {
      results.push({ name: spec.name, skipped: true, reason: spec.reason });
      continue;
    }
    results.push(await inspect(browser, token, spec, false));
    results.push(await inspect(browser, token, spec, true));
  }
  await browser.close();

  console.log(JSON.stringify({ dates, results }, null, 2));
})();
