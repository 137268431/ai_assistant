// Shared IBKR page primitives: formatting, auth headers, request helpers, and base scheduler state.

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatTimeLabel(value) {
    if (value === undefined || value === null || value === '') return '--';
    const text = String(value).trim();
    if (!text) return '--';

    if (/^\d{10,13}$/.test(text)) {
        const timestamp = text.length === 10 ? Number(text) * 1000 : Number(text);
        return formatBarTimeMsToET(timestamp);
    }

    if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
        return formatTime(text, 'America/New_York', 'default');
    }

    if (/^\d{4}-\d{2}-\d{2}/.test(text)) return text;
    return text;
}

function formatIbkrTimeLabelWithFallback(value) {
    const formatted = formatTimeLabel(value);
    if (formatted && formatted !== '--') return formatted;
    const raw = String(value || '').trim();
    if (!raw) return '--';
    return raw.replace('T', ' ').slice(0, 19);
}

function formatIbkrSecondsLabel(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--';
    if (number < 1) return `${number.toFixed(3)}s`;
    if (number < 10) return `${number.toFixed(2)}s`;
    if (number < 60) return `${number.toFixed(1)}s`;
    return `${Math.round(number)}s`;
}

function getIbkrRecordBarLabel(record) {
    return getIbkrBarStartLabel(record);
}

function normalizeIbkrInterval(value, fallback = '') {
    const text = String(value ?? '').trim().toLowerCase();
    if (!text) return String(fallback ?? '').trim().toLowerCase();
    const mapping = {
        '1': '1m',
        '1m': '1m',
        '5': '5m',
        '5m': '5m',
        '15': '15m',
        '15m': '15m',
        '30': '30m',
        '30m': '30m',
        '60': '1h',
        '1h': '1h',
        '240': '4h',
        '4h': '4h',
        'd': '1d',
        '1d': '1d',
    };
    return mapping[text] || text;
}

function formatSymbolPreview(values, limit = 4) {
    if (!Array.isArray(values)) return '';
    const normalized = [];
    const seen = new Set();
    for (let i = 0; i < values.length; i++) {
        const symbol = String(values[i] ?? '').trim().toUpperCase();
        if (!symbol || seen.has(symbol)) continue;
        seen.add(symbol);
        normalized.push(symbol);
    }
    if (!normalized.length) return '';
    const clipped = normalized.slice(0, Math.max(1, limit));
    const more = normalized.length - clipped.length;
    return more > 0 ? `${clipped.join(', ')} +${more}` : clipped.join(', ');
}

function formatIbkrIntervalLabel(value, fallback = '--') {
    const normalized = normalizeIbkrInterval(value);
    return normalized || String(fallback ?? '--');
}

function normalizeBooleanText(value, fallback = 'TRUE') {
    const text = String(value ?? fallback).trim().toUpperCase();
    if (!text) return String(fallback || 'TRUE').trim().toUpperCase();
    return text;
}

function isTruthyConfigValue(value, fallback = 'TRUE') {
    const text = normalizeBooleanText(value, fallback);
    return !(text === 'FALSE' || text === '0' || text === 'OFF' || text === 'NO');
}

function getAuthHeaders(extra = {}) {
    const headers = { ...extra };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
}

function withTimeout(promise, ms, label) {
    const timeoutMs = Math.max(1000, Number(ms) || 0);
    return Promise.race([
        promise,
        new Promise((_, reject) => {
            setTimeout(() => {
                reject(new Error(`${label || 'request'} timed out after ${timeoutMs}ms`));
            }, timeoutMs);
        })
    ]);
}

async function requestIbkrPageJson(path, {
    environment = '',
    method = 'GET',
    body = null,
    retryAttempts = 3,
    retryDelayMs = 500
} = {}) {
    const options = {
        method,
        headers: getAuthHeaders(body ? { 'Content-Type': 'application/json' } : {})
    };
    if (body) options.body = JSON.stringify(body);

    const response = await fetchWithRetry(
        buildPageUrl(path, {}, environment ? { environment } : {}),
        options,
        {
            attempts: retryAttempts,
            retryDelayMs
        }
    );
    if (response.status === 401 || response.status === 403) {
        handleAuthError();
        throw new Error('Authentication failed');
    }

    const text = await response.text();
    let data = {};
    try {
        data = text ? JSON.parse(text) : {};
    } catch (_) {
        data = { ok: false, raw: text };
    }

    if (!response.ok) {
        const error = new Error(data.message || data.error || `Request failed (${response.status})`);
        error.status = response.status;
        error.payload = data;
        throw error;
    }
    if (typeof syncBrokerModeFromPayload === 'function') {
        syncBrokerModeFromPayload(data);
    }
    return data;
}

function setIbkrPageLoading(active, title, copy, {
    overlayId = 'pageLoading',
    titleId = 'pageLoadingTitle',
    copyId = 'pageLoadingCopy',
} = {}) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    if (title) {
        const titleEl = document.getElementById(titleId);
        if (titleEl) titleEl.textContent = title;
    }
    if (copy) {
        const copyEl = document.getElementById(copyId);
        if (copyEl) copyEl.textContent = copy;
    }
    overlay.classList.toggle('is-hidden', !active);
}

function ensureIbkrPageAuth(returnPath = `${location.pathname}${location.search}`) {
    const token = getToken();
    if (!token) {
        redirectToLogin(returnPath);
        return false;
    }
    return true;
}

async function requestIbkrEnvironmentJson(path, environment, options = {}) {
    return requestIbkrPageJson(path, {
        environment,
        ...options,
    });
}

function getCronEffectiveState(definition, configMap) {
    const schedulerValue = configMap?.pb_scheduler_enabled ?? 'TRUE';
    const cronValue = configMap?.[definition?.config_key] ?? definition?.default_value ?? 'TRUE';
    const schedulerEnabled = isTruthyConfigValue(schedulerValue, 'TRUE');
    const cronEnabled = isTruthyConfigValue(cronValue, definition?.default_value ?? 'TRUE');
    return {
        schedulerEnabled,
        cronEnabled,
        effectiveEnabled: schedulerEnabled && cronEnabled,
    };
}
