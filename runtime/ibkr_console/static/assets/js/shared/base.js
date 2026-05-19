// ═══════════════════════════════════════════════════════════════
// common.js - 公共模块
// 所有管理页面的共享逻辑和组件
// ═══════════════════════════════════════════════════════════════

// ── 配置常量 ──
function normalizeRuntimeBaseUrl(value, fallback) {
  const text = String(value || fallback || '').trim();
  return text ? text.replace(/\/+$/, '') : '';
}

const RUNTIME_CONFIG = (typeof window !== 'undefined' && window.__IBKR_RUNTIME_CONFIG__)
  ? window.__IBKR_RUNTIME_CONFIG__
  : {};
const API_BASE_URL = normalizeRuntimeBaseUrl(
  RUNTIME_CONFIG.API_BASE_URL,
  typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:5102'
);
const PB_AUTH_BASE_URL = normalizeRuntimeBaseUrl(
  RUNTIME_CONFIG.PB_AUTH_BASE_URL,
  API_BASE_URL
);
const CONSOLE_BASE_URL = normalizeRuntimeBaseUrl(
  RUNTIME_CONFIG.CONSOLE_BASE_URL,
  typeof window !== 'undefined' ? window.location.origin : ''
);
const BASE_URL = API_BASE_URL;
const COLLECTIONS_BASE_URL = normalizeRuntimeBaseUrl(PB_AUTH_BASE_URL, API_BASE_URL);

function createPocketBaseClient(baseUrl = COLLECTIONS_BASE_URL) {
  if (typeof PocketBase !== 'function') {
    throw new Error('PocketBase SDK is not loaded');
  }
  return new PocketBase(normalizeRuntimeBaseUrl(baseUrl, COLLECTIONS_BASE_URL));
}

function createPocketBaseAuthClient(baseUrl = PB_AUTH_BASE_URL) {
  return createPocketBaseClient(baseUrl || PB_AUTH_BASE_URL);
}

if (typeof window !== 'undefined') {
  window.API_BASE_URL = API_BASE_URL;
  window.PB_AUTH_BASE_URL = PB_AUTH_BASE_URL;
  window.CONSOLE_BASE_URL = CONSOLE_BASE_URL;
  window.BASE_URL = BASE_URL;
  window.COLLECTIONS_BASE_URL = COLLECTIONS_BASE_URL;
  window.createPocketBaseClient = createPocketBaseClient;
  window.createPocketBaseAuthClient = createPocketBaseAuthClient;
}
const ENVIRONMENT_STORAGE_KEY = 'pb_environment';
const BROKER_MODE_STORAGE_KEY = 'ibkr_broker_mode';
const MARKET_DATA_MODE_STORAGE_KEY = 'ibkr_market_data_mode';
const PENDING_ENVIRONMENT_WINDOW_KEY = '__pb_pending_environment';
const BROKER_MODES = ['paper', 'live'];
const RUNTIME_ENVIRONMENTS = ['live', 'backtest'];
const CONFIG_ENVIRONMENTS = ['global', 'live', 'paper', 'backtest'];
const ENVIRONMENT_LABELS = {
  live: 'LIVE',
  paper: 'PAPER',
  backtest: 'BACKTEST',
  global: 'GLOBAL'
};
const SHARED_DATA_LABEL = 'Shared Data';

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
    sim: 'live',
    simulated: 'live',
    simulation: 'live',
    test: 'backtest'
  };
  const normalized = aliases[text] || text;
  return RUNTIME_ENVIRONMENTS.includes(normalized) ? normalized : fallback;
}

function normalizeBrokerMode(value, fallback = 'paper') {
  const text = String(value || '').trim().toLowerCase();
  const aliases = {
    prod: 'live',
    production: 'live',
    sim: 'paper',
    simulated: 'paper',
    simulation: 'paper'
  };
  const normalized = aliases[text] || text;
  return BROKER_MODES.includes(normalized) ? normalized : fallback;
}

function normalizeConfigEnvironment(value, fallback = 'global') {
  const text = String(value || '').trim().toLowerCase();
  if (text === 'global') return 'global';
  if (CONFIG_ENVIRONMENTS.includes(text)) return text;
  return normalizeRuntimeEnvironment(text, fallback === 'global' ? 'live' : fallback);
}

function getStoredEnvironment() {
  return localStorage.getItem(MARKET_DATA_MODE_STORAGE_KEY) || localStorage.getItem(ENVIRONMENT_STORAGE_KEY) || '';
}

function setStoredEnvironment(environment) {
  localStorage.setItem(ENVIRONMENT_STORAGE_KEY, environment);
}

function setStoredBrokerMode(brokerMode) {
  const normalized = normalizeBrokerMode(brokerMode, 'paper');
  localStorage.setItem(BROKER_MODE_STORAGE_KEY, normalized);
  localStorage.setItem(ENVIRONMENT_STORAGE_KEY, normalized);
}

function setStoredMarketDataMode(marketDataMode) {
  localStorage.setItem(MARKET_DATA_MODE_STORAGE_KEY, normalizeRuntimeEnvironment(marketDataMode, 'live'));
}

function getCurrentRuntimeEnvironment() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get('environment') || '';
  const urlMarketDataMode = params.get('market_data_mode') || params.get('data_environment') || '';
  const contextMarketDataMode = (
    window.__ibkrBrokerModeContext?.market_data_mode
    || window.__ibkrBrokerModeContext?.data_environment
    || RUNTIME_CONFIG.MARKET_DATA_MODE
    || ''
  );
  const runtimeEnvironment = normalizeRuntimeEnvironment(
    contextMarketDataMode || urlMarketDataMode || (normalizeRuntimeEnvironment(fromUrl, '') === 'backtest' ? 'backtest' : 'live'),
    'live'
  );
  setStoredMarketDataMode(runtimeEnvironment);
  return runtimeEnvironment;
}

function getCurrentBrokerMode() {
  const params = new URLSearchParams(window.location.search);
  const contextBrokerMode = (
    window.__ibkrBrokerModeContext?.broker_mode
    || RUNTIME_CONFIG.BROKER_MODE
    || ''
  );
  const brokerMode = normalizeBrokerMode(
    contextBrokerMode || params.get('broker_mode') || localStorage.getItem(BROKER_MODE_STORAGE_KEY) || 'paper',
    'paper'
  );
  setStoredBrokerMode(brokerMode);
  return brokerMode;
}

function getCurrentConfigEnvironment() {
  const fromUrl = new URLSearchParams(window.location.search).get('environment') || '';
  const configEnvironment = normalizeConfigEnvironment(fromUrl || 'global', 'global');
  setStoredEnvironment(configEnvironment);
  return configEnvironment;
}

function getEnvironmentLabel(environment, allowGlobal = false) {
  const text = String(environment || '').trim().toLowerCase();
  const normalized = allowGlobal
    ? normalizeConfigEnvironment(environment, 'global')
    : (BROKER_MODES.includes(text)
        ? normalizeBrokerMode(environment, 'paper')
        : normalizeRuntimeEnvironment(environment, 'live'));
  return ENVIRONMENT_LABELS[normalized] || normalized.toUpperCase();
}

function getBrokerModeContext() {
  const context = (typeof window !== 'undefined' && window.__ibkrBrokerModeContext && typeof window.__ibkrBrokerModeContext === 'object')
    ? window.__ibkrBrokerModeContext
    : {};
  const brokerMode = normalizeBrokerMode(
    context.broker_mode || getCurrentBrokerMode(),
    'paper'
  );
  const dataEnvironment = normalizeRuntimeEnvironment(
    context.data_environment || context.market_data_environment || 'live',
    'live'
  );
  return {
    ...context,
    broker_mode: brokerMode,
    environment: brokerMode,
    data_environment: dataEnvironment,
    market_data_environment: dataEnvironment,
    shared_market_data: context.shared_market_data !== false && dataEnvironment === 'live',
  };
}

function getCurrentDataEnvironment() {
  return getCurrentRuntimeEnvironment();
}

function getCurrentMarketDataMode() {
  return getCurrentRuntimeEnvironment();
}

function getSharedDataEnvironment() {
  return 'live';
}

function getConsoleBrokerMode() {
  return getCurrentBrokerMode();
}

function buildModePayload(payload = {}, options = {}) {
  const brokerMode = normalizeBrokerMode(
    options.brokerMode || payload.broker_mode || getCurrentBrokerMode(),
    'paper'
  );
  const dataEnvironment = normalizeRuntimeEnvironment(
    options.dataEnvironment || payload.market_data_mode || payload.data_environment || getSharedDataEnvironment(),
    getSharedDataEnvironment()
  );
  return {
    ...(payload || {}),
    broker_mode: brokerMode,
    market_data_mode: dataEnvironment,
    data_environment: dataEnvironment,
  };
}

function setBrokerModeContext(payload = {}) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const brokerMode = normalizeBrokerMode(
    source.broker_mode || source.actual_runtime_environment || getCurrentBrokerMode(),
    'paper'
  );
  const dataEnvironment = normalizeRuntimeEnvironment(
    source.data_environment || source.market_data_environment || 'live',
    'live'
  );
  window.__ibkrBrokerModeContext = {
    ...(window.__ibkrBrokerModeContext || {}),
    ...source,
    broker_mode: brokerMode,
    environment: brokerMode,
    data_environment: dataEnvironment,
    market_data_environment: dataEnvironment,
    shared_market_data: source.shared_market_data !== false && dataEnvironment === 'live',
  };
  setStoredBrokerMode(brokerMode);
  setStoredMarketDataMode(dataEnvironment);
  document.querySelectorAll('[data-broker-mode-badge]').forEach((node) => {
    node.className = `env-badge broker-badge broker-${brokerMode} env-${brokerMode}`;
    node.textContent = `Broker ${getEnvironmentLabel(brokerMode)}`;
  });
  document.querySelectorAll('[data-shared-data-badge]').forEach((node) => {
    node.className = `env-badge data-badge data-${dataEnvironment} env-${dataEnvironment}`;
    node.textContent = dataEnvironment === 'live' ? SHARED_DATA_LABEL : `Data ${getEnvironmentLabel(dataEnvironment)}`;
  });
  document.querySelectorAll('[data-page-context-meta]').forEach((node) => {
    if (typeof setPageContextMeta === 'function' && Array.isArray(window.__ibkrPageContextSourceMetaItems)) {
      setPageContextMeta(window.__ibkrPageContextSourceMetaItems);
    }
  });
  return window.__ibkrBrokerModeContext;
}

function syncBrokerModeFromPayload(payload = {}) {
  if (!payload || typeof payload !== 'object') return getBrokerModeContext();
  return setBrokerModeContext(payload);
}

function renderBrokerModeBadge() {
  const context = getBrokerModeContext();
  return `<span class="env-badge broker-badge broker-${context.broker_mode} env-${context.broker_mode}" data-broker-mode-badge>Broker ${getEnvironmentLabel(context.broker_mode)}</span>`;
}

function renderSharedDataBadge() {
  const context = getBrokerModeContext();
  const label = context.data_environment === 'live'
    ? SHARED_DATA_LABEL
    : `Data ${getEnvironmentLabel(context.data_environment)}`;
  return `<span class="env-badge data-badge data-${context.data_environment} env-${context.data_environment}" data-shared-data-badge>${label}</span>`;
}

function getCurrentEtDateString(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  const safeDate = Number.isNaN(date.getTime()) ? new Date() : date;
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).format(safeDate);
  } catch (_) {
    return safeDate.toISOString().slice(0, 10);
  }
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
    includeModeParams = true,
    environment: explicitEnvironment = '',
    brokerMode: explicitBrokerMode = '',
    dataEnvironment: explicitDataEnvironment = ''
  } = options;
  const url = new URL(path, window.location.origin);
  const explicitText = String(explicitEnvironment || '').trim().toLowerCase();
  const explicitIsBrokerMode = explicitText === 'paper';
  const currentEnvironment = allowGlobal
    ? getCurrentConfigEnvironment()
    : (explicitIsBrokerMode ? getCurrentBrokerMode() : getCurrentRuntimeEnvironment());
  const normalizedExplicitEnvironment = explicitEnvironment
    ? (allowGlobal
        ? normalizeConfigEnvironment(explicitEnvironment, 'global')
        : (explicitIsBrokerMode
            ? normalizeBrokerMode(explicitEnvironment, 'paper')
            : normalizeRuntimeEnvironment(explicitEnvironment, 'live')))
    : '';
  const environment = normalizedExplicitEnvironment || getPendingEnvironment(allowGlobal) || currentEnvironment;

  if (includeEnvironment && environment) {
    url.searchParams.set('environment', environment);
  }

  if (!allowGlobal && includeModeParams) {
    const brokerMode = explicitBrokerMode
      ? normalizeBrokerMode(explicitBrokerMode, getCurrentBrokerMode())
      : explicitIsBrokerMode
      ? normalizeBrokerMode(explicitEnvironment, getCurrentBrokerMode())
      : getCurrentBrokerMode();
    const dataEnvironment = explicitDataEnvironment
      ? normalizeRuntimeEnvironment(explicitDataEnvironment, getSharedDataEnvironment())
      : explicitIsBrokerMode
      ? getSharedDataEnvironment()
      : normalizeRuntimeEnvironment(environment, getCurrentRuntimeEnvironment());
    url.searchParams.set('broker_mode', brokerMode);
    url.searchParams.set('market_data_mode', dataEnvironment);
    url.searchParams.set('data_environment', dataEnvironment);
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
  if (!allowGlobal) {
    return renderBrokerModeBadge();
  }
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
  if (!allowGlobal) {
    return renderBrokerModeBadge();
  }
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

async function refreshBrokerModeContext() {
  if (typeof window === 'undefined') return getBrokerModeContext();
  try {
    const headers = {};
    const token = typeof getToken === 'function' ? getToken() : '';
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await fetchWithRetry(
      `${BASE_URL}${buildPageUrl('/api/custom/ibkr/runtime/config', {}, { environment: getCurrentBrokerMode() })}`,
      { method: 'GET', headers },
      { attempts: 1 }
    );
    if (!response.ok) return getBrokerModeContext();
    const payload = await response.json();
    return syncBrokerModeFromPayload(payload);
  } catch (_) {
    return getBrokerModeContext();
  }
}

if (typeof window !== 'undefined') {
  window.refreshBrokerModeContext = refreshBrokerModeContext;
  window.setTimeout(() => {
    refreshBrokerModeContext();
  }, 50);
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

  let url = `${COLLECTIONS_BASE_URL}/api/collections/${collection}/records`;

  // 构建查询参数
  const queryParams = new URLSearchParams();
  if (params.filter) queryParams.append('filter', params.filter);
  if (params.sort) queryParams.append('sort', params.sort);
  if (params.perPage) queryParams.append('perPage', params.perPage);
  if (params.page) queryParams.append('page', params.page);
  if (params.fields) queryParams.append('fields', params.fields);
  if (params.skipTotal != null) queryParams.append('skipTotal', params.skipTotal);

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

function getSharedDataCenter() {
  return (typeof window !== 'undefined' && window.IbkrDataCenter)
    ? window.IbkrDataCenter
    : null;
}

function getSharedDataCacheKey(namespace, payload) {
  if (typeof buildDataCacheKey === 'function') {
    return buildDataCacheKey(namespace, payload);
  }
  return `${namespace}:${JSON.stringify(payload || {})}`;
}

function getCachedValue(key, loader, cacheOptions = {}) {
  const dataCenter = getSharedDataCenter();
  if (!dataCenter || typeof dataCenter.get !== 'function') {
    return loader();
  }
  return dataCenter.get(key, loader, cacheOptions);
}

function cachedApiFetch(collection, params = {}, cacheOptions = {}) {
  const key = getSharedDataCacheKey('apiFetch', { collection, params });
  return getCachedValue(key, () => apiFetch(collection, params), {
    tags: ['apiFetch', collection].concat(cacheOptions.tags || []),
    ...cacheOptions
  });
}

function buildCustomJsonRequestPath(path, environment) {
  const sourcePath = String(path || '');
  return buildPageUrl(sourcePath, {}, { environment });
}

async function customJsonFetch(path, environment = '', requestOptions = {}) {
  const token = getToken();
  const headers = {
    ...(requestOptions.headers || {})
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const requestBody = requestOptions.body && typeof requestOptions.body === 'object'
    ? JSON.stringify(requestOptions.body)
    : requestOptions.body;
  if (requestOptions.body && typeof requestOptions.body === 'object' && !headers['Content-Type'] && !headers['content-type']) {
    headers['Content-Type'] = 'application/json';
  }
  const requestPath = buildCustomJsonRequestPath(path, environment);
  const response = await fetchWithRetry(
    `${BASE_URL}${requestPath}`,
    {
      ...requestOptions,
      body: requestBody,
      method: requestOptions.method || 'GET',
      headers
    },
    {
      attempts: requestOptions.retryAttempts || 3,
      retryDelayMs: requestOptions.retryDelayMs || 500
    }
  );
  if (response.status === 401 || response.status === 403) {
    handleAuthError();
    throw new Error('Authentication failed');
  }
  const payload = await response.json();
  if (!response.ok || payload?.ok === false) {
    throw new Error(payload?.error || payload?.message || `Request failed (${response.status})`);
  }
  return payload;
}

function isCustomJsonRequestOptions(options) {
  if (!options || typeof options !== 'object') return false;
  return ['headers', 'method', 'body', 'retryAttempts', 'retryDelayMs'].some((key) => Object.prototype.hasOwnProperty.call(options, key));
}

function getCustomJsonCacheRequestKey(requestOptions = {}) {
  const method = String(requestOptions.method || 'GET').toUpperCase();
  const keyOptions = { method };
  if (requestOptions.body != null) keyOptions.body = requestOptions.body;
  if (requestOptions.headers && typeof requestOptions.headers === 'object') keyOptions.headers = requestOptions.headers;
  return keyOptions;
}

function cachedCustomJson(path, environment = '', requestOptionsOrCacheOptions = {}, maybeCacheOptions = undefined) {
  let requestOptions = {};
  let cacheOptions = {};
  if (maybeCacheOptions !== undefined) {
    requestOptions = requestOptionsOrCacheOptions || {};
    cacheOptions = maybeCacheOptions || {};
  } else if (isCustomJsonRequestOptions(requestOptionsOrCacheOptions)) {
    requestOptions = requestOptionsOrCacheOptions || {};
  } else {
    cacheOptions = requestOptionsOrCacheOptions || {};
  }
  const key = getSharedDataCacheKey('customJson', {
    path,
    environment,
    request: getCustomJsonCacheRequestKey(requestOptions)
  });
  return getCachedValue(key, () => customJsonFetch(path, environment, requestOptions), {
    tags: ['customJson'].concat(cacheOptions.tags || []),
    ...cacheOptions
  });
}

async function fetchCollectionFullListUncached(collection, options = {}) {
  const perPage = Math.max(1, Math.min(Number(options.perPage || 200), 200));
  const maxPages = Math.max(1, Number(options.maxPages || 20));
  const startPage = Math.max(1, Number(options.startPage || 1));
  const items = [];

  for (let page = startPage; page <= maxPages; page += 1) {
    const result = await apiFetch(collection, {
      filter: options.filter || '',
      sort: options.sort || '',
      fields: options.fields || '',
      skipTotal: options.skipTotal,
      perPage,
      page
    });
    const pageItems = Array.isArray(result?.items) ? result.items : [];
    items.push(...pageItems);
    if (pageItems.length < perPage) break;
  }

  return items;
}

async function fetchCollectionFullList(collection, options = {}) {
  return fetchCollectionFullListUncached(collection, options);
}

function fetchCollectionFullListCached(collection, options = {}, cacheOptions = {}) {
  const key = getSharedDataCacheKey('collectionFullList', { collection, options });
  return getCachedValue(key, () => fetchCollectionFullListUncached(collection, options), {
    tags: ['apiFetch', 'collectionFullList', collection].concat(cacheOptions.tags || []),
    ...cacheOptions
  });
}

async function countFetch(collection, filter = '', options = {}) {
  const result = await apiFetch(collection, {
    filter: filter || '',
    perPage: 1,
    page: 1,
    skipTotal: false,
    fields: options.fields || 'id'
  });
  return Number(result?.totalItems || 0);
}

function cachedCountFetch(collection, filter = '', cacheOptions = {}) {
  const key = getSharedDataCacheKey('countFetch', { collection, filter });
  return getCachedValue(key, () => countFetch(collection, filter), {
    tags: ['apiFetch', 'countFetch', collection].concat(cacheOptions.tags || []),
    ...cacheOptions
  });
}

let realtimeQuoteCache = {};
let realtimeQuoteCacheUpdatedAt = 0;
const DEFAULT_REALTIME_QUOTE_MAX_AGE_S = 600;

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

function isFreshRealtimeQuote(quote, maxAgeS = DEFAULT_REALTIME_QUOTE_MAX_AGE_S) {
  if (!quote || typeof quote !== 'object') return false;
  if (quote.quote_fallback === true) return false;
  const price = Number(quote.last_price);
  if (!Number.isFinite(price) || price <= 0) return false;
  const age = Number(quote.quote_age_s);
  const boundedMaxAge = Math.max(0, Number(maxAgeS) || 0);
  return Number.isFinite(age) && age <= boundedMaxAge;
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

async function fetchRealtimeQuotesIfNeeded(symbols = [], { reset = false, force = false, maxAgeMs = 15000, swrMs = 15000 } = {}) {
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

  if (reset || force) {
    return fetchRealtimeQuotes(uniqueSymbols, { reset });
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
  const dataCenter = getSharedDataCenter();
  if (!dataCenter || typeof dataCenter.get !== 'function') {
    return fetchRealtimeQuotes(fetchSymbols, { reset: false });
  }

  const key = getSharedDataCacheKey('realtimeQuotes', { symbols: fetchSymbols.slice().sort() });
  const payload = await dataCenter.get(
    key,
    () => fetchRealtimeQuotes(fetchSymbols, { reset: false }),
    {
      ttlMs: boundedMaxAgeMs,
      swrMs,
      tags: ['realtimeQuotes'],
      onRefresh: (freshPayload) => {
        cacheRealtimeQuoteItems(freshPayload?.items || [], {
          reset: false,
          requestedSymbols: fetchSymbols
        });
      }
    }
  );
  cacheRealtimeQuoteItems(payload?.items || [], {
    reset: false,
    requestedSymbols: fetchSymbols
  });
  return payload;
}

function mergeIndicatorWithRealtimeQuote(indicator, realtimeQuoteOverride = null) {
  if (!indicator) return null;
  const merged = { ...indicator };
  const quote = realtimeQuoteOverride || getRealtimeQuote(merged.symbol);
  if (!isFreshRealtimeQuote(quote)) {
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

if (typeof window !== 'undefined') {
  window.normalizeBrokerMode = normalizeBrokerMode;
  window.getCurrentBrokerMode = getCurrentBrokerMode;
  window.getCurrentDataEnvironment = getCurrentDataEnvironment;
  window.getCurrentMarketDataMode = getCurrentMarketDataMode;
  window.getSharedDataEnvironment = getSharedDataEnvironment;
  window.getConsoleBrokerMode = getConsoleBrokerMode;
  window.apiFetch = apiFetch;
  window.cachedApiFetch = cachedApiFetch;
  window.cachedCustomJson = cachedCustomJson;
  window.fetchCollectionFullList = window.fetchCollectionFullList || fetchCollectionFullList;
  window.fetchCollectionFullListCached = fetchCollectionFullListCached;
  window.cachedCountFetch = cachedCountFetch;
  window.fetchRealtimeQuotes = fetchRealtimeQuotes;
  window.fetchRealtimeQuotesIfNeeded = fetchRealtimeQuotesIfNeeded;
  window.getRealtimeQuote = getRealtimeQuote;
  window.isFreshRealtimeQuote = isFreshRealtimeQuote;
  window.mergeIndicatorWithRealtimeQuote = mergeIndicatorWithRealtimeQuote;
}
