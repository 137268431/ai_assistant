const { chromium } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const COMPUTE_BASE = process.env.IBKR_COMPUTE_BASE || 'http://127.0.0.1:5100';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const ENVIRONMENT = 'live';
const TARGET_CANDIDATES = ['KO', 'PEP', 'GIS', 'KMB', 'CL', 'HSY', 'SYY', 'ADM', 'KHC', 'CPB'];
const WATCHLIST_CANDIDATES = ['ADP', 'PAYX', 'CHD', 'HRL', 'MKC', 'SJM', 'EL', 'CLX', 'TAP', 'CAG'];

function shouldIgnoreRequestFailure(req) {
  const errorText = req.failure()?.errorText || '';
  const url = req.url() || '';
  if (url.includes('fonts.googleapis.com')) return true;
  if (url.includes('fonts.gstatic.com')) return true;
  return errorText === 'net::ERR_ABORTED';
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function escapeRegex(text) {
  return String(text || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function escapeFilterValue(text) {
  return String(text || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

async function login(page, targetUrl) {
  const auth = await fetchJson(`${PB_BASE}/api/collections/_superusers/auth-with-password`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      identity: EMAIL,
      password: PASSWORD,
    }),
  });
  const token = String(auth?.token || '').trim();
  if (!token) {
    throw new Error('PocketBase auth token missing');
  }

  await page.goto(`${CONSOLE_BASE}/login.html`, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });
  await page.evaluate((nextToken) => {
    localStorage.setItem('pb_token', nextToken);
  }, token);
  await page.goto(targetUrl, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });
  return token;
}

async function fetchJson(url, options = {}) {
  let lastError = null;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      const response = await fetch(url, options);
      const text = await response.text();
      let payload = {};
      try {
        payload = text ? JSON.parse(text) : {};
      } catch (_) {
        payload = { ok: false, raw: text };
      }
      if (!response.ok) {
        const error = new Error(payload.message || payload.error || `HTTP ${response.status} ${url}`);
        if (response.status >= 500 && attempt < 5) {
          lastError = error;
          await delay(1000 * attempt);
          continue;
        }
        throw error;
      }
      return payload;
    } catch (error) {
      lastError = error;
      if (attempt >= 5) break;
      await delay(1000 * attempt);
    }
  }
  throw lastError || new Error(`fetch failed: ${url}`);
}

async function pbList(token, collection, filter, extraParams = {}) {
  const params = new URLSearchParams({
    page: '1',
    perPage: '1',
    fields: 'id,symbol,environment,manual_member,status,date',
    ...extraParams,
  });
  if (filter) params.set('filter', filter);
  const url = `${PB_BASE}/api/collections/${collection}/records?${params.toString()}`;
  return fetchJson(url, {
    headers: {
      Authorization: `Bearer ${token}`,
    },
  });
}

async function pbCount(token, collection, filter) {
  const payload = await pbList(token, collection, filter);
  return Number(payload.totalItems || 0);
}

async function pbRecords(token, collection, filter, perPage = 50) {
  const payload = await pbList(token, collection, filter, {
    perPage: String(perPage),
    fields: 'id,symbol,environment,manual_member,status,date',
  });
  return Array.isArray(payload.items) ? payload.items : [];
}

async function postCustom(token, route, body) {
  return fetchJson(`${CONSOLE_BASE}${route}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
}

async function getRuntimeStatus() {
  return fetchJson(`${COMPUTE_BASE}/ibkr/status?environment=${ENVIRONMENT}`);
}

function runtimeHasSymbol(status, symbol) {
  const marketUniverse = status?.market_universe || {};
  const subscriptions = Array.isArray(marketUniverse.active_subscription_symbols)
    ? marketUniverse.active_subscription_symbols
    : [];
  const dataSymbols = Array.isArray(marketUniverse.data_symbols)
    ? marketUniverse.data_symbols
    : [];
  return subscriptions.includes(symbol) || dataSymbols.includes(symbol);
}

async function getSymbolSnapshot(token, symbol, marketDate) {
  const escapedSymbol = escapeFilterValue(symbol);
  const escapedEnvironment = escapeFilterValue(ENVIRONMENT);
  const escapedDate = escapeFilterValue(marketDate);
  const filters = {
    watchlist: `symbol = "${escapedSymbol}" && (environment = "${escapedEnvironment}" || environment = "global" || environment = "")`,
    targets: `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}" && date = "${escapedDate}"`,
    bars: `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}"`,
    indicators: `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}"`,
    signals: `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}"`,
    orders: `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}"`,
  };
  const [watchlist, targets, bars, indicators, signals, orders, runtimeStatus] = await Promise.all([
    pbCount(token, 'watchlist', filters.watchlist),
    pbCount(token, 'ibkr_targets', filters.targets),
    pbCount(token, 'ibkr_bars', filters.bars),
    pbCount(token, 'ibkr_indicators', filters.indicators),
    pbCount(token, 'ibkr_signals', filters.signals),
    pbCount(token, 'orders', filters.orders),
    getRuntimeStatus(),
  ]);
  return {
    symbol,
    marketDate,
    watchlist,
    targets,
    bars,
    indicators,
    signals,
    orders,
    runtime_present: runtimeHasSymbol(runtimeStatus, symbol),
  };
}

async function pickCleanSymbol(token, candidates, marketDate, exclude = new Set()) {
  const inspected = [];
  for (const symbol of candidates) {
    if (exclude.has(symbol)) continue;
    const snapshot = await getSymbolSnapshot(token, symbol, marketDate);
    inspected.push(snapshot);
    const clean = !snapshot.watchlist
      && !snapshot.targets
      && !snapshot.bars
      && !snapshot.indicators
      && !snapshot.signals
      && !snapshot.orders
      && !snapshot.runtime_present;
    if (clean) {
      return { selected: snapshot, inspected };
    }
  }
  throw new Error(`No clean symbol available. inspected=${JSON.stringify(inspected)}`);
}

async function waitForCondition(label, fn, timeoutMs = 120000, intervalMs = 3000) {
  const started = Date.now();
  let last = null;
  while ((Date.now() - started) < timeoutMs) {
    last = await fn();
    if (last && last.ok) {
      return last;
    }
    await delay(intervalMs);
  }
  throw new Error(`${label} timed out. last=${JSON.stringify(last)}`);
}

async function waitForTargetsPage(page) {
  await page.goto(`${CONSOLE_BASE}/ibkr_screener.html?environment=${ENVIRONMENT}&tab=targets`, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });
  await page.waitForSelector('#targetsTab.active #dailyTargetsTable', { timeout: 30000 });
  await page.waitForFunction(() => {
    const text = document.getElementById('dailyTargetListMeta')?.textContent || '';
    return text && !text.includes('尚未加载');
  }, { timeout: 30000 });
}

async function waitForWatchlistPage(page) {
  await page.locator('#pageBridge .domain-tab[data-tab="watchlist"]').click();
  await page.waitForSelector('#watchlistTab.active #watchlistTable', { timeout: 30000 });
}

async function refreshTargets(page) {
  await page.locator('#dailyTargetRefreshBtn').click();
  await page.waitForTimeout(1500);
}

async function refreshWatchlist(page) {
  await page.locator('#refreshListBtn').click();
  await page.waitForTimeout(1500);
}

function targetSearchCard(page, symbol) {
  return page.locator('#dailyTargetSearchResults .result-card').filter({
    has: page.locator('.symbol-chip', { hasText: new RegExp(`^${escapeRegex(symbol)}$`) }),
  }).first();
}

function watchlistSearchCard(page, symbol) {
  return page.locator('#searchResults .result-card').filter({
    has: page.locator('.symbol-chip', { hasText: new RegExp(`^${escapeRegex(symbol)}$`) }),
  }).first();
}

async function searchTargetSymbol(page, symbol) {
  await page.locator('#dailyTargetSearchInput').fill(symbol);
  await page.locator('#dailyTargetSearchBtn').click();
  const card = targetSearchCard(page, symbol);
  await card.waitFor({ state: 'visible', timeout: 30000 });
  return card;
}

async function searchWatchlistSymbol(page, symbol) {
  await page.locator('#searchInput').fill(symbol);
  await page.locator('#searchBtn').click();
  const card = watchlistSearchCard(page, symbol);
  await card.waitFor({ state: 'visible', timeout: 30000 });
  return card;
}

async function setTargetDate(page, dateValue) {
  await page.locator('#dailyTargetDate').evaluate((el, value) => {
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, dateValue);
  await page.waitForTimeout(600);
}

async function waitForJsonResponse(page, urlPart, action) {
  const responsePromise = page.waitForResponse(
    (resp) => resp.url().includes(urlPart) && resp.request().method() === 'POST',
    { timeout: 120000 }
  );
  await action();
  const response = await responsePromise;
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (_) {
    payload = { ok: false, raw: text };
  }
  if (!response.ok()) {
    throw new Error(`${urlPart} failed: ${text}`);
  }
  return payload;
}

async function getTargetRow(page, symbol) {
  await page.locator('#dailyTargetTableSearchInput').fill(symbol);
  await page.waitForTimeout(500);
  const row = page.locator('#dailyTargetsTable tr').filter({ hasText: symbol }).first();
  return row;
}

async function getWatchlistRow(page, symbol) {
  await page.locator('#listSearchInput').fill(symbol);
  await page.waitForTimeout(500);
  const row = page.locator('#watchlistTable tr').filter({ hasText: symbol }).first();
  return row;
}

async function bestEffortCleanup(token, marketDate, symbol) {
  if (!token || !symbol) return;
  const escapedSymbol = escapeFilterValue(symbol);
  const escapedEnvironment = escapeFilterValue(ENVIRONMENT);
  const escapedDate = escapeFilterValue(marketDate);
  const targetRecords = await pbRecords(
    token,
    'ibkr_targets',
    `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}" && date = "${escapedDate}"`,
    20
  );
  for (const item of targetRecords) {
    await postCustom(token, '/api/custom/ibkr/targets/remove', {
      environment: ENVIRONMENT,
      source: 'ops_cleanup',
      record_id: item.id,
      symbol,
    }).catch(() => null);
  }
  const watchlistRecords = await pbRecords(
    token,
    'watchlist',
    `symbol = "${escapedSymbol}" && environment = "${escapedEnvironment}"`,
    20
  );
  for (const item of watchlistRecords) {
    await postCustom(token, '/api/custom/ibkr/watchlist/remove', {
      environment: ENVIRONMENT,
      source: 'ops_cleanup',
      record_id: item.id,
      symbol,
      remove_current_day_targets: false,
    }).catch(() => null);
  }
}

function nextUsDate(dateString) {
  const date = new Date(`${dateString}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({});
  const page = await context.newPage();
  const result = {
    environment: ENVIRONMENT,
    health: {},
    symbols: {},
    validations: {},
    responses: {},
    snapshots: {},
    inspected_candidates: {},
    warnings: [],
    errors: [],
  };

  page.on('dialog', (dialog) => dialog.accept());
  page.on('pageerror', (err) => result.errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    const text = msg.text();
    if (text.includes('fonts.googleapis.com') || text.includes('fonts.gstatic.com')) {
      return;
    }
    if (msg.type() === 'error') {
      result.errors.push(`console:${msg.type()}:${text}`);
    } else if (msg.type() === 'warning') {
      result.warnings.push(`console:${msg.type()}:${text}`);
    }
  });
  page.on('requestfailed', (req) => {
    if (shouldIgnoreRequestFailure(req)) return;
    result.errors.push(`requestfailed:${req.failure()?.errorText || 'unknown'}:${req.url()}`);
  });

  let token = '';
  let marketDate = '';
  let targetSymbol = '';
  let manualWatchlistSymbol = '';

  try {
    const runtimeStatus = await getRuntimeStatus();
    result.health.runtime_phase = runtimeStatus.runtime_phase || '';
    result.health.market_date = runtimeStatus?.market_universe?.market_date || '';
    result.health.subscribed_count = runtimeStatus?.websocket?.subscribed_count || 0;
    marketDate = result.health.market_date;
    if (!marketDate) {
      throw new Error('Missing market date from runtime status');
    }

    token = await login(page, `${CONSOLE_BASE}/ibkr_screener.html?environment=${ENVIRONMENT}&tab=targets`);
    await waitForTargetsPage(page);
    if (!token) {
      throw new Error('PocketBase token missing after login');
    }

    const targetPick = await pickCleanSymbol(token, TARGET_CANDIDATES, marketDate);
    result.inspected_candidates.target = targetPick.inspected;
    targetSymbol = targetPick.selected.symbol;
    const exclude = new Set([targetSymbol]);
    const watchlistPick = await pickCleanSymbol(token, WATCHLIST_CANDIDATES, marketDate, exclude);
    result.inspected_candidates.watchlist = watchlistPick.inspected;
    manualWatchlistSymbol = watchlistPick.selected.symbol;
    result.symbols.target = targetSymbol;
    result.symbols.manual_watchlist = manualWatchlistSymbol;

    result.snapshots.target_before = await getSymbolSnapshot(token, targetSymbol, marketDate);
    result.snapshots.manual_watchlist_before = await getSymbolSnapshot(token, manualWatchlistSymbol, marketDate);

    const blockedDate = nextUsDate(marketDate);
    await setTargetDate(page, blockedDate);
    let targetCard = await searchTargetSymbol(page, targetSymbol);
    const blockedButton = targetCard.locator('button');
    result.validations.manual_target_future_date = {
      blocked_date: blockedDate,
      input_value: await page.locator('#dailyTargetDate').inputValue(),
      draft_date: await targetCard.locator('.status-chip').nth(1).innerText().catch(() => ''),
      button_text: await blockedButton.innerText(),
      disabled: await blockedButton.isDisabled(),
    };
    if (!result.validations.manual_target_future_date.disabled) {
      throw new Error('Future-date manual target add was not blocked');
    }

    await setTargetDate(page, marketDate);
    await page.locator('#dailyTargetStatusInput').selectOption('candidate');
    await page.locator('#dailyTargetDirectionInput').selectOption('neutral');
    await page.locator('#dailyTargetScoreInput').fill('0');
    await page.locator('#dailyTargetReasonInput').fill(`ops walkthrough ${Date.now()}`);
    targetCard = await searchTargetSymbol(page, targetSymbol);
    result.responses.target_add = await waitForJsonResponse(page, '/api/custom/ibkr/targets/upsert', async () => {
      await targetCard.locator('button:has-text("加入目标池")').click();
    });
    await refreshTargets(page);
    const targetRow = await getTargetRow(page, targetSymbol);
    await targetRow.waitFor({ state: 'visible', timeout: 30000 });

    await waitForWatchlistPage(page);
    await refreshWatchlist(page);
    const targetWatchlistRow = await getWatchlistRow(page, targetSymbol);
    await targetWatchlistRow.waitFor({ state: 'visible', timeout: 30000 });
    result.validations.target_watchlist_member = await targetWatchlistRow.locator('td').nth(5).innerText();
    if (!String(result.validations.target_watchlist_member || '').includes('AUTO(TARGET)')) {
      throw new Error(`Expected AUTO(TARGET) watchlist row for ${targetSymbol}`);
    }

    result.snapshots.target_after_add = await waitForCondition(`target add snapshot ${targetSymbol}`, async () => {
      const snapshot = await getSymbolSnapshot(token, targetSymbol, marketDate);
      const ok = snapshot.watchlist === 1
        && snapshot.targets === 1
        && snapshot.bars > 0
        && snapshot.indicators > 0
        && snapshot.runtime_present;
      return { ok, snapshot };
    }).then((x) => x.snapshot);

    await page.locator('#pageBridge .domain-tab[data-tab="targets"]').click();
    await refreshTargets(page);
    const deleteTargetRow = await getTargetRow(page, targetSymbol);
    await deleteTargetRow.waitFor({ state: 'visible', timeout: 30000 });
    result.responses.target_remove = await waitForJsonResponse(page, '/api/custom/ibkr/targets/remove', async () => {
      await deleteTargetRow.locator('button:has-text("删除")').click();
    });
    await refreshTargets(page);
    await waitForWatchlistPage(page);
    await refreshWatchlist(page);

    result.snapshots.target_after_remove = await waitForCondition(`target remove snapshot ${targetSymbol}`, async () => {
      const snapshot = await getSymbolSnapshot(token, targetSymbol, marketDate);
      const ok = snapshot.watchlist === 0
        && snapshot.targets === 0
        && snapshot.bars === 0
        && snapshot.indicators === 0
        && snapshot.signals === 0
        && snapshot.orders === 0
        && !snapshot.runtime_present;
      return { ok, snapshot };
    }).then((x) => x.snapshot);

    await page.locator('#listSearchInput').fill('');
    await page.locator('#noteInput').fill(`ops manual walkthrough ${Date.now()}`);
    const manualCard = await searchWatchlistSymbol(page, manualWatchlistSymbol);
    result.responses.watchlist_add = await waitForJsonResponse(page, '/api/custom/ibkr/watchlist/upsert', async () => {
      await manualCard.locator('button:has-text("加入")').click();
    });
    await refreshWatchlist(page);
    const manualRow = await getWatchlistRow(page, manualWatchlistSymbol);
    await manualRow.waitFor({ state: 'visible', timeout: 30000 });
    result.validations.manual_watchlist_member = await manualRow.locator('td').nth(5).innerText();
    if (!String(result.validations.manual_watchlist_member || '').includes('MANUAL')) {
      throw new Error(`Expected MANUAL watchlist row for ${manualWatchlistSymbol}`);
    }

    result.snapshots.manual_watchlist_after_add = await waitForCondition(`watchlist add snapshot ${manualWatchlistSymbol}`, async () => {
      const snapshot = await getSymbolSnapshot(token, manualWatchlistSymbol, marketDate);
      const ok = snapshot.watchlist === 1
        && snapshot.targets === 0
        && snapshot.bars > 0
        && snapshot.indicators > 0
        && snapshot.runtime_present;
      return { ok, snapshot };
    }).then((x) => x.snapshot);

    const deleteManualRow = await getWatchlistRow(page, manualWatchlistSymbol);
    result.responses.watchlist_remove = await waitForJsonResponse(page, '/api/custom/ibkr/watchlist/remove', async () => {
      await deleteManualRow.locator('button:has-text("删除")').click();
    });
    await refreshWatchlist(page);
    result.snapshots.manual_watchlist_after_remove = await waitForCondition(`watchlist remove snapshot ${manualWatchlistSymbol}`, async () => {
      const snapshot = await getSymbolSnapshot(token, manualWatchlistSymbol, marketDate);
      const ok = snapshot.watchlist === 0
        && snapshot.targets === 0
        && snapshot.bars === 0
        && snapshot.indicators === 0
        && snapshot.signals === 0
        && snapshot.orders === 0
        && !snapshot.runtime_present;
      return { ok, snapshot };
    }).then((x) => x.snapshot);

    result.ok = result.errors.length === 0;
    console.log(JSON.stringify(result, null, 2));
  } catch (err) {
    result.ok = false;
    result.fatal = err.message || String(err);
    console.log(JSON.stringify(result, null, 2));
    throw err;
  } finally {
    await bestEffortCleanup(token, marketDate, targetSymbol).catch(() => null);
    await bestEffortCleanup(token, marketDate, manualWatchlistSymbol).catch(() => null);
    await context.close().catch(() => null);
    await browser.close().catch(() => null);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
