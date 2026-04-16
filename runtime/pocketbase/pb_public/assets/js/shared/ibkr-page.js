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
        throw new Error(data.message || data.error || `Request failed (${response.status})`);
    }
    return data;
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

function getSortedEngineEntries(engines) {
    if (!engines || typeof engines !== 'object') return [];
    return Object.entries(engines).sort((a, b) => {
        const readyDiff = Number(Boolean(b[1]?.is_ready)) - Number(Boolean(a[1]?.is_ready));
        if (readyDiff) return readyDiff;
        const barDiff = Number(b[1]?.bar_count || 0) - Number(a[1]?.bar_count || 0);
        if (barDiff) return barDiff;
        return a[0].localeCompare(b[0]);
    });
}

const IBKR_CONFIG_DETAIL_KEYS = [
    'ibkr_compute_public_url',
    'ibkr_compute_enabled',
    'pb_scheduler_enabled',
    'ibkr_bar_publish_enabled',
    'ibkr_trading_enabled',
    'ibkr_signal_source',
    'signal_manual_confirm_enabled',
    'position_limit_max',
    'eod_close_time',
    'eod_keep_symbols',
    'watchlist_interval_min',
    'ibkr_target_refresh_sec',
    'ibkr_target_subscription_limit',
    'ibkr_active_repair_interval_min',
    'ibkr_watchlist_backfill_interval_min',
    'ibkr_watchlist_backfill_batch_size',
    'ibkr_watchlist_backfill_stale_min',
    'trade_window_start_time',
    'trade_window_end_time',
    'order_window_end_time',
    'signal_validity_minutes',
    'order_validity_minutes',
    'signal_poll_interval_sec',
    'reverse_signal_threshold'
];

const IBKR_SYSTEM_PRIMARY_DETAIL_KEYS = new Set([
    'ibkr_signal_source',
    'ibkr_target_refresh_sec',
    'trade_window_start_time',
    'trade_window_end_time',
    'order_window_end_time'
]);

function buildIbkrConfigMap(summary = {}, runtimeConfig = []) {
    const configMap = {};
    const runtimeItems = Array.isArray(runtimeConfig) ? runtimeConfig : [];

    runtimeItems.forEach((item) => {
        if (item?.key && !(item.key in configMap)) {
            configMap[item.key] = item.value;
        }
    });

    Object.entries(summary?.config || {}).forEach(([key, value]) => {
        if (!(key in configMap)) {
            configMap[key] = value;
        }
    });

    if (summary?.ibkr_trading_enabled !== undefined && !('ibkr_trading_enabled' in configMap)) {
        configMap.ibkr_trading_enabled = summary.ibkr_trading_enabled;
    }
    if (summary?.compute_enabled !== undefined && !('ibkr_compute_enabled' in configMap)) {
        configMap.ibkr_compute_enabled = summary.compute_enabled;
    }

    return configMap;
}

function getOrderedIbkrConfigEntries(configMap, keys = IBKR_CONFIG_DETAIL_KEYS) {
    if (!configMap || typeof configMap !== 'object') return [];
    return keys
        .filter((key) => configMap[key] !== undefined && configMap[key] !== '')
        .map((key) => ({ key, value: configMap[key] }));
}

function formatIbkrConfigKeyLabel(key) {
    return String(key || '').trim().toUpperCase().replace(/_/g, ' ');
}

function getIbkrConfigDisplayState(value, emptyDisplay = '--') {
    const raw = value === undefined || value === null ? '' : String(value).trim();
    if (!raw) {
        return {
            display: emptyDisplay,
            tone: 'neutral',
        };
    }

    const normalized = raw.toLowerCase();
    if (normalized === 'true') {
        return {
            display: 'ON',
            tone: 'on',
        };
    }
    if (normalized === 'false') {
        return {
            display: 'OFF',
            tone: 'off',
        };
    }

    return {
        display: raw,
        tone: 'neutral',
    };
}

function getIbkrTradeWindowLabel(configMap = {}) {
    const parts = [
        configMap?.trade_window_start_time,
        configMap?.trade_window_end_time
    ].filter((value) => value !== undefined && value !== null && String(value).trim() !== '');
    return parts.length ? parts.join(' - ') : '--';
}

function getIbkrSystemPrimaryConfigItems(summary = {}, configMap = {}) {
    return [
        { label: 'TRADING', value: summary?.ibkr_trading_enabled ?? configMap?.ibkr_trading_enabled ?? '--' },
        { label: 'COMPUTE', value: summary?.compute_enabled ?? configMap?.ibkr_compute_enabled ?? '--' },
        { label: 'PB SCHEDULER', value: configMap?.pb_scheduler_enabled ?? '--' },
        { label: 'BAR PUBLISH', value: configMap?.ibkr_bar_publish_enabled ?? '--' },
        { label: 'SIGNAL SOURCE', value: configMap?.ibkr_signal_source ?? '--' },
        {
            label: 'TARGET REFRESH',
            value: configMap?.ibkr_target_refresh_sec ? `${configMap.ibkr_target_refresh_sec}s` : '--'
        },
        { label: 'TRADE WINDOW', value: getIbkrTradeWindowLabel(configMap) },
        { label: 'ORDER CUT', value: configMap?.order_window_end_time ?? '--' }
    ];
}

function getIbkrSystemSecondaryConfigItems(configMap = {}) {
    const items = [];

    if (configMap.ibkr_compute_public_url) {
        items.push({ label: 'COMPUTE URL', value: configMap.ibkr_compute_public_url });
    }

    getOrderedIbkrConfigEntries(configMap).forEach(({ key, value }) => {
        if (key === 'ibkr_compute_public_url' || IBKR_SYSTEM_PRIMARY_DETAIL_KEYS.has(key)) return;
        items.push({
            label: formatIbkrConfigKeyLabel(key),
            value
        });
    });

    return items;
}

function getIbkrCronCardData(definition = {}, configMap = {}, environment = '') {
    const state = getCronEffectiveState(definition, configMap);
    return {
        title: definition.display_name || definition.config_display_name || definition.id || 'PB Cron',
        configKey: definition.config_key || definition.id || '-',
        effectiveEnabled: state.effectiveEnabled,
        schedulerEnabled: state.schedulerEnabled,
        cronEnabled: state.cronEnabled,
        functionSummary: definition.function_summary || '--',
        primaryCycleLabel: definition.beijing_cycle_label || definition.cycle_label || '--',
        utcCycleLabel: definition.cycle_label || '--',
        etCycleLabel: definition.et_cycle_label || definition.cycle_label || '--',
        cronExpr: definition.cron_expr || '--',
        windowLabel: definition.window_label || '--',
        environmentLabel: getEnvironmentLabel(environment),
    };
}

function getIbkrCronCardDataList(definitions, configMap = {}, environment = '') {
    if (!Array.isArray(definitions)) return [];
    return definitions.map((definition) => getIbkrCronCardData(definition, configMap, environment));
}
