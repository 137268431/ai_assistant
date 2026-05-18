const fs = require('fs');
const path = require('path');
const { chromium, devices, request } = require('playwright');

const PB_BASE = process.env.PB_AUTH_BASE_URL || process.env.PB_BASE_URL || 'https://pb.lzw-glory.top';
const CONSOLE_BASE = process.env.CONSOLE_BASE_URL || process.env.QUANT_BASE_URL || process.env.PB_PAGE_BASE_URL || process.env.PB_BASE || 'https://quant.lzw-glory.top';
const EMAIL = process.env.PB_EMAIL || '137268431@qq.com';
const PASSWORD = process.env.PB_PASSWORD || 'Asd@2750066';
const ENVIRONMENT = process.env.IBKR_ENVIRONMENT || 'live';
const TIMEOUT_MS = Number(process.env.PB_SMOKE_NAV_TIMEOUT_MS || 20000);
const ARTIFACT_DIR = process.env.PB_SMOKE_ARTIFACT_DIR || '/tmp/ai_assistant_pb_smoke';
const VISUAL_WAIT_MS = Number(process.env.VISUAL_WAIT_MS || 1400);
const DESKTOP_VIEWPORT_WIDTH = Number(process.env.VISUAL_DESKTOP_WIDTH || 2048);
const DESKTOP_VIEWPORT_HEIGHT = Number(process.env.VISUAL_DESKTOP_HEIGHT || 1100);

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

function getHtmlTargets() {
  if (process.env.VISUAL_TARGETS) {
    return process.env.VISUAL_TARGETS
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return fs.readdirSync(staticRoot)
    .filter((name) => name.endsWith('.html'))
    .filter((name) => name !== 'login.html')
    .sort();
}

async function fetchToken() {
  const api = await request.newContext({
    baseURL: PB_BASE,
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: { 'Content-Type': 'application/json' },
  });

  try {
    const response = await api.post('/api/collections/_superusers/auth-with-password', {
      data: { identity: EMAIL, password: PASSWORD },
      timeout: TIMEOUT_MS,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok() || !payload?.token) {
      throw new Error(`auth_failed:${response.status()}:${JSON.stringify(payload)}`);
    }
    return payload.token;
  } finally {
    await api.dispose();
  }
}

function createTargetUrl(fileName) {
  const joiner = fileName.includes('?') ? '&' : '?';
  return `${CONSOLE_BASE.replace(/\/$/, '')}/${fileName}${joiner}environment=${encodeURIComponent(ENVIRONMENT)}`;
}

async function inspectPage(browser, token, fileName, mobile = false) {
  const deviceLabel = mobile ? 'mobile' : 'desktop';
  const context = mobile
    ? await browser.newContext({ ...devices['iPhone 12'], ignoreHTTPSErrors: true })
    : await browser.newContext({ viewport: { width: DESKTOP_VIEWPORT_WIDTH, height: DESKTOP_VIEWPORT_HEIGHT }, ignoreHTTPSErrors: true });
  await context.addInitScript((savedToken) => {
    localStorage.setItem('pb_token', savedToken);
  }, token);

  const page = await context.newPage();
  const errors = [];

  try {
    await page.goto(createTargetUrl(fileName), { waitUntil: 'domcontentloaded', timeout: TIMEOUT_MS });
  } catch (error) {
    errors.push(`goto:${error.message}`);
  }
  await page.waitForTimeout(mobile ? Math.max(900, VISUAL_WAIT_MS - 250) : VISUAL_WAIT_MS);

  const audit = await page.evaluate(({ targetFile, isMobile }) => {
    const defaultColors = new Set(['rgb(0, 0, 238)', 'rgb(85, 26, 139)']);
    const specialSelectors = [
      '.ops-route-panel',
      '.ops-summary-shell',
      '.quality-route-panel',
      '.rebuild-guard-panel',
      '.warmup-guide-card',
      '.warmup-link-summary',
      '.runtime-link-summary',
      '.service-topology-shell',
      '.config-action-bar',
      '.config-toolbar',
    ];

    function visible(el) {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 2 && rect.height > 2 && style.visibility !== 'hidden' && style.display !== 'none';
    }

    function hasStyledSurface(style) {
      const borderWidth = parseFloat(style.borderTopWidth || '0') || 0;
      const radius = parseFloat(style.borderTopLeftRadius || '0') || 0;
      return borderWidth > 0
        || radius > 0
        || style.backgroundImage !== 'none'
        || !['rgba(0, 0, 0, 0)', 'transparent'].includes(style.backgroundColor);
    }

    function defaultishLink(anchor) {
      const style = getComputedStyle(anchor);
      return defaultColors.has(style.color)
        || (style.textDecorationLine.includes('underline') && defaultColors.has(style.color));
    }

    const defaultLinks = Array.from(document.querySelectorAll('a'))
      .filter(visible)
      .filter(defaultishLink)
      .slice(0, 12)
      .map((anchor) => ({
        text: String(anchor.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
        href: anchor.getAttribute('href') || '',
        className: String(anchor.className || ''),
        color: getComputedStyle(anchor).color,
      }));

    const panelIssues = [];
    specialSelectors.forEach((selector) => {
      document.querySelectorAll(selector).forEach((node, index) => {
        if (!visible(node)) return;
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        const nestedDefaultLinks = Array.from(node.querySelectorAll('a'))
          .filter(visible)
          .filter(defaultishLink)
          .map((anchor) => String(anchor.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40));
        if (!hasStyledSurface(style)) {
          panelIssues.push(`${selector}[${index}]:unstyled_surface`);
        }
        if (nestedDefaultLinks.length) {
          panelIssues.push(`${selector}[${index}]:default_links:${nestedDefaultLinks.join('|')}`);
        }
        if (!isMobile && ['.ops-route-panel', '.quality-route-panel', '.rebuild-guard-panel'].includes(selector) && rect.height > 340) {
          panelIssues.push(`${selector}[${index}]:too_tall:${Math.round(rect.height)}`);
        }
      });
    });

    const overflowX = Math.max(
      0,
      document.documentElement.scrollWidth - document.documentElement.clientWidth,
      document.body.scrollWidth - document.documentElement.clientWidth
    );
    const shell = document.querySelector('.page-shell, .home-shell, .content, .workspace');
    const shellRect = shell ? shell.getBoundingClientRect() : null;
    const shellWidthGap = shellRect ? Math.max(0, document.documentElement.clientWidth - shellRect.width) : 0;
    const bridge = document.querySelector('#pageBridge .page-bridge');
    const bridgeRect = bridge ? bridge.getBoundingClientRect() : null;
    const pageGutter = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--page-gutter')) || 16;
    const configAction = document.querySelector('.config-action-bar');
    const configGap = bridgeRect && configAction
      ? Math.round(configAction.getBoundingClientRect().top - bridgeRect.bottom)
      : null;
    return {
      title: document.title,
      finalPath: window.location.pathname,
      targetFile,
      defaultLinks,
      panelIssues,
      overflowX,
      shellWidth: shellRect ? Math.round(shellRect.width) : null,
      viewportWidth: document.documentElement.clientWidth,
      shellWidthGap: Math.round(shellWidthGap),
      bridgeLeft: bridgeRect ? Math.round(bridgeRect.left) : null,
      bridgeRight: bridgeRect ? Math.round(document.documentElement.clientWidth - bridgeRect.right) : null,
      bridgeHeight: bridgeRect ? Math.round(bridgeRect.height) : null,
      pageGutter: Math.round(pageGutter),
      configGap,
    };
  }, { targetFile: fileName, isMobile: mobile }).catch((error) => {
    errors.push(`eval:${error.message}`);
    return null;
  });

  if (audit) {
    if (audit.defaultLinks.length) {
      errors.push(`default_links:${audit.defaultLinks.map((link) => `${link.text || link.href}@${link.finalPath || audit.finalPath}`).join('|')}`);
    }
    audit.panelIssues.forEach((issue) => errors.push(`panel:${issue}`));
    if (audit.overflowX > 8) {
      errors.push(`horizontal_overflow:${audit.overflowX}`);
    }
    if (!mobile && audit.shellWidth != null && audit.shellWidthGap > 48) {
      errors.push(`shell_not_fluid:${audit.shellWidth}/${audit.viewportWidth}`);
    }
    if (!mobile && audit.bridgeLeft != null) {
      if (Math.abs(audit.bridgeLeft - audit.pageGutter) > 2 || Math.abs(audit.bridgeRight - audit.pageGutter) > 2) {
        errors.push(`bridge_gutter_mismatch:${audit.bridgeLeft}/${audit.bridgeRight}/expected:${audit.pageGutter}`);
      }
      if (audit.bridgeHeight < 52 || audit.bridgeHeight > 72) {
        errors.push(`bridge_height_mismatch:${audit.bridgeHeight}`);
      }
    }
    if (audit.finalPath === '/ibkr_config.html' && audit.configGap != null && audit.configGap > 28) {
      errors.push(`config_bridge_gap:${audit.configGap}`);
    }
  }

  let screenshot = '';
  if (errors.length) {
    fs.mkdirSync(ARTIFACT_DIR, { recursive: true });
    const stem = `${fileName.replace(/\.html$/, '')}_${deviceLabel}`.replace(/[^a-zA-Z0-9_-]+/g, '_');
    screenshot = path.join(ARTIFACT_DIR, `visual_${stem}.png`);
    await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {
      screenshot = '';
    });
  }

  await context.close();
  return {
    target: fileName,
    device: deviceLabel,
    url: createTargetUrl(fileName),
    audit,
    errors,
    screenshot,
  };
}

(async () => {
  const token = await fetchToken();
  const browser = await chromium.launch({ headless: true });
  const targets = getHtmlTargets();
  const results = [];

  for (const target of targets) {
    results.push(await inspectPage(browser, token, target, false));
    results.push(await inspectPage(browser, token, target, true));
  }

  await browser.close();
  const failing = results.filter((result) => result.errors.length);
  console.log(JSON.stringify({
    ok: failing.length === 0,
    targetCount: targets.length,
    checks: results.length,
    failures: failing.length,
    results,
  }, null, 2));
  if (failing.length) process.exit(1);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
