// ═══════════════════════════════════════════════════════════════
// common.js - 公共模块
// 所有管理页面的共享逻辑和组件
// ═══════════════════════════════════════════════════════════════

// ── 配置常量 ──
const BASE_URL = 'https://pb.lzw-glory.top';
const ENVIRONMENT_STORAGE_KEY = 'pb_environment';
const PENDING_ENVIRONMENT_WINDOW_KEY = '__pb_pending_environment';
const RUNTIME_ENVIRONMENTS = ['live', 'paper', 'backtest'];
const CONFIG_ENVIRONMENTS = ['global', 'live', 'paper', 'backtest'];
const ENVIRONMENT_LABELS = {
  live: 'LIVE',
  paper: 'PAPER',
  backtest: 'BACKTEST',
  global: 'GLOBAL'
};

function escapeQueryValue(value) {
  return String(value ?? '');
}

function escapeFilterValue(value) {
  return String(value || '')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
}

function normalizeRuntimeEnvironment(value, fallback = 'live') {
  const text = String(value || '').trim().toLowerCase();
  const aliases = {
    prod: 'live',
    production: 'live',
    sim: 'paper',
    simulated: 'paper',
    simulation: 'paper',
    test: 'backtest'
  };
  const normalized = aliases[text] || text;
  return RUNTIME_ENVIRONMENTS.includes(normalized) ? normalized : fallback;
}

function normalizeConfigEnvironment(value, fallback = 'global') {
  const text = String(value || '').trim().toLowerCase();
  if (text === 'global') return 'global';
  return normalizeRuntimeEnvironment(text, fallback === 'global' ? 'live' : fallback);
}

function getStoredEnvironment() {
  return localStorage.getItem(ENVIRONMENT_STORAGE_KEY) || '';
}

function setStoredEnvironment(environment) {
  localStorage.setItem(ENVIRONMENT_STORAGE_KEY, environment);
}

function getCurrentRuntimeEnvironment() {
  const fromUrl = new URLSearchParams(window.location.search).get('environment') || '';
  const runtimeEnvironment = normalizeRuntimeEnvironment(fromUrl || getStoredEnvironment() || 'live', 'live');
  setStoredEnvironment(runtimeEnvironment);
  return runtimeEnvironment;
}

function getCurrentConfigEnvironment() {
  const fromUrl = new URLSearchParams(window.location.search).get('environment') || '';
  const configEnvironment = normalizeConfigEnvironment(fromUrl || getStoredEnvironment() || 'global', 'global');
  setStoredEnvironment(configEnvironment);
  return configEnvironment;
}

function getEnvironmentLabel(environment, allowGlobal = false) {
  const normalized = allowGlobal
    ? normalizeConfigEnvironment(environment, 'global')
    : normalizeRuntimeEnvironment(environment, 'live');
  return ENVIRONMENT_LABELS[normalized] || normalized.toUpperCase();
}

function getPendingEnvironment(allowGlobal = false) {
  const pending = typeof window !== 'undefined' ? String(window[PENDING_ENVIRONMENT_WINDOW_KEY] || '').trim() : '';
  if (!pending) return '';
  return allowGlobal
    ? normalizeConfigEnvironment(pending, 'global')
    : normalizeRuntimeEnvironment(pending, 'live');
}

function buildPageUrl(path, params = {}, options = {}) {
  const {
    allowGlobal = false,
    includeEnvironment = true,
    environment: explicitEnvironment = ''
  } = options;
  const url = new URL(path, window.location.origin);
  const currentEnvironment = allowGlobal ? getCurrentConfigEnvironment() : getCurrentRuntimeEnvironment();
  const normalizedExplicitEnvironment = explicitEnvironment
    ? (allowGlobal
        ? normalizeConfigEnvironment(explicitEnvironment, 'global')
        : normalizeRuntimeEnvironment(explicitEnvironment, 'live'))
    : '';
  const environment = normalizedExplicitEnvironment || getPendingEnvironment(allowGlobal) || currentEnvironment;

  if (includeEnvironment && environment) {
    url.searchParams.set('environment', environment);
  }

  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, escapeQueryValue(value));
    }
  });

  return `${url.pathname}${url.search}`;
}

function renderEnvironmentSwitcher(options = {}) {
  const allowGlobal = Boolean(options.allowGlobal);
  const selected = allowGlobal ? getCurrentConfigEnvironment() : getCurrentRuntimeEnvironment();
  const environments = allowGlobal ? CONFIG_ENVIRONMENTS : RUNTIME_ENVIRONMENTS;

  return `
    <div class="env-switcher">
      <span class="env-switcher-label">ENV</span>
      <select class="env-switcher-select" onchange="handleEnvironmentChange(this.value, ${allowGlobal ? 'true' : 'false'})">
        ${environments.map((environment) => `
          <option value="${environment}" ${environment === selected ? 'selected' : ''}>${getEnvironmentLabel(environment, allowGlobal)}</option>
        `).join('')}
      </select>
    </div>
  `;
}

function renderEnvironmentBadge(options = {}) {
  const allowGlobal = Boolean(options.allowGlobal);
  const environment = allowGlobal ? getCurrentConfigEnvironment() : getCurrentRuntimeEnvironment();
  return `<span class="env-badge env-${environment}">${getEnvironmentLabel(environment, allowGlobal)}</span>`;
}

window.handleEnvironmentChange = function(value, allowGlobal = false) {
  const normalized = allowGlobal
    ? normalizeConfigEnvironment(value, getCurrentConfigEnvironment())
    : normalizeRuntimeEnvironment(value, getCurrentRuntimeEnvironment());

  setStoredEnvironment(normalized);
  window[PENDING_ENVIRONMENT_WINDOW_KEY] = normalized;

  if (typeof window.onEnvironmentChange === 'function') {
    window.onEnvironmentChange(normalized, allowGlobal);
    return;
  }

  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.set('environment', normalized);
  window.location.href = nextUrl.toString();
};

// ── 时间转换 ──
// 将 UTC 毫秒时间戳转换为美国东部时间字符串，格式: 2026-02-02 10:00:00
function formatBarTimeMsToET(barTimeMs) {
    if (!barTimeMs) return '-';
    const date = new Date(Number(barTimeMs));
    const options = {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    };
    const parts = date.toLocaleString('en-US', options).split(/[\s/,:]+/);
    // parts: [MM, DD, YYYY, HH, mm, ss]
    const [mm, dd, yyyy, HH, MM, ss] = parts;
    return `${yyyy}-${mm}-${dd} ${HH}:${MM}:${ss}`;
}

// ── 通用时间格式化（ISO 字符串 → 任意时区 + 格式）──
function formatTime(utcTimeString, timezone, format) {
    if (!utcTimeString) return '-';
    const date = new Date(utcTimeString);
    if (isNaN(date.getTime())) return '-';

    const parseParts = () => {
        const str = date.toLocaleString('en-US', {
            timeZone: timezone,
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
        // en-US 格式: MM/DD/YYYY HH:mm:ss
        const m = str.match(/^(\d+)\/(\d+)\/(\d+)\s+(\d+):(\d+):(\d+)$/);
        return m ? [m[1], m[2], m[3], m[4], m[5], m[6]] : null; // [MM, DD, YYYY, HH, mm, ss]
    };

    if (format === 'time') {
        return date.toLocaleTimeString('en-US', {
            timeZone: timezone,
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
    } else if (format === 'short') {
        const p = parseParts();
        return p ? `${p[0]}-${p[1]} ${p[3]}:${p[4]}` : '-';
    } else if (format === 'date') {
        const p = parseParts();
        return p ? `${p[0]}-${p[1]}` : '-';
    } else {
        const p = parseParts();
        return p ? `${p[2]}-${p[0]}-${p[1]} ${p[3]}:${p[4]}:${p[5]}` : '-';
    }
}

// ── Token 管理 ──
function getToken() {
  return localStorage.getItem('pb_token') || '';
}

function clearToken() {
  localStorage.removeItem('pb_token');
}

function redirectToLogin(fromPage) {
  clearToken();
  const target = fromPage || `${location.pathname}${location.search}`;
  location.href = `/login.html?from=${encodeURIComponent(target)}`;
}

function handleAuthError() {
  redirectToLogin(`${location.pathname}${location.search}`);
}

// 通用错误处理包装器 - 自动处理认证错误
function withAuthCheck(promise, errorHandler) {
  return promise.catch(error => {
    // 检查是否是认证错误 (401/403 或消息包含认证失败)
    if (error.status === 401 || error.status === 403 ||
        error.message?.includes('Authentication') ||
        error.message?.includes('Unauthorized') ||
        error.message?.includes('auth')) {
      handleAuthError();
      return;
    }

    // 调用自定义错误处理
    if (errorHandler) errorHandler(error);
  });
}

function requireAuth(fromPage) {
  if (!getToken()) {
    location.href = `/login.html?from=${encodeURIComponent(fromPage || `${location.pathname}${location.search}`)}`;
    throw new Error('Not authenticated');
  }
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
}

async function fetchWithRetry(url, options = {}, retryOptions = {}) {
  const {
    attempts = 3,
    retryDelayMs = 400,
    retryOnStatuses = [408, 425, 429, 500, 502, 503, 504],
  } = retryOptions || {};

  let lastError = null;
  for (let attempt = 1; attempt <= Math.max(1, Number(attempts) || 1); attempt += 1) {
    try {
      const response = await fetch(url, {
        cache: 'no-store',
        ...options,
      });
      if (
        attempt < attempts &&
        retryOnStatuses.includes(response.status) &&
        response.status !== 401 &&
        response.status !== 403
      ) {
        await sleepMs(retryDelayMs * attempt);
        continue;
      }
      return response;
    } catch (error) {
      lastError = error;
      if (attempt >= attempts) {
        throw error;
      }
      await sleepMs(retryDelayMs * attempt);
    }
  }

  throw lastError || new Error('Request failed');
}

// ── API 请求封装 ──
async function apiFetch(collection, params = {}) {
  const token = getToken();
  const headers = {
    'Content-Type': 'application/json'
  };

  // PocketBase 0.36+ 使用 Bearer token
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  let url = `${BASE_URL}/api/collections/${collection}/records`;

  // 构建查询参数
  const queryParams = new URLSearchParams();
  if (params.filter) queryParams.append('filter', params.filter);
  if (params.sort) queryParams.append('sort', params.sort);
  if (params.perPage) queryParams.append('perPage', params.perPage);
  if (params.page) queryParams.append('page', params.page);

  const queryString = queryParams.toString();
  if (queryString) url += `?${queryString}`;

  const options = {
    method: params.method || 'GET',
    headers
  };

  if (params.body) {
    options.body = JSON.stringify(params.body);
  }

  const res = await fetchWithRetry(url, options, {
    attempts: 3,
    retryDelayMs: 500
  });

  // 401/403 自动跳转登录
  if (res.status === 401 || res.status === 403) {
    localStorage.removeItem('pb_token');
    location.href = `/login.html?from=${encodeURIComponent(`${location.pathname}${location.search}`)}`;
    throw new Error('Authentication failed');
  }

  if (!res.ok) {
    const error = await res.json();
    throw new Error(error.message || 'Request failed');
  }

  return res.json();
}

let realtimeQuoteCache = {};
let realtimeQuoteCacheUpdatedAt = 0;

function normalizeRealtimeQuoteRecord(record) {
  if (!record || typeof record !== 'object') return null;
  const symbol = String(record.symbol || '').trim().toUpperCase();
  if (!symbol) return null;
  return {
    ...record,
    symbol,
    last_price: record.last_price != null ? Number(record.last_price) : null,
    prev_close: record.prev_close != null ? Number(record.prev_close) : null,
    day_change: record.day_change != null ? Number(record.day_change) : null,
    day_change_pct: record.day_change_pct != null ? Number(record.day_change_pct) : null,
    quote_age_s: record.quote_age_s != null ? Number(record.quote_age_s) : null,
  };
}

function cacheRealtimeQuoteItems(items, { reset = false, requestedSymbols = [] } = {}) {
  const nextCache = reset ? {} : { ...realtimeQuoteCache };
  const requestedSet = new Set((Array.isArray(requestedSymbols) ? requestedSymbols : [])
    .map((symbol) => String(symbol || '').trim().toUpperCase())
    .filter(Boolean));

  // Requested symbols should mirror the latest response so pages don't keep
  // rendering quotes that are no longer present upstream.
  requestedSet.forEach((symbol) => {
    delete nextCache[symbol];
  });

  (Array.isArray(items) ? items : []).forEach((item) => {
    const normalized = normalizeRealtimeQuoteRecord(item);
    if (!normalized) return;
    nextCache[normalized.symbol] = normalized;
  });
  realtimeQuoteCache = nextCache;
  realtimeQuoteCacheUpdatedAt = Date.now();
  return realtimeQuoteCache;
}

function getRealtimeQuote(symbol) {
  const normalized = String(symbol || '').trim().toUpperCase();
  if (!normalized) return null;
  return realtimeQuoteCache[normalized] || null;
}

async function fetchRealtimeQuotes(symbols = [], { reset = false } = {}) {
  const uniqueSymbols = [...new Set((Array.isArray(symbols) ? symbols : [])
    .map((symbol) => String(symbol || '').trim().toUpperCase())
    .filter(Boolean))];
  if (!uniqueSymbols.length) {
    if (reset) {
      realtimeQuoteCache = {};
      realtimeQuoteCacheUpdatedAt = Date.now();
    }
    return { ok: true, count: 0, items: [] };
  }

  const token = getToken();
  const headers = {};
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  const url = `${BASE_URL}${buildPageUrl('/api/custom/ibkr/quotes', { symbols: uniqueSymbols.join(',') })}`;
  const response = await fetchWithRetry(url, { method: 'GET', headers }, { attempts: 3, retryDelayMs: 400 });
  if (response.status === 401 || response.status === 403) {
    handleAuthError();
    throw new Error('Authentication failed');
  }
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || payload.message || `Quotes request failed (${response.status})`);
  }
  cacheRealtimeQuoteItems(payload.items || [], { reset, requestedSymbols: uniqueSymbols });
  return payload;
}

function mergeIndicatorWithRealtimeQuote(indicator, realtimeQuoteOverride = null) {
  if (!indicator) return null;
  const merged = { ...indicator };
  const quote = realtimeQuoteOverride || getRealtimeQuote(merged.symbol);
  if (!quote) {
    merged.display_close = merged.close;
    merged.display_day_change_pct = merged.day_change_pct;
    merged.display_prev_close_change_pct = merged.prev_close_change_pct;
    merged.display_change_7d = merged.change_7d;
    return merged;
  }
  merged.realtime_quote = quote;
  merged.display_close = quote.last_price != null ? quote.last_price : merged.close;
  merged.display_day_change = quote.day_change != null ? quote.day_change : null;
  merged.display_day_change_pct = quote.day_change_pct != null ? quote.day_change_pct : merged.day_change_pct;
  merged.display_prev_close_change_pct = merged.prev_close_change_pct;
  merged.display_change_7d = merged.change_7d;
  merged.realtime_quote_age_s = quote.quote_age_s;
  return merged;
}

// ── Toast 通知 ──
function showToast(msg, duration = 2500) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  if (toast._hideTimer) {
    clearTimeout(toast._hideTimer);
    toast._hideTimer = null;
  }
  toast.textContent = msg;
  toast.classList.add('show');
  toast._hideTimer = setTimeout(() => {
    toast.classList.remove('show');
    toast._hideTimer = null;
  }, duration);
}

// ── 底部导航 ──
function renderNav(activePage) {
  const pages = [
    { path: '/index.html', icon: '🏠', label: '首页' },
    { path: '/ibkr_signals.html', aliases: ['/ibkr_reverse_signals.html'], icon: '📡', label: '信号' },
    { path: '/orders.html', aliases: ['/ibkr_order_details.html'], icon: '📋', label: '订单' },
    { path: '/ibkr_account.html', icon: '💼', label: '账户' },
    { path: '/ibkr_indicators.html', aliases: ['/ibkr_chart.html', '/ibkr_stats.html'], icon: '📈', label: '指标' },
    { path: '/ibkr_screener.html', aliases: ['/ibkr_watchlist.html', '/ibkr_targets.html'], icon: '🔎', label: '筛选' },
    { path: '/ibkr_monitor.html', aliases: ['/ibkr_warmup.html', '/ibkr_data_quality.html'], icon: '🛠️', label: '运维' },
    { path: '/ibkr_system.html', aliases: ['/ibkr_runtime.html'], icon: '🖥️', label: '系统' },
    { path: '/ibkr_backtests.html', icon: '🧪', label: '回测' }
  ];

  return `
    <div class="nav" style="--nav-count:${pages.length}">
      ${pages.map(p => `
        <a href="${buildPageUrl(p.path)}" class="nav-item ${(p.path === activePage || (Array.isArray(p.aliases) && p.aliases.includes(activePage))) ? 'active' : ''}">
          <span class="nav-icon">${p.icon}</span>${p.label}
        </a>
      `).join('')}
    </div>
  `;
}

// ── 登出功能 ──
window.handleLogout = function(event) {
  event.preventDefault();
  if (confirm('确定要登出吗？')) {
    localStorage.removeItem('pb_token');
    location.href = buildPageUrl('/login.html', {}, { includeEnvironment: false });
  }
};

// ── 日期选择器 ──
function renderDatePicker(onChange) {
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  const formatDate = (d) => d.toISOString().split('T')[0];

  return `
    <div class="date-picker">
      <div class="date-shortcuts">
        <button class="date-btn" data-date="${formatDate(yesterday)}" onclick="selectDate('${formatDate(yesterday)}', this)">昨天</button>
        <button class="date-btn active" data-date="${formatDate(today)}" onclick="selectDate('${formatDate(today)}', this)">今天</button>
      </div>
      <input type="date" class="date-input" id="customDate" value="${formatDate(today)}" onchange="selectCustomDate(this.value)">
    </div>
  `;
}

// 日期选择回调（需要在页面中定义 window.onDateChange）
window.selectDate = function(date, btn) {
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('customDate').value = date;
  if (typeof window.onDateChange === 'function') {
    window.onDateChange(date, date);
  }
};


window.selectCustomDate = function(date) {
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  if (typeof window.onDateChange === 'function') {
    window.onDateChange(date, date);
  }
};

// ── 多空方向筛选 ──
function renderDirectionTabs(onChange) {
  return `
    <div class="direction-tabs">
      <button class="dir-tab active" data-dir="all" onclick="selectDirection('all', this)">全部<span class="tab-count" data-dir="all">(<span class="count-value">0</span>)</span></button>
      <button class="dir-tab" data-dir="long" onclick="selectDirection('long', this)">做多<span class="tab-count" data-dir="long">(<span class="count-value">0</span>)</span></button>
      <button class="dir-tab" data-dir="short" onclick="selectDirection('short', this)">做空<span class="tab-count" data-dir="short">(<span class="count-value">0</span>)</span></button>
    </div>
  `;
}

window.selectDirection = function(direction, btn) {
  document.querySelectorAll('.dir-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (typeof onDirectionChange === 'function') {
    onDirectionChange(direction);
  }
};

// ── 时间格式化（UTC → 北京时间）──
function formatBeijingTime(utcTimeString, format = 'datetime') {
  if (!utcTimeString) return '-';

  const date = new Date(utcTimeString);

  // 检查是否为有效日期
  if (isNaN(date.getTime())) return '-';

  const options = {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  };

  if (format === 'date') {
    // 只显示日期: 2025-03-15
    return date.toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
  } else if (format === 'time') {
    // 只显示时间: 14:30:45
    return date.toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
  } else if (format === 'short') {
    // 短格式: 03-15 14:30
    const dateStr = date.toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
    const timeStr = date.toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
    return `${dateStr} ${timeStr}`;
  } else {
    // 完整格式: 2025-03-15 14:30:45
    const dateStr = date.toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
    const timeStr = date.toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
    return `${dateStr} ${timeStr}`;
  }
}

// ── 相对时间（多久之前）──
function formatRelativeTime(utcTimeString) {
  if (!utcTimeString) return '-';

  const date = new Date(utcTimeString);
  if (isNaN(date.getTime())) return '-';

  const now = new Date();
  const diffMs = now - date;
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffSec < 60) return `${diffSec}秒前`;
  if (diffMin < 60) return `${diffMin}分钟前`;
  if (diffHour < 24) return `${diffHour}小时前`;
  if (diffDay < 7) return `${diffDay}天前`;

  // 超过7天显示完整日期
  return formatBeijingTime(utcTimeString, 'short');
}

// ── 标准化指标记录（直接返回原生数据）──
function normalizeIndicatorRecord(record) {
  if (!record) return null;

  // 处理 extra 可能是 JSON 字符串的情况，解析后展开到 record 中
  if (typeof record.extra === 'string') {
    try {
      record.extra = JSON.parse(record.extra);
    } catch (e) {
      record.extra = {};
    }
  }

  // 把 extra 的字段展开到 record 中（保持 snake_case）
  return { ...record, ...record.extra };
}

// ── 构建技术指标徽章 HTML ──
function buildIndicatorBadges(signal, latestIndicator) {
  const badges = [];

  // 涨幅徽章（从最新指标数据获取）
  const indicator = mergeIndicatorWithRealtimeQuote(latestIndicator || {}) || {};
  const dayChangePct = indicator.display_day_change_pct ?? indicator.dayChangePct ?? indicator.day_change_pct ?? 0;
  const prevCloseChangePct = indicator.display_prev_close_change_pct ?? indicator.prevCloseChangePct ?? indicator.prev_close_change_pct ?? 0;
  const change7d = indicator.display_change_7d ?? indicator.change7d ?? indicator.change_7d ?? 0;

  if (dayChangePct !== 0) {
    const changeClass = dayChangePct > 0 ? 'badge-change-up' : 'badge-change-down';
    const changeSymbol = dayChangePct > 0 ? '+' : '';
    badges.push(`<span class="indicator-badge ${changeClass}">日内${changeSymbol}${dayChangePct.toFixed(2)}%</span>`);
  }
  if (prevCloseChangePct !== 0) {
    const changeClass = prevCloseChangePct > 0 ? 'badge-change-up' : 'badge-change-down';
    const changeSymbol = prevCloseChangePct > 0 ? '+' : '';
    badges.push(`<span class="indicator-badge ${changeClass}">较昨${changeSymbol}${prevCloseChangePct.toFixed(2)}%</span>`);
  }
  if (change7d !== 0) {
    const changeClass = change7d > 0 ? 'badge-change-up' : 'badge-change-down';
    const changeSymbol = change7d > 0 ? '+' : '';
    badges.push(`<span class="indicator-badge ${changeClass}">7日${changeSymbol}${change7d.toFixed(2)}%</span>`);
  }

  // 技术指标徽章
  if (latestIndicator) {
    // 背离信号
    if (latestIndicator.crsi_bull_div || latestIndicator.obv_bull_div) {
      badges.push(`<span class="indicator-badge badge-div">多头背离</span>`);
    }
    if (latestIndicator.crsi_bear_div || latestIndicator.obv_bear_div) {
      badges.push(`<span class="indicator-badge badge-div">空头背离</span>`);
    }
    // 分形信号
    if (latestIndicator.fractal_bull) {
      badges.push(`<span class="indicator-badge badge-fractal">分形↑</span>`);
    }
    if (latestIndicator.fractal_bear) {
      badges.push(`<span class="indicator-badge badge-fractal">分形↓</span>`);
    }
    // EMA触及
    if (latestIndicator.ema_bull_touch) {
      badges.push(`<span class="indicator-badge badge-ema">EMA触及↑</span>`);
    }
    if (latestIndicator.ema_bear_touch) {
      badges.push(`<span class="indicator-badge badge-ema">EMA触及↓</span>`);
    }
    // EMA 多头/空头状态
    if (latestIndicator.ema_bullish) {
      badges.push(`<span class="indicator-badge badge-ema">EMA 多头</span>`);
    }
    if (latestIndicator.ema_bearish) {
      badges.push(`<span class="indicator-badge badge-ema">EMA 空头</span>`);
    }
  }

  return badges.join('');
}

// ── 渲染技术指标详情浮层 ──
function renderIndicatorModal(latestIndicator) {
  if (!latestIndicator) return '<div class="modal-section">暂无技术指标数据</div>';
  const indicatorView = mergeIndicatorWithRealtimeQuote(latestIndicator) || latestIndicator;

  // 时间信息 section（放在最上面）
  const timeSection = `
    <div class="modal-section" style="background: var(--surface2); border-radius: 8px; padding: 12px; margin-bottom: 16px;">
      <div class="modal-section-title">⏰ 时间信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">美国时间</div>
          <div class="modal-value">${latestIndicator.us_time || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">中国时间</div>
          <div class="modal-value">${latestIndicator.cn_time || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Bar时间戳</div>
          <div class="modal-value">${latestIndicator.bar_time_ms ? formatBarTimeMsToET(latestIndicator.bar_time_ms) : '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">创建时间</div>
          <div class="modal-value">${latestIndicator.created ? formatBeijingTime(latestIndicator.created) : '-'}</div>
        </div>
      </div>
    </div>
  `;

  return `
    ${timeSection}
    <div class="modal-section">
      <div class="modal-section-title">价格信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">收盘价</div>
          <div class="modal-value">$${(indicatorView.display_close || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">最高价</div>
          <div class="modal-value">$${(latestIndicator.high || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">最低价</div>
          <div class="modal-value">$${(latestIndicator.low || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">开盘价</div>
          <div class="modal-value">$${(latestIndicator.open || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">涨幅</div>
          <div class="modal-value" style="color: ${(indicatorView.display_day_change_pct || 0) > 0 ? 'var(--long)' : 'var(--short)'}">
            ${(indicatorView.display_day_change_pct || 0) > 0 ? '+' : ''}${((indicatorView.display_day_change_pct || 0)).toFixed(2)}% / ${(indicatorView.display_prev_close_change_pct || 0) > 0 ? '+' : ''}${((indicatorView.display_prev_close_change_pct || 0)).toFixed(2)}% / ${(indicatorView.display_change_7d || 0) > 0 ? '+' : ''}${((indicatorView.display_change_7d || 0)).toFixed(2)}%
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR波动率</div>
          <div class="modal-value" style="color: ${latestIndicator.atr_pct >= 3 ? '#e53e3e' : latestIndicator.atr_pct >= 1.5 ? '#ed8936' : '#48bb78'}">
            ${latestIndicator.atr_pct >= 3 ? '⚡高' : latestIndicator.atr_pct >= 1.5 ? '〜中' : '·低'} ${(latestIndicator.atr_pct || 0).toFixed(2)}%
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">EMA 均线</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">EMA Fast (20)</div>
          <div class="modal-value">$${(latestIndicator.ema_fast || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Slow (50)</div>
          <div class="modal-value">$${(latestIndicator.ema_slow || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Trend (100)</div>
          <div class="modal-value">$${(latestIndicator.ema_trend || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Longest (200)</div>
          <div class="modal-value">$${(latestIndicator.ema_longest || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">趋势方向</div>
          <div class="modal-value">${latestIndicator.trend_dir === 1 ? '📈 多头' : latestIndicator.trend_dir === -1 ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA状态</div>
          <div class="modal-value">${latestIndicator.ema_bullish ? '📈 多头' : latestIndicator.ema_bearish ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA触及</div>
          <div class="modal-value">
            ${latestIndicator.ema_bull_touch ? '✅ 多头触及' : latestIndicator.ema_bear_touch ? '✅ 空头触及' : '❌ 无'}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Slow斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slope_slow > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slope_slow > 0 ? '+' : ''}${(latestIndicator.slope_slow || 0).toFixed(4)}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Trend斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slope_trend > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slope_trend > 0 ? '+' : ''}${(latestIndicator.slope_trend || 0).toFixed(4)}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Longest斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slope_longest > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slope_longest > 0 ? '+' : ''}${(latestIndicator.slope_longest || 0).toFixed(4)}
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">背离信号</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">cRSI 多头背离</div>
          <div class="modal-value">${latestIndicator.crsi_bull_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 空头背离</div>
          <div class="modal-value">${latestIndicator.crsi_bear_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 隐藏多头</div>
          <div class="modal-value">${latestIndicator.crsi_hid_bull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 隐藏空头</div>
          <div class="modal-value">${latestIndicator.crsi_hid_bear ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 多头背离</div>
          <div class="modal-value">${latestIndicator.obv_bull_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 空头背离</div>
          <div class="modal-value">${latestIndicator.obv_bear_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 隐藏多头</div>
          <div class="modal-value">${latestIndicator.obv_hid_bull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 隐藏空头</div>
          <div class="modal-value">${latestIndicator.obv_hid_bear ? '✅ 是' : '❌ 否'}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">分形 & 通道</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">分形多头</div>
          <div class="modal-value">${latestIndicator.fractal_bull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">分形空头</div>
          <div class="modal-value">${latestIndicator.fractal_bear ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 下轨触及</div>
          <div class="modal-value">${latestIndicator.sd_lower ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 上轨触及</div>
          <div class="modal-value">${latestIndicator.sd_upper ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 区域</div>
          <div class="modal-value">${latestIndicator.sd_zone === 1 ? '超买' : latestIndicator.sd_zone === -1 ? '超卖' : '正常'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 趋势</div>
          <div class="modal-value">${latestIndicator.sd_trend === 1 ? '📈 上升' : latestIndicator.sd_trend === -1 ? '📉 下降' : '➡️ 平坦'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 标准差</div>
          <div class="modal-value">${(latestIndicator.sd_std_dev || 0).toFixed(4)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 回归值</div>
          <div class="modal-value">${(latestIndicator.sd_reg || 0).toFixed(2)}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">DTP & RSI</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">DTP 方向</div>
          <div class="modal-value">${latestIndicator.dtp_dir === 1 ? '📈 多头' : latestIndicator.dtp_dir === -1 ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 阶段</div>
          <div class="modal-value">${latestIndicator.dtp_phase || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 阶段Bar数</div>
          <div class="modal-value">${latestIndicator.dtp_phase_bars || 0}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 平均值</div>
          <div class="modal-value">${(latestIndicator.dtp_avg || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP ATR</div>
          <div class="modal-value">${(latestIndicator.dtp_atr || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI</div>
          <div class="modal-value">${(latestIndicator.crsi || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 上界</div>
          <div class="modal-value">${(latestIndicator.crsi_ub || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 下界</div>
          <div class="modal-value">${(latestIndicator.crsi_db || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 状态</div>
          <div class="modal-value">${latestIndicator.crsi_ob ? '超买' : latestIndicator.crsi_os ? '超卖' : '正常'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV RSI</div>
          <div class="modal-value">${(latestIndicator.obv_rsi || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR</div>
          <div class="modal-value">$${(latestIndicator.atr || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR Raw</div>
          <div class="modal-value">${(latestIndicator.atr_raw || 0).toFixed(4)}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">止损管理</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">止损距离(%)</div>
          <div class="modal-value">${(latestIndicator.sl_dist_pct || 0).toFixed(2)}%</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">止损ATR比率</div>
          <div class="modal-value" style="color: ${latestIndicator.sl_atr_ratio >= 0.8 ? 'var(--long)' : 'var(--short)'}">
            ${(latestIndicator.sl_atr_ratio || 0).toFixed(2)}x ${latestIndicator.sl_atr_ratio >= 0.8 ? '✅' : '⚠️'}
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">VWAP</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">VWAP</div>
          <div class="modal-value">$${(latestIndicator.vwap || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">VWAP 偏离</div>
          <div class="modal-value" style="color: ${latestIndicator.vwap_dist > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.vwap_dist > 0 ? '+' : ''}${(latestIndicator.vwap_dist || 0).toFixed(2)}%
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">VWAP 趋势</div>
          <div class="modal-value">${latestIndicator.vwap_bullish ? '📈 多头' : '📉 空头'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">U1 上轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_upper1 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">L1 下轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_lower1 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">U2 上轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_upper2 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">L2 下轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_lower2 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">成交量</div>
          <div class="modal-value">${(latestIndicator.volume || 0).toLocaleString()}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">时间信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">美东时间</div>
          <div class="modal-value">${latestIndicator.us_time || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">北京时间</div>
          <div class="modal-value">${latestIndicator.cn_time || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">K线索引</div>
          <div class="modal-value">${latestIndicator.bar_index || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">时间周期</div>
          <div class="modal-value">${latestIndicator.interval || 'N/A'}</div>
        </div>
      </div>
    </div>
  `;
}

// ── 刷新栏（刷新按钮 + 周期选择）──
function renderRefreshBar(onRefresh, onIntervalChange) {
  return `
    <div class="refresh-bar">
      <button class="refresh-btn" id="refreshBtn" onclick="handleRefresh()">↻</button>
      <select class="interval-select" id="intervalSelect" onchange="handleIntervalChange(this.value)">
        <option value="0">关闭</option>
        <option value="10">10秒</option>
        <option value="30" selected>30秒</option>
        <option value="60">60秒</option>
      </select>
    </div>
  `;
}

window.handleRefresh = function() {
  const btn = document.getElementById('refreshBtn');
  btn.classList.add('spinning');
  setTimeout(() => btn.classList.remove('spinning'), 600);
  if (typeof onRefresh === 'function') {
    onRefresh();
  }
};

window.handleIntervalChange = function(seconds) {
  if (typeof onIntervalChange === 'function') {
    onIntervalChange(parseInt(seconds));
  }
};

function renderPageContextBar(title, options = {}) {
  const allowGlobal = Boolean(options.allowGlobal);
  const description = options.description || '';
  return `
    <div class="page-context-bar">
      <div class="page-context-title">
        ${title ? `<span>${title}</span>` : ''}
        ${renderEnvironmentBadge({ allowGlobal })}
        ${description ? `<span>${description}</span>` : ''}
      </div>
      ${renderEnvironmentSwitcher({ allowGlobal })}
    </div>
  `;
}

function renderPageBridge(items = []) {
  if (!Array.isArray(items) || items.length === 0) return '';
  return `
    <div class="page-bridge">
      ${items.map((item) => {
        const href = buildPageUrl(item.path || '/', item.params || {}, item.options || {});
        return `
          <a href="${href}" class="page-bridge-link${item.active ? ' active' : ''}">
            <span class="page-bridge-kicker">${item.kicker || ''}</span>
            <span class="page-bridge-label">${item.label || ''}</span>
            <span class="page-bridge-copy">${item.copy || ''}</span>
          </a>
        `;
      }).join('')}
    </div>
  `;
}

function renderSystemBridge(activePage) {
  return renderPageBridge([
    {
      path: '/ibkr_system.html',
      kicker: 'Overview',
      label: '总览',
      copy: '健康 / freshness / config',
      active: activePage === '/ibkr_system.html'
    },
    {
      path: '/ibkr_runtime.html',
      kicker: 'Console',
      label: '控制台',
      copy: '启动 / 2FA / compute / flow trace',
      active: activePage === '/ibkr_runtime.html'
    }
  ]);
}

function renderOpsBridge(activePage) {
  return renderPageBridge([
    {
      path: '/ibkr_monitor.html',
      kicker: 'Dashboard',
      label: '监控大盘',
      copy: '请求 / 订阅 / 主机健康',
      active: activePage === '/ibkr_monitor.html'
    },
    {
      path: '/ibkr_warmup.html',
      kicker: 'Warmup',
      label: '预热',
      copy: 'startup / gate / repair / symbol status',
      active: activePage === '/ibkr_warmup.html'
    },
    {
      path: '/ibkr_data_quality.html',
      kicker: 'Quality',
      label: '数据质量',
      copy: 'bars 缺口 / 重复 / 安全修复',
      active: activePage === '/ibkr_data_quality.html'
    }
  ]);
}

// ── 通用 CSS 样式 ──
function getCommonStyles() {
  return `
    <style>
      :root {
        --bg:        #080B10;
        --surface:   #0E1420;
        --surface2:  #141C2E;
        --border:    rgba(99,179,237,0.1);
        --text:      #E2EAF4;
        --muted:     #8BA4C4;
        --accent:    #63B3ED;
        --long:      #48BB78;
        --short:     #FC8181;
        --pending:   #F6AD55;
        --confirmed: #63B3ED;
        --executed:  #68D391;
        --rejected:  #718096;
        --failed:    #FC8181;
      }

      * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }

      /* 防止 iOS 输入时放大页面 */
      input, textarea, select {
        font-size: 16px;
      }

      body {
        background: var(--bg);
        color: var(--text);
        font-family: 'Sora', sans-serif;
        min-height: 100vh;
        padding-bottom: 80px;
        overflow-x: hidden;
      }

      body::before {
        content: '';
        position: fixed;
        inset: 0;
        background-image:
          linear-gradient(rgba(99,179,237,0.03) 1px, transparent 1px),
          linear-gradient(90deg, rgba(99,179,237,0.03) 1px, transparent 1px);
        background-size: 32px 32px;
        pointer-events: none;
        z-index: 0;
      }

      /* ── Toast ── */
      .toast {
        position: fixed;
        top: 24px;
        left: 50%;
        transform: translate(-50%, calc(-100% - 24px));
        background: var(--surface2);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 12px 20px;
        font-size: 13px;
        z-index: 200;
        white-space: nowrap;
        opacity: 0;
        visibility: hidden;
        transition:
          transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1),
          opacity 0.2s ease,
          visibility 0s linear 0.2s;
        pointer-events: none;
      }

      .toast.show {
        transform: translate(-50%, 0);
        opacity: 1;
        visibility: visible;
        transition:
          transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1),
          opacity 0.2s ease;
      }

      .page-context-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin: 0 16px 12px;
        padding: 10px 12px;
        border: 1px solid var(--border);
        border-radius: 12px;
        background: rgba(20,28,46,0.9);
        position: relative;
        z-index: 1;
      }

      .page-context-title {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
        font-size: 12px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
      }

      .page-bridge {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 10px;
        margin: 0 16px 16px;
        position: relative;
        z-index: 1;
      }

      .page-bridge-link {
        display: flex;
        flex-direction: column;
        gap: 4px;
        min-width: 0;
        padding: 12px 14px;
        border-radius: 14px;
        border: 1px solid var(--border);
        background: rgba(14,20,32,0.88);
        text-decoration: none;
        color: var(--text);
        transition: transform 0.18s ease, border-color 0.18s ease, background 0.18s ease;
      }

      .page-bridge-link:hover {
        transform: translateY(-1px);
        border-color: rgba(99,179,237,0.28);
        background: rgba(20,28,46,0.96);
      }

      .page-bridge-link.active {
        border-color: rgba(99,179,237,0.34);
        background: linear-gradient(180deg, rgba(99,179,237,0.16), rgba(20,28,46,0.94));
        box-shadow: 0 12px 28px rgba(2, 8, 23, 0.24);
      }

      .page-bridge-kicker {
        color: var(--accent);
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        letter-spacing: 1px;
        text-transform: uppercase;
      }

      .page-bridge-label {
        color: var(--text);
        font-size: 14px;
        font-weight: 700;
      }

      .page-bridge-copy {
        color: var(--muted);
        font-size: 11px;
        line-height: 1.5;
      }

      .env-badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 4px 10px;
        border-radius: 999px;
        border: 1px solid rgba(99,179,237,0.24);
        background: rgba(99,179,237,0.12);
        color: var(--accent);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 700;
        letter-spacing: 0.6px;
      }

      .env-badge.env-live {
        border-color: rgba(72,187,120,0.28);
        background: rgba(72,187,120,0.14);
        color: var(--long);
      }

      .env-badge.env-paper {
        border-color: rgba(246,173,85,0.28);
        background: rgba(246,173,85,0.14);
        color: var(--pending);
      }

      .env-badge.env-backtest {
        border-color: rgba(99,179,237,0.28);
        background: rgba(99,179,237,0.14);
        color: var(--accent);
      }

      .env-badge.env-global {
        border-color: rgba(226,232,240,0.22);
        background: rgba(148,163,184,0.14);
        color: #E2E8F0;
      }

      .env-switcher {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: var(--surface2);
        min-width: 0;
      }

      .env-switcher-label {
        color: var(--muted);
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        letter-spacing: 1px;
      }

      .env-switcher-select {
        min-width: 104px;
        border: none;
        background: transparent;
        color: var(--text);
        font-size: 12px;
        font-family: 'JetBrains Mono', monospace;
        outline: none;
      }

      .filter-note {
        margin-bottom: 12px;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: var(--surface2);
        color: var(--muted);
        font-size: 12px;
        font-family: 'JetBrains Mono', monospace;
      }

      /* ── Bottom Nav ── */
      .nav {
        position: fixed;
        bottom: 0;
        left: 0; right: 0;
        background: rgba(8,11,16,0.96);
        backdrop-filter: blur(16px);
        border-top: 1px solid var(--border);
        display: grid;
        grid-template-columns: repeat(var(--nav-count, 6), minmax(0, 1fr));
        padding: 10px 0 max(20px, env(safe-area-inset-bottom));
        z-index: 50;
      }

      .nav-item {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 3px;
        text-decoration: none;
        color: var(--muted);
        font-size: 8px;
        font-family: 'JetBrains Mono', monospace;
        letter-spacing: 0.5px;
        transition: color 0.2s;
        padding: 0 2px;
        min-width: 0;
      }

      .nav-item.active { color: var(--accent); }
      .nav-icon { font-size: 18px; line-height: 1; }

      /* ── Date Picker ── */
      .date-picker {
        display: flex;
        gap: 8px;
        align-items: center;
        margin-bottom: 12px;
      }

      .date-shortcuts {
        display: flex;
        gap: 6px;
      }

      .date-btn {
        padding: 8px 16px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: transparent;
        color: var(--muted);
        font-size: clamp(11px, 2.8vw, 13px);
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        transition: all 0.2s;
        white-space: nowrap;
        flex: 1;
        min-width: 0;
      }

      .date-btn.active {
        background: var(--accent);
        border-color: var(--accent);
        color: var(--bg);
        font-weight: 700;
      }

      .date-input {
        padding: 6px 10px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface2);
        color: var(--text);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        outline: none;
      }

      .date-input:focus {
        border-color: var(--accent);
      }

      /* ── Direction Tabs ── */
      .direction-tabs {
        display: flex;
        gap: 6px;
        margin-bottom: 12px;
      }

      .dir-tab {
        flex: 1;
        padding: 10px 16px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: transparent;
        color: #C4D4E4;
        font-size: clamp(11px, 3vw, 13px);
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        transition: all 0.2s;
        min-width: 0;
      }

      .dir-tab.active {
        background: var(--accent);
        border-color: var(--accent);
        color: var(--bg);
        font-weight: 700;
      }

      .dir-tab[data-dir="long"].active {
        background: var(--long);
        border-color: var(--long);
      }

      .dir-tab[data-dir="short"].active {
        background: var(--short);
        border-color: var(--short);
      }

      /* ── Refresh Bar ── */
      .refresh-bar {
        display: flex;
        gap: 8px;
        align-items: center;
      }

      .refresh-btn {
        background: none;
        border: 1px solid var(--border);
        border-radius: 8px;
        color: var(--muted);
        padding: 4px 8px;
        font-size: 14px;
        cursor: pointer;
        transition: all 0.2s;
      }

      .refresh-btn:active {
        color: var(--accent);
        border-color: var(--accent);
      }

      .refresh-btn.spinning {
        animation: spin 0.6s linear;
        border-color: transparent !important;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      .interval-select,
      .refresh-select {
        padding: 4px 8px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface2);
        color: var(--text);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        outline: none;
        cursor: pointer;
      }

      .interval-select:focus,
      .refresh-select:focus {
        border-color: var(--accent);
      }

      @media (max-width: 480px) {
        .page-context-bar {
          flex-wrap: wrap;
          align-items: flex-start;
        }

        .env-switcher {
          padding: 6px 8px;
        }

        .env-switcher-select {
          min-width: 82px;
          font-size: 11px;
        }

        .nav-item {
          font-size: 7px;
        }

        .nav-icon {
          font-size: 17px;
        }
      }

      /* ── Header ── */
      .header {
        position: sticky;
        top: 0;
        z-index: 50;
        background: rgba(8,11,16,0.92);
        backdrop-filter: blur(16px);
        border-bottom: 1px solid var(--border);
        padding: 16px 20px 12px;
      }

      .header-top {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        margin-bottom: 14px;
      }

      .logo {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .logo-label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 9px;
        letter-spacing: 3px;
        color: var(--accent);
        opacity: 0.7;
      }

      .logo-title {
        font-size: 18px;
        font-weight: 700;
        letter-spacing: -0.5px;
      }

      .live-badge {
        display: flex;
        align-items: center;
        gap: 6px;
        background: rgba(72,187,120,0.1);
        border: 1px solid rgba(72,187,120,0.25);
        border-radius: 20px;
        padding: 5px 10px;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--long);
        letter-spacing: 1px;
      }

      .live-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--long);
        animation: blink 1.4s infinite;
      }

      @keyframes blink {
        0%, 100% { opacity: 1; }
        50%       { opacity: 0.2; }
      }

      /* ── Content ── */
      .content {
        position: relative;
        z-index: 1;
        padding: 12px 16px;
      }

      /* ── Loading & Empty ── */
      .loading {
        text-align: center;
        padding: 40px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        letter-spacing: 1px;
      }

      /* Loading 动画 */
      .loading-spinner {
        display: inline-block;
        width: 20px;
        height: 20px;
        border: 2px solid var(--border);
        border-top-color: var(--accent);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
        margin-right: 8px;
        vertical-align: middle;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* 全局 Loading 遮罩层 */
      .global-loading {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(8, 11, 16, 0.85);
        backdrop-filter: blur(4px);
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        z-index: 9999;
        gap: 16px;
      }

      .global-loading .spinner {
        width: 40px;
        height: 40px;
        border: 3px solid var(--border);
        border-top-color: var(--accent);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .global-loading .text {
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
        font-size: 13px;
        letter-spacing: 1px;
      }

      .global-loading.hidden {
        display: none;
      }

      .empty {
        text-align: center;
        padding: 60px 20px;
        color: var(--muted);
      }

      .empty-icon {
        font-size: 40px;
        margin-bottom: 12px;
        opacity: 0.4;
      }

      .empty-text {
        font-size: 13px;
      }

      /* ── 技术指标徽章 ── */
      .indicator-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 10px;
      }

      .indicator-badge {
        padding: 4px 8px;
        border-radius: 6px;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 700;
        letter-spacing: 0.5px;
        white-space: nowrap;
      }

      .badge-change-up {
        background: rgba(72,187,120,0.2);
        color: var(--long);
      }

      .badge-change-down {
        background: rgba(252,129,129,0.2);
        color: var(--short);
      }

      .badge-div {
        background: rgba(99,179,237,0.2);
        color: var(--accent);
      }

      .badge-fractal {
        background: rgba(246,173,85,0.2);
        color: var(--pending);
      }

      .badge-ema {
        background: rgba(139,92,246,0.2);
        color: #a78bfa;
      }

      .view-details-btn {
        margin-top: 10px;
        padding: 8px;
        background: rgba(99,179,237,0.1);
        border: 1px solid rgba(99,179,237,0.2);
        border-radius: 8px;
        color: var(--accent);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        transition: all 0.2s;
        text-align: center;
      }

      .view-details-btn:active {
        background: rgba(99,179,237,0.2);
      }

      /* ── 浮层样式 ── */
      .modal-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.8);
        z-index: 1000;
        display: none;
        align-items: center;
        justify-content: center;
        padding: 20px;
      }

      .modal-overlay.show {
        display: flex;
      }

      .modal-content {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        max-width: 600px;
        width: 100%;
        max-height: 80vh;
        overflow-y: auto;
        padding: 20px;
      }

      .modal-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid var(--border);
      }

      .modal-title {
        font-size: 16px;
        font-weight: 700;
        color: var(--text);
      }

      .modal-close {
        background: none;
        border: none;
        color: var(--muted);
        font-size: 24px;
        cursor: pointer;
        padding: 0;
        width: 32px;
        height: 32px;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      .modal-section {
        margin-bottom: 16px;
      }

      .modal-section-title {
        font-size: 12px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
        margin-bottom: 8px;
        letter-spacing: 1px;
      }

      .modal-grid {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
      }

      .modal-item {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .modal-label {
        font-size: 10px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
      }

      .modal-value {
        font-size: 13px;
        font-weight: 700;
        color: var(--text);
      }
    </style>
  `;
}

// ── 全局 Loading 遮罩层 ──
function showLoading(text = '加载中...') {
  let loading = document.getElementById('globalLoading');
  if (!loading) {
    loading = document.createElement('div');
    loading.id = 'globalLoading';
    loading.className = 'global-loading hidden';
    loading.innerHTML = `
      <div class="spinner"></div>
      <div class="text">${text}</div>
    `;
    document.body.appendChild(loading);
  }
  loading.querySelector('.text').textContent = text;
  loading.classList.remove('hidden');
}

function hideLoading() {
  const loading = document.getElementById('globalLoading');
  if (loading) {
    loading.classList.add('hidden');
  }
}

// 自动管理 Loading 状态的包装函数
function withLoading(promise, text = '加载中...') {
  showLoading(text);
  return promise.finally(() => hideLoading());
}

/**
 * 创建带防重入 guard 的加载函数
 * 用法：const loadFn = guardedLoader(async () => { ... }, '加载中...')
 * 之后用 loadFn() 替代原始调用，重复调用会被忽略
 */
function guardedLoader(asyncFn, loadingText) {
  let isRunning = false;
  return async function() {
    if (isRunning) return;
    isRunning = true;
    showLoading(loadingText || '加载中...');
    try {
      await asyncFn();
    } finally {
      isRunning = false;
      hideLoading();
    }
  };
}

// ── 导出（如果使用模块化）──
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    BASE_URL,
    getToken,
    requireAuth,
    fetchWithRetry,
    apiFetch,
    showToast,
    renderNav,
    renderDatePicker,
    renderDirectionTabs,
    renderRefreshBar,
    getCommonStyles,
    formatBeijingTime,
    formatRelativeTime,
    formatTime,
    showLoading,
    hideLoading,
    withLoading
  };
}

// ── 技术指标弹窗（ibkr_signals.html / ibkr_indicators.html 共用） ──
function getIndicatorModalStyles() {
  return `
    .modal-overlay {
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.8);
      z-index: 1000;
      display: none;
      align-items: center; justify-content: center;
      padding: 20px;
    }
    .modal-overlay.show { display: flex; }
    .modal-content {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      max-width: 600px; width: 100%;
      max-height: 80vh; overflow-y: auto;
      padding: 20px;
    }
    .modal-header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 16px; padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
    }
    .modal-title { font-size: 16px; font-weight: 700; color: var(--text); }
    .modal-close {
      background: none; border: none; color: var(--muted);
      font-size: 24px; cursor: pointer;
      padding: 0; width: 32px; height: 32px;
      display: flex; align-items: center; justify-content: center;
    }
    .modal-section { margin-bottom: 16px; }
    .modal-section-title {
      font-size: 12px; color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
      margin-bottom: 8px; letter-spacing: 1px;
    }
    .modal-grid {
      display: grid; grid-template-columns: repeat(2, 1fr);
      gap: 10px;
    }
    .modal-item { display: flex; flex-direction: column; gap: 4px; }
    .modal-label {
      font-size: 10px; color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
    }
    .modal-value {
      font-size: 13px; font-weight: 700; color: var(--text);
    }
  `;
}

function renderIndicatorModalHTML() {
  return `<div class="modal-overlay" id="indicatorModal" onclick="if(event.target===this)window.closeIndicatorModal()">
    <div class="modal-content">
      <div class="modal-header">
        <div class="modal-title">技术指标详情</div>
        <button class="modal-close" onclick="window.closeIndicatorModal()">×</button>
      </div>
      <div id="modalBody"></div>
    </div>
  </div>`;
}

function showIndicatorModal(normalized) {
  const container = document.getElementById('indicatorModalContainer');
  if (!container) return;
  // 首次渲染 HTML
  if (!document.getElementById('indicatorModal')) {
    container.innerHTML = renderIndicatorModalHTML();
  }
  document.getElementById('modalBody').innerHTML = renderIndicatorModal(normalized);
  document.getElementById('indicatorModal').classList.add('show');
}

window.closeIndicatorModal = function() {
  const modal = document.getElementById('indicatorModal');
  if (modal) modal.classList.remove('show');
};

window.showIndicatorModal = showIndicatorModal;
window.getIndicatorModalStyles = getIndicatorModalStyles;
window.renderSystemBridge = renderSystemBridge;
window.renderOpsBridge = renderOpsBridge;
