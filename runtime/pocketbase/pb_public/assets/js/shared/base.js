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

function getRealtimeQuoteCacheAgeMs() {
  if (!realtimeQuoteCacheUpdatedAt) return Number.POSITIVE_INFINITY;
  return Math.max(0, Date.now() - realtimeQuoteCacheUpdatedAt);
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

async function fetchRealtimeQuotesIfNeeded(symbols = [], { reset = false, maxAgeMs = 15000 } = {}) {
  const uniqueSymbols = [...new Set((Array.isArray(symbols) ? symbols : [])
    .map((symbol) => String(symbol || '').trim().toUpperCase())
    .filter(Boolean))];

  if (!uniqueSymbols.length) {
    if (reset) {
      realtimeQuoteCache = {};
      realtimeQuoteCacheUpdatedAt = Date.now();
    }
    return { ok: true, count: 0, items: [], cached: true };
  }

  if (reset) {
    return fetchRealtimeQuotes(uniqueSymbols, { reset: true });
  }

  const cacheAgeMs = getRealtimeQuoteCacheAgeMs();
  const boundedMaxAgeMs = Math.max(0, Number(maxAgeMs) || 0);
  const missingSymbols = uniqueSymbols.filter((symbol) => !realtimeQuoteCache[symbol]);
  const cacheIsFresh = cacheAgeMs <= boundedMaxAgeMs;

  if (cacheIsFresh && !missingSymbols.length) {
    return {
      ok: true,
      count: uniqueSymbols.length,
      items: uniqueSymbols.map((symbol) => realtimeQuoteCache[symbol]).filter(Boolean),
      cached: true,
      cache_age_ms: cacheAgeMs,
    };
  }

  const fetchSymbols = cacheIsFresh ? missingSymbols : uniqueSymbols;
  return fetchRealtimeQuotes(fetchSymbols, { reset: false });
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
