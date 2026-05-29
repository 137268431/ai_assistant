const fs = require('fs');
const path = require('path');

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
const signalsHtml = fs.readFileSync(path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'ibkr_signals.html'), 'utf8');
const signalsCss = fs.readFileSync(path.join(repoRoot, 'runtime', 'ibkr_console', 'static', 'assets', 'css', 'pages', 'ibkr_signals', 'page.css'), 'utf8');
const tradingviewRoot = path.join(repoRoot, 'tradingview');
const strategyPine = fs.readFileSync(path.join(tradingviewRoot, 'Signal_Strategy_Core[Glory].pine'), 'utf8');
const pinePayloadFiles = fs.readdirSync(tradingviewRoot)
  .filter((name) => /^(Signal_Strategy_Core|Signal_Alert_Core|Indicator_Audit_).*\.pine$/.test(name))
  .map((name) => path.join(tradingviewRoot, name));

const issues = [];
let checkCount = 0;
function assert(condition, issue) {
  checkCount += 1;
  if (!condition) issues.push(issue);
}

['bar_open_ms', 'bar_close_ms', 'pine_eval_ms', 'interval'].forEach((field) => {
  assert(strategyPine.includes(field), `strategy_missing_${field}`);
});
pinePayloadFiles.forEach((filePath) => {
  const text = fs.readFileSync(filePath, 'utf8');
  ['bar_open_ms', 'bar_close_ms', 'pine_eval_ms'].forEach((field) => {
    assert(text.includes(field), `pine_missing_${field}:${path.basename(filePath)}`);
  });
});
['getRecordBarCloseMs', 'getRecordPineEvalMs', 'latency_trace', 'Bar收盘→Pine执行', 'Pine执行→API收到', 'API收到→PB入库', 'PB入库→路由完成'].forEach((token) => {
  assert(signalsHtml.includes(token), `signals_missing_${token}`);
});
assert(signalsHtml.includes('event_type = "entry"'), 'signals_latency_not_scoped_to_entry');
assert(signalsHtml.includes("if (text.startsWith('chart='))"), 'signals_missing_chart_timeframe_parser');
assert(/grid-template-columns:\s*repeat\(4,\s*minmax\(150px,\s*1fr\)\)/.test(signalsCss), 'signals_latency_grid_not_four_columns');

if (issues.length) {
  console.error(JSON.stringify({ ok: false, issues }, null, 2));
  process.exit(1);
}
console.log(JSON.stringify({ ok: true, checks: checkCount }, null, 2));
