const path = require('path');
const { chromium } = require('playwright');

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || 'http://127.0.0.1:5104';
const STATIC_ROOT = process.env.IBKR_CONSOLE_STATIC_ROOT
  || path.resolve(__dirname, '../../../../runtime/ibkr_console/static');

function staticPathForUrl(url) {
  const parsed = new URL(url);
  const pathname = parsed.pathname === '/' ? '/ibkr_screener.html' : parsed.pathname;
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

const mockRulesPayload = {
  ok: true,
  computed_at_us: '2026-05-04 10:31:00',
  selection: {
    title: 'Mock selection rules',
    subtitle: 'static check',
    chips: [],
    sections: [],
  },
};

const mockWindowProgressPayload = {
  ok: true,
  environment: 'live',
  market_date: '2026-05-04',
  computed_at_us: '2026-05-04 10:31:00',
  summary: { total: 6, active_count: 1, candidate_count: 1, blocked_count: 1, near_expiry_count: 1 },
  items: [
    {
      symbol: 'MOCK',
      status: 'near_expiry',
      target_status: 'active',
      score: 42,
      latest_us_time: '2026-05-04 10:25:00',
      freshness_min: 6,
      bars_remaining: 2,
      component_progress: 0.5,
      sd_upper_active: false,
      sd_lower_active: true,
      lower_window: { active: true, valid: true, status: 'near_expiry', age_bars: 10, bars_remaining: 2 },
      upper_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      components: {
        collected: ['下轨空头分形'],
        missing: ['下轨多头分形', '多头背离(cRSI/OBV)', '下轨 EMA 空头触及', '空头背离(cRSI/OBV)'],
      },
      collected_components: ['下轨空头分形'],
      missing_components: ['下轨多头分形', '多头背离(cRSI/OBV)', '下轨 EMA 空头触及', '空头背离(cRSI/OBV)'],
      component_groups: {
        type1_long_trend: {
          label: 'Type1 顺势多',
          active: false,
          ready: false,
          completed: 0,
          total: 3,
          progress: 0,
          present_labels: [],
          missing_labels: ['上轨 EMA 多头触及', '上轨多头分形', '多头背离(cRSI/OBV)'],
        },
        type2_long_mr: {
          label: 'Type2 回归多',
          active: true,
          ready: false,
          completed: 0,
          total: 2,
          progress: 0,
          present_labels: [],
          missing_labels: ['下轨多头分形', '多头背离(cRSI/OBV)'],
        },
        type3_short_mr: {
          label: 'Type3 回归空',
          active: false,
          ready: false,
          completed: 0,
          total: 2,
          progress: 0,
          present_labels: [],
          missing_labels: ['上轨空头分形', '空头背离(cRSI/OBV)'],
        },
        type4_short_trend: {
          label: 'Type4 顺势空',
          active: true,
          ready: false,
          completed: 1,
          total: 3,
          progress: 0.3333,
          present_labels: ['下轨空头分形'],
          missing_labels: ['下轨 EMA 空头触及', '空头背离(cRSI/OBV)'],
        },
      },
      candidate_signal_label: '',
      filter_reasons: [],
      trace_url: '/ibkr_chart.html?symbol=MOCK',
    },
    {
      symbol: 'CAND',
      status: 'candidate',
      target_status: 'active',
      score: 39,
      latest_us_time: '2026-05-04 10:25:00',
      freshness_min: 4,
      bars_remaining: 7,
      component_progress: 1,
      sd_upper_active: true,
      sd_lower_active: false,
      upper_window: { active: true, valid: true, status: 'upper_active', age_bars: 3, bars_remaining: 7 },
      lower_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      collected_components: ['上轨 EMA 多头触及', '上轨多头分形', '多头背离(cRSI/OBV)'],
      missing_components: [],
      candidate_signal_label: 'LONG',
      filter_reasons: [],
      trace_url: '/ibkr_chart.html?symbol=CAND',
    },
    {
      symbol: 'BLKD',
      status: 'blocked',
      target_status: 'active',
      score: 31,
      latest_us_time: '2026-05-04 10:25:00',
      freshness_min: 5,
      bars_remaining: 5,
      component_progress: 0.75,
      sd_upper_active: true,
      sd_lower_active: false,
      upper_window: { active: true, valid: true, status: 'upper_active', age_bars: 5, bars_remaining: 5 },
      lower_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      collected_components: ['上轨 EMA 多头触及'],
      missing_components: ['上轨多头分形'],
      candidate_signal_label: '',
      filter_reasons: ['MR Short 过滤'],
      trace_url: '/ibkr_chart.html?symbol=BLKD',
    },
    {
      symbol: 'ACTV',
      status: 'upper_active',
      target_status: 'active',
      score: 28,
      latest_us_time: '2026-05-04 10:25:00',
      freshness_min: 7,
      bars_remaining: 8,
      component_progress: 0.25,
      sd_upper_active: true,
      sd_lower_active: false,
      upper_window: { active: true, valid: true, status: 'upper_active', age_bars: 2, bars_remaining: 8 },
      lower_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      collected_components: ['上轨 EMA 多头触及'],
      missing_components: ['上轨多头分形'],
      candidate_signal_label: '',
      filter_reasons: [],
      trace_url: '/ibkr_chart.html?symbol=ACTV',
    },
    {
      symbol: 'NONE',
      status: 'no_window',
      target_status: 'candidate',
      score: 18,
      latest_us_time: '2026-05-04 10:25:00',
      freshness_min: 9,
      bars_remaining: 0,
      component_progress: 0,
      sd_upper_active: false,
      sd_lower_active: false,
      upper_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      lower_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      collected_components: [],
      missing_components: [],
      candidate_signal_label: '',
      filter_reasons: [],
      trace_url: '/ibkr_chart.html?symbol=NONE',
    },
    {
      symbol: 'DONE',
      status: 'confirmed',
      target_status: 'active',
      score: 44,
      latest_us_time: '2026-05-04 10:25:00',
      freshness_min: 2,
      bars_remaining: 3,
      component_progress: 1,
      sd_upper_active: false,
      sd_lower_active: true,
      upper_window: { active: false, valid: false, status: 'inactive', age_bars: 0, bars_remaining: 0 },
      lower_window: { active: true, valid: true, status: 'lower_active', age_bars: 7, bars_remaining: 3 },
      collected_components: ['下轨空头分形', '空头背离(cRSI/OBV)'],
      missing_components: [],
      candidate_signal_label: 'SHORT',
      filter_reasons: [],
      trace_url: '/ibkr_chart.html?symbol=DONE',
    },
  ],
};

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.addInitScript(() => {
    localStorage.setItem('pb_token', 'mock-token');
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
    if (url.includes('/api/custom/ibkr/rules')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mockRulesPayload) });
      return;
    }
    if (url.includes('/api/custom/ibkr/active-window-progress')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mockWindowProgressPayload) });
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

  await page.goto(`${CONSOLE_BASE}/ibkr_screener.html?environment=live&tab=screener&view=window-progress`, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });

  await page.waitForSelector('#windowProgressViewPanel.active #windowProgressTable .window-component-group', {
    timeout: 20000,
  });

  const result = await page.evaluate(() => {
    const text = document.querySelector('#windowProgressTable')?.textContent || '';
    const visibleGroups = Array.from(document.querySelectorAll('#windowProgressTable .window-component-group'))
      .map((node) => ({
        key: node.getAttribute('data-component-group'),
        text: node.textContent || '',
      }));
    const tabCounts = Object.fromEntries(Array.from(document.querySelectorAll('#windowProgressStatusTabs [data-window-status]'))
      .map((node) => [
        node.getAttribute('data-window-status'),
        node.querySelector('.window-progress-status-count')?.textContent?.trim(),
      ]));
    return {
      visibleGroups,
      tabCounts,
      defaultRowCount: document.querySelectorAll('#windowProgressTable tr').length,
      hasType2: text.includes('Type2 回归多'),
      hasType4: text.includes('Type4 顺势空'),
      hasInactiveType1: text.includes('Type1 顺势多'),
      hasInactiveType3: text.includes('Type3 回归空'),
      hasCollectedByPath: visibleGroups.some((group) => group.key === 'type4_short_trend' && group.text.includes('下轨空头分形')),
      hasMissingByPath: visibleGroups.some((group) => group.key === 'type2_long_mr' && group.text.includes('下轨多头分形') && group.text.includes('多头背离')),
    };
  });

  await page.click('#windowProgressStatusTabs [data-window-status="blocked"]');
  await page.waitForFunction(() => {
    const text = document.querySelector('#windowProgressTable')?.textContent || '';
    return text.includes('BLKD') && !text.includes('CAND') && !text.includes('MOCK');
  });

  const blockedResult = await page.evaluate(() => ({
    tableText: document.querySelector('#windowProgressTable')?.textContent || '',
    cardText: document.querySelector('#windowProgressCards')?.textContent || '',
    metaText: document.querySelector('#windowProgressMeta')?.textContent || '',
    activeStatus: document.querySelector('#windowProgressStatusTabs .window-progress-status-tab.active')?.getAttribute('data-window-status'),
    url: window.location.href,
  }));

  await page.goto(`${CONSOLE_BASE}/ibkr_screener.html?environment=live&tab=screener&view=window-progress&window_status=active`, {
    waitUntil: 'domcontentloaded',
    timeout: 20000,
  });
  await page.waitForFunction(() => {
    const active = document.querySelector('#windowProgressStatusTabs .window-progress-status-tab.active')?.getAttribute('data-window-status');
    const text = document.querySelector('#windowProgressTable')?.textContent || '';
    return active === 'active' && text.includes('ACTV') && !text.includes('BLKD');
  });

  const deepLinkResult = await page.evaluate(() => ({
    tableText: document.querySelector('#windowProgressTable')?.textContent || '',
    cardText: document.querySelector('#windowProgressCards')?.textContent || '',
    activeStatus: document.querySelector('#windowProgressStatusTabs .window-progress-status-tab.active')?.getAttribute('data-window-status'),
    url: window.location.href,
  }));

  await browser.close();

  const failures = [];
  if (errors.length) failures.push(...errors);
  if (!result.hasType2) failures.push('missing_type2_group');
  if (!result.hasType4) failures.push('missing_type4_group');
  if (result.hasInactiveType1) failures.push('inactive_type1_visible');
  if (result.hasInactiveType3) failures.push('inactive_type3_visible');
  if (!result.hasCollectedByPath) failures.push('type4_collected_missing');
  if (!result.hasMissingByPath) failures.push('type2_missing_missing');
  if (result.defaultRowCount !== 6) failures.push(`unexpected_default_row_count:${result.defaultRowCount}`);
  const expectedCounts = { all: '6', candidate: '1', blocked: '1', near_expiry: '1', active: '1', no_window: '1', other: '1' };
  for (const [status, expected] of Object.entries(expectedCounts)) {
    if (result.tabCounts[status] !== expected) failures.push(`bad_tab_count:${status}:${result.tabCounts[status]}`);
  }
  if (blockedResult.activeStatus !== 'blocked') failures.push(`blocked_tab_not_active:${blockedResult.activeStatus}`);
  if (!blockedResult.tableText.includes('BLKD') || blockedResult.tableText.includes('CAND') || blockedResult.tableText.includes('MOCK')) {
    failures.push('blocked_table_filter_failed');
  }
  if (!blockedResult.cardText.includes('BLKD') || blockedResult.cardText.includes('CAND') || blockedResult.cardText.includes('MOCK')) {
    failures.push('blocked_mobile_filter_failed');
  }
  if (!blockedResult.metaText.includes('1/6 条') || !blockedResult.metaText.includes('当前 阻塞')) {
    failures.push(`blocked_meta_bad:${blockedResult.metaText}`);
  }
  if (!blockedResult.url.includes('window_status=blocked')) failures.push(`blocked_url_missing:${blockedResult.url}`);
  if (deepLinkResult.activeStatus !== 'active') failures.push(`deeplink_active_tab_bad:${deepLinkResult.activeStatus}`);
  if (!deepLinkResult.tableText.includes('ACTV') || deepLinkResult.tableText.includes('BLKD')) {
    failures.push('deeplink_active_filter_failed');
  }
  if (!deepLinkResult.cardText.includes('ACTV') || deepLinkResult.cardText.includes('BLKD')) {
    failures.push('deeplink_active_mobile_filter_failed');
  }

  const output = { ok: failures.length === 0, failures, result, blockedResult, deepLinkResult };
  console.log(JSON.stringify(output, null, 2));
  if (failures.length) process.exit(1);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
