const { chromium, devices } = require("playwright");

const BASE = process.env.PB_BASE || "https://pb.lzw-glory.top";
const ENVIRONMENT = process.env.IBKR_ENVIRONMENT || "live";

async function login(page) {
  await page.goto(`${BASE}/login.html`, { waitUntil: "domcontentloaded" });
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

async function inspectMonitor(browser, deviceName = null) {
  const context = deviceName ? await browser.newContext({ ...devices[deviceName] }) : await browser.newContext();
  const page = await context.newPage();
  const errors = [];

  page.on("pageerror", (error) => errors.push(`pageerror:${error.message}`));
  page.on("console", (msg) => {
    if (["error", "warning"].includes(msg.type())) {
      errors.push(`console:${msg.type()}:${msg.text()}`);
    }
  });
  page.on("response", (resp) => {
    if (resp.status() >= 400) {
      errors.push(`response:${resp.status()}:${resp.url()}`);
    }
  });

  await login(page);
  const target = `${BASE}/ibkr_monitor.html?environment=${encodeURIComponent(ENVIRONMENT)}`;
  await page.goto(target, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(1800);

  const heroText = await page.locator(".hero").innerText().catch(() => "");
  const sectionTitles = await page.locator(".section-title").allTextContents().catch(() => []);
  const navTexts = await page.locator("#nav .nav-item").allTextContents().catch(() => []);

  const summary = {
    url: target,
    device: deviceName || "desktop",
    title: await page.title(),
    navTexts,
    sectionTitles,
    hasApiSection: sectionTitles.includes("IBKR API 利用率"),
    hasSubscriptionSection: sectionTitles.includes("订阅视图"),
    hasHostSection: sectionTitles.includes("主机健康"),
    hasFlagsSection: sectionTitles.includes("当前告警"),
    hasHeroEnv: heroText.includes(ENVIRONMENT.toUpperCase()),
    hasRefreshButton: await page.locator("#refreshBtn").count().catch(() => 0),
    errors,
  };

  await context.close();
  return summary;
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const results = [];
  results.push(await inspectMonitor(browser));
  results.push(await inspectMonitor(browser, "iPhone 12"));
  console.log(JSON.stringify(results, null, 2));
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
