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

function getIbkrDataHealthStatus(ageMin, { noDataStatus = 'no_data' } = {}) {
    const numericAge = Number(ageMin);
    if (!Number.isFinite(numericAge) || numericAge < 0) return noDataStatus;
    if (numericAge <= 5) return 'online';
    if (numericAge <= 15) return 'delayed';
    return 'offline';
}

function buildIbkrDataHealth(lastBarTimeMs, { symbol = '', noDataStatus = 'no_data' } = {}) {
    const numericTime = Number(lastBarTimeMs || 0) || 0;
    if (numericTime <= 0) {
        return {
            status: noDataStatus,
            last_bar_age_min: null,
            last_symbol: String(symbol || ''),
            last_bar_time_ms: 0,
            last_bar_label: '--',
        };
    }

    const ageMin = Math.max(0, Math.round((Date.now() - numericTime) / 60000));
    return {
        status: getIbkrDataHealthStatus(ageMin, { noDataStatus }),
        last_bar_age_min: ageMin,
        last_symbol: String(symbol || ''),
        last_bar_time_ms: numericTime,
        last_bar_label: formatBarTimeMsToET(numericTime),
    };
}

function getIbkrFreshnessVisualState(ageMin) {
    const numericAge = Number(ageMin);
    if (!Number.isFinite(numericAge) || numericAge < 0) {
        return {
            color: '#64748b',
            pct: 0,
            chipClass: 'is-empty',
            stateText: '缺失',
            ageLabel: '--',
        };
    }

    if (numericAge <= 2) {
        return {
            color: '#22c55e',
            pct: 100,
            chipClass: 'is-fresh',
            stateText: '正常',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= 5) {
        return {
            color: '#eab308',
            pct: 72,
            chipClass: 'is-warn',
            stateText: '延迟',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= 15) {
        return {
            color: '#f97316',
            pct: 45,
            chipClass: 'is-warn',
            stateText: '偏慢',
            ageLabel: `${numericAge}m`,
        };
    }

    return {
        color: '#ef4444',
        pct: 18,
        chipClass: 'is-stale',
        stateText: '滞后',
        ageLabel: `${numericAge}m`,
    };
}

function getIbkrEngineViewModel(key, engine = {}, defaultEnvironment = '') {
    const safeKey = String(key || '');
    const [environment, symbol = '', interval = ''] = safeKey.split(':');
    const effectiveEnvironment = environment || defaultEnvironment || '';
    const environmentLabel = getEnvironmentLabel(effectiveEnvironment);
    const hasSymbol = Boolean(symbol);
    const normalizedInterval = interval || '5m';
    return {
        key: safeKey,
        environment: effectiveEnvironment,
        environmentLabel,
        symbol,
        interval,
        displayName: hasSymbol ? `${symbol}${interval ? ` · ${interval}` : ''}` : safeKey,
        subtitle: hasSymbol ? environmentLabel : `${environmentLabel} · ${safeKey}`,
        chartHref: hasSymbol
            ? buildPageUrl('/ibkr_chart.html', { symbol, interval: normalizedInterval }, { environment: effectiveEnvironment })
            : '',
        ready: Boolean(engine?.is_ready),
        readyLabel: engine?.is_ready ? 'READY' : 'WARMING',
        barCount: Number(engine?.bar_count || 0) || 0,
        lastCloseLabel: engine?.last_close != null ? `$${Number(engine.last_close).toFixed(2)}` : '--',
        lastBarLabel: engine?.last_bar_time_ms ? formatBarTimeMsToET(engine.last_bar_time_ms) : '--',
    };
}

function isIbkrGatewayActive(runtime = {}) {
    return Boolean(runtime?.gateway?.running || runtime?.gateway?.reachable);
}

function getIbkrRuntimeStarted(runtime = {}) {
    return Boolean(
        runtime?.starting
        || runtime?.session?.running
        || runtime?.websocket?.running
        || runtime?.order_tracker?.running
    );
}

function normalizeIbkrComputeHealth(health = {}) {
    return {
        status: health?.ibkr_compute?.status || health?.status || 'unknown',
        last_compute: health?.ibkr_compute?.last_compute || health?.last_compute || null,
        last_scan: health?.ibkr_compute?.last_scan || health?.last_scan || null,
        error_count: health?.ibkr_compute?.error_count ?? health?.error_count ?? 0,
        uptime_s: health?.ibkr_compute?.uptime_s ?? health?.uptime_s ?? 0
    };
}

function getIbkrDataStatusCardModel(dataHealth = {}) {
    let dotClass = 'dot-gray';
    let mainText = dataHealth?.status || '无数据';

    if (dataHealth?.status === 'online') {
        dotClass = 'dot-green';
        mainText = '在线';
    } else if (dataHealth?.status === 'delayed') {
        dotClass = 'dot-yellow';
        mainText = '延迟';
    } else if (dataHealth?.status === 'offline') {
        dotClass = 'dot-red';
        mainText = '离线';
    }

    if (dataHealth?.last_bar_age_min != null && dataHealth.last_bar_age_min !== '') {
        mainText += ` · ${String(dataHealth.last_bar_age_min)}m`;
    }

    const metaParts = [];
    if (dataHealth?.last_bar_time_ms) {
        metaParts.push(formatTimeLabel(dataHealth.last_bar_time_ms));
    }
    if (dataHealth?.last_symbol) {
        metaParts.push(String(dataHealth.last_symbol));
    }

    const bucketMeta = [];
    if (dataHealth?.bar_bucket_status) bucketMeta.push(`bar bucket ${String(dataHealth.bar_bucket_status)}`);
    if (Number(dataHealth?.pending_symbols_total || 0) > 0) bucketMeta.push(`pending ${Number(dataHealth.pending_symbols_total || 0)}`);
    if (Number(dataHealth?.lag_s || 0) > 0) bucketMeta.push(`lag ${Math.round(Number(dataHealth.lag_s || 0))}s`);
    if (dataHealth?.last_due_bucket_us) bucketMeta.push(`due ${String(dataHealth.last_due_bucket_us)}`);
    if (dataHealth?.last_completed_bucket_us) bucketMeta.push(`done ${String(dataHealth.last_completed_bucket_us)}`);
    if (bucketMeta.length) metaParts.push(bucketMeta.join(' · '));

    return {
        dotClass,
        mainText,
        subText: metaParts.join(' · ')
    };
}

function getIbkrComputeStatusCardModel(compute = {}) {
    const metaParts = [];
    if (Number(compute?.total_engines || 0) > 0) {
        metaParts.push(`${Number(compute.ready_engines || 0)}/${Number(compute.total_engines || 0)} ready`);
    }
    if (Number(compute?.last_realtime_elapsed_s || 0) > 0) metaParts.push(`${Number(compute.last_realtime_elapsed_s || 0).toFixed(2)}s`);
    if (Number(compute?.last_realtime_signals || 0) > 0) metaParts.push(`sig ${Number(compute.last_realtime_signals || 0)}`);
    if (Number(compute?.last_realtime_errors || 0) > 0) metaParts.push(`err ${Number(compute.last_realtime_errors || 0)}`);
    if (Number(compute?.queue_size || 0) > 0) metaParts.push(`queue ${Number(compute.queue_size || 0)}`);
    if (!metaParts.length && compute?.last_realtime_run) metaParts.push(`run ${formatTimeLabel(compute.last_realtime_run)}`);

    if (compute?.status === 'running') {
        return {
            dotClass: 'dot-green',
            mainText: '运行',
            subText: metaParts.join(' · ')
        };
    }

    if (compute?.status === 'offline') {
        return {
            dotClass: 'dot-red',
            mainText: '离线',
            subText: metaParts.join(' · ')
        };
    }

    return {
        dotClass: compute?.status === 'error' ? 'dot-red' : 'dot-gray',
        mainText: compute?.status || '--',
        subText: metaParts.join(' · ')
    };
}

function getIbkrRuntimeStatusCardModel(runtime = {}) {
    const started = getIbkrRuntimeStarted(runtime);
    const gatewayActive = isIbkrGatewayActive(runtime);
    const authenticated = Boolean(runtime?.session?.authenticated);
    const metaParts = [];

    if (gatewayActive) metaParts.push('gateway');
    if (runtime?.session?.running) metaParts.push(authenticated ? 'session 已认证' : 'session 待认证');
    if (runtime?.websocket?.running) metaParts.push('ws 运行');
    if (runtime?.order_tracker?.running) metaParts.push('orders 运行');

    if (started && authenticated) {
        return {
            dotClass: 'dot-green',
            mainText: '认证',
            subText: metaParts.join(' · '),
            started,
            gatewayActive,
            authenticated
        };
    }

    if (started) {
        return {
            dotClass: 'dot-yellow',
            mainText: '待认证',
            subText: metaParts.join(' · '),
            started,
            gatewayActive,
            authenticated
        };
    }

    if (gatewayActive) {
        return {
            dotClass: 'dot-red',
            mainText: '未启动',
            subText: metaParts.join(' · '),
            started,
            gatewayActive,
            authenticated
        };
    }

    return {
        dotClass: 'dot-gray',
        mainText: '离线',
        subText: metaParts.join(' · '),
        started,
        gatewayActive,
        authenticated
    };
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
