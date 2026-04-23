const { chromium, request } = require('playwright');

const DEFAULT_EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const DEFAULT_PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const DEFAULT_CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const DEFAULT_PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const NAV_TIMEOUT_MS = Number(process.env.PB_SMOKE_NAV_TIMEOUT_MS || 20000);
const READY_TIMEOUT_MS = Number(process.env.PB_SCREENER_PERF_READY_TIMEOUT_MS || 30000);
const POST_READY_SETTLE_MS = Number(process.env.PB_SCREENER_PERF_SETTLE_MS || 800);

const DEFAULT_TARGETS = [
  `${DEFAULT_CONSOLE_BASE}/ibkr_screener.html?environment=live&tab=screener&view=current`,
  `${DEFAULT_CONSOLE_BASE}/ibkr_screener.html?environment=live&tab=screener&view=universe`,
];

function parseArgs(argv) {
  const opts = {
    headless: true,
    targets: [],
  };
  for (let index = 2; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--headed') {
      opts.headless = false;
      continue;
    }
    if (arg.startsWith('--target=')) {
      opts.targets.push(arg.slice('--target='.length));
      continue;
    }
    if (arg === '--target' && argv[index + 1]) {
      opts.targets.push(argv[index + 1]);
      index += 1;
    }
  }
  if (!opts.targets.length) opts.targets = DEFAULT_TARGETS.slice();
  return opts;
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
          timeout: NAV_TIMEOUT_MS,
        });
        const payload = await resp.json().catch(() => ({}));
        if (!resp.ok || !payload?.token) {
          lastError = new Error(`auth_failed:${resp.status()}:${JSON.stringify(payload)}`);
        } else {
          return payload.token;
        }
      } catch (error) {
        lastError = error;
      }
      await new Promise((resolve) => setTimeout(resolve, attempt * 500));
    }
    throw lastError || new Error('auth_failed:unknown');
  } finally {
    await api.dispose();
  }
}

async function createContext(browser, token) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    ignoreHTTPSErrors: true,
  });
  await context.addInitScript((savedToken) => {
    localStorage.setItem('pb_token', savedToken);
  }, token);
  return context;
}

function getRouteKey(rawUrl) {
  try {
    const path = new URL(rawUrl).pathname || '';
    if (path.endsWith('/api/custom/ibkr/rules')) return 'rules';
    if (path.endsWith('/api/custom/ibkr/today-targets')) return 'today-targets';
    if (path.endsWith('/api/custom/ibkr/screener')) return 'screener';
    if (path.endsWith('/api/custom/ibkr/quotes')) return 'quotes';
    return '';
  } catch (_) {
    return '';
  }
}

async function waitForReady(page, view) {
  if (view === 'universe') {
    await page.waitForFunction(() => {
      const meta = document.getElementById('tableMeta')?.textContent || '';
      const table = document.getElementById('screenerTable')?.textContent || '';
      const rules = document.getElementById('rulesBoard')?.textContent || '';
      return (
        meta &&
        !meta.includes('等待加载') &&
        !meta.includes('加载中') &&
        table &&
        !table.includes('加载中') &&
        rules &&
        !rules.includes('规则摘要加载中')
      );
    }, { timeout: READY_TIMEOUT_MS });
    return;
  }

  await page.waitForFunction(() => {
    const meta = document.getElementById('currentTargetsMeta')?.textContent || '';
    const table = document.getElementById('currentTargetsTable')?.textContent || '';
    const rules = document.getElementById('rulesBoard')?.textContent || '';
    return (
      meta &&
      !meta.includes('等待加载') &&
      !meta.includes('正在加载') &&
      table &&
      !table.includes('加载中') &&
      rules &&
      !rules.includes('规则摘要加载中')
    );
  }, { timeout: READY_TIMEOUT_MS });
}

function summarizeApiCalls(apiCalls) {
  const summary = {};
  apiCalls.forEach((call) => {
    const bucket = summary[call.route] || {
      count: 0,
      statuses: {},
      durations_ms: [],
      max_ms: 0,
      min_ms: Number.POSITIVE_INFINITY,
    };
    bucket.count += 1;
    bucket.statuses[String(call.status)] = (bucket.statuses[String(call.status)] || 0) + 1;
    bucket.durations_ms.push(call.duration_ms);
    bucket.max_ms = Math.max(bucket.max_ms, call.duration_ms);
    bucket.min_ms = Math.min(bucket.min_ms, call.duration_ms);
    summary[call.route] = bucket;
  });

  Object.values(summary).forEach((bucket) => {
    const total = bucket.durations_ms.reduce((sum, value) => sum + value, 0);
    bucket.avg_ms = bucket.durations_ms.length ? Math.round((total / bucket.durations_ms.length) * 10) / 10 : 0;
    bucket.min_ms = Number.isFinite(bucket.min_ms) ? bucket.min_ms : 0;
    delete bucket.durations_ms;
  });
  return summary;
}

async function inspectTarget(browser, token, targetUrl) {
  const parsed = new URL(targetUrl);
  const view = String(parsed.searchParams.get('view') || 'current').trim().toLowerCase() === 'universe'
    ? 'universe'
    : 'current';
  const context = await createContext(browser, token);
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(NAV_TIMEOUT_MS);
  page.setDefaultTimeout(READY_TIMEOUT_MS);

  const requestStartMs = new Map();
  const apiCalls = [];
  const consoleErrors = [];

  page.on('request', (req) => {
    const route = getRouteKey(req.url());
    if (!route) return;
    requestStartMs.set(req, { route, startedAt: Date.now(), method: req.method() });
  });
  page.on('response', (resp) => {
    const req = resp.request();
    const meta = requestStartMs.get(req);
    if (!meta) return;
    apiCalls.push({
      route: meta.route,
      method: meta.method,
      status: resp.status(),
      url: resp.url(),
      duration_ms: Math.max(0, Date.now() - meta.startedAt),
    });
    requestStartMs.delete(req);
  });
  page.on('requestfailed', (req) => {
    const meta = requestStartMs.get(req);
    if (!meta) return;
    apiCalls.push({
      route: meta.route,
      method: meta.method,
      status: 'failed',
      url: req.url(),
      duration_ms: Math.max(0, Date.now() - meta.startedAt),
      error: req.failure()?.errorText || 'unknown',
    });
    requestStartMs.delete(req);
  });
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) {
      consoleErrors.push(`${msg.type()}:${msg.text()}`);
    }
  });

  const gotoStartedAt = Date.now();
  await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
  const domContentLoadedMs = Date.now() - gotoStartedAt;
  await waitForReady(page, view);
  const readyMs = Date.now() - gotoStartedAt;
  await page.waitForTimeout(POST_READY_SETTLE_MS);

  const navTiming = await page.evaluate(() => {
    const entry = performance.getEntriesByType('navigation')[0];
    if (!entry) return {};
    return {
      type: entry.type || '',
      redirect_count: entry.redirectCount || 0,
      dns_ms: Math.round((entry.domainLookupEnd - entry.domainLookupStart) * 10) / 10,
      connect_ms: Math.round((entry.connectEnd - entry.connectStart) * 10) / 10,
      ttfb_ms: Math.round((entry.responseStart - entry.requestStart) * 10) / 10,
      response_ms: Math.round((entry.responseEnd - entry.responseStart) * 10) / 10,
      dom_content_loaded_ms: Math.round(entry.domContentLoadedEventEnd * 10) / 10,
      load_ms: Math.round(entry.loadEventEnd * 10) / 10,
    };
  });

  const pageState = await page.evaluate(() => ({
    market_date: document.getElementById('marketDate')?.value || '',
    current_meta: document.getElementById('currentTargetsMeta')?.textContent || '',
    universe_meta: document.getElementById('tableMeta')?.textContent || '',
    refresh_info: document.getElementById('refreshInfo')?.textContent || '',
  }));

  await context.close();
  return {
    target_url: targetUrl,
    final_url: page.url(),
    view,
    domcontentloaded_wall_ms: domContentLoadedMs,
    ready_wall_ms: readyMs,
    navigation_timing: navTiming,
    page_state: pageState,
    api_calls: apiCalls,
    api_summary: summarizeApiCalls(apiCalls),
    console_errors: consoleErrors,
  };
}

(async () => {
  const opts = parseArgs(process.argv);
  const token = await fetchToken();
  const browser = await chromium.launch({ headless: opts.headless });
  const results = [];
  try {
    for (const target of opts.targets) {
      results.push(await inspectTarget(browser, token, target));
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(results, null, 2));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
