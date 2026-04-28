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

function readStatic(relativePath) {
  return fs.readFileSync(path.join(staticRoot, relativePath), 'utf8');
}

function assert(condition, issue) {
  if (!condition) issues.push(issue);
}

function includesAll(text, values) {
  return values.every((value) => text.includes(value));
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
  return sandbox;
}

try {
  const ui = buildUiSandbox();
  const navHtml = ui.renderNav('/ibkr_monitor.html');
  const navLabels = collectNavLabels(navHtml);
  assert(navLabels.length === 6, `nav_label_count:${navLabels.length}`);
  assert(JSON.stringify(navLabels) === JSON.stringify(['首页', '执行', '标的', '研究', '回测', '系统']), `nav_labels:${navLabels.join('/')}`);
  assert(!navLabels.includes('运维'), 'legacy_ops_bottom_nav_label');
  assert(/nav-item active/.test(navHtml) && navHtml.includes('系统'), 'system_nav_not_active_for_ops_page');

  const systemBridge = ui.renderSystemBridge('/ibkr_config.html');
  assert(includesAll(systemBridge, ['总览', '运维', '控制台', '配置']), 'system_bridge_missing_expected_labels');
  const opsBridge = ui.renderOpsBridge('/ibkr_data_quality.html');
  assert(includesAll(opsBridge, ['总览', '运维', '控制台', '配置', '监控大盘', '预热', '数据质量', '历史重建']), 'ops_bridge_missing_expected_labels');
} catch (error) {
  issues.push(`ui_render_failed:${error.message}`);
}

const commonCss = readStatic('assets/css/common.css');
assert(!commonCss.includes('var(--nav-count, 7)'), 'common_css_legacy_nav_default_7');
assert(commonCss.includes('var(--nav-count, 6)'), 'common_css_missing_nav_default_6');

const indexHtml = readStatic('index.html');
assert(indexHtml.includes('id="actionConfigLink"'), 'home_missing_config_entry');
assert(indexHtml.includes('/ibkr_config.html'), 'home_config_entry_missing_href');

const systemHtml = readStatic('ibkr_system.html');
const systemJs = readStatic('assets/js/pages/ibkr_system/page.js');
assert(systemHtml.includes('运维摘要'), 'system_missing_ops_summary_section');
assert(systemJs.includes('ops-summary-link') && systemJs.includes('/ibkr_monitor.html'), 'system_summary_missing_monitor_link');

const runtimeHtml = readStatic('ibkr_runtime.html');
const runtimeJs = readStatic('assets/js/pages/ibkr_runtime/page.js');
assert(runtimeHtml.includes('操作前链路摘要'), 'runtime_missing_link_summary_title');
assert(runtimeJs.includes('runtime-link-action') && runtimeJs.includes('/ibkr_monitor.html'), 'runtime_summary_missing_monitor_link');

const monitorHtml = readStatic('ibkr_monitor.html');
assert(monitorHtml.includes('ops-route-panel'), 'monitor_missing_ops_route_panel');
assert(includesAll(monitorHtml, ['监控大盘', '预热', '数据质量', '历史重建']), 'monitor_ops_route_missing_labels');

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

console.log(JSON.stringify({ ok: true, checks: 25, staticRoot }, null, 2));
