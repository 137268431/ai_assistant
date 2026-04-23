const { chromium, devices } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';

async function auth() {
  const resp = await fetch(`${PB_BASE}/api/collections/_superusers/auth-with-password`, {
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
  const resp = await fetch(`${PB_BASE}/api/collections/${collection}/records?${query.toString()}`, {
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

function buildContext(browser, mobile) {
  return mobile
    ? browser.newContext({ ...devices['iPhone 12'] })
    : browser.newContext({ viewport: { width: 1440, height: 960 } });
}

async function withPage(browser, token, mobile, runner) {
  const context = await buildContext(browser, mobile);
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

  try {
    const result = await runner(page, errors);
    result.errors = errors;
    result.device = mobile ? 'iPhone 12' : 'desktop';
    return result;
  } finally {
    await context.close();
  }
}

async function readCursorStrip(page) {
  const values = await page.locator('#indicatorChartCursorStrip .cursor-card .cursor-value').allInnerTexts().catch(() => []);
  return values.map((item) => item.replace(/\s+/g, ' ').trim()).join(' | ');
}

async function readTooltipText(page) {
  return page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('body div'));
    const candidate = nodes.find((node) => {
      const text = String(node.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text || !text.includes('EMA20') || !text.includes('CRSI')) return false;
      const style = window.getComputedStyle(node);
      return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
    });
    return candidate ? String(candidate.textContent || '').replace(/\s+/g, ' ').trim() : '';
  });
}

async function waitForQuickviewReady(page) {
  await page.locator('#indicatorChartModal').waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForFunction(() => {
    const cursorCards = document.querySelectorAll('#indicatorChartCursorStrip .cursor-card').length;
    const canvas = document.querySelector('#indicatorChartCanvas canvas');
    const dom = document.getElementById('indicatorChartCanvas');
    const chart = window.echarts && window.echarts.getInstanceByDom(dom);
    return cursorCards === 4 && !!canvas && !!chart;
  }, null, { timeout: 30000 });
  await page.waitForTimeout(800);
}

async function getChartState(page) {
  return page.evaluate(() => {
    const dom = document.getElementById('indicatorChartCanvas');
    const chart = window.echarts && window.echarts.getInstanceByDom(dom);
    if (!chart) return null;
    const option = chart.getOption();
    const series = Array.isArray(option?.series) ? option.series : [];
    const priceSeries = series.find((item) => item?.name === 'Price');
    const priceData = Array.isArray(priceSeries?.data) ? priceSeries.data : [];
    return {
      has_chart_instance: true,
      series_count: series.length,
      bar_count: priceData.length,
      series_names: series.map((item) => String(item?.name || '')),
    };
  });
}

async function getProbePoint(page, ratio) {
  const info = await page.evaluate((targetRatio) => {
    const dom = document.getElementById('indicatorChartCanvas');
    const chart = window.echarts && window.echarts.getInstanceByDom(dom);
    if (!chart) return null;
    const option = chart.getOption();
    const series = Array.isArray(option?.series) ? option.series : [];
    const priceSeries = series.find((item) => item?.name === 'Price');
    const priceData = Array.isArray(priceSeries?.data) ? priceSeries.data : [];
    const barCount = priceData.length;
    if (!barCount) return null;
    const zoomList = Array.isArray(option?.dataZoom) ? option.dataZoom : [];
    const primaryZoom = zoomList[0] || {};
    const startPct = Math.max(0, Math.min(100, Number(primaryZoom.start ?? 0)));
    const endPct = Math.max(startPct, Math.min(100, Number(primaryZoom.end ?? 100)));
    const maxIndex = Math.max(0, barCount - 1);
    const startIndex = Math.min(maxIndex, Math.max(0, Math.floor(maxIndex * (startPct / 100))));
    const endIndex = Math.min(maxIndex, Math.max(startIndex, Math.ceil(maxIndex * (endPct / 100))));
    const clampedRatio = Math.max(0, Math.min(1, Number(targetRatio || 0)));
    const rawIndex = startIndex + Math.floor((endIndex - startIndex) * clampedRatio);
    const targetIndex = Math.min(Math.max(rawIndex, startIndex), endIndex);
    const bar = Array.isArray(priceData[targetIndex]) ? priceData[targetIndex] : [];
    const yValue = Number(bar[1] ?? bar[0] ?? 0);
    const point = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [targetIndex, yValue]);
    return {
      targetIndex,
      barCount,
      startIndex,
      endIndex,
      x: Array.isArray(point) ? Number(point[0]) : NaN,
      y: Array.isArray(point) ? Number(point[1]) : NaN,
    };
  }, ratio);

  const canvas = page.locator('#indicatorChartCanvas canvas').first();
  const box = await canvas.boundingBox();
  if (!info || !box) return null;

  const x = Number.isFinite(info.x) ? info.x : box.width * Math.min(Math.max(ratio, 0.1), 0.9);
  const y = Number.isFinite(info.y) ? info.y : box.height * 0.28;
  return {
    targetIndex: info.targetIndex,
    barCount: info.barCount,
    startIndex: info.startIndex,
    endIndex: info.endIndex,
    x: Math.min(Math.max(x, 6), Math.max(6, box.width - 6)),
    y: Math.min(Math.max(y, 6), Math.max(6, box.height - 6)),
  };
}

async function openQuickview(page, seed) {
  const url = new URL('/ibkr_indicators.html', BASE);
  url.searchParams.set('environment', 'live');
  if (seed.date) url.searchParams.set('date', seed.date);
  url.searchParams.set('ts', String(Date.now()));

  for (let attempt = 0; attempt < 2; attempt += 1) {
    await page.goto(url.toString(), { waitUntil: 'networkidle', timeout: 60000 });
    try {
      await page.locator('#searchBox').waitFor({ state: 'visible', timeout: 12000 });
      break;
    } catch (err) {
      if (attempt === 1) throw err;
      await page.waitForTimeout(1200);
    }
  }
  await page.waitForTimeout(1200);

  await page.locator('#searchBox').fill(String(seed.symbol || ''));
  await page.waitForTimeout(900);
  await page.locator('#intervalFilter').selectOption('5');
  await page.waitForTimeout(1400);

  const cards = page.locator('.indicator-card');
  const matchingCards = cards.filter({ hasText: String(seed.symbol || '') });
  const matchingCount = await matchingCards.count().catch(() => 0);
  const targetCard = matchingCount ? matchingCards.first() : cards.first();
  const quickviewButton = targetCard.locator('button').filter({ hasText: '快览' }).first();
  await quickviewButton.click();
  await waitForQuickviewReady(page);

  return {
    url: page.url(),
    visible_cards: await cards.count().catch(() => 0),
    matched_cards: matchingCount,
    modal_title: await page.locator('#indicatorChartTitle').innerText().catch(() => ''),
  };
}

async function hoverCanvas(page, ratio) {
  const probe = await getProbePoint(page, ratio);
  if (!probe) return { ratio, ok: false, reason: 'no_probe' };
  const canvas = page.locator('#indicatorChartCanvas canvas').first();
  const box = await canvas.boundingBox();
  if (!box) return { ratio, ok: false, reason: 'no_canvas_box' };
  await page.mouse.move(box.x + probe.x, box.y + probe.y);
  await page.waitForTimeout(700);
  return { ratio, ok: true, target_index: probe.targetIndex, bar_count: probe.barCount };
}

async function clickCanvas(page, ratio) {
  const probe = await getProbePoint(page, ratio);
  if (!probe) return { ratio, ok: false, reason: 'no_probe' };
  const canvas = page.locator('#indicatorChartCanvas canvas').first();
  const box = await canvas.boundingBox();
  if (!box) return { ratio, ok: false, reason: 'no_canvas_box' };
  await page.mouse.click(box.x + probe.x, box.y + probe.y);
  await page.waitForTimeout(900);
  return { ratio, ok: true, target_index: probe.targetIndex, bar_count: probe.barCount };
}

async function dispatchZrEvent(page, ratio, eventName) {
  const probe = await getProbePoint(page, ratio);
  if (!probe) return { ratio, ok: false, reason: 'no_probe' };
  const dispatched = await page.evaluate(({ name, x, y }) => {
    const chart = window.echarts && window.echarts.getInstanceByDom(document.getElementById('indicatorChartCanvas'));
    const zr = chart && chart.getZr ? chart.getZr() : null;
    const handler = zr && zr.handler;
    if (!handler || typeof handler.dispatch !== 'function') return false;
    handler.dispatch(name, { zrX: x, zrY: y, offsetX: x, offsetY: y });
    return true;
  }, { name: eventName, x: probe.x, y: probe.y });
  await page.waitForTimeout(500);
  return {
    ratio,
    ok: !!dispatched,
    target_index: probe.targetIndex,
    bar_count: probe.barCount,
  };
}

async function inspectQuickview(page, seed, mobile) {
  const openState = await openQuickview(page, seed);
  const initialCursor = await readCursorStrip(page);
  const initialState = await page.evaluate(() => ({
    hover: indicatorChartHoverBarIndex,
    selected: indicatorChartSelectedBarIndex,
  }));
  const chartState = await getChartState(page);
  const rangeButtonCount = await page.locator('#indicatorChartRangeGroup .chart-range-btn').count().catch(() => 0);
  const layerButtonCount = await page.locator('#indicatorChartLayerGroup .chart-layer-btn').count().catch(() => 0);
  const focusButtonCount = await page.locator('#indicatorChartFocusGroup .chart-focus-btn').count().catch(() => 0);
  const cursorCardCount = await page.locator('#indicatorChartCursorStrip .cursor-card').count().catch(() => 0);
  const focusStatusInitial = await page.locator('#indicatorChartFocusGroup .chart-focus-status').innerText().catch(() => '');

  let hoverState = null;
  let hoverCursor = '';
  let hoverInternalState = null;
  let tooltipText = '';
  if (!mobile) {
    hoverState = await hoverCanvas(page, 0.2);
    hoverCursor = await readCursorStrip(page);
    hoverInternalState = await page.evaluate(() => ({
      hover: indicatorChartHoverBarIndex,
      selected: indicatorChartSelectedBarIndex,
    }));
    if (hoverCursor === initialCursor) {
      hoverState = await hoverCanvas(page, 0.4);
      hoverCursor = await readCursorStrip(page);
      hoverInternalState = await page.evaluate(() => ({
        hover: indicatorChartHoverBarIndex,
        selected: indicatorChartSelectedBarIndex,
      }));
    }
    tooltipText = await readTooltipText(page);
  }

  const clickState = await clickCanvas(page, mobile ? 0.22 : 0.18);
  const clickCursor = await readCursorStrip(page);
  const clickInternalState = await page.evaluate(() => ({
    hover: indicatorChartHoverBarIndex,
    selected: indicatorChartSelectedBarIndex,
  }));
  const zrHoverState = await dispatchZrEvent(page, 0.4, 'mousemove');
  const zrHoverCursor = await readCursorStrip(page);
  const zrHoverInternalState = await page.evaluate(() => ({
    hover: indicatorChartHoverBarIndex,
    selected: indicatorChartSelectedBarIndex,
  }));
  const zrClickState = await dispatchZrEvent(page, 0.18, 'click');
  const zrClickCursor = await readCursorStrip(page);
  const zrClickInternalState = await page.evaluate(() => ({
    hover: indicatorChartHoverBarIndex,
    selected: indicatorChartSelectedBarIndex,
  }));

  await page.locator('#indicatorChartFocusGroup .chart-focus-btn').filter({ hasText: 'Prev' }).first().click();
  await page.waitForTimeout(600);
  const prevCursor = await readCursorStrip(page);
  const prevInternalState = await page.evaluate(() => ({
    hover: indicatorChartHoverBarIndex,
    selected: indicatorChartSelectedBarIndex,
  }));
  const focusStatusPrev = await page.locator('#indicatorChartFocusGroup .chart-focus-status').innerText().catch(() => '');

  await page.locator('#indicatorChartFocusGroup .chart-focus-btn').filter({ hasText: 'Latest' }).first().click();
  await page.waitForTimeout(600);
  const latestCursor = await readCursorStrip(page);
  const latestInternalState = await page.evaluate(() => ({
    hover: indicatorChartHoverBarIndex,
    selected: indicatorChartSelectedBarIndex,
  }));
  const focusStatusLatest = await page.locator('#indicatorChartFocusGroup .chart-focus-status').innerText().catch(() => '');

  await page.locator('#indicatorChartRangeGroup .chart-range-btn').filter({ hasText: '1W' }).first().click();
  await page.waitForFunction(() => {
    const cards = document.querySelectorAll('#indicatorChartCursorStrip .cursor-card').length;
    const chart = document.querySelector('#indicatorChartCanvas canvas');
    return cards === 4 && !!chart;
  }, null, { timeout: 30000 });
  await page.waitForTimeout(1000);

  const rangeCursor = await readCursorStrip(page);
  const rangeSummary = await page.locator('#indicatorChartSummary').innerText().catch(() => '');
  const activeRange = await page.locator('#indicatorChartRangeGroup .chart-range-btn.active').innerText().catch(() => '');

  return {
    seed,
    ...openState,
    ...chartState,
    range_button_count: rangeButtonCount,
    layer_button_count: layerButtonCount,
    focus_button_count: focusButtonCount,
    cursor_card_count: cursorCardCount,
    focus_status_initial: focusStatusInitial,
    initial_state: initialState,
    initial_cursor: initialCursor,
    hover_state: hoverState,
    hover_internal_state: hoverInternalState,
    hover_cursor: hoverCursor,
    hover_changed: !!hoverCursor && hoverCursor !== initialCursor,
    tooltip_text: tooltipText,
    tooltip_visible: !!tooltipText,
    click_state: clickState,
    click_internal_state: clickInternalState,
    click_cursor: clickCursor,
    click_changed: !!clickCursor && clickCursor !== initialCursor,
    zr_hover_state: zrHoverState,
    zr_hover_internal_state: zrHoverInternalState,
    zr_hover_cursor: zrHoverCursor,
    zr_hover_changed: !!zrHoverCursor && zrHoverCursor !== initialCursor,
    zr_click_state: zrClickState,
    zr_click_internal_state: zrClickInternalState,
    zr_click_cursor: zrClickCursor,
    zr_click_changed: !!zrClickCursor && zrClickCursor !== initialCursor,
    prev_cursor: prevCursor,
    prev_internal_state: prevInternalState,
    prev_changed: !!prevCursor && prevCursor !== initialCursor,
    focus_status_prev: focusStatusPrev,
    latest_cursor: latestCursor,
    latest_internal_state: latestInternalState,
    latest_back_to_initial: !!latestCursor && latestCursor === initialCursor,
    focus_status_latest: focusStatusLatest,
    post_range_cursor: rangeCursor,
    post_range_summary: rangeSummary,
    active_range: activeRange,
    post_range_cursor_ok: !!rangeCursor && rangeCursor.includes('|'),
  };
}

(async () => {
  const token = await auth();
  const seed = (await pbList(token, 'ibkr_indicators', {
    perPage: 10,
    sort: '-bar_time_ms,-created',
    filter: 'environment = "live" && (interval = "5" || interval = "5m")',
  }).catch(() => []))[0] || null;

  if (!seed) {
    console.log(JSON.stringify({ skipped: true, reason: 'no_live_5m_indicator_seed' }, null, 2));
    return;
  }

  const normalizedSeed = {
    id: seed.id,
    symbol: seed.symbol || '',
    interval: seed.interval || '',
    us_time: seed.us_time || '',
    date: deriveDate(seed),
  };

  const browser = await chromium.launch({ headless: true });
  const results = [];

  for (const mobile of [false, true]) {
    results.push(await withPage(browser, token, mobile, (page) => inspectQuickview(page, normalizedSeed, mobile)));
  }

  await browser.close();
  console.log(JSON.stringify({ seed: normalizedSeed, results }, null, 2));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
