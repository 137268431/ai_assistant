function escapePageUiText(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function coerceRefreshDate(value = new Date()) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (value === undefined || value === null || value === '') return new Date();
  if (typeof value === 'number' || /^\d{10,13}$/.test(String(value).trim())) {
    const raw = Number(value);
    const ms = String(value).trim().length === 10 ? raw * 1000 : raw;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const text = String(value).trim();
  if (!text) return new Date();
  let normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(text) ? text.replace(' ', 'T') : text;
  if (/^\d{4}-\d{2}-\d{2}T/.test(normalized) && !/(Z|[+-]\d{2}:?\d{2})$/.test(normalized)) {
    normalized = `${normalized}Z`;
  }
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatEtRefreshClock(value = new Date()) {
  const date = coerceRefreshDate(value);
  if (!date) return '--';
  const parts = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date).forEach((part) => {
    if (part.type !== 'literal') parts[part.type] = part.value;
  });
  return parts.hour && parts.minute && parts.second
    ? `${parts.hour}:${parts.minute}:${parts.second} ET`
    : '--';
}

function formatEtRefreshDateTime(value = new Date()) {
  const date = coerceRefreshDate(value);
  if (!date) return '--';
  const parts = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date).forEach((part) => {
    if (part.type !== 'literal') parts[part.type] = part.value;
  });
  return parts.month && parts.day && parts.hour && parts.minute && parts.second
    ? `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ET`
    : '--';
}

function buildPageRefreshTimeText(value = new Date(), label = '更新') {
  const prefix = String(label || '更新').trim() || '更新';
  const clock = formatEtRefreshClock(value);
  return `${prefix} ${clock}`;
}

function updatePageRefreshClockText(value = new Date(), label = window.__ibkrPageRefreshTimeLabel || '更新') {
  const text = buildPageRefreshTimeText(value, label);
  window.__ibkrPageRefreshTimeText = text;
  window.__ibkrPageRefreshTimeLabel = String(label || '更新').trim() || '更新';
  document.querySelectorAll('[data-page-refresh-time]').forEach((node) => {
    node.textContent = text;
  });
  return text;
}

function startPageRefreshClock(label = window.__ibkrPageRefreshTimeLabel || '更新', initialValue = new Date()) {
  updatePageRefreshClockText(initialValue, label);
  if (window.__ibkrPageRefreshClockTimer) return window.__ibkrPageRefreshTimeText;
  window.__ibkrPageRefreshClockTimer = window.setInterval(() => {
    updatePageRefreshClockText(new Date(), window.__ibkrPageRefreshTimeLabel || label);
  }, 1000);
  return window.__ibkrPageRefreshTimeText;
}

function setPageRefreshTime(value = new Date(), label = '更新') {
  return startPageRefreshClock(label, value || new Date());
}

function normalizePageContextMetaItem(item) {
  if (item === undefined || item === null || item === '') return null;
  if (typeof item === 'string' || typeof item === 'number') {
    const text = String(item).trim();
    return text ? { label: '', value: text } : null;
  }
  if (typeof item !== 'object') return null;
  const label = String(item.label ?? item.name ?? '').trim();
  const value = String(item.value ?? item.text ?? '').trim();
  if (!label && !value) return null;
  return {
    label,
    value,
    tone: String(item.tone || '').trim(),
    title: String(item.title || item.tip || '').trim(),
    includeInContext: Boolean(item.includeInContext || item.context || item.contextChip),
  };
}

function normalizePageContextMetaLabel(label) {
  return String(label ?? '').trim().toLowerCase().replace(/[\s_-]+/g, '');
}

function extractPageContextDateToken(value) {
  const text = String(value ?? '').trim();
  if (!text || text === '--' || text === '-') return '';
  const dateMatch = text.match(/\b\d{4}-\d{2}-\d{2}\b/);
  return dateMatch ? dateMatch[0] : text;
}

function isPageContextTradingDateLabel(label) {
  const normalized = normalizePageContextMetaLabel(label);
  return [
    '交易日',
    'date',
    'marketdate',
    'targetdate',
    'tradingdate',
    'tradingday',
  ].includes(normalized);
}

function isPageContextMarketSessionLabel(label) {
  const normalized = normalizePageContextMetaLabel(label);
  return [
    '时段',
    'session',
    'marketsession',
    'marketphase',
    'timewindow',
    'markettimewindow',
  ].includes(normalized);
}

function isPageContextMarketSessionPlaceholder(value) {
  const text = String(value || '').trim().toLowerCase();
  return !text
    || ['--', '-', 'n/a'].includes(text)
    || text.includes('待确认')
    || text.includes('确认中')
    || text.includes('loading');
}

function isPageContextBaseMetaLabel(label) {
  const normalized = normalizePageContextMetaLabel(label);
  return [
    'broker',
    'brokermode',
    'data',
    'dataenvironment',
    'marketdata',
    'marketdatamode',
    'environment',
    'env',
  ].includes(normalized) || isPageContextTradingDateLabel(label);
}

function getPageContextUrlTradingDate() {
  if (typeof window === 'undefined') return '';
  const params = new URLSearchParams(window.location.search);
  return extractPageContextDateToken(
    params.get('date')
      || params.get('market_date')
      || params.get('marketDate')
      || params.get('trading_date')
      || ''
  );
}

function getPageContextDomTradingDate() {
  if (typeof document === 'undefined') return '';
  const ids = ['dailyTargetDate', 'marketDate', 'customDate'];
  for (const id of ids) {
    const value = document.getElementById(id)?.value || '';
    const date = extractPageContextDateToken(value);
    if (date) return date;
  }
  return '';
}

function getPageContextFallbackTradingDate() {
  if (typeof getCurrentEtDateString === 'function') {
    return getCurrentEtDateString();
  }
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date());
  } catch (_) {
    return '--';
  }
}

function resolvePageContextTradingDate(items = []) {
  const normalized = (Array.isArray(items) ? items : [])
    .map(normalizePageContextMetaItem)
    .filter(Boolean);
  for (const item of normalized) {
    if (!isPageContextTradingDateLabel(item.label)) continue;
    const date = extractPageContextDateToken(item.value);
    if (date) return date;
  }
  return getPageContextUrlTradingDate()
    || getPageContextDomTradingDate()
    || getPageContextFallbackTradingDate()
    || '--';
}

function resolvePageContextEnvironment(allowGlobal = false) {
  try {
    return allowGlobal ? getCurrentConfigEnvironment() : getCurrentRuntimeEnvironment();
  } catch (_) {
    return allowGlobal ? 'global' : 'live';
  }
}

function getPageContextRuntimeConfigValue(key) {
  const config = typeof window !== 'undefined' && window.__IBKR_RUNTIME_CONFIG__
    ? window.__IBKR_RUNTIME_CONFIG__
    : {};
  return String(config[key] || '').trim();
}

function resolvePageContextBrokerContext(allowGlobal = false, environment = '') {
  if (!allowGlobal && typeof getBrokerModeContext === 'function') return getBrokerModeContext();

  let params = null;
  try {
    params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null;
  } catch (_) {
    params = null;
  }
  const context = typeof window !== 'undefined' && window.__ibkrBrokerModeContext
    ? window.__ibkrBrokerModeContext
    : {};
  const storedBrokerMode = (() => {
    try { return localStorage.getItem('ibkr_broker_mode') || ''; } catch (_) { return ''; }
  })();
  const storedDataEnvironment = (() => {
    try { return localStorage.getItem('ibkr_market_data_mode') || ''; } catch (_) { return ''; }
  })();
  const brokerMode = normalizePageContextBrokerMode(
    context.broker_mode
      || params?.get('broker_mode')
      || getPageContextRuntimeConfigValue('BROKER_MODE')
      || storedBrokerMode
      || environment
      || 'paper',
    'paper'
  );
  const dataEnvironment = normalizePageContextDataEnvironment(
    context.data_environment
      || context.market_data_environment
      || params?.get('market_data_mode')
      || params?.get('data_environment')
      || getPageContextRuntimeConfigValue('MARKET_DATA_MODE')
      || storedDataEnvironment
      || 'live',
    'live'
  );
  return {
    ...context,
    broker_mode: brokerMode,
    environment: brokerMode,
    data_environment: dataEnvironment,
    market_data_environment: dataEnvironment,
  };
}

function normalizePageContextBrokerMode(value, fallback = 'paper') {
  if (typeof normalizeBrokerMode === 'function') return normalizeBrokerMode(value, fallback);
  const text = String(value || fallback || 'paper').trim().toLowerCase();
  return text === 'live' ? 'live' : 'paper';
}

function normalizePageContextDataEnvironment(value, fallback = 'live') {
  if (typeof normalizeRuntimeEnvironment === 'function') return normalizeRuntimeEnvironment(value, fallback);
  const text = String(value || fallback || 'live').trim().toLowerCase();
  return ['live', 'paper', 'backtest'].includes(text) ? text : fallback;
}

function buildPageContextCalendarSignature({ date, brokerMode, dataEnvironment }) {
  const safeDate = extractPageContextDateToken(date);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(safeDate)) return '';
  return [
    normalizePageContextBrokerMode(brokerMode),
    normalizePageContextDataEnvironment(dataEnvironment),
    safeDate,
  ].join('::');
}

function getPageContextCalendarState() {
  if (typeof window === 'undefined') return {};
  const state = window.__ibkrPageContextMarketCalendar;
  return state && typeof state === 'object' ? state : {};
}

function formatPageContextCalendarSource(source) {
  const text = String(source || '').trim().toLowerCase();
  if (text === 'ibkr_schedule') return 'IBKR 日历';
  if (text === 'local_nyse_fallback') return '本地 NYSE 日历';
  return source ? String(source) : '日历';
}

function formatPageContextClosedReason(reason) {
  const text = String(reason || '').trim().toLowerCase();
  if (text === 'weekend') return '周末休市';
  if (text === 'nyse_holiday') return '美股假日休市';
  if (text === 'ibkr_closed') return 'IBKR 闭市';
  if (text.includes('holiday')) return '假日休市';
  if (text.includes('closed')) return '休市';
  return '休市';
}

function formatPageContextMarketDayLabel(calendar) {
  if (!calendar || typeof calendar !== 'object') return '';
  if (calendar.is_closed) return formatPageContextClosedReason(calendar.closed_reason);
  if (calendar.is_trading_day) return '正常交易日';
  return '日历待确认';
}

function buildPageContextMarketCalendarTitle(calendar) {
  if (!calendar || typeof calendar !== 'object') return '';
  const lines = [
    `来源：${formatPageContextCalendarSource(calendar.source)}`,
    `状态：${formatPageContextMarketDayLabel(calendar)}`,
  ];
  const session = calendar.session && typeof calendar.session === 'object' ? calendar.session : {};
  if (session.open_us || session.close_us) {
    lines.push(`美东：${session.open_us || '--'} - ${session.close_us || '--'}`);
  }
  if (session.open_beijing || session.close_beijing) {
    lines.push(`北京：${session.open_beijing || '--'} - ${session.close_beijing || '--'}`);
  }
  if (calendar.next_open_us || calendar.next_open_beijing) {
    lines.push(`下次开盘：${calendar.next_open_us || '--'} ET / ${calendar.next_open_beijing || '--'} 北京`);
  }
  if (calendar.source_error) lines.push(`fallback：${calendar.source_error}`);
  return lines.join('\n');
}

function getPageContextMarketSession(calendar) {
  const source = calendar && typeof calendar === 'object' ? calendar : {};
  const session = source.market_session && typeof source.market_session === 'object'
    ? source.market_session
    : {};
  return { source, session };
}

function formatPageContextMarketSessionLabel(calendar) {
  const { source, session } = getPageContextMarketSession(calendar);
  const kind = String(session.kind || source.session_kind || '').trim().toLowerCase();
  const labels = {
    premarket: '盘前',
    regular: '盘中',
    close_transition: '盘后过渡',
    afterhours: '盘后',
    overnight: '夜盘',
    night: '夜盘',
    closed: '闭市',
  };
  const display = String(session.label_zh || session.display_label || '').trim();
  if (display) return display;
  return labels[kind] || String(session.label || kind || '').trim();
}

function getPageContextMarketSessionTone(calendar) {
  const { source, session } = getPageContextMarketSession(calendar);
  const kind = String(session.kind || source.session_kind || '').trim().toLowerCase();
  if (kind === 'regular') return 'ok';
  if (['premarket', 'close_transition', 'afterhours', 'overnight', 'night'].includes(kind)) return 'shared';
  if (kind === 'closed' || source.is_closed || source.is_trading_day === false) return 'warn';
  return '';
}

function buildPageContextMarketSessionTitle(calendar, fallback = '') {
  const { source, session } = getPageContextMarketSession(calendar);
  if (!source || !Object.keys(source).length) return String(fallback || '');
  const lines = [
    `来源：${formatPageContextCalendarSource(session.source || source.source)}`,
    `状态：${formatPageContextMarketDayLabel(source) || formatPageContextMarketSessionLabel(source) || '日历待确认'}`,
  ];
  const sourceError = String(session.source_error || source.source_error || fallback || '').trim();
  if (session.us_time || session.cn_time) {
    lines.push(`当前：${session.us_time || '--'} ET / ${session.cn_time || '--'} 北京`);
  }
  if (session.regular_open_us || session.regular_close_us) {
    lines.push(`常规美东：${session.regular_open_us || '--'} - ${session.regular_close_us || '--'}`);
  }
  if (session.regular_open_beijing || session.regular_close_beijing) {
    lines.push(`常规北京：${session.regular_open_beijing || '--'} - ${session.regular_close_beijing || '--'}`);
  }
  if (session.extended_open_us || session.extended_close_us) {
    lines.push(`扩展美东：${session.extended_open_us || '--'} - ${session.extended_close_us || '--'}`);
  }
  if (session.extended_open_beijing || session.extended_close_beijing) {
    lines.push(`扩展北京：${session.extended_open_beijing || '--'} - ${session.extended_close_beijing || '--'}`);
  }
  if (session.next_open_us || session.next_open_beijing || source.next_open_us || source.next_open_beijing) {
    lines.push(`下次开盘：${session.next_open_us || source.next_open_us || '--'} ET / ${session.next_open_beijing || source.next_open_beijing || '--'} 北京`);
  }
  if (sourceError) lines.push(`fallback：${sourceError}`);
  return lines.join('\n');
}

function buildPageContextTradingDateItem({ tradingDate, allowGlobal, brokerMode, dataEnvironment }) {
  const safeDate = tradingDate || '--';
  const item = { label: '交易日', value: safeDate };
  if (allowGlobal || !/^\d{4}-\d{2}-\d{2}$/.test(safeDate)) return item;

  const signature = buildPageContextCalendarSignature({ date: safeDate, brokerMode, dataEnvironment });
  const state = getPageContextCalendarState();
  if (!signature || state.signature !== signature) return item;

  if (state.status === 'loading') {
    return {
      ...item,
      value: `${safeDate} · 日历确认中`,
    };
  }

  if (state.status === 'error') {
    return {
      ...item,
      value: `${safeDate} · 日历待确认`,
      tone: 'warn',
      title: String(state.error || 'market calendar unavailable'),
    };
  }

  const calendar = state.payload && typeof state.payload === 'object' ? state.payload : {};
  const dayLabel = formatPageContextMarketDayLabel(calendar);
  if (!dayLabel) return item;
  return {
    ...item,
    value: `${safeDate} · ${dayLabel}`,
    tone: calendar.is_closed ? 'warn' : 'ok',
    title: buildPageContextMarketCalendarTitle(calendar),
  };
}

function buildPageContextMarketSessionItem({ tradingDate, brokerMode, dataEnvironment }) {
  const safeDate = tradingDate || '--';
  const item = { label: '时段', value: '日历待确认', tone: 'warn' };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(safeDate)) return item;

  const signature = buildPageContextCalendarSignature({ date: safeDate, brokerMode, dataEnvironment });
  const state = getPageContextCalendarState();
  if (!signature || state.signature !== signature) {
    return { label: '时段', value: '日历确认中' };
  }

  if (state.status === 'loading') {
    return { label: '时段', value: '日历确认中' };
  }

  if (state.status === 'error') {
    return {
      label: '时段',
      value: '日历待确认',
      tone: 'warn',
      title: String(state.error || 'market calendar unavailable'),
    };
  }

  const calendar = state.payload && typeof state.payload === 'object' ? state.payload : {};
  const label = formatPageContextMarketSessionLabel(calendar);
  return {
    label: '时段',
    value: label || '日历待确认',
    tone: label ? getPageContextMarketSessionTone(calendar) : 'warn',
    title: buildPageContextMarketSessionTitle(calendar),
  };
}

function buildPageContextMetaItems(items = [], options = {}) {
  const normalizedItems = (Array.isArray(items) ? items : [])
    .map(normalizePageContextMetaItem)
    .filter(Boolean);
  const pageAllowGlobal = typeof window !== 'undefined' ? window.__ibkrPageContextAllowGlobal : false;
  const allowGlobal = Boolean(options.allowGlobal ?? pageAllowGlobal);
  const environment = resolvePageContextEnvironment(allowGlobal);
  const brokerContext = resolvePageContextBrokerContext(allowGlobal, environment);
  const brokerMode = brokerContext.broker_mode || environment;
  const dataEnvironment = brokerContext.data_environment || brokerContext.market_data_environment || 'live';
  const tradingDate = resolvePageContextTradingDate(normalizedItems);
  const baseItems = allowGlobal
    ? [{ label: '环境', value: getEnvironmentLabel(environment, true), tone: environment }]
    : [
        { label: 'Broker', value: getEnvironmentLabel(brokerMode), tone: brokerMode },
        {
          label: '数据',
          value: dataEnvironment === 'live' ? 'Shared Data' : getEnvironmentLabel(dataEnvironment),
          tone: dataEnvironment === 'live' ? 'shared' : dataEnvironment,
        },
      ];
  const explicitSessionItem = normalizedItems.find((item) => (
    isPageContextMarketSessionLabel(item.label)
    && !isPageContextMarketSessionPlaceholder(item.value)
  ));
  const sessionItem = explicitSessionItem || buildPageContextMarketSessionItem({ tradingDate, brokerMode, dataEnvironment });
  const extraItems = normalizedItems.filter((item) => (
    item.includeInContext
    && !isPageContextBaseMetaLabel(item.label)
    && !isPageContextMarketSessionLabel(item.label)
  ));
  return [
    ...baseItems,
    buildPageContextTradingDateItem({ tradingDate, allowGlobal, brokerMode, dataEnvironment }),
    sessionItem,
    ...extraItems,
  ].filter(Boolean);
}

function renderPageContextMeta(items = [], options = {}) {
  const normalized = buildPageContextMetaItems(items, options);
  if (!normalized.length) return '';
  return normalized.map((item) => {
    const toneClass = item.tone ? ` is-${escapePageUiText(item.tone)}` : '';
    const titleAttr = item.title ? ` title="${escapePageUiText(item.title)}"` : '';
    return `
      <span class="page-context-meta-chip${toneClass}"${titleAttr}>
        ${item.label ? `<span class="page-context-meta-label">${escapePageUiText(item.label)}</span>` : ''}
        ${item.value ? `<span class="page-context-meta-value">${escapePageUiText(item.value)}</span>` : ''}
      </span>
    `;
  }).join('');
}

function rerenderPageContextMetaOnly() {
  if (typeof document === 'undefined') return [];
  const allowGlobal = typeof window !== 'undefined' ? Boolean(window.__ibkrPageContextAllowGlobal) : false;
  const sourceItems = typeof window !== 'undefined' && Array.isArray(window.__ibkrPageContextSourceMetaItems)
    ? window.__ibkrPageContextSourceMetaItems
    : [];
  const normalized = buildPageContextMetaItems(sourceItems, { allowGlobal });
  if (typeof window !== 'undefined') window.__ibkrPageContextMetaItems = normalized;
  const html = renderPageContextMeta(sourceItems, { allowGlobal });
  document.querySelectorAll('[data-page-context-meta]').forEach((node) => {
    node.innerHTML = html;
    node.classList.toggle('is-empty', !html);
  });
  return normalized;
}

async function fetchPageContextMarketCalendar({ date, brokerMode, dataEnvironment }) {
  if (typeof cachedMarketCalendar === 'function') {
    return cachedMarketCalendar({
      date,
      broker_mode: brokerMode,
      market_data_mode: dataEnvironment,
      data_environment: dataEnvironment,
      symbol: 'SPY',
    }, {
      tags: ['pageContext'],
    });
  }
  const params = new URLSearchParams();
  params.set('date', date);
  params.set('symbol', 'SPY');
  params.set('broker_mode', normalizePageContextBrokerMode(brokerMode));
  params.set('market_data_mode', normalizePageContextDataEnvironment(dataEnvironment));
  params.set('data_environment', normalizePageContextDataEnvironment(dataEnvironment));
  const headers = {};
  const token = typeof getToken === 'function' ? getToken() : '';
  if (token) headers.Authorization = `Bearer ${token}`;
  const fetcher = typeof fetchWithRetry === 'function' ? fetchWithRetry : fetch;
  const response = await fetcher(
    `/api/custom/system/market_calendar?${params.toString()}`,
    { method: 'GET', headers },
    { attempts: 2, retryDelayMs: 300 },
  );
  if (response.status === 401 || response.status === 403) {
    if (typeof handleAuthError === 'function') handleAuthError();
    throw new Error('Authentication failed');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || payload.message || `market_calendar_${response.status}`);
  }
  return payload;
}

function schedulePageContextMarketCalendarLoad(items = [], options = {}) {
  if (typeof window === 'undefined') return;
  const tradingDate = resolvePageContextTradingDate(items);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(tradingDate || '')) return;
  const pageAllowGlobal = Boolean(options.allowGlobal ?? window.__ibkrPageContextAllowGlobal);
  const environment = resolvePageContextEnvironment(pageAllowGlobal);
  const brokerContext = resolvePageContextBrokerContext(pageAllowGlobal, environment);
  const brokerMode = brokerContext.broker_mode || 'paper';
  const dataEnvironment = brokerContext.data_environment || brokerContext.market_data_environment || 'live';
  const signature = buildPageContextCalendarSignature({ date: tradingDate, brokerMode, dataEnvironment });
  if (!signature) return;

  const state = getPageContextCalendarState();
  if (state.signature === signature && ['loading', 'loaded'].includes(String(state.status || ''))) return;
  window.__ibkrPageContextMarketCalendar = { signature, status: 'loading', payload: null, error: '' };
  rerenderPageContextMetaOnly();

  fetchPageContextMarketCalendar({ date: tradingDate, brokerMode, dataEnvironment })
    .then((payload) => {
      const current = getPageContextCalendarState();
      if (current.signature !== signature) return;
      window.__ibkrPageContextMarketCalendar = { signature, status: 'loaded', payload, error: '' };
      rerenderPageContextMetaOnly();
    })
    .catch((error) => {
      const current = getPageContextCalendarState();
      if (current.signature !== signature) return;
      window.__ibkrPageContextMarketCalendar = {
        signature,
        status: 'error',
        payload: null,
        error: error?.message || String(error || 'market calendar unavailable'),
      };
      rerenderPageContextMetaOnly();
    });
}

function setPageContextMeta(items = []) {
  window.__ibkrPageContextSourceMetaItems = Array.isArray(items) ? items : [];
  const normalized = buildPageContextMetaItems(items);
  window.__ibkrPageContextMetaItems = normalized;
  const html = renderPageContextMeta(normalized);
  document.querySelectorAll('[data-page-context-meta]').forEach((node) => {
    node.innerHTML = html;
    node.classList.toggle('is-empty', !html);
  });
  schedulePageContextMarketCalendarLoad(items);
  return normalized;
}

function renderPageRefreshControl(options = {}) {
  const refreshOptions = options && typeof options === 'object' ? options : {};
  const mode = refreshOptions.mode === 'manual' ? 'manual' : 'polling';
  const buttonId = refreshOptions.buttonId || 'refreshBtn';
  const intervalId = refreshOptions.intervalId || 'refreshInterval';
  const buttonLabel = refreshOptions.buttonLabel || '↻';
  const buttonTitle = refreshOptions.buttonTitle || '刷新';
  const buttonClass = refreshOptions.buttonClass || '';
  const selectClass = refreshOptions.selectClass || '';
  const compact = refreshOptions.compact !== false;
  const onClick = refreshOptions.onClick || 'manualRefresh()';
  const onChange = refreshOptions.onChange || 'changeRefreshInterval(this.value)';
  const defaultSeconds = String(refreshOptions.defaultSeconds ?? '30');
  const optionItems = Array.isArray(refreshOptions.options) && refreshOptions.options.length
    ? refreshOptions.options
    : [
        { value: '0', label: '关闭' },
        { value: '5', label: '5秒' },
        { value: '30', label: '30秒' },
        { value: '60', label: '1分钟' },
      ];
  const compactClass = compact ? ' is-compact' : '';
  const safeButtonClass = buttonClass ? ` ${buttonClass}` : '';
  const safeSelectClass = selectClass ? ` ${selectClass}` : '';

  return `
    <div class="refresh-control${compactClass}" data-refresh-mode="${mode}">
      <button
        class="refresh-btn page-refresh-trigger${compactClass}${safeButtonClass}"
        id="${escapePageUiText(buttonId)}"
        type="button"
        title="${escapePageUiText(buttonTitle)}"
        onclick="${escapePageUiText(onClick)}"
      >${escapePageUiText(buttonLabel)}</button>
      ${mode === 'polling' ? `
        <select
          class="refresh-select${safeSelectClass}"
          id="${escapePageUiText(intervalId)}"
          onchange="${escapePageUiText(onChange)}"
        >
          ${optionItems.map((item) => {
            const value = String(item?.value ?? '');
            const label = String(item?.label ?? value);
            return `<option value="${escapePageUiText(value)}" ${value === defaultSeconds ? 'selected' : ''}>${escapePageUiText(label)}</option>`;
          }).join('')}
        </select>
      ` : ''}
    </div>
  `;
}

function renderPageTopSection(options = {}) {
  const section = options && typeof options === 'object' ? options : {};
  const mode = section.mode === 'hero' ? 'hero' : 'compact';
  const titleHtml = section.titleHtml ?? escapePageUiText(section.title || '');
  const copyHtml = section.copyHtml ?? (section.copy ? escapePageUiText(section.copy) : '');
  const kickerHtml = section.kickerHtml ?? (section.kicker ? escapePageUiText(section.kicker) : '');
  const metaHtml = section.metaHtml || '';
  const actionsHtml = section.actionsHtml || '';
  const statusHtml = section.statusHtml || '';
  const modeClass = mode === 'hero' ? 'hero' : 'page-header';
  const titleClass = mode === 'hero' ? 'hero-title' : 'page-title';
  const copyClass = mode === 'hero' ? 'hero-copy' : 'page-copy';
  const kickerClass = mode === 'hero' ? 'hero-kicker' : 'page-kicker';
  const rowClass = mode === 'hero' ? 'hero-top' : 'page-top-section-row';
  const sideClass = mode === 'hero' ? 'hero-actions' : 'page-top-section-side';
  const metaClass = mode === 'hero' ? 'hero-meta' : 'page-top-section-meta';

  return `
    <section class="${modeClass} page-top-section" data-page-top-section="${mode}">
      <div class="${rowClass}">
        <div class="page-top-section-main">
          ${kickerHtml ? `<div class="${kickerClass}">${kickerHtml}</div>` : ''}
          <div class="${titleClass}">${titleHtml}</div>
          ${copyHtml ? `<div class="${copyClass}">${copyHtml}</div>` : ''}
        </div>
        ${(metaHtml || actionsHtml) ? `
          <div class="${sideClass}">
            ${actionsHtml ? `<div class="page-top-section-actions">${actionsHtml}</div>` : ''}
            ${metaHtml ? `<div class="${metaClass}">${metaHtml}</div>` : ''}
          </div>
        ` : ''}
      </div>
      ${statusHtml ? `<div class="page-top-section-status">${statusHtml}</div>` : ''}
    </section>
  `;
}

function coerceClientPaginationInteger(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.floor(number) : fallback;
}

function normalizeClientPaginationPageSizeOptions(options = [], defaultPageSize = 12) {
  const values = (Array.isArray(options) && options.length ? options : [defaultPageSize])
    .map((value) => coerceClientPaginationInteger(value, 0))
    .filter((value) => value > 0);
  const uniqueValues = Array.from(new Set(values));
  return uniqueValues.length ? uniqueValues : [Math.max(1, coerceClientPaginationInteger(defaultPageSize, 12))];
}

function escapePageUiJsString(value) {
  return String(value ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n');
}

function buildClientPaginationCall(handlerName, key, args = []) {
  const handler = String(handlerName || '').trim();
  if (!handler) return '';
  const jsArgs = [];
  if (key !== undefined && key !== null && String(key) !== '') {
    jsArgs.push(`'${escapePageUiJsString(key)}'`);
  }
  jsArgs.push(...args.map((arg) => String(arg)));
  return escapePageUiText(`${handler}(${jsArgs.join(', ')})`);
}

function createClientPaginationModel(rows = [], state = {}, options = {}) {
  const items = Array.isArray(rows) ? rows : [];
  const config = options && typeof options === 'object' ? options : {};
  const currentState = state && typeof state === 'object' ? state : {};
  const pageSizeOptions = normalizeClientPaginationPageSizeOptions(
    config.pageSizeOptions,
    config.pageSize || currentState.pageSize || 12
  );
  const requestedPageSize = coerceClientPaginationInteger(currentState.pageSize, 0);
  const defaultPageSize = coerceClientPaginationInteger(config.pageSize, pageSizeOptions[0] || 12);
  const pageSize = pageSizeOptions.includes(requestedPageSize)
    ? requestedPageSize
    : (pageSizeOptions.includes(defaultPageSize) ? defaultPageSize : pageSizeOptions[0]);
  const total = items.length;
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  const requestedPage = coerceClientPaginationInteger(currentState.page, 1);
  const page = Math.min(Math.max(1, requestedPage || 1), totalPages);
  const start = (page - 1) * pageSize;
  const end = Math.min(total, start + pageSize);
  return {
    page,
    pageSize,
    total,
    totalPages,
    start,
    end,
    hasPrev: page > 1,
    hasNext: page < totalPages,
    pageRows: items.slice(start, end),
    pageSizeOptions,
  };
}

function renderClientPaginationBar(model, options = {}) {
  const pageModel = model && typeof model === 'object' ? model : createClientPaginationModel([], {}, {});
  const config = options && typeof options === 'object' ? options : {};
  const total = Number(pageModel.total || 0);
  const pageSize = Math.max(1, Number(pageModel.pageSize || 1));
  if (config.hideSinglePage !== false && total <= pageSize) return '';

  const label = config.label || 'Records';
  const key = config.key || '';
  const pageAction = config.pageAction || config.onPageChange || 'setClientPaginationPage';
  const pageSizeAction = config.pageSizeAction || config.onPageSizeChange || 'setClientPaginationPageSize';
  const buttonClass = config.buttonClass || 'btn ghost';
  const rootClass = config.rootClass || 'client-pagination page-pagination';
  const shownFrom = total ? Number(pageModel.start || 0) + 1 : 0;
  const shownTo = Number(pageModel.end || 0);
  const statusText = config.statusText || `${label} ${shownFrom}-${shownTo} / ${total}`;
  const pageText = config.pageText || `${pageModel.page} / ${pageModel.totalPages}`;
  const firstLabel = config.firstLabel || '首页';
  const prevLabel = config.prevLabel || '上一页';
  const nextLabel = config.nextLabel || '下一页';
  const lastLabel = config.lastLabel || '末页';
  const sizeLabel = config.sizeLabel || '每页';
  const selectOptions = (Array.isArray(pageModel.pageSizeOptions) ? pageModel.pageSizeOptions : [pageSize])
    .map((value) => {
      const selected = Number(value) === pageSize ? ' selected' : '';
      return `<option value="${escapePageUiText(value)}"${selected}>${escapePageUiText(`${value}/页`)}</option>`;
    })
    .join('');
  const pageCall = (page) => buildClientPaginationCall(pageAction, key, [Number(page || 1)]);
  const pageSizeCall = buildClientPaginationCall(pageSizeAction, key, ['this.value']);

  return `
    <div class="${escapePageUiText(rootClass)}">
      <div class="client-pagination-copy page-pagination-copy">${escapePageUiText(statusText)}</div>
      <div class="client-pagination-actions page-pagination-actions">
        <button class="${escapePageUiText(buttonClass)}" type="button" ${pageModel.hasPrev ? '' : 'disabled'} onclick="${pageCall(1)}">${escapePageUiText(firstLabel)}</button>
        <button class="${escapePageUiText(buttonClass)}" type="button" ${pageModel.hasPrev ? '' : 'disabled'} onclick="${pageCall(Math.max(1, pageModel.page - 1))}">${escapePageUiText(prevLabel)}</button>
        <span class="client-pagination-page page-pagination-page mono">${escapePageUiText(pageText)}</span>
        <button class="${escapePageUiText(buttonClass)}" type="button" ${pageModel.hasNext ? '' : 'disabled'} onclick="${pageCall(Math.min(pageModel.totalPages, pageModel.page + 1))}">${escapePageUiText(nextLabel)}</button>
        <button class="${escapePageUiText(buttonClass)}" type="button" ${pageModel.hasNext ? '' : 'disabled'} onclick="${pageCall(pageModel.totalPages)}">${escapePageUiText(lastLabel)}</button>
        <label class="client-pagination-size page-pagination-size">
          <span>${escapePageUiText(sizeLabel)}</span>
          <select onchange="${pageSizeCall}">
            ${selectOptions}
          </select>
        </label>
      </div>
    </div>
  `;
}

function renderPageLoadingOverlay(options = {}) {
  const overlay = options && typeof options === 'object' ? options : {};
  const overlayId = overlay.overlayId || 'pageLoading';
  const titleId = overlay.titleId || 'pageLoadingTitle';
  const copyId = overlay.copyId || 'pageLoadingCopy';
  const title = overlay.title || '页面加载中';
  const copy = overlay.copy || '正在同步当前页面需要的数据，请稍候。';

  return `
    <div class="page-loading-overlay" id="${escapePageUiText(overlayId)}">
      <div class="page-loading-card">
        <div class="page-loading-title" id="${escapePageUiText(titleId)}">${escapePageUiText(title)}</div>
        <div class="page-loading-copy" id="${escapePageUiText(copyId)}">${escapePageUiText(copy)}</div>
        <div class="page-loading-bars">
          <div class="page-loading-bar"></div>
          <div class="page-loading-bar"></div>
          <div class="page-loading-bar"></div>
        </div>
      </div>
    </div>
  `;
}

function ensurePageLoadingOverlay(options = {}) {
  const overlay = options && typeof options === 'object' ? options : {};
  const overlayId = overlay.overlayId || 'pageLoading';
  let element = document.getElementById(overlayId);
  if (element) return element;
  document.body.insertAdjacentHTML('afterbegin', renderPageLoadingOverlay(overlay));
  element = document.getElementById(overlayId);
  return element;
}

function setPageLoading(active, options = {}) {
  const overlay = options && typeof options === 'object' ? options : {};
  const overlayId = overlay.overlayId || 'pageLoading';
  const titleId = overlay.titleId || 'pageLoadingTitle';
  const copyId = overlay.copyId || 'pageLoadingCopy';
  const element = ensurePageLoadingOverlay(overlay);
  if (!element) return;

  if (overlay.title) {
    const titleEl = document.getElementById(titleId);
    if (titleEl) titleEl.textContent = overlay.title;
  }
  if (overlay.copy) {
    const copyEl = document.getElementById(copyId);
    if (copyEl) copyEl.textContent = overlay.copy;
  }
  element.classList.toggle('is-hidden', !active);
}

function withPageLoading(task, options = {}) {
  setPageLoading(true, options);
  const result = typeof task === 'function' ? task() : task;
  return Promise.resolve(result).finally(() => {
    setPageLoading(false, options);
  });
}

function spinPageRefreshButton(buttonId = 'refreshBtn') {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.classList.add('spinning');
  setTimeout(() => button.classList.remove('spinning'), 600);
}

function renderPageContextBar(title, options = {}) {
  const allowGlobal = Boolean(options.allowGlobal);
  window.__ibkrPageContextAllowGlobal = allowGlobal;
  const description = options.description || '';
  const subtitle = options.subtitle || '';
  const actionsHtml = options.actionsHtml || '';
  const metaItems = Array.isArray(options.metaItems)
    ? options.metaItems
    : (Array.isArray(window.__ibkrPageContextMetaItems) ? window.__ibkrPageContextMetaItems : []);
  window.__ibkrPageContextSourceMetaItems = metaItems;
  const normalizedMetaItems = buildPageContextMetaItems(metaItems, { allowGlobal });
  window.__ibkrPageContextMetaItems = normalizedMetaItems;
  const metaHtml = renderPageContextMeta(normalizedMetaItems, { allowGlobal });
  const safeTitle = title ? escapePageUiText(title) : '';
  const safeDescription = description ? escapePageUiText(description) : '';
  const safeSubtitle = subtitle ? escapePageUiText(subtitle) : '';
  const refreshLabel = window.__ibkrPageRefreshTimeLabel || '更新';
  const refreshText = buildPageRefreshTimeText(new Date(), refreshLabel);
  window.setTimeout(() => startPageRefreshClock(refreshLabel), 0);
  window.setTimeout(() => schedulePageContextMarketCalendarLoad(metaItems, { allowGlobal }), 0);
  return `
    <div class="page-context-bar${safeSubtitle ? ' has-subtitle' : ''}">
      <div class="page-context-main">
        <div class="page-context-title">
          ${safeTitle ? `<span class="page-context-heading">${safeTitle}</span>` : ''}
          ${renderEnvironmentBadge({ allowGlobal })}
          ${allowGlobal ? '' : renderSharedDataBadge()}
          ${safeDescription ? `<span class="page-context-description">${safeDescription}</span>` : ''}
        </div>
        ${safeSubtitle ? `<div class="page-context-subtitle">${safeSubtitle}</div>` : ''}
        <div class="page-context-meta${metaHtml ? '' : ' is-empty'}" data-page-context-meta>${metaHtml}</div>
      </div>
      <div class="page-context-tools">
        ${actionsHtml ? `<div class="page-context-actions">${actionsHtml}</div>` : ''}
        <div class="page-context-refresh-time" data-page-refresh-time>${escapePageUiText(refreshText)}</div>
        ${allowGlobal ? renderEnvironmentSwitcher({ allowGlobal }) : ''}
      </div>
    </div>
  `;
}
