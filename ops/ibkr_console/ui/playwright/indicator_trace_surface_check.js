const fs = require('fs');
const path = require('path');
const http = require('http');
const { chromium } = require('playwright');

const REMOTE_BASE = process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const STATIC_ROOT = path.resolve(__dirname, '../../../../runtime/ibkr_console/static');
const MAIN_TRACE_ROW_SELECTOR = '#tracePanelShell [data-trace-bar-ms]';
const MAIN_TRACE_ACTIVE_ROW_SELECTOR = '#tracePanelShell [data-trace-bar-ms].active';

const CONTENT_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
};

async function auth() {
  const resp = await fetch(`${REMOTE_BASE}/api/collections/_superusers/auth-with-password`, {
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
  const resp = await fetch(`${REMOTE_BASE}/api/collections/${collection}/records?${query.toString()}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const json = await resp.json();
  if (!resp.ok) throw new Error(`${collection}_query_failed:${JSON.stringify(json)}`);
  return Array.isArray(json.items) ? json.items : [];
}

function deriveDate(item) {
  if (!item) return '';
  if (item.us_time) return String(item.us_time).slice(0, 10);
  if (item.bar_time_ms) return new Date(Number(item.bar_time_ms)).toISOString().slice(0, 10);
  if (item.created) return String(item.created).slice(0, 10);
  return '';
}

async function resolveSeedIndicator(token) {
  const preferred = await pbList(token, 'ibkr_indicators', {
    perPage: 8,
    sort: '-bar_time_ms,-created',
    filter: "environment = 'live' && (interval = '5' || interval = '5m')",
  }).catch(() => []);
  if (preferred.length) return preferred[0];

  const fallback = await pbList(token, 'ibkr_indicators', {
    perPage: 20,
    sort: '-bar_time_ms,-created',
  });
  return fallback.find((item) => item?.id && item?.symbol) || null;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(chunks.length ? Buffer.concat(chunks) : null));
    req.on('error', reject);
  });
}

async function proxyRequest(req, res, proxyState) {
  const body = await readBody(req);
  const headers = { ...req.headers };
  delete headers.host;
  delete headers.connection;
  let requestJson = null;
  if (body && body.length) {
    try {
      requestJson = JSON.parse(body.toString('utf8'));
    } catch (_) {}
  }

  const upstream = await fetch(`${REMOTE_BASE}${req.url}`, {
    method: req.method,
    headers,
    body: body && body.length ? body : undefined,
  });

  const buffer = Buffer.from(await upstream.arrayBuffer());
  if (
    proxyState
    && String(req.url || '').startsWith('/api/custom/ibkr/proxy')
    && requestJson?.action === 'chart/timeline'
  ) {
    const record = {
      at: new Date().toISOString(),
      symbol: requestJson.symbol || '',
      interval: requestJson.interval || '',
      include_trace: Boolean(requestJson.include_trace),
      bars_count: 0,
      indicator_timeline_count: 0,
      trace_timeline_count: 0,
      has_trace_timeline: false,
      response_parsed: false,
    };
    try {
      const payload = JSON.parse(buffer.toString('utf8'));
      record.bars_count = Array.isArray(payload?.bars) ? payload.bars.length : 0;
      record.indicator_timeline_count = Array.isArray(payload?.indicator_timeline) ? payload.indicator_timeline.length : 0;
      record.trace_timeline_count = Array.isArray(payload?.trace_timeline) ? payload.trace_timeline.length : 0;
      record.has_trace_timeline = Array.isArray(payload?.trace_timeline) && payload.trace_timeline.length > 0;
      record.response_parsed = true;
    } catch (_) {}
    proxyState.chartTimelineResponses.push(record);
    if (proxyState.chartTimelineResponses.length > 20) {
      proxyState.chartTimelineResponses.splice(0, proxyState.chartTimelineResponses.length - 20);
    }
  }
  res.statusCode = upstream.status;
  upstream.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (lower === 'connection' || lower === 'content-encoding' || lower === 'content-length' || lower === 'transfer-encoding') return;
    res.setHeader(key, value);
  });
  res.setHeader('content-length', String(buffer.length));
  res.end(buffer);
}

async function serveStatic(req, res, origin) {
  const url = new URL(req.url, origin);
  let pathname = decodeURIComponent(url.pathname || '/');
  if (pathname === '/') pathname = '/ibkr_indicators.html';

  const filePath = path.resolve(STATIC_ROOT, `.${pathname}`);
  if (!filePath.startsWith(STATIC_ROOT)) {
    res.statusCode = 403;
    res.end('forbidden');
    return;
  }

  let sourcePath = filePath;
  try {
    const stat = fs.statSync(sourcePath);
    if (stat.isDirectory()) sourcePath = path.join(sourcePath, 'index.html');
  } catch (_) {
    res.statusCode = 404;
    res.end('not found');
    return;
  }

  try {
    let content = fs.readFileSync(sourcePath);
    if (sourcePath.endsWith(path.join('assets', 'js', 'shared', 'base.js'))) {
      content = Buffer.from(String(content).replace("const BASE_URL = 'https://pb.lzw-glory.top';", `const BASE_URL = '${origin}';`), 'utf8');
    }
    const contentType = CONTENT_TYPES[path.extname(sourcePath).toLowerCase()] || 'application/octet-stream';
    res.statusCode = 200;
    res.setHeader('content-type', contentType);
    res.setHeader('cache-control', 'no-store');
    res.end(content);
  } catch (error) {
    res.statusCode = 500;
    res.end(`static_error:${error.message}`);
  }
}

async function startLocalServer() {
  const proxyState = {
    chartTimelineResponses: [],
  };
  const server = http.createServer(async (req, res) => {
    const origin = `http://${req.headers.host}`;
    try {
      if (String(req.url || '').startsWith('/api/')) {
        await proxyRequest(req, res, proxyState);
        return;
      }
      await serveStatic(req, res, origin);
    } catch (error) {
      res.statusCode = 500;
      res.setHeader('content-type', 'application/json; charset=utf-8');
      res.end(JSON.stringify({ ok: false, error: error.message }));
    }
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  const address = server.address();
  const origin = `http://127.0.0.1:${address.port}`;
  return { server, origin, proxyState };
}

async function closeServer(server) {
  if (!server) return;
  await new Promise((resolve) => server.close(resolve));
}

async function withPage(browser, token, origin, runner) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.addInitScript((savedToken) => localStorage.setItem('pb_token', savedToken), token);
  const page = await context.newPage();
  const errors = [];

  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('response', (resp) => {
    const url = resp.url();
    if (resp.status() < 400) return;
    if (!url.startsWith(origin)) return;
    if (url.endsWith('/favicon.ico')) return;
    errors.push(`response:${resp.status()}:${url}`);
  });

  try {
    const result = await runner(page, errors);
    result.errors = errors;
    return result;
  } finally {
    await context.close();
  }
}

async function waitForIndicatorButtons(page, indicatorId) {
  await page.waitForFunction((id) => {
    return [
      `button[onclick*="openIndicatorChart(event, '${id}')"]`,
      `button[onclick*="toggleIndicatorEvolution(event, '${id}')"]`,
      `button[onclick*="openIndicatorChartPage(event, '${id}')"]`,
    ].every((selector) => !!document.querySelector(selector));
  }, indicatorId, { timeout: 30000 });
}

async function openIndicatorPage(page, origin, seed) {
  const url = new URL('/ibkr_indicators.html', origin);
  const date = deriveDate(seed);
  url.searchParams.set('environment', 'live');
  if (date) url.searchParams.set('date', date);
  url.searchParams.set('search', String(seed.id || ''));
  url.searchParams.set('ts', String(Date.now()));

  await page.goto(url.toString(), { waitUntil: 'networkidle', timeout: 60000 });
  await page.locator('#searchBox').waitFor({ state: 'visible', timeout: 15000 });
  await waitForIndicatorButtons(page, seed.id);
  await page.waitForTimeout(1200);
}

async function switchToTableView(page) {
  const toggle = page.locator('#indicatorViewToggle .view-toggle-btn').nth(1);
  await toggle.click();
  await page.locator('.indicator-table').waitFor({ state: 'visible', timeout: 15000 });
}

async function expandEvolution(page, seed) {
  await page.locator(`button[onclick*="toggleIndicatorEvolution(event, '${seed.id}')"]`).first().click();
  await page.waitForFunction((id) => {
    const root = document.getElementById(`indicatorEvolution-${id}`);
    if (!root) return false;
    return root.querySelectorAll('.evolution-pill').length > 0;
  }, seed.id, { timeout: 30000 });
  return page.locator(`#indicatorEvolution-${seed.id} .evolution-pill`).count().catch(() => 0);
}

async function openQuickview(page, seed) {
  await page.locator(`button[onclick*="openIndicatorChart(event, '${seed.id}')"]`).first().click();
  await page.locator('#indicatorChartModal').waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForFunction(() => {
    const chips = Array.from(document.querySelectorAll('#indicatorChartSummary .chart-summary-chip')).map((node) => String(node.textContent || '').trim());
    const compare = document.querySelector('#indicatorChartCompareGroup .chart-compare-btn');
    const cursorCards = document.querySelectorAll('#indicatorChartCursorStrip .cursor-card').length;
    return chips.includes('computed') && !!compare && cursorCards >= 4;
  }, null, { timeout: 40000 });

  const beforeToggleText = await page.locator('#indicatorChartCompareGroup .chart-compare-btn').innerText().catch(() => '');
  await page.locator('#indicatorChartCompareGroup .chart-compare-btn').click();
  await page.waitForFunction(() => {
    const button = document.querySelector('#indicatorChartCompareGroup .chart-compare-btn');
    return !!button && /On/.test(String(button.textContent || ''));
  }, null, { timeout: 20000 });

  const result = {
    summary_text: await page.locator('#indicatorChartSummary').innerText().catch(() => ''),
    compare_before: beforeToggleText,
    compare_after: await page.locator('#indicatorChartCompareGroup .chart-compare-btn').innerText().catch(() => ''),
    cursor_card_count: await page.locator('#indicatorChartCursorStrip .cursor-card').count().catch(() => 0),
    focus_text: await page.locator('#indicatorChartFocusGroup').innerText().catch(() => ''),
  };

  await page.locator('.chart-modal-close').click();
  await page.waitForFunction(() => {
    const modal = document.getElementById('indicatorChartModal');
    return !!modal && !modal.classList.contains('show');
  }, null, { timeout: 15000 });
  return result;
}

async function openTracePage(page, seed) {
  await page.locator(`button[onclick*="openIndicatorChartPage(event, '${seed.id}')"]`).first().click();
  await page.waitForURL(/\/ibkr_chart\.html.*trace=1/, { timeout: 20000 });
  await page.waitForFunction(() => {
    const shell = document.getElementById('tracePanelShell');
    return !!shell && !shell.classList.contains('hidden') && !!document.querySelector('#tracePanelShell .trace-panel-title');
  }, null, { timeout: 30000 });
  await page.waitForTimeout(2000);
}

async function waitForTraceRows(page, timeoutMs = 15000) {
  await page.waitForFunction((selector) => document.querySelectorAll(selector).length > 0, MAIN_TRACE_ROW_SELECTOR, {
    timeout: timeoutMs,
  });
}

async function focusAnotherTraceRow(page) {
  const rows = page.locator(MAIN_TRACE_ROW_SELECTOR);
  const count = await rows.count();
  const activeBefore = await page.locator(MAIN_TRACE_ACTIVE_ROW_SELECTOR).first().getAttribute('data-trace-bar-ms').catch(() => '');
  let targetBarMs = '';

  for (let index = 0; index < count; index += 1) {
    const candidate = await rows.nth(index).getAttribute('data-trace-bar-ms').catch(() => '');
    if (candidate && candidate !== activeBefore) {
      targetBarMs = candidate;
      break;
    }
  }

  if (!targetBarMs) {
    return { active_before: activeBefore, active_after: activeBefore, clicked: false };
  }

  const target = page.locator(`${MAIN_TRACE_ROW_SELECTOR}[data-trace-bar-ms="${targetBarMs}"]`).first();
  await target.scrollIntoViewIfNeeded().catch(() => {});
  await target.click();
  await page.waitForFunction(({ activeSelector, barMs }) => {
    const node = document.querySelector(`${activeSelector}[data-trace-bar-ms="${barMs}"]`);
    return !!node && node.classList.contains('active');
  }, { activeSelector: MAIN_TRACE_ROW_SELECTOR, barMs: targetBarMs }, { timeout: 15000 });

  return {
    active_before: activeBefore,
    active_after: targetBarMs,
    clicked: true,
  };
}

function summarizeTraceBackend(proxyState) {
  const responses = Array.isArray(proxyState?.chartTimelineResponses) ? proxyState.chartTimelineResponses.slice(-4) : [];
  const latest = responses[responses.length - 1] || null;
  return {
    ready: responses.some((item) => item?.has_trace_timeline),
    latest,
    responses,
  };
}

async function inspectIndicatorTraceFlow(page, origin, seed, proxyState) {
  await openIndicatorPage(page, origin, seed);

  const result = {
    seed: {
      id: seed.id,
      symbol: seed.symbol,
      interval: seed.interval,
      us_time: seed.us_time,
      bar_time_ms: seed.bar_time_ms,
      date: deriveDate(seed),
    },
    page_title: await page.title(),
    view_toggle_count: await page.locator('#indicatorViewToggle .view-toggle-btn').count().catch(() => 0),
    sort_selector_visible: await page.locator('#sortSelector').isVisible().catch(() => false),
    initial_card_count: await page.locator('.indicator-card').count().catch(() => 0),
  };

  await switchToTableView(page);
  result.table_row_count = await page.locator('.indicator-table tbody tr').count().catch(() => 0);
  result.evolution_count = await expandEvolution(page, seed);
  result.quickview = await openQuickview(page, seed);

  await openTracePage(page, seed);
  const traceBackend = summarizeTraceBackend(proxyState);
  let traceRowCount = await page.locator(MAIN_TRACE_ROW_SELECTOR).count().catch(() => 0);
  if (traceBackend.ready && traceRowCount < 1) {
    await waitForTraceRows(page, 20000).catch(() => {});
    traceRowCount = await page.locator(MAIN_TRACE_ROW_SELECTOR).count().catch(() => 0);
  }
  const focusShift = traceRowCount > 0 ? await focusAnotherTraceRow(page) : { active_before: '', active_after: '', clicked: false };
  result.trace = {
    final_url: page.url(),
    shell_visible: await page.locator('#tracePanelShell').isVisible().catch(() => false),
    row_count: traceRowCount,
    summary_text: await page.locator('#tracePanelShell .trace-panel-summary').innerText().catch(() => ''),
    title_text: await page.locator('#tracePanelShell .trace-panel-title').innerText().catch(() => ''),
    backend: traceBackend,
    focus_shift: focusShift,
  };

  const baseChecksOk = Boolean(
    result.view_toggle_count >= 2
      && result.sort_selector_visible
      && result.table_row_count >= 1
      && result.evolution_count >= 1
      && /computed/.test(result.quickview.summary_text)
      && /On/.test(result.quickview.compare_after)
      && /trace=1/.test(result.trace.final_url)
      && result.trace.shell_visible
  );
  const traceRowsReady = Boolean(
    result.trace.row_count >= 1
      && result.trace.focus_shift.clicked
      && result.trace.focus_shift.active_after
      && result.trace.focus_shift.active_after !== result.trace.focus_shift.active_before
  );
  result.trace.status = traceRowsReady
    ? 'ready'
    : result.trace.backend.ready
    ? 'ui_missing_rows'
    : 'upstream_missing_trace_timeline';
  result.partial = !traceRowsReady;
  result.ok = Boolean(
    baseChecksOk
      && (traceRowsReady || !result.trace.backend.ready)
  );
  return result;
}

async function runIndicatorTraceSurfaceCheck() {
  const token = await auth();
  const seed = await resolveSeedIndicator(token);
  if (!seed) throw new Error('no_indicator_seed_found');

  const { server, origin, proxyState } = await startLocalServer();
  const browser = await chromium.launch({ headless: true });
  let result;
  try {
    result = await withPage(browser, token, origin, (page) => inspectIndicatorTraceFlow(page, origin, seed, proxyState));
  } finally {
    await browser.close().catch(() => {});
    await closeServer(server).catch(() => {});
  }

  const output = {
    ok: Boolean(result?.ok) && (!Array.isArray(result?.errors) || result.errors.length === 0),
    checked_at: new Date().toISOString(),
    origin,
    result,
  };

  return output;
}

module.exports = {
  runIndicatorTraceSurfaceCheck,
};

if (require.main === module) {
  runIndicatorTraceSurfaceCheck()
    .then((output) => {
      console.log(JSON.stringify(output, null, 2));
      if (!output.ok) process.exitCode = 1;
    })
    .catch((error) => {
      console.error(JSON.stringify({
        ok: false,
        error: error?.message || String(error),
        checked_at: new Date().toISOString(),
      }, null, 2));
      process.exit(1);
    });
}
