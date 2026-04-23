const { chromium } = require('playwright');

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

async function buildContext(browser, token) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 },
    ignoreHTTPSErrors: true,
  });
  await context.addInitScript((savedToken) => {
    localStorage.setItem('pb_token', savedToken);
  }, token);
  return context;
}

function hasFailures(item) {
  return !item?.ok || (Array.isArray(item?.errors) && item.errors.length > 0);
}

async function withPage(browser, token, runner) {
  const context = await buildContext(browser, token);
  const page = await context.newPage();
  const errors = [];

  page.on('pageerror', (err) => errors.push(`pageerror:${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console:error:${msg.text()}`);
  });
  page.on('response', (resp) => {
    if (resp.status() >= 500) errors.push(`response:${resp.status()}:${resp.url()}`);
  });

  try {
    const result = await runner(page, errors);
    result.errors = errors;
    return result;
  } finally {
    await context.close();
  }
}

async function inspectChartWheel(page) {
  await page.goto(`${CONSOLE_BASE}/ibkr_chart.html?environment=live&symbol=ZTS&interval=5m&range=1d&trace=1`, {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  });
  await page.waitForFunction(() => {
    const dom = document.getElementById('chartCanvas');
    return dom && window.echarts && window.echarts.getInstanceByDom(dom);
  }, { timeout: 30000 });
  await page.waitForTimeout(1200);

  const canvas = page.locator('#chartCanvas');
  await canvas.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  const box = await canvas.boundingBox();
  if (!box) throw new Error('chart_canvas_box_missing');

  const before = await page.evaluate(() => {
    const dom = document.getElementById('chartCanvas');
    const inst = window.echarts.getInstanceByDom(dom);
    const zoom = inst.getOption().dataZoom?.[0] || {};
    return {
      windowScrollY: window.scrollY,
      zoomStart: zoom.start,
      zoomEnd: zoom.end,
      zoomOnMouseWheel: zoom.zoomOnMouseWheel,
      moveOnMouseWheel: zoom.moveOnMouseWheel,
    };
  });

  await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 240));
  for (let i = 0; i < 3; i += 1) {
    await page.mouse.wheel(0, 900);
    await page.waitForTimeout(220);
  }

  const after = await page.evaluate(() => {
    const dom = document.getElementById('chartCanvas');
    const inst = window.echarts.getInstanceByDom(dom);
    const zoom = inst.getOption().dataZoom?.[0] || {};
    return {
      windowScrollY: window.scrollY,
      zoomStart: zoom.start,
      zoomEnd: zoom.end,
    };
  });

  return {
    scenario: 'ibkr_chart_canvas_wheel',
    url: page.url(),
    before,
    after,
    windowDelta: after.windowScrollY - before.windowScrollY,
    zoomChanged: before.zoomStart !== after.zoomStart || before.zoomEnd !== after.zoomEnd,
    ok: before.zoomOnMouseWheel === false
      && before.moveOnMouseWheel === false
      && (after.windowScrollY - before.windowScrollY) >= 120
      && before.zoomStart === after.zoomStart
      && before.zoomEnd === after.zoomEnd,
  };
}

async function waitForIndicatorListReady(page) {
  await page.waitForFunction(() => {
    const overlayVisible = Array.from(document.querySelectorAll('.page-loading-overlay')).some((node) => {
      const style = window.getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && style.opacity !== '0'
        && rect.width > 1
        && rect.height > 1;
    });
    const buttons = Array.from(document.querySelectorAll('.action-btn.btn-chart, .table-action-btn')).filter((node) => {
      const rect = node.getBoundingClientRect();
      const style = window.getComputedStyle(node);
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && rect.width > 20
        && rect.height > 20
        && /快览/.test(node.textContent || '');
    });
    return !overlayVisible && buttons.length > 0;
  }, { timeout: 30000 });
}

async function inspectIndicatorQuickviewWheel(page) {
  await page.goto(`${CONSOLE_BASE}/ibkr_indicators.html?environment=live`, {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  });
  await waitForIndicatorListReady(page);
  await page.locator('.action-btn.btn-chart, .table-action-btn').filter({ hasText: '快览' }).first().click();
  await page.waitForFunction(() => {
    const modal = document.getElementById('indicatorChartModal');
    const canvas = document.getElementById('indicatorChartCanvas');
    const content = document.querySelector('.chart-modal-content');
    return modal
      && modal.classList.contains('show')
      && canvas
      && content
      && window.echarts
      && window.echarts.getInstanceByDom(canvas);
  }, { timeout: 30000 });
  await page.waitForTimeout(1200);

  const canvas = page.locator('#indicatorChartCanvas');
  const box = await canvas.boundingBox();
  if (!box) throw new Error('indicator_chart_canvas_box_missing');

  const before = await page.evaluate(() => {
    const canvas = document.getElementById('indicatorChartCanvas');
    const inst = window.echarts.getInstanceByDom(canvas);
    const zoom = inst.getOption().dataZoom?.[0] || {};
    const modalContent = document.querySelector('.chart-modal-content');
    return {
      modalScrollTop: modalContent ? modalContent.scrollTop : null,
      modalScrollHeight: modalContent ? modalContent.scrollHeight : null,
      modalClientHeight: modalContent ? modalContent.clientHeight : null,
      zoomStart: zoom.start,
      zoomEnd: zoom.end,
      zoomOnMouseWheel: zoom.zoomOnMouseWheel,
      moveOnMouseWheel: zoom.moveOnMouseWheel,
    };
  });

  await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 240));
  for (let i = 0; i < 3; i += 1) {
    await page.mouse.wheel(0, 900);
    await page.waitForTimeout(220);
  }

  const after = await page.evaluate(() => {
    const canvas = document.getElementById('indicatorChartCanvas');
    const inst = window.echarts.getInstanceByDom(canvas);
    const zoom = inst.getOption().dataZoom?.[0] || {};
    const modalContent = document.querySelector('.chart-modal-content');
    return {
      modalScrollTop: modalContent ? modalContent.scrollTop : null,
      zoomStart: zoom.start,
      zoomEnd: zoom.end,
    };
  });

  return {
    scenario: 'ibkr_indicators_modal_wheel',
    url: page.url(),
    before,
    after,
    modalDelta: (after.modalScrollTop ?? 0) - (before.modalScrollTop ?? 0),
    zoomChanged: before.zoomStart !== after.zoomStart || before.zoomEnd !== after.zoomEnd,
    ok: before.zoomOnMouseWheel === false
      && before.moveOnMouseWheel === false
      && (after.modalScrollTop - before.modalScrollTop) >= 80
      && before.zoomStart === after.zoomStart
      && before.zoomEnd === after.zoomEnd,
  };
}

function printSummary(results) {
  const failed = results.filter(hasFailures);
  const status = failed.length ? 'FAILED' : 'OK';
  const lines = [
    `[wheel-scroll-check] ${status} scenarios=${results.length} failed=${failed.length}`,
  ];
  failed.forEach((item) => {
    lines.push(`[wheel-scroll-check] issue ${item.scenario} -> ${item.errors?.[0] || 'assertion_failed'}`);
  });
  process.stderr.write(`${lines.join('\n')}\n`);
}

async function runWheelScrollSurfaceCheck() {
  const token = await auth();
  const browser = await chromium.launch({ headless: true });
  try {
    const results = [
      await withPage(browser, token, async (page) => inspectChartWheel(page)),
      await withPage(browser, token, async (page) => inspectIndicatorQuickviewWheel(page)),
    ];
    return {
      ok: !results.some(hasFailures),
      checked_at: new Date().toISOString(),
      results,
    };
  } finally {
    await browser.close().catch(() => {});
  }
}

module.exports = {
  runWheelScrollSurfaceCheck,
};

if (require.main === module) {
  runWheelScrollSurfaceCheck()
    .then((output) => {
      printSummary(output.results || []);
      console.log(JSON.stringify(output.results || [], null, 2));
      if (!output.ok) process.exitCode = 1;
    })
    .catch((error) => {
      process.stderr.write(`[wheel-scroll-check] fatal ${error.message}\n`);
      process.exit(1);
    });
}
