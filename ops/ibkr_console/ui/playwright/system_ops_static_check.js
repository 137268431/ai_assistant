const fs = require('fs');
const path = require('path');
const vm = require('vm');

function findRepoRoot(startDir) {
  let current = startDir;
  for (let i = 0; i < 8; i += 1) {
    if (fs.existsSync(path.join(current, 'runtime', 'ibkr_console', 'static'))) return current;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  throw new Error(`Unable to locate repo root from ${startDir}`);
}

const repoRoot = findRepoRoot(__dirname);
const staticRoot = path.join(repoRoot, 'runtime', 'ibkr_console', 'static');
const issues = [];
let checkCount = 0;

function readStatic(relativePath) {
  return fs.readFileSync(path.join(staticRoot, relativePath), 'utf8');
}

function listStaticFiles(relativeDir, extension) {
  const root = path.join(staticRoot, relativeDir);
  const files = [];
  function walk(dir) {
    fs.readdirSync(dir, { withFileTypes: true }).forEach((entry) => {
      const abs = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(abs);
      } else if (entry.isFile() && entry.name.endsWith(extension)) {
        files.push(path.relative(staticRoot, abs));
      }
    });
  }
  walk(root);
  return files;
}

function readPageScriptBundle(htmlRelativePath, scriptPrefix, fallbackRelativePath) {
  const html = readStatic(htmlRelativePath);
  const matches = Array.from(html.matchAll(/<script\s+[^>]*src="([^"]+)"[^>]*><\/script>/g));
  const scriptPaths = matches
    .map((match) => String(match[1] || '').split('?')[0].replace(/^\/+/, ''))
    .filter((src) => src.startsWith(scriptPrefix));
  if (!scriptPaths.length) return readStatic(fallbackRelativePath);
  return scriptPaths.map((src) => readStatic(src)).join('\n');
}

function assert(condition, issue) {
  checkCount += 1;
  if (!condition) issues.push(issue);
}

function includesAll(text, values) {
  return values.every((value) => text.includes(value));
}

function collectDataActions(text) {
  return Array.from(new Set(Array.from(text.matchAll(/data-action="([^"]+)"/g)).map((match) => match[1]).filter(Boolean)));
}

function collectNavLabels(html) {
  return Array.from(html.matchAll(/<span class="nav-icon">[\s\S]*?<\/span>([^<\n]+)/g))
    .map((match) => match[1].trim())
    .filter(Boolean);
}

function buildUiSandbox() {
  const localStore = new Map([['ibkr_environment', 'live']]);
  const localStorage = {
    getItem: (key) => localStore.get(key) || '',
    setItem: (key, value) => localStore.set(key, String(value)),
    removeItem: (key) => localStore.delete(key),
  };
  const sandbox = {
    window: {
      __IBKR_RUNTIME_CONFIG__: {},
      location: { origin: 'https://static-check.local', search: '?environment=live', pathname: '/ibkr_monitor.html' },
      localStorage,
      setTimeout: () => 0,
      clearTimeout: () => {},
    },
    document: {},
    localStorage,
    URL,
    URLSearchParams,
    Intl,
    Date,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readStatic('assets/js/shared/base.js'), sandbox, { filename: 'base.js' });
  vm.runInContext(readStatic('assets/js/shared/ui.js'), sandbox, { filename: 'ui.js' });
  vm.runInContext(readStatic('assets/js/shared/ibkr-page-config.js'), sandbox, { filename: 'ibkr-page-config.js' });
  return sandbox;
}

function buildPageUiSandbox() {
  const localStore = new Map([
    ['ibkr_environment', 'live'],
    ['ibkr_broker_mode', 'paper'],
    ['ibkr_market_data_mode', 'live'],
  ]);
  const localStorage = {
    getItem: (key) => localStore.get(key) || '',
    setItem: (key, value) => localStore.set(key, String(value)),
    removeItem: (key) => localStore.delete(key),
  };
  const location = {
    href: 'https://static-check.local/ibkr_runtime.html?environment=live&broker_mode=paper&market_data_mode=live',
    origin: 'https://static-check.local',
    search: '?environment=live&broker_mode=paper&market_data_mode=live',
    pathname: '/ibkr_runtime.html',
  };
  const sandbox = {
    window: {
      __IBKR_RUNTIME_CONFIG__: {},
      __ibkrBrokerModeContext: {
        broker_mode: 'paper',
        data_environment: 'live',
        market_data_environment: 'live',
      },
      location,
      localStorage,
      setTimeout: () => 0,
      clearTimeout: () => {},
      setInterval: () => 0,
      clearInterval: () => {},
    },
    document: {
      getElementById: () => null,
      querySelectorAll: () => [],
      body: { insertAdjacentHTML: () => {} },
    },
    localStorage,
    location,
    URL,
    URLSearchParams,
    Intl,
    Date,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readStatic('assets/js/shared/base.js'), sandbox, { filename: 'base.js' });
  vm.runInContext(readStatic('assets/js/shared/ui-page.js'), sandbox, { filename: 'ui-page.js' });
  return sandbox;
}

function renderSystemFreshnessFixture(freshnessPayload) {
  const sandbox = buildUiSandbox();
  const elements = new Map();
  sandbox.document.getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, { innerHTML: '' });
    return elements.get(id);
  };
  vm.runInContext(readStatic('assets/js/shared/ibkr-page-core.js'), sandbox, { filename: 'ibkr-page-core.js' });
  vm.runInContext(readStatic('assets/js/pages/ibkr_system/state-api.js'), sandbox, { filename: 'ibkr_system/state-api.js' });
  vm.runInContext(readStatic('assets/js/pages/ibkr_system/renderers.js'), sandbox, { filename: 'ibkr_system/renderers.js' });
  sandbox.renderFreshness(freshnessPayload);
  return elements.get('freshnessArea').innerHTML;
}

try {
  const ui = buildUiSandbox();
  const navHtml = ui.renderNav('/ibkr_monitor.html');
  const navLabels = collectNavLabels(navHtml);
  assert(navLabels.length === 6, `nav_label_count:${navLabels.length}`);
  assert(JSON.stringify(navLabels) === JSON.stringify(['首页', '执行', '标的', '复盘', '回测', '系统']), `nav_labels:${navLabels.join('/')}`);
  assert(!navLabels.includes('运维'), 'legacy_ops_bottom_nav_label');
  assert(/nav-item active/.test(navHtml) && navHtml.includes('系统'), 'system_nav_not_active_for_ops_page');
  assert(ui.getIbkrServiceHealthTone('idle') === 'neutral', 'shared_service_tone_idle_not_neutral');

  const reviewBridge = ui.renderReviewBridge('/ibkr_trade_review.html', { market_date: '2026-05-21', limit: '500' });
  assert(includesAll(reviewBridge, ['收益统计', '每日复盘', '生命周期']), 'review_bridge_missing_expected_labels');
  assert(/page-bridge-link active[\s\S]*每日复盘/.test(reviewBridge), 'review_bridge_trade_review_not_active');
  const executionBridge = ui.renderExecutionBridge('/ibkr_signals.html');
  assert(!executionBridge.includes('每日复盘') && !executionBridge.includes('生命周期'), 'execution_bridge_still_contains_review_domain');
  const systemBridge = ui.renderSystemBridge('/ibkr_config.html');
  assert(includesAll(systemBridge, ['总览', '运维', '控制台', '配置']), 'system_bridge_missing_expected_labels');
  const opsBridge = ui.renderOpsBridge('/ibkr_data_quality.html');
  assert(includesAll(opsBridge, ['总览', '运维', '控制台', '配置', '监控大盘', '预热', '数据质量', '历史重建']), 'ops_bridge_missing_expected_labels');
  const analyticsBridge = ui.renderAnalyticsBridge('/ibkr_chart.html');
  assert(includesAll(analyticsBridge, ['指标列表', '图表工作台', '统计']), 'analytics_bridge_missing_expected_labels');
  assert(/page-bridge-link active[\s\S]*图表工作台/.test(analyticsBridge), 'analytics_bridge_chart_not_active');
} catch (error) {
  issues.push(`ui_render_failed:${error.message}`);
}

try {
  const pageUi = buildPageUiSandbox();
  const contextMeta = pageUi.buildPageContextMetaItems([
    { label: '交易日', value: '2026-05-26' },
    { label: 'Broker', value: 'should-not-duplicate', includeInContext: true },
    { label: '链路', value: 'ready', tone: 'ok', includeInContext: true },
  ]);
  const contextHtml = pageUi.renderPageContextBar('测试', {
    description: 'Context regression',
    metaItems: [
      { label: '交易日', value: '2026-05-26' },
      { label: '链路', value: 'ready', tone: 'ok', includeInContext: true },
    ],
  });
  assert(contextMeta.some((item) => item.label === 'Broker' && item.value === 'PAPER'), 'page_context_missing_broker_chip');
  assert(contextMeta.some((item) => item.label === '数据' && item.value === 'Shared Data'), 'page_context_missing_data_chip');
  assert(contextMeta.some((item) => item.label === '交易日' && item.value.includes('2026-05-26')), 'page_context_missing_trading_date_chip');
  assert(contextMeta.some((item) => item.label === '链路' && item.value === 'ready'), 'page_context_missing_custom_chip');
  assert(!contextMeta.some((item) => item.label === 'Broker' && item.value === 'should-not-duplicate'), 'page_context_duplicate_base_chip');
  assert(includesAll(contextHtml, ['page-context-bar', '测试', 'Broker', 'PAPER', 'Shared Data', '交易日', '2026-05-26', '链路', 'ready']), 'page_context_render_missing_expected_copy');
  assert(typeof pageUi.getIbkrCacheProfile === 'function', 'cache_profile_helper_missing');
  assert(typeof pageUi.cachedPageJson === 'function', 'cached_page_json_missing');
  assert(typeof pageUi.cachedMarketCalendar === 'function', 'cached_market_calendar_missing');
  const cacheDate = pageUi.getCurrentEtDateString();
  const calendarCache = pageUi.getMarketCalendarCacheOptions({
    ok: true,
    market_date: cacheDate,
    market_session: { kind: 'regular' },
  }, { date: cacheDate });
  assert(calendarCache.ttlMs === 30000 && calendarCache.swrMs >= 60000, `market_calendar_active_cache_profile:${JSON.stringify(calendarCache)}`);
} catch (error) {
  issues.push(`page_context_render_failed:${error.message}`);
}

const commonCss = readStatic('assets/css/common.css');
assert(!commonCss.includes('var(--nav-count, 7)'), 'common_css_legacy_nav_default_7');
assert(commonCss.includes('var(--nav-count, 6)'), 'common_css_missing_nav_default_6');
assert(/--page-shell-max:\s*100%;/.test(commonCss), 'common_css_page_shell_not_fluid');
assert(/--page-shell-max-wide:\s*100%;/.test(commonCss), 'common_css_page_shell_wide_not_fluid');
const fixedShellMaxWidth = /\.(?:page-shell|home-shell|content|lifecycle-shell|account-shell)(?![-\w])[^{}]*\{[^}]*max-width\s*:\s*\d+px/gs;
listStaticFiles('assets/css', '.css').forEach((relativePath) => {
  const fixedMatches = Array.from(readStatic(relativePath).matchAll(fixedShellMaxWidth));
  assert(fixedMatches.length === 0, `fixed_shell_max_width:${relativePath}:${fixedMatches.length}`);
});

const indexHtml = readStatic('index.html');
assert(indexHtml.includes('id="actionConfigLink"'), 'home_missing_config_entry');
assert(indexHtml.includes('/ibkr_config.html'), 'home_config_entry_missing_href');
assert(includesAll(indexHtml, ["services['ibkr-backtest']", "name: 'Backtest'"]), 'home_stack_missing_backtest_service_row');
assert(includesAll(indexHtml, ['id="reverseSignalsBreakdown"', 'pending_by_action', "status: 'pending'"]), 'home_actions_missing_pending_breakdown');

const executionActionsHtml = readStatic('ibkr_execution_actions.html');
const executionActionsCss = readStatic('assets/css/pages/ibkr_reverse_signals/page.css');
assert(includesAll(executionActionsHtml, ['id="actionTabs"', 'function renderActionTabs', 'normalizeActionGroup', "urlParams.get('action')", 'updateActionCounts']), 'execution_actions_missing_action_filter_ui');
assert(includesAll(executionActionsHtml, ['adjust_bracket', "data-action=\"${action}\"", "currentAction !== 'all'"]), 'execution_actions_missing_action_grouping_logic');
assert(includesAll(executionActionsCss, ['.action-tabs', 'top: 100px']), 'execution_actions_missing_action_tab_styles');

const chartCss = readStatic('assets/css/pages/ibkr_chart/page.css');
const chartJs = readPageScriptBundle(
  'ibkr_chart.html',
  'assets/js/pages/ibkr_chart/',
  'assets/js/pages/ibkr_chart/page.js',
);
assert(chartJs.includes("renderAnalyticsBridge('/ibkr_chart.html')"), 'chart_missing_analytics_bridge_render');
assert(!chartJs.includes("document.getElementById('pageBridge').innerHTML = ''"), 'chart_still_clears_page_bridge');
assert(!/(^|})\s*#pageBridge\s*\{\s*display:\s*none;\s*\}/m.test(chartCss), 'chart_page_bridge_hidden_globally');

const systemHtml = readStatic('ibkr_system.html');
const systemCss = readStatic('assets/css/pages/ibkr_system/page.css');
const systemJs = readPageScriptBundle(
  'ibkr_system.html',
  'assets/js/pages/ibkr_system/',
  'assets/js/pages/ibkr_system/page.js',
);
assert(systemHtml.includes('运维摘要'), 'system_missing_ops_summary_section');
assert(/\.status-bar\s*\{[^}]*padding:\s*0 0 6px;/.test(systemCss), 'system_status_bar_has_extra_horizontal_gutter');
assert(/\.section\s*\{[^}]*margin:\s*0 0 10px;/.test(systemCss), 'system_section_has_extra_horizontal_gutter');
assert(/\.overview-columns\s*\{[^}]*padding:\s*0 0 10px;/.test(systemCss), 'system_overview_columns_has_extra_horizontal_gutter');
assert(systemJs.includes('ops-summary-link') && systemJs.includes('/ibkr_monitor.html'), 'system_summary_missing_monitor_link');
assert(includesAll(systemJs, ["'ibkr-backtest'", 'Backtest Service', 'backtestIdle', 'IB client', 'IB Clients']), 'system_summary_missing_backtest_idle_or_client_copy');
assert(includesAll(systemJs, ['lastStableIbkrDataHealth', 'preserveIbkrData', 'loadingOnMissingIbkrData', "status: 'loading'"]), 'system_missing_ibkr_data_stable_cache');
assert(includesAll(systemJs, ['lastStableTodayStats', 'rememberStableTodayStats', 'mergeTodayStats', 'summaryTodayStats']), 'system_missing_today_stats_stable_cache');
assert(includesAll(systemJs, ['dataEnvFilterBase', 'brokerEnvFilterBase', 'count:ibkr_indicators', 'swrMs: 300000']), 'system_today_stats_missing_split_env_or_indicator_cache');
assert(systemJs.includes('summaryLite?.data_freshness'), 'system_freshness_not_using_summary_lite_payload');
assert(includesAll(systemJs, ['ready_pct', 'coverage_pct', 'waiting_5m', 'not_due', 'quiet_extended', 'sample_lag_symbols']), 'system_freshness_missing_aggregate_fields');
assert(!systemJs.includes('freshnessIntervals.map'), 'system_freshness_still_fetches_interval_rows');
assert(!systemJs.includes('freshness:'), 'system_freshness_still_has_pb_fetch_labels');
assert(includesAll(systemJs, ['freshness-overall', 'freshness-reasons', '应更新', '异常样例']), 'system_freshness_missing_aggregate_rendering');
try {
  const idleDailyHtml = renderSystemFreshnessFixture({
    overall: {
      status: 'ready',
      ready_pct: 100,
      coverage_pct: 100,
      total_checks: 254,
      due_checks: 127,
      total_symbols: 127,
      ready: 127,
      not_due: 127,
    },
    intervals: {
      '5m': {
        interval: '5m',
        status: 'ready',
        ready_pct: 100,
        coverage_pct: 100,
        total_symbols: 127,
        due_symbols: 127,
        ready: 127,
        expected_close_us: '2026-05-21 13:25:00',
      },
      '1d': {
        interval: '1d',
        status: 'not_due',
        ready_pct: 100,
        coverage_pct: 100,
        total_symbols: 127,
        due_symbols: 0,
        ready: 0,
        not_due: 127,
        expected_close_us: '2026-05-20 20:00:00',
      },
    },
    scopes: {
      daily_1d: {
        name: 'daily_1d',
        label: 'Daily 1d',
        critical: false,
        symbols_total: 127,
        overall: {
          status: 'not_due',
          ready_pct: 100,
          coverage_pct: 100,
          total_checks: 127,
          due_checks: 0,
          total_symbols: 127,
          ready: 0,
          not_due: 127,
        },
      },
    },
  });
  assert((idleDailyHtml.match(/NOT DUE/g) || []).length >= 2, 'system_freshness_idle_1d_missing_not_due');
  assert(idleDailyHtml.includes('<strong>due</strong>0/127'), 'system_freshness_idle_1d_missing_due_reason');
  assert(idleDailyHtml.includes('<strong>not due</strong>127'), 'system_freshness_idle_1d_missing_not_due_reason');
  assert(!/freshness-percent">100%<\/span>\s*<span class="freshness-state"[^>]*>未到周期/.test(idleDailyHtml), 'system_freshness_idle_percent_shows_100');
  assert(/freshness-percent">--<\/span>\s*<span class="freshness-state"[^>]*>未到周期/.test(idleDailyHtml), 'system_freshness_idle_percent_missing_placeholder');
  assert(/<span class="freshness-label">5m<\/span>[\s\S]*?<span class="freshness-percent">100%<\/span>[\s\S]*?正常/.test(idleDailyHtml), 'system_freshness_ready_5m_percent_missing');
} catch (error) {
  issues.push(`system_freshness_idle_render_failed:${error.message}`);
}

const screenerCss = readStatic('assets/css/pages/ibkr_screener/page.css');
assert(!/screener-domain-bridge[\s\S]{0,240}page-bridge-copy[\s\S]{0,80}display:\s*none/.test(screenerCss), 'screener_bridge_copy_hidden');
assert(!/screener-domain-tab\.page-bridge-link[\s\S]{0,120}min-height:\s*42px/.test(screenerCss), 'screener_bridge_compact_height');

const runtimeHtml = readStatic('ibkr_runtime.html');
const runtimeContextFixVersion = '20260529-tv-primary-lean-1';
const commonJs = readStatic('common.js');
assert(commonJs.includes(`sharedBundleVersion = '${runtimeContextFixVersion}'`), 'common_missing_runtime_context_fix_cachebuster');
assert(runtimeHtml.includes(`common.js?v=${runtimeContextFixVersion}`), 'runtime_html_missing_runtime_context_fix_cachebuster');
assert(commonJs.includes('cachedPageJson') && commonJs.includes('cachedMarketCalendar'), 'common_missing_cache_helper_exports');
assert(!commonJs.includes('20260526-market-calendar-context'), 'common_still_has_market_calendar_cachebuster');
assert(!runtimeHtml.includes('common.js?v=20260526-market-calendar-context'), 'runtime_html_still_has_market_calendar_cachebuster');
const runtimeCss = readStatic('assets/css/pages/ibkr_runtime/page.css');
const runtimeJs = readPageScriptBundle(
  'ibkr_runtime.html',
  'assets/js/pages/ibkr_runtime/',
  'assets/js/pages/ibkr_runtime/page.js',
);
assert(runtimeHtml.includes('操作前链路摘要'), 'runtime_missing_link_summary_title');
assert(runtimeJs.includes('runtime-link-action') && runtimeJs.includes('/ibkr_monitor.html'), 'runtime_summary_missing_monitor_link');
assert(includesAll(runtimeJs, ['History Fetch', 'Watchlist Topup', 'Canonical Trace', 'History Active Requests']), 'runtime_missing_history_watchlist_monitoring');
assert(runtimeHtml.includes('核心模块控制') && runtimeHtml.includes('id="serviceControlGrid"'), 'runtime_missing_service_control_panel');
assert(runtimeJs.includes('/api/custom/ibkr/services/action'), 'runtime_missing_service_action_api');
assert(includesAll(runtimeJs, ['ibkr-runtime', 'ibkr-gateway', 'ibkr-compute', 'ibkr-backtest', 'ibkr-scheduler']), 'runtime_service_control_missing_core_services');
assert(includesAll(runtimeJs, ["service: 'ibkr-backtest'", 'Backtest Service']), 'runtime_service_control_missing_backtest_card');
assert(includesAll(runtimeJs, ['shouldShowServiceStopAction', '停止服务', "'stop'"]), 'runtime_service_control_missing_stop_action');
assert(includesAll(runtimeJs, ['activeRuntimeOperation', 'RUNTIME_OPERATION_WATCH_ACTIONS', 'startRuntimeOperationWatch', 'deriveRuntimeOperationProgress', 'getActiveRuntimeOperationLockReason']), 'runtime_missing_operation_watch_state');
assert(includesAll(runtimeJs, ['请求可能已提交', '正在追踪 Gateway / 2FA / Session 状态', '追踪中，请勿重复点击']), 'runtime_missing_operation_watch_copy');
assert(includesAll(runtimeCss, ['runtime-operation-watch', 'runtime-operation-steps', 'runtimeOperationPulse']), 'runtime_missing_operation_watch_styles');
assert(runtimeHtml.includes('20260601-operation-watch-1'), 'runtime_missing_operation_watch_cachebuster');
assert(includesAll(runtimeHtml, ['appLoginHandoffSection', 'action-login-primary', 'runtimeFlowToggleButton', 'runtime-secondary-actions', '全部急停', '恢复运行开关', 'runtime-control-danger']), 'runtime_trimmed_controls_missing');
assert(runtimeJs.includes('recover_all') && runtimeJs.includes('/api/custom/ibkr/recover'), 'runtime_missing_recover_action');
const runtimeExpectedStaticActions = ['app_login_handoff', 'start', 'probe', 'compute', 'emergency_all', 'recover_all'];
const runtimeUnexpectedStaticActions = collectDataActions(runtimeHtml).filter((action) => !runtimeExpectedStaticActions.includes(action));
assert(runtimeUnexpectedStaticActions.length === 0, `runtime_unexpected_static_actions:${runtimeUnexpectedStaticActions.join(',')}`);
['刷新飞书 2FA 卡片', '开始人工接管', '结束人工接管', '执行 Scan', '重算历史缓存', '急停运行线程', '停止 IB Gateway 服务', '关闭计算调度', '关闭 Bars 写入', '关闭 Scheduler', '关闭交易执行', '放弃当前轮次并干净重开'].forEach((label) => {
  assert(!runtimeHtml.includes(label), `runtime_removed_button_still_visible:${label}`);
});
['scan: {', 'recompute: {', 'gateway_stop:', 'takeover_on:', 'takeover_off:', 'emergency_compute:', 'emergency_publish:', 'emergency_scheduler:', 'emergency_trading:'].forEach((snippet) => {
  assert(!runtimeJs.includes(snippet), `runtime_removed_action_map_still_present:${snippet}`);
});

const monitorHtml = readStatic('ibkr_monitor.html');
assert(monitorHtml.includes('ops-route-panel'), 'monitor_missing_ops_route_panel');
assert(includesAll(monitorHtml, ['监控大盘', '预热', '数据质量', '历史重建']), 'monitor_ops_route_missing_labels');
assert(includesAll(monitorHtml, ['其它市场监控', '关键监控指标带', 'marketOverviewGrid', 'criticalMetricsGrid']), 'monitor_missing_market_or_critical_metrics');
assert(includesAll(monitorHtml, ['History Fetch', 'Watchlist Topup', 'Backfill Trace', 'Watchlist Load']), 'monitor_missing_history_watchlist_cards');
assert(monitorHtml.includes("'ibkr-backtest'"), 'monitor_service_order_missing_backtest');

const warmupHtml = readStatic('ibkr_warmup.html');
assert(warmupHtml.includes('warmup-guide-section'), 'warmup_missing_guide_section');
assert(warmupHtml.includes('warmup-link-action') && warmupHtml.includes('/ibkr_monitor.html'), 'warmup_missing_monitor_link');
assert(includesAll(warmupHtml, ['交易闸门', '数据质量', '历史重建']), 'warmup_guide_missing_boundaries');

const qualityHtml = readStatic('ibkr_data_quality.html');
assert(qualityHtml.includes('quality-route-panel'), 'quality_missing_route_panel');
assert(includesAll(qualityHtml, ['日常安全修复入口', '预热', '历史重建']), 'quality_route_missing_boundaries');

const historyHtml = readStatic('ibkr_history_rebuild.html');
assert(historyHtml.includes('rebuild-guard-panel'), 'history_missing_guard_panel');
assert(historyHtml.includes('id="confirmFullRebuild"'), 'history_missing_confirm_checkbox');
assert(/id="startButton"[^>]*disabled/.test(historyHtml), 'history_full_rebuild_not_disabled_by_default');
assert(includesAll(historyHtml, ['最后恢复手段', '预热', '数据质量']), 'history_guard_missing_boundaries');

if (issues.length) {
  console.error(JSON.stringify({ ok: false, issues }, null, 2));
  process.exit(1);
}

console.log(JSON.stringify({ ok: true, checks: checkCount, staticRoot }, null, 2));
