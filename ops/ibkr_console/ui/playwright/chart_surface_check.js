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

function deriveDate(item) {
  if (!item) return '';
  if (item.us_time) return String(item.us_time).split(' ')[0];
  if (item.bar_time_ms) return new Date(Number(item.bar_time_ms)).toISOString().slice(0, 10);
  if (item.created) return String(item.created).slice(0, 10);
  return '';
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

async function inspectChartSurface(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(2500);
  await page.waitForFunction(() => {
    const dom = document.getElementById('chartCanvas');
    return dom && window.echarts && window.echarts.getInstanceByDom(dom);
  }, { timeout: 30000 }).catch(() => {});
  const compareButtonCount = await page.locator('#compareBtn').count().catch(() => 0);
  const compareButtonDisabled = await page.locator('#compareBtn').isDisabled().catch(() => true);
  const selectionResult = await inspectChartSelection(page).catch((error) => ({
    ok: false,
    reason: error?.message || String(error),
  }));
  let compareButtonText = '';
  let compareRailCount = 0;
  let compareCursorCount = 0;
  let compareSummaryText = '';
  let firstCompareItemTitle = '';
  let firstCompareItemMeta = '';
  let compareItemCount = 0;
  let compareTriggered = false;
  if (compareButtonCount > 0 && !compareButtonDisabled) {
    compareTriggered = true;
    await page.locator('#compareBtn').click().catch(() => {});
    await page.waitForFunction(() => {
      const btn = document.getElementById('compareBtn');
      return !btn || !String(btn.textContent || '').includes('对比中');
    }, null, { timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(1200);
    compareButtonText = await page.locator('#compareBtn').innerText().catch(() => '');
    compareRailCount = await page.locator('#infoRail .rail-card').filter({ hasText: /IBKR Compare|Stored Bars Chain|IBKR API Chain|Comparison/ }).count().catch(() => 0);
    compareCursorCount = await page.locator('#cursorStrip .cursor-card').count().catch(() => 0);
    compareSummaryText = await page.locator('#infoRail .rail-card').filter({ hasText: 'Comparison' }).first().innerText().catch(() => '');
    compareItemCount = await page.locator('#infoRail .compare-item').count().catch(() => 0);
    firstCompareItemTitle = await page.locator('#infoRail .compare-item .compare-title').first().innerText().catch(() => '');
    firstCompareItemMeta = await page.locator('#infoRail .compare-item .compare-meta').first().innerText().catch(() => '');
  }
  return {
    url,
    title: await page.title(),
    hero_title: await page.locator('#heroTitle').innerText().catch(() => ''),
    chart_title: await page.locator('#chartPanelTitle').innerText().catch(() => ''),
    compare_button_count: compareButtonCount,
    compare_button_disabled: compareButtonDisabled,
    compare_triggered: compareTriggered,
    compare_button_text: compareButtonText,
    compare_rail_count: compareRailCount,
    compare_cursor_count: compareCursorCount,
    compare_summary_text: compareSummaryText,
    compare_item_count: compareItemCount,
    first_compare_item_title: firstCompareItemTitle,
    first_compare_item_meta: firstCompareItemMeta,
    selection_result: selectionResult,
    clear_compare_button_count: await page.locator('#clearCompareBtn').count().catch(() => 0),
    layer_count: await page.locator('#layerStrip .layer-btn').count().catch(() => 0),
    active_layer_count: await page.locator('#layerStrip .layer-btn.active').count().catch(() => 0),
    cursor_count: await page.locator('#cursorStrip .cursor-card').count().catch(() => 0),
    rail_count: await page.locator('#infoRail .rail-card').count().catch(() => 0),
    tf_count: await page.locator('.tf-btn').count().catch(() => 0),
  };
}

async function readPrimaryChartZoom(page) {
  return page.evaluate(() => {
    const dom = document.getElementById('chartCanvas');
    const inst = window.echarts?.getInstanceByDom?.(dom);
    const zoom = inst?.getOption?.().dataZoom?.[0] || {};
    return {
      start: Number(zoom.start),
      end: Number(zoom.end),
      span: Number(zoom.end) - Number(zoom.start),
    };
  });
}

async function dragChartWindow(page, box, startRatio, endRatio, modifiers = []) {
  const y = box.y + Math.min(box.height * 0.34, 260);
  const startX = box.x + box.width * startRatio;
  const endX = box.x + box.width * endRatio;
  for (const key of modifiers) await page.keyboard.down(key);
  await page.mouse.move(startX, y);
  await page.mouse.down();
  await page.mouse.move(endX, y, { steps: 10 });
  await page.mouse.up();
  for (const key of modifiers.slice().reverse()) await page.keyboard.up(key);
  await page.waitForTimeout(350);
}

async function inspectChartSelection(page) {
  const selectButton = page.locator('#chartToolbar .tv-tool-btn').filter({ hasText: /^Select$/ }).first();
  const selectButtonCount = await selectButton.count().catch(() => 0);
  const canvas = page.locator('#chartCanvas');
  await canvas.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  const box = await canvas.boundingBox();
  if (!box) throw new Error('chart_canvas_box_missing');
  const before = await readPrimaryChartZoom(page);
  if (!selectButtonCount) {
    const compactViewport = await page.evaluate(() => Number(window.innerWidth || 0) <= 768).catch(() => false);
    return { ok: Boolean(compactViewport), skipped: Boolean(compactViewport), reason: 'select_button_missing', before };
  }
  await selectButton.click();
  await page.waitForTimeout(120);
  await dragChartWindow(page, box, 0.28, 0.68);
  const afterButtonDrag = await readPrimaryChartZoom(page);
  await page.locator('#chartToolbar .tv-tool-btn').filter({ hasText: /^Reset$/ }).first().click().catch(() => {});
  await page.waitForTimeout(300);
  const afterReset = await readPrimaryChartZoom(page);
  await dragChartWindow(page, box, 0.32, 0.58, ['Shift']);
  const afterShiftDrag = await readPrimaryChartZoom(page);
  const buttonChanged = Number.isFinite(before.span)
    && Number.isFinite(afterButtonDrag.span)
    && afterButtonDrag.span < before.span
    && (before.start !== afterButtonDrag.start || before.end !== afterButtonDrag.end);
  const resetRestored = Number.isFinite(afterReset.span)
    && afterReset.span >= afterButtonDrag.span
    && Math.abs(afterReset.start - before.start) < 0.8
    && Math.abs(afterReset.end - before.end) < 0.8;
  const shiftChanged = Number.isFinite(afterReset.span)
    && Number.isFinite(afterShiftDrag.span)
    && afterShiftDrag.span < afterReset.span
    && (afterReset.start !== afterShiftDrag.start || afterReset.end !== afterShiftDrag.end);
  return {
    ok: buttonChanged && resetRestored && shiftChanged,
    select_button_count: selectButtonCount,
    before,
    after_button_drag: afterButtonDrag,
    after_reset: afterReset,
    after_shift_drag: afterShiftDrag,
    button_changed: buttonChanged,
    reset_restored: resetRestored,
    shift_changed: shiftChanged,
  };
}

async function clickFirstChartButton(page, errors) {
  const buttons = page.locator('button:not([disabled])').filter({ hasText: /图表页|打开图表/ });
  const count = await buttons.count();
  if (!count) return { button_count: 0, skipped: true, reason: 'no_chart_button' };
  await buttons.first().click();
  await page.waitForURL(/\/ibkr_chart\.html/, { timeout: 15000 });
  await page.waitForTimeout(1600);
  return {
    button_count: count,
    final_url: page.url(),
    chart_title: await page.locator('#chartPanelTitle').innerText().catch(() => ''),
    layer_count: await page.locator('#layerStrip .layer-btn').count().catch(() => 0),
    rail_count: await page.locator('#infoRail .rail-card').count().catch(() => 0),
    ok: /\/ibkr_chart\.html/.test(page.url()),
  };
}

(async () => {
  const token = await auth();
  const orderDetails = await pbList(token, 'order_details', {
    perPage: 1,
    sort: '-bar_time_ms,-created',
    filter: 'environment = "live"',
  }).catch(() => []);
  const latestOrderDetail = orderDetails[0] || null;
  const latestSignal = (await pbList(token, 'ibkr_signals', {
    perPage: 1,
    sort: '-bar_time_ms,-created',
    filter: 'environment = "live"',
  }).catch(() => []))[0] || null;

  const browser = await chromium.launch({ headless: true });
  const results = [];

  for (const mobile of [false, true]) {
    results.push({
      name: 'chart_surface',
      ...(await withPage(browser, token, mobile, (page) => inspectChartSurface(page, `${CONSOLE_BASE}/ibkr_chart.html?environment=live`))),
    });
  }

  for (const mobile of [false, true]) {
    results.push({
      name: 'account_to_chart',
      ...(await withPage(browser, token, mobile, async (page) => {
        const url = `${CONSOLE_BASE}/ibkr_account.html?environment=live`;
        await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
        await page.waitForTimeout(2500);
        if (latestSignal?.symbol) {
          const visibleTicketInput = page.locator('#ticketSymbol:visible').first();
          if (await visibleTicketInput.count().catch(() => 0)) {
            await visibleTicketInput.fill(String(latestSignal.symbol));
            await page.waitForTimeout(300);
          }
        }
        return {
          url,
          summary_text: await page.locator('#summaryArea').innerText().catch(() => ''),
          ...(await clickFirstChartButton(page)),
        };
      })),
    });
  }

  if (latestOrderDetail) {
    const detailDate = deriveDate(latestOrderDetail);
    const detailUrl = new URL('/ibkr_order_details.html', BASE);
    detailUrl.searchParams.set('environment', 'live');
    if (detailDate) detailUrl.searchParams.set('date', detailDate);
    if (latestOrderDetail.trade_group_id) detailUrl.searchParams.set('trade_group_id', latestOrderDetail.trade_group_id);
    else if (latestOrderDetail.order_id) detailUrl.searchParams.set('order_id', latestOrderDetail.order_id);
    if (latestOrderDetail.signal_id) detailUrl.searchParams.set('signal_id', latestOrderDetail.signal_id);
    results.push({
      name: 'order_details_to_chart',
      ...(await withPage(browser, token, false, async (page) => {
        const url = detailUrl.toString();
        await page.goto(url, { waitUntil: 'networkidle', timeout: 60000 });
        await page.waitForTimeout(2500);
        return {
          url,
          detail_symbol: await page.locator('.focus-symbol').innerText().catch(() => ''),
          ...(await clickFirstChartButton(page)),
        };
      })),
    });
  } else {
    results.push({ name: 'order_details_to_chart', skipped: true, reason: 'no_live_order_details' });
  }

  if (latestSignal) {
    const signalUrl = `${CONSOLE_BASE}/ibkr_signals.html?environment=live&date=${deriveDate(latestSignal)}`;
    results.push({
      name: 'signal_to_chart_layers',
      ...(await withPage(browser, token, false, async (page) => {
        await page.goto(signalUrl, { waitUntil: 'networkidle', timeout: 60000 });
        await page.waitForTimeout(2200);
        await page.locator('button:not([disabled])').filter({ hasText: '图表页' }).first().click();
        await page.waitForURL(/\/ibkr_chart\.html/, { timeout: 15000 });
        await page.waitForTimeout(1600);
        const chartUrlBeforeNav = page.url();
        const beforeLayers = await page.locator('#layerStrip .layer-btn.active').count().catch(() => 0);
        await page.locator('#layerStrip .layer-btn').filter({ hasText: 'VWAP' }).first().click().catch(() => {});
        await page.waitForTimeout(500);
        const afterLayers = await page.locator('#layerStrip .layer-btn.active').count().catch(() => 0);
        const layerCount = await page.locator('#layerStrip .layer-btn').count().catch(() => 0);
        const cursorCount = await page.locator('#cursorStrip .cursor-card').count().catch(() => 0);
        const railCount = await page.locator('#infoRail .rail-card').count().catch(() => 0);
        const recentSignalCount = await page.locator('#infoRail .signal-item.interactive').count().catch(() => 0);
        const compareButtonCount = await page.locator('#compareBtn').count().catch(() => 0);
        const compareButtonDisabled = await page.locator('#compareBtn').isDisabled().catch(() => true);
        let activeSignalCount = 0;
        let drawerVisible = 0;
        let drawerActionCount = 0;
        let drawerHasOrderDetailsLink = 0;
        let focusHasOrderDetailsLink = 0;
        let focusActionCount = await page.locator('.focus-actions .mini-link').count().catch(() => 0);
        let orderDetailsNavigationOk = false;
        let orderDetailsUrl = '';
        let orderDetailsBodyHasEmptyState = false;
        let compareButtonText = '';
        let compareRailCount = 0;
        if (recentSignalCount > 0) {
          await page.locator('#infoRail .signal-item.interactive').first().click();
          await page.waitForTimeout(500);
          activeSignalCount = await page.locator('#infoRail .signal-item.interactive.active').count().catch(() => 0);
          drawerVisible = await page.locator('#signalDetailDrawer.show .detail-drawer-card').count().catch(() => 0);
          drawerActionCount = await page.locator('#signalDetailDrawer.show .drawer-actions .mini-link').count().catch(() => 0);
          drawerHasOrderDetailsLink = await page.locator('#signalDetailDrawer.show .drawer-actions .mini-link').filter({ hasText: '订单明细' }).count().catch(() => 0);
          focusHasOrderDetailsLink = await page.locator('.focus-actions .mini-link').filter({ hasText: '订单明细' }).count().catch(() => 0);
          focusActionCount = await page.locator('.focus-actions .mini-link').count().catch(() => 0);
          if (drawerHasOrderDetailsLink > 0) {
            await page.locator('#signalDetailDrawer.show .drawer-actions .mini-link').filter({ hasText: '订单明细' }).first().click();
            await page.waitForURL(/\/ibkr_order_details\.html/, { timeout: 15000 });
            await page.waitForTimeout(1200);
            orderDetailsUrl = page.url();
            orderDetailsNavigationOk = /\/ibkr_order_details\.html/.test(orderDetailsUrl);
            orderDetailsBodyHasEmptyState = await page.locator('body').innerText().then((text) => /暂无订单事件|订单事件|交易组/.test(text)).catch(() => false);
          }
        }
        if (compareButtonCount > 0 && !compareButtonDisabled) {
          await page.locator('#compareBtn').click().catch(() => {});
          await page.waitForFunction(() => {
            const btn = document.getElementById('compareBtn');
            return !btn || !String(btn.textContent || '').includes('对比中');
          }, null, { timeout: 12000 }).catch(() => {});
          await page.waitForTimeout(800);
          compareButtonText = await page.locator('#compareBtn').innerText().catch(() => '');
          compareRailCount = await page.locator('#infoRail .rail-card').filter({ hasText: /IBKR Compare|Stored Bars Chain|IBKR API Chain|Comparison/ }).count().catch(() => 0);
        }
        return {
          url: signalUrl,
          chart_url: chartUrlBeforeNav,
          signal_button_count: await page.locator('button').filter({ hasText: '图表页' }).count().catch(() => 0),
          layer_count: layerCount,
          active_layer_count: afterLayers,
          active_layer_before: beforeLayers,
          cursor_count: cursorCount,
          rail_count: railCount,
          focus_action_count: focusActionCount,
          recent_signal_count: recentSignalCount,
          active_signal_count: activeSignalCount,
          drawer_visible: drawerVisible,
          drawer_action_count: drawerActionCount,
          drawer_order_details_link_count: drawerHasOrderDetailsLink,
          focus_order_details_link_count: focusHasOrderDetailsLink,
          order_details_navigation_ok: orderDetailsNavigationOk,
          order_details_url: orderDetailsUrl,
          order_details_body_has_expected_text: orderDetailsBodyHasEmptyState,
          compare_button_count: compareButtonCount,
          compare_button_disabled: compareButtonDisabled,
          compare_button_text: compareButtonText,
          compare_rail_count: compareRailCount,
        };
      })),
    });
  }

  await browser.close();
  console.log(JSON.stringify({ latest_order_detail: latestOrderDetail, latest_signal: latestSignal, results }, null, 2));
})();
