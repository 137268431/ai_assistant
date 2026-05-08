const { chromium, devices } = require("playwright");

const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';

async function login(page) {
  await page.goto(`${CONSOLE_BASE}/login.html`, { waitUntil: "domcontentloaded" });
  if (page.url().includes("/ibkr_") || page.url().includes("/index.html")) return;
  const email = page.locator('input[type="email"], input[name="identity"]');
  const password = page.locator('input[type="password"]');
  if (await email.count()) {
    await email.first().fill("137268431@qq.com");
    await password.first().fill("Asd@2750066");
    const loginButton = page.locator('button:has-text("登录"), button:has-text("Login"), button[type="submit"]');
    await loginButton.first().click();
    await page.waitForTimeout(2000);
  }
}

async function inspectPage(browser, url, deviceName = null) {
  const context = deviceName ? await browser.newContext({ ...devices[deviceName] }) : await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(`pageerror:${e.message}`));
  page.on("console", msg => {
    if (["error", "warning"].includes(msg.type())) errors.push(`console:${msg.type()}:${msg.text()}`);
  });
  page.on("response", resp => {
    if (resp.status() >= 400) errors.push(`response:${resp.status()}:${resp.url()}`);
  });

  await login(page);
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1500);

  const title = await page.title();
  const navTexts = await page.locator("#nav .nav-item").allTextContents();
  const bridgeTexts = await page.locator("#pageBridge .page-bridge-link").allTextContents();
  const contextText = await page.locator("#contextBar").innerText().catch(() => "");
  const bodyText = await page.locator("body").innerText();
  const serviceControlText = await page.locator("#serviceControlGrid").innerText().catch(() => "");
  const serviceControlCount = await page.locator("#serviceControlGrid .service-control-card").count().catch(() => 0);
  const path = new URL(url).pathname;

  if (path === "/ibkr_runtime.html") {
    if (serviceControlCount < 5) errors.push(`runtime_service_control_count:${serviceControlCount}`);
    if (!serviceControlText.includes("ibkr-backtest")) errors.push("runtime_service_control_missing_ibkr_backtest");
  }

  const summary = {
    url,
    device: deviceName || "desktop",
    title,
    navCount: navTexts.length,
    navTexts,
    bridgeCount: bridgeTexts.length,
    bridgeTexts,
    contextText,
    hasGatewayActive: /Gateway\s+ACTIVE|Gateway\s+RUNNING/.test(bodyText),
    hasChallengeHint: bodyText.includes("仅在手机上点确认不会完成验证") || bodyText.includes("Challenge/Response"),
    serviceControlCount,
    hasBacktestServiceControl: serviceControlText.includes("ibkr-backtest"),
    errors,
  };

  await context.close();
  return summary;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const results = [];
  results.push(await inspectPage(browser, `${CONSOLE_BASE}/ibkr_runtime.html?environment=live`));
  results.push(await inspectPage(browser, `${CONSOLE_BASE}/ibkr_system.html?environment=live`));
  results.push(await inspectPage(browser, `${CONSOLE_BASE}/ibkr_runtime.html?environment=live`, "iPhone 12"));
  console.log(JSON.stringify(results, null, 2));
  await browser.close();
})();
