const path = require('path');
const { chromium } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || 'http://127.0.0.1:5104';
const STATIC_ROOT = process.env.IBKR_CONSOLE_STATIC_ROOT
  || path.resolve(__dirname, '../../../../runtime/ibkr_console/static');

const BAR_TIMES = [
  1777905000000,
  1777905300000,
  1777905600000,
  1777905900000,
  1777906200000,
  1777906500000,
  1777906800000,
];

function staticPathForUrl(url) {
  const parsed = new URL(url);
  const pathname = parsed.pathname === '/' ? '/ibkr_chart.html' : parsed.pathname;
  if (pathname.endsWith('/')) return null;
  return path.join(STATIC_ROOT, pathname.replace(/^\/+/, ''));
}

async function fulfillStatic(route) {
  const filePath = staticPathForUrl(route.request().url());
  if (!filePath) {
    await route.fulfill({ status: 404, body: 'not found' });
    return;
  }
  await route.fulfill({ path: filePath });
}

function makeBar(ms, index) {
  const open = 100 + index * 0.25;
  const close = open + (index % 2 === 0 ? 0.18 : -0.12);
  return {
    symbol: 'MOCK',
    interval: '5m',
    bar_time_ms: ms,
    us_time: `2026-05-04 ${String(10 + Math.floor(index / 6)).padStart(2, '0')}:${String(30 + (index % 6) * 5).padStart(2, '0')}:00`,
    open,
    high: open + 0.7,
    low: open - 0.7,
    close,
    volume: 100000 + index * 1000,
  };
}

function makeIndicator(bar, index) {
  return {
    ...bar,
    close: bar.close,
    ema_fast: 100 + index * 0.2,
    ema_slow: 99.8 + index * 0.17,
    ema_trend: 99.3 + index * 0.13,
    vwap: 100.1 + index * 0.18,
    sd_reg: 100 + index * 0.12,
    sd_signal_upper: 102 + index * 0.12,
    sd_signal_lower: 98 + index * 0.12,
    sd_filter_upper: 102.4 + index * 0.12,
    sd_filter_lower: 97.6 + index * 0.12,
    sd_zone: index <= 4 ? -1 : 0,
    sd_trend: 1,
    atr: 0.5,
    atr_raw: 0.5,
    atr_pct: 0.5,
    crsi: 38 + index * 4,
    crsi_ub: 70,
    crsi_db: 30,
    obv_rsi: 45 + index * 2,
    vwap_dist: 0.2,
    sd_lower: index === 0,
    fractal_bull: index === 2,
    obv_reg_bull_div: index === 3,
    crsi_reg_bull_div: index === 4,
    dtp_phase: index >= 3 ? '红初中' : '',
  };
}

function makeTrace(bar, index) {
  const eventsByIndex = {
    0: ['SD下轨触发，开启下轨窗口'],
    2: ['下轨窗口记录多头分形'],
    3: ['记录 OBV 多头背离'],
    4: ['记录 cRSI 多头背离', '多头候选被过滤: DTP红初中'],
    5: ['出现反向空头组件，清空多头窗口组件'],
  };
  const componentFlags = {
    sd_lower_bull_fractal_seen: index >= 2 && index <= 4,
    bull_obv_div_seen: index >= 3 && index <= 4,
    bull_crsi_div_seen: index >= 4 && index <= 4,
    buy_raw: index === 4,
  };
  const signalPayload = index === 4 ? {
    signal_id: 'mock-blocked-1050',
    symbol: 'MOCK',
    direction: 'long',
    signal: 'long',
    entry: bar.close,
    reason: 'DTP红初中',
    extra: {
      signal_window: 'sd_lower',
      signal_mode: 'mr',
      source_kind: 'computed',
    },
  } : null;
  return {
    bar_time_ms: bar.bar_time_ms,
    bar_index: index + 1,
    us_time: bar.us_time,
    cn_time: '2026-05-04 22:50:00',
    close: bar.close,
    event_chain: eventsByIndex[index] || [],
    filters: index === 4 ? ['多头过滤: DTP红初中'] : [],
    signal_state: {
      stage: index === 4 ? 'blocked' : 'none',
      direction: index === 4 ? 'long' : '',
      signal: index === 4 ? 'long' : '',
      label: index === 4 ? '回归多已过滤' : '无信号',
      reason: '',
      filter_reason: index === 4 ? 'DTP红初中' : '',
      signal_window: index === 4 ? 'sd_lower' : '',
      signal_mode: index === 4 ? 'mr' : '',
      ema_touch_line: '',
      div_source: '',
      signal_payload: signalPayload,
    },
    window_flags: {
      sd_upper_valid: false,
      sd_lower_valid: index >= 0 && index <= 4,
      sd_upper_active: false,
      sd_lower_active: index >= 0 && index <= 4,
      sd_upper_used: false,
      sd_lower_used: false,
      sd_upper_age_bars: 0,
      sd_lower_age_bars: index <= 4 ? index : 0,
    },
    component_flags: componentFlags,
    structure: {
      ema_bullish: true,
      ema_bearish: false,
      dtp_phase: index >= 3 ? '红初中' : '',
      fractal_tokens: index === 2 ? ['F↑'] : [],
      touch_tokens: [],
    },
    position: { vwap_dist: 0.2, sd_zone: index <= 4 ? -1 : 0, sd_trend: 1 },
    volatility: { atr: 0.5, atr_pct: 0.5 },
    momentum: {
      crsi: 38 + index * 4,
      obv_rsi: 45 + index * 2,
      divergence_tokens: index >= 3 ? ['oR↑', 'cR↑'] : [],
    },
  };
}

const bars = BAR_TIMES.map(makeBar);
const indicatorTimeline = bars.map(makeIndicator);
const traceTimeline = bars.map(makeTrace);
const timelineRequests = [];

const mockTimelinePayload = {
  ok: true,
  environment: 'live',
  symbol: 'MOCK',
  interval: '5m',
  bars,
  indicator_timeline: indicatorTimeline,
  latest_indicator: indicatorTimeline[indicatorTimeline.length - 1],
  signals: [],
  trace_timeline: traceTimeline,
  meta: { trace_mode: 'computed' },
};

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.addInitScript(() => {
    localStorage.setItem('pb_token', 'mock-token');
    localStorage.removeItem('ibkr_chart_ui_prefs_v2');
  });
  const page = await context.newPage();
  const errors = [];

  page.on('pageerror', (error) => errors.push(`pageerror:${error.message}`));
  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type())) {
      errors.push(`console:${message.type()}:${message.text()}`);
    }
  });

  await page.route('**/*', async (route) => {
    const url = route.request().url();
    if (url.includes('/api/custom/ibkr/proxy')) {
      const requestJson = route.request().postDataJSON();
      if (requestJson?.action === 'chart/timeline') {
        timelineRequests.push(requestJson);
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mockTimelinePayload) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) });
      return;
    }
    if (url.includes('/api/custom/ibkr/quotes')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, quotes: [] }) });
      return;
    }
    if (url.includes('/api/collections/')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) });
      return;
    }
    if (url.includes('fonts.googleapis.com') || url.includes('fonts.gstatic.com')) {
      await route.fulfill({ status: 204, body: '' });
      return;
    }
    if (url.startsWith(CONSOLE_BASE)) {
      await fulfillStatic(route);
      return;
    }
    await route.continue();
  });

  await page.goto(`${CONSOLE_BASE}/ibkr_chart.html?environment=live&symbol=MOCK&interval=5m&range=custom&start_ms=${BAR_TIMES[0]}&end_ms=${BAR_TIMES[BAR_TIMES.length - 1]}&bar_time_ms=${BAR_TIMES[4]}&trace=1`, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });

  await page.waitForFunction(() => {
    const dom = document.getElementById('chartCanvas');
    return dom && window.echarts && window.echarts.getInstanceByDom(dom);
  }, { timeout: 30000 });
  await page.waitForTimeout(800);

  const beforeClick = await page.evaluate(() => {
    const dom = document.getElementById('chartCanvas');
    const chart = window.echarts.getInstanceByDom(dom);
    const option = chart.getOption();
    const series = Array.isArray(option.series) ? option.series : [];
    const componentSeries = series.find((item) => item.name === 'Flow 组件收集');
    const candidateSeries = series.find((item) => item.name === 'Flow 候选');
    const blockedSeries = series.find((item) => item.name === 'Flow 已过滤');
    const blockedMain = series.find((item) => item.name === 'Blocked Trace');
    const flowNames = series.map((item) => item.name).filter((name) => String(name || '').startsWith('Flow '));
    return {
      gridCount: Array.isArray(option.grid) ? option.grid.length : 0,
      xAxisCount: Array.isArray(option.xAxis) ? option.xAxis.length : 0,
      yAxisCount: Array.isArray(option.yAxis) ? option.yAxis.length : 0,
      flowNames,
      hasFlowTitle: (Array.isArray(option.title) ? option.title : []).some((item) => /Signal Flow/.test(item.text || '')),
      componentDataCount: Array.isArray(componentSeries?.data) ? componentSeries.data.length : 0,
      componentLabelShow: Boolean(componentSeries?.label?.show),
      componentFirstLabel: componentSeries?.data?.[0]?.labelText || '',
      candidateLabelShow: Boolean(candidateSeries?.label?.show),
      blockedDataCount: Array.isArray(blockedSeries?.data) ? blockedSeries.data.length : 0,
      blockedLabel: blockedSeries?.data?.[0]?.labelText || '',
      blockedLabelShow: Boolean(blockedSeries?.label?.show),
      blockedMainCount: Array.isArray(blockedMain?.data) ? blockedMain.data.length : 0,
      customStartValue: document.getElementById('customRangeStart')?.value || '',
      customEndValue: document.getElementById('customRangeEnd')?.value || '',
      customZoneText: document.getElementById('customRangeForm')?.textContent || '',
      cursorText: document.getElementById('cursorStrip')?.textContent || '',
      traceText: document.getElementById('tracePanelShell')?.textContent || '',
      layerText: document.getElementById('layerStrip')?.textContent || '',
    };
  });

  await page.locator('#chartCanvas').scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  const clickProbe = await page.evaluate(() => {
    const dom = document.getElementById('chartCanvas');
    const chart = window.echarts.getInstanceByDom(dom);
    const option = chart.getOption();
    const seriesIndex = option.series.findIndex((item) => item.name === 'Flow 已过滤');
    if (seriesIndex < 0) return { ok: false, reason: 'series_missing' };
    const point = chart.convertToPixel({ xAxisIndex: 1, yAxisIndex: 1 }, [4, 1.05]);
    if (!Array.isArray(point)) return { ok: false, reason: 'pixel_missing' };
    return { ok: true, seriesIndex, x: Number(point[0]), y: Number(point[1]) };
  });
  if (clickProbe.ok) {
    const canvasBox = await page.locator('#chartCanvas canvas').first().boundingBox();
    if (canvasBox) {
      await page.mouse.click(canvasBox.x + clickProbe.x, canvasBox.y + clickProbe.y);
    }
  }
  await page.waitForTimeout(600);

  const afterClick = await page.evaluate(() => ({
    drawerVisible: Boolean(document.querySelector('#signalDetailDrawer.show')),
    drawerText: document.getElementById('signalDetailContent')?.textContent || '',
    cursorText: document.getElementById('cursorStrip')?.textContent || '',
    activeTraceText: document.querySelector('#tracePanelShell tr.active')?.textContent || '',
  }));

  const beforeCustomApplyRequests = timelineRequests.length;
  await page.evaluate(() => {
    if (typeof window.closeSignalDrawer === 'function') window.closeSignalDrawer();
  });
  await page.locator('button.range-btn', { hasText: '自定义' }).click();
  await page.waitForTimeout(250);
  const afterCustomSelectRequests = timelineRequests.length;
  await page.fill('#customRangeStart', '2026-05-04T10:35');
  await page.fill('#customRangeEnd', '2026-05-04T10:50');
  const applyResponsePromise = page.waitForResponse((response) => {
    if (!response.url().includes('/api/custom/ibkr/proxy')) return false;
    const requestBody = response.request().postData() || '';
    return requestBody.includes('"action":"chart/timeline"') || requestBody.includes('"action": "chart/timeline"');
  }, { timeout: 30000 });
  await Promise.all([
    applyResponsePromise,
    page.click('.custom-range-apply'),
  ]);
  await page.waitForTimeout(800);
  const afterCustomApplyRequests = timelineRequests.length;
  const customApplyRequest = timelineRequests[timelineRequests.length - 1] || {};
  const customState = await page.evaluate(() => ({
    startValue: document.getElementById('customRangeStart')?.value || '',
    endValue: document.getElementById('customRangeEnd')?.value || '',
    href: window.location.href,
  }));

  await browser.close();

  const failures = [];
  if (errors.length) failures.push(...errors);
  if (beforeClick.gridCount !== 4) failures.push(`grid_count_${beforeClick.gridCount}`);
  if (beforeClick.xAxisCount !== 4) failures.push(`x_axis_count_${beforeClick.xAxisCount}`);
  if (beforeClick.yAxisCount !== 5) failures.push(`y_axis_count_${beforeClick.yAxisCount}`);
  if (!beforeClick.hasFlowTitle) failures.push('missing_signal_flow_title');
  if (!beforeClick.layerText.includes('Signal Flow')) failures.push('missing_signal_flow_layer');
  if (!beforeClick.flowNames.includes('Flow 下轨窗口')) failures.push('missing_lower_window_series');
  if (!beforeClick.flowNames.includes('Flow 组件收集')) failures.push('missing_component_flow_series');
  if (!beforeClick.flowNames.includes('Flow 已过滤')) failures.push('missing_blocked_flow_series');
  if (beforeClick.componentDataCount < 1) failures.push(`component_flow_count_${beforeClick.componentDataCount}`);
  if (beforeClick.componentLabelShow) failures.push('component_label_should_be_hidden');
  if (beforeClick.candidateLabelShow) failures.push('candidate_label_should_be_hidden');
  if (beforeClick.blockedDataCount !== 1) failures.push(`blocked_flow_count_${beforeClick.blockedDataCount}`);
  if (beforeClick.blockedMainCount !== 1) failures.push(`blocked_main_count_${beforeClick.blockedMainCount}`);
  if (!beforeClick.blockedLabel.includes('DTP红初中')) failures.push('missing_blocked_label_reason');
  if (!beforeClick.blockedLabelShow) failures.push('blocked_label_should_be_visible');
  if (beforeClick.customStartValue !== '2026-05-04T10:30') failures.push(`custom_start_not_et_${beforeClick.customStartValue}`);
  if (beforeClick.customEndValue !== '2026-05-04T11:00') failures.push(`custom_end_not_et_${beforeClick.customEndValue}`);
  if (!beforeClick.customZoneText.includes('ET')) failures.push('missing_custom_range_et_badge');
  if (!beforeClick.cursorText.includes('blocked · DTP红初中')) failures.push('cursor_missing_blocked_reason');
  if (!beforeClick.traceText.includes('blocked · DTP红初中')) failures.push('trace_missing_blocked_reason');
  if (!clickProbe.ok) failures.push(`click_failed_${clickProbe.reason || 'unknown'}`);
  if (!afterClick.drawerVisible) failures.push('blocked_click_did_not_open_drawer');
  if (!afterClick.drawerText.includes('DTP红初中')) failures.push('drawer_missing_blocked_reason');
  if (!afterClick.activeTraceText.includes('DTP红初中')) failures.push('active_trace_missing_reason');
  if (afterCustomSelectRequests !== beforeCustomApplyRequests) failures.push('custom_select_should_not_reload');
  if (afterCustomApplyRequests <= afterCustomSelectRequests) failures.push('custom_apply_did_not_reload');
  if (customApplyRequest.start_ms !== BAR_TIMES[1]) failures.push(`custom_apply_start_ms_${customApplyRequest.start_ms}`);
  if (customApplyRequest.end_ms !== BAR_TIMES[4]) failures.push(`custom_apply_end_ms_${customApplyRequest.end_ms}`);
  if (customState.startValue !== '2026-05-04T10:35') failures.push(`custom_state_start_${customState.startValue}`);
  if (customState.endValue !== '2026-05-04T10:50') failures.push(`custom_state_end_${customState.endValue}`);
  if (!customState.href.includes(`start_ms=${BAR_TIMES[1]}`) || !customState.href.includes(`end_ms=${BAR_TIMES[4]}`)) {
    failures.push('custom_url_missing_applied_ms');
  }

  const output = {
    ok: failures.length === 0,
    failures,
    beforeClick,
    clickProbe,
    afterClick,
    customRange: {
      beforeCustomApplyRequests,
      afterCustomSelectRequests,
      afterCustomApplyRequests,
      customApplyRequest,
      customState,
    },
  };
  console.log(JSON.stringify(output, null, 2));
  if (failures.length) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
