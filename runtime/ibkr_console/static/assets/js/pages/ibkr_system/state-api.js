let refreshTimer = null;
let currentEnvironment = getCurrentRuntimeEnvironment();
let currentFocus = '';
let hasLoadedSystemData = false;
let latestSystemLoadId = 0;
let lastStableIbkrDataHealth = null;
let lastStableTodayStats = null;
let lastSystemSecondaryLoadedAt = 0;
let lastSystemSecondarySnapshot = null;
let latestSchedulerCronPayload = null;
let latestSchedulerCronSummary = null;
let latestConfigCronSummary = null;
let latestConfigCronDefinitions = [];

const TODAY_STATS_KEYS = ['orders', 'ibkr_bars', 'ibkr_signals', 'ibkr_targets', 'tv_webhook_events', 'events'];
const SYSTEM_SECONDARY_REFRESH_MS = 5 * 60 * 1000;
const SYSTEM_CRON_PAGINATION_CONFIG = {
    schedulerOverview: {
        pageSize: 4,
        pageSizeOptions: [4, 8, 12],
        label: 'Scheduler Cron',
    },
    configCronSummary: {
        pageSize: 5,
        pageSizeOptions: [5, 10, 15],
        label: '配置 Cron',
    },
};
const systemCronPaginationState = {
    schedulerOverview: {
        page: 1,
        pageSize: SYSTEM_CRON_PAGINATION_CONFIG.schedulerOverview.pageSize,
    },
    configCronSummary: {
        page: 1,
        pageSize: SYSTEM_CRON_PAGINATION_CONFIG.configCronSummary.pageSize,
    },
};

function getSystemCronPaginationConfig(key) {
    return SYSTEM_CRON_PAGINATION_CONFIG[key] || SYSTEM_CRON_PAGINATION_CONFIG.schedulerOverview;
}

function getSystemCronPaginationState(key) {
    const config = getSystemCronPaginationConfig(key);
    if (!systemCronPaginationState[key]) {
        systemCronPaginationState[key] = {
            page: 1,
            pageSize: config.pageSize,
        };
    }
    return systemCronPaginationState[key];
}

function updateSystemCronPaginationState(key, patch = {}) {
    const config = getSystemCronPaginationConfig(key);
    const current = getSystemCronPaginationState(key);
    const pageSizeOptions = Array.isArray(config.pageSizeOptions) && config.pageSizeOptions.length
        ? config.pageSizeOptions
        : [config.pageSize];
    const requestedPage = Number(patch.page ?? current.page ?? 1);
    const requestedPageSize = Number(patch.pageSize ?? current.pageSize ?? config.pageSize);
    const pageSize = pageSizeOptions.includes(requestedPageSize) ? requestedPageSize : config.pageSize;
    current.page = Number.isFinite(requestedPage) && requestedPage > 0 ? Math.floor(requestedPage) : 1;
    current.pageSize = pageSize;
    return current;
}

function rememberSchedulerCronRenderInputs(cronPayload = {}, summary = {}) {
    latestSchedulerCronPayload = cronPayload || {};
    latestSchedulerCronSummary = summary || {};
}

function rememberConfigCronRenderInputs(summary = {}, cronDefinitions = []) {
    latestConfigCronSummary = summary || {};
    latestConfigCronDefinitions = Array.isArray(cronDefinitions) ? cronDefinitions : [];
}

function rerenderSystemCronPaginationTarget(key) {
    if (key === 'configCronSummary') {
        if (typeof renderConfig === 'function') {
            renderConfig(latestConfigCronSummary || {}, latestConfigCronDefinitions || []);
        }
        return;
    }
    if (typeof renderSchedulerOverview === 'function') {
        renderSchedulerOverview(latestSchedulerCronPayload || {}, latestSchedulerCronSummary || {});
    }
}

window.setSystemCronPage = function(key, page) {
    updateSystemCronPaginationState(key, { page });
    rerenderSystemCronPaginationTarget(key);
};

window.setSystemCronPageSize = function(key, value) {
    updateSystemCronPaginationState(key, {
        page: 1,
        pageSize: Number(value),
    });
    rerenderSystemCronPaginationTarget(key);
};

function normalizeTodayStatValue(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function normalizeTodayStatsPayload(payload = {}) {
    const source = payload && typeof payload === 'object' ? payload : {};
    const normalized = {};
    const orderValue = source.main_orders != null
        ? source.main_orders
        : (source.order_groups != null ? source.order_groups : source.orders);
    const orders = normalizeTodayStatValue(orderValue);
    if (orders !== null) normalized.orders = orders;
    TODAY_STATS_KEYS.filter((key) => key !== 'orders').forEach((key) => {
        const value = normalizeTodayStatValue(source[key]);
        if (value !== null) normalized[key] = value;
    });
    return normalized;
}

function mergeTodayStats(...sources) {
    return sources.reduce((merged, source) => {
        const normalized = normalizeTodayStatsPayload(source);
        TODAY_STATS_KEYS.forEach((key) => {
            if (normalized[key] !== undefined) merged[key] = normalized[key];
        });
        return merged;
    }, {});
}

function cloneTodayStats(today = {}) {
    return mergeTodayStats(today);
}

function rememberStableTodayStats(today = {}) {
    const normalized = normalizeTodayStatsPayload(today);
    if (Object.keys(normalized).length) {
        lastStableTodayStats = {
            ...(lastStableTodayStats || {}),
            ...normalized,
        };
        return cloneTodayStats(lastStableTodayStats);
    }
    return lastStableTodayStats ? cloneTodayStats(lastStableTodayStats) : {};
}

function shouldRefreshSystemSecondary(showToastOnSuccess = false) {
    return Boolean(showToastOnSuccess)
        || !lastSystemSecondaryLoadedAt
        || (Date.now() - lastSystemSecondaryLoadedAt) >= SYSTEM_SECONDARY_REFRESH_MS;
}

function rememberSystemSecondarySnapshot(snapshot = {}) {
    lastSystemSecondaryLoadedAt = Date.now();
    lastSystemSecondarySnapshot = {
        eventsResp: snapshot.eventsResp || { items: [] },
        backtestBatchResp: snapshot.backtestBatchResp || { items: [] },
        backtestRunsResp: snapshot.backtestRunsResp || { items: [] },
        todayStats: cloneTodayStats(snapshot.todayStats || {}),
    };
    return lastSystemSecondarySnapshot;
}

function getLastSystemSecondarySnapshot() {
    return lastSystemSecondarySnapshot || {
        eventsResp: { items: [] },
        backtestBatchResp: { items: [] },
        backtestRunsResp: { items: [] },
        todayStats: {},
    };
}

function cloneIbkrDataHealth(dataHealth = {}) {
    if (!dataHealth || typeof dataHealth !== 'object') return null;
    return {
        ...dataHealth,
        pending_symbols: Array.isArray(dataHealth.pending_symbols) ? dataHealth.pending_symbols.slice() : dataHealth.pending_symbols,
    };
}

function isStableIbkrDataHealth(dataHealth = {}) {
    const status = String(dataHealth?.status || '').trim().toLowerCase();
    return Boolean(status && !['unknown', 'loading'].includes(status));
}

function rememberStableIbkrDataHealth(dataHealth = {}) {
    if (isStableIbkrDataHealth(dataHealth)) {
        lastStableIbkrDataHealth = cloneIbkrDataHealth(dataHealth);
    }
    return dataHealth;
}

function buildSystemComputeSummary(healthPayload = {}, statusPayload = {}, fallbackPayload = {}) {
    const statusCompute = statusPayload?.compute || {};
    const healthCompute = healthPayload?.compute || {};
    const runtimeCompute = statusPayload?.runtime?.realtime_compute || {};
    const fallbackCompute = fallbackPayload || {};
    return {
        status: statusCompute.status || statusPayload.status || fallbackCompute.status || healthPayload.status || 'unknown',
        engines: statusCompute.engines || fallbackCompute.engines || {},
        total_engines: Number(statusCompute.total_engines || fallbackCompute.total_engines || healthPayload.total_engines || 0) || 0,
        ready_engines: Number(statusCompute.ready_engines || fallbackCompute.ready_engines || healthPayload.ready_engines || 0) || 0,
        compute_count: Number(statusCompute.compute_count || fallbackCompute.compute_count || healthPayload.compute_count || 0) || 0,
        error_count: Number(statusCompute.error_count || fallbackCompute.error_count || healthPayload.error_count || 0) || 0,
        uptime_s: Number(statusCompute.uptime_s || fallbackCompute.uptime_s || healthPayload.uptime_s || 0) || 0,
        last_compute: statusCompute.last_compute || fallbackCompute.last_compute || healthPayload.last_compute || null,
        last_scan: statusCompute.last_scan || fallbackCompute.last_scan || healthPayload.last_scan || null,
        queue_size: Number(runtimeCompute.queue_size || fallbackCompute.queue_size || 0) || 0,
        last_realtime_run: runtimeCompute.last_run || fallbackCompute.last_realtime_run || null,
        last_bar_close: runtimeCompute.last_bar_close || fallbackCompute.last_bar_close || null,
        last_realtime_elapsed_s: Number(runtimeCompute.last_elapsed_s || fallbackCompute.last_realtime_elapsed_s || 0) || 0,
        last_realtime_processed: Number(runtimeCompute.last_processed || fallbackCompute.last_realtime_processed || 0) || 0,
        last_realtime_signals: Number(runtimeCompute.last_signals || fallbackCompute.last_realtime_signals || 0) || 0,
        last_realtime_errors: Number(runtimeCompute.last_errors || fallbackCompute.last_realtime_errors || 0) || 0,
        startup_preload: statusCompute.compute_startup_preload
            || statusPayload.compute_startup_preload
            || healthCompute.compute_startup_preload
            || healthPayload.compute_startup_preload
            || fallbackCompute.compute_startup_preload
            || null,
    };
}

function ensureSystemPageTopSection() {
    const statusBar = document.getElementById('statusBar');
    if (statusBar) statusBar.classList.add('page-top-section');
}

function normalizeStartupPreloadState(payload = {}) {
    const source = payload && typeof payload === 'object' ? payload : {};
    const status = String(source.status || '').trim().toLowerCase()
        || (source.running ? 'running' : (source.finished_at ? 'completed' : (source.scheduled ? 'scheduled' : 'idle')));
    const environments = Array.isArray(source.environments)
        ? source.environments.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean)
        : [];
    const results = source.results && typeof source.results === 'object' ? source.results : {};
    const envTotal = Math.max(Number(source.env_total || 0) || 0, environments.length);
    const envCompleted = Number(source.env_completed || 0) || 0;
    const symbolTotal = Number(source.symbol_total || 0) || 0;
    const symbolCompleted = Number(source.symbol_completed || 0) || 0;
    const readyCount = Number(source.ready_count || 0) || 0;

    return {
        enabled: source.enabled !== false,
        should_schedule: source.should_schedule !== false,
        scheduled: source.scheduled === true,
        running: source.running === true,
        status,
        environments,
        env_total: envTotal,
        env_completed: envCompleted,
        symbol_total: symbolTotal,
        symbol_completed: symbolCompleted,
        ready_count: readyCount,
        started_at: source.started_at || null,
        finished_at: source.finished_at || null,
        elapsed_s: Number(source.elapsed_s || 0) || 0,
        reason: String(source.reason || '').trim(),
        error: String(source.error || '').trim(),
        results,
    };
}

function buildStartupPreloadStatusLabel(preload) {
    const state = normalizeStartupPreloadState(preload);
    if (!state.enabled) return '';
    if (state.status === 'running') return `preload RUNNING ${state.symbol_completed}/${state.symbol_total || '--'}`;
    if (state.status === 'scheduled') return `preload SCHEDULED ${state.env_total || 0} env`;
    if (state.status === 'completed') return 'preload DONE';
    if (state.status === 'failed') return 'preload FAILED';
    if (state.status === 'skipped') return 'preload SKIPPED';
    if (state.status === 'disabled') return 'preload DISABLED';
    return '';
}

function formatStartupPreloadTimestamp(value) {
    const raw = String(value || '').trim();
    if (!raw) return '--';
    const formatted = formatTimeLabel(raw);
    if (formatted && formatted !== '--' && formatted !== '-') return formatted;
    return raw.replace('T', ' ').slice(0, 19);
}

function buildStartupPreloadSummary(preload) {
    const state = normalizeStartupPreloadState(preload);
    if (!state.enabled) return '';
    const meta = [];
    if (state.env_total > 0) meta.push(`env ${state.env_completed}/${state.env_total}`);
    if (state.symbol_total > 0) meta.push(`symbols ${state.symbol_completed}/${state.symbol_total}`);
    if (state.status === 'completed') meta.push(`startup ready ${state.ready_count}/${state.symbol_total || 0}`);
    else if (state.ready_count > 0) meta.push(`ready ${state.ready_count}/${state.symbol_total || 0}`);
    if (state.elapsed_s > 0) meta.push(`elapsed ${formatIbkrSecondsLabel(state.elapsed_s)}`);
    const metaText = meta.join(' · ');
    if (state.status === 'running') return `startup preload 正在恢复${metaText ? ` · ${metaText}` : ''}`;
    if (state.status === 'scheduled') return `startup preload 已排队 · ${state.environments.join(', ') || '--'}`;
    if (state.status === 'completed') {
        const finishedLabel = state.finished_at ? formatStartupPreloadTimestamp(state.finished_at) : '--';
        const parts = [];
        if (metaText) parts.push(metaText);
        if (finishedLabel && finishedLabel !== '--') parts.push(`finish ${finishedLabel}`);
        return `startup preload 已完成${parts.length ? ` · ${parts.join(' · ')}` : ''}`;
    }
    if (state.status === 'failed') return `startup preload 失败 · ${state.error || state.reason || 'unknown'}`;
    if (state.status === 'skipped') return `startup preload 已跳过 · ${state.reason || 'no_preload_environments'}`;
    if (state.status === 'disabled') return `startup preload 已禁用 · ${state.reason || 'schedule_guard_blocked'}`;
    return '';
}

function buildStartupPreloadDetail(preload) {
    const state = normalizeStartupPreloadState(preload);
    if (!state.enabled) return '';
    const orderedEnvNames = state.environments.length
        ? state.environments
        : Object.keys(state.results || {});
    const envLines = orderedEnvNames.map((environment) => {
        const item = state.results?.[environment] || {};
        const symbolTotal = Number(item.symbol_total || 0) || 0;
        const symbolCompleted = Number(item.symbol_completed || 0) || 0;
        const readyCount = Number(item.ready_count || 0) || 0;
        const cursorCount = Number(item.cursor_count || 0) || 0;
        const envStatus = String(item.status || 'pending').trim().toLowerCase();
        const meta = [];
        if (cursorCount > 0) meta.push(`cursor ${cursorCount}`);
        if (symbolTotal > 0) meta.push(`symbols ${symbolCompleted}/${symbolTotal}`);
        if (envStatus === 'completed') meta.push(`startup ready ${readyCount}/${symbolTotal || 0}`);
        else if (readyCount > 0) meta.push(`ready ${readyCount}/${symbolTotal || 0}`);
        return `${String(environment || '--').toUpperCase()} ${envStatus.toUpperCase()}${meta.length ? ` · ${meta.join(' · ')}` : ''}`;
    });
    return envLines.join(' | ');
}

const IBKR_FRESHNESS_INTERVALS = ['5m', '15m', '30m', '1h', '4h', '1d'];
const IBKR_FRESHNESS_COUNT_KEYS = [
    'total_symbols',
    'due_symbols',
    'total_checks',
    'due_checks',
    'ready',
    'overdue',
    'missing',
    'waiting_5m',
    'not_due',
    'quiet_extended',
];

function normalizeFreshnessNumber(value, fallback = 0) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : fallback;
}

function normalizeFreshnessPercent(value) {
    if (value === null || value === undefined || value === '') return null;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return null;
    return Math.max(0, Math.min(100, numeric));
}

function normalizeFreshnessSamples(values) {
    if (!Array.isArray(values)) return [];
    const seen = new Set();
    const samples = [];
    values.forEach((item) => {
        const symbol = typeof item === 'object' && item !== null
            ? String(item.symbol || item.ticker || item.name || '').trim().toUpperCase()
            : String(item || '').trim().toUpperCase();
        if (!symbol || seen.has(symbol)) return;
        seen.add(symbol);
        samples.push(symbol);
    });
    return samples;
}

function normalizeFreshnessIntervalItem(item = {}, intervalFallback = '') {
    const source = item && typeof item === 'object' ? item : {};
    const normalized = {
        ...source,
        interval: normalizeIbkrInterval(source.interval || intervalFallback, intervalFallback),
        status: String(source.status || '').trim().toLowerCase(),
        ready_pct: normalizeFreshnessPercent(source.ready_pct),
        coverage_pct: normalizeFreshnessPercent(source.coverage_pct),
        expected_close_ms: normalizeFreshnessNumber(source.expected_close_ms, 0) || 0,
        expected_close_us: String(source.expected_close_us || '').trim(),
        sample_lag_symbols: normalizeFreshnessSamples(source.sample_lag_symbols),
        last_bar_time_ms: normalizeFreshnessNumber(source.last_bar_time_ms, 0) || 0,
        age_min: Number.isFinite(Number(source.age_min)) ? Number(source.age_min) : null,
        symbol: source.symbol || '',
    };
    IBKR_FRESHNESS_COUNT_KEYS.forEach((key) => {
        normalized[key] = Math.max(0, Math.round(normalizeFreshnessNumber(source[key], 0)));
    });
    normalized.has_aggregate_counts = IBKR_FRESHNESS_COUNT_KEYS.some((key) => source[key] != null)
        || source.ready_pct != null
        || source.coverage_pct != null
        || source.expected_close_ms != null
        || source.expected_close_us != null
        || source.status != null;
    return normalized;
}

function normalizeFreshnessOverall(overall = {}) {
    const source = overall && typeof overall === 'object' ? overall : {};
    const normalized = {
        ...source,
        status: String(source.status || '').trim().toLowerCase(),
        ready_pct: normalizeFreshnessPercent(source.ready_pct),
        coverage_pct: normalizeFreshnessPercent(source.coverage_pct),
        expected_close_ms: normalizeFreshnessNumber(source.expected_close_ms, 0) || 0,
        expected_close_us: String(source.expected_close_us || '').trim(),
        checked_at_ms: normalizeFreshnessNumber(source.checked_at_ms, 0) || 0,
        sample_lag_symbols: normalizeFreshnessSamples(source.sample_lag_symbols),
    };
    IBKR_FRESHNESS_COUNT_KEYS.forEach((key) => {
        normalized[key] = Math.max(0, Math.round(normalizeFreshnessNumber(source[key], 0)));
    });
    if (source.total_checks == null) normalized.total_checks = normalized.total_symbols;
    if (source.due_checks == null) normalized.due_checks = normalized.due_symbols;
    normalized.has_aggregate_counts = IBKR_FRESHNESS_COUNT_KEYS.some((key) => source[key] != null)
        || source.ready_pct != null
        || source.coverage_pct != null
        || source.status != null;
    return normalized;
}

function aggregateFreshnessIntervals(intervals = []) {
    const overall = normalizeFreshnessOverall({});
    if (!Array.isArray(intervals) || !intervals.length) return overall;
    intervals.forEach((item) => {
        IBKR_FRESHNESS_COUNT_KEYS.forEach((key) => {
            overall[key] += Math.max(0, Math.round(normalizeFreshnessNumber(item?.[key], 0)));
        });
    });
    if (!overall.total_checks) overall.total_checks = overall.total_symbols;
    if (!overall.due_checks) overall.due_checks = overall.due_symbols;
    if (overall.due_symbols > 0) {
        overall.ready_pct = Math.round((overall.ready / overall.due_symbols) * 1000) / 10;
    } else {
        const readyPctValues = intervals
            .map((item) => item?.ready_pct)
            .filter((value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)));
        overall.ready_pct = readyPctValues.length
            ? Math.round((readyPctValues.reduce((sum, value) => sum + Number(value), 0) / readyPctValues.length) * 10) / 10
            : null;
    }
    if (overall.total_symbols > 0) {
        overall.coverage_pct = Math.round((overall.due_symbols / overall.total_symbols) * 1000) / 10;
    }
    overall.has_aggregate_counts = intervals.some((item) => item?.has_aggregate_counts);
    return overall;
}

function normalizeFreshnessScope(scope = {}, fallbackName = '') {
    const source = scope && typeof scope === 'object' ? scope : {};
    const intervalSource = Array.isArray(source.intervals)
        ? source.intervals.map((item) => [item?.interval, item])
        : (source.intervals && typeof source.intervals === 'object' ? Object.entries(source.intervals) : []);
    const intervals = intervalSource
        .map(([interval, item]) => normalizeFreshnessIntervalItem(item, interval))
        .filter((item) => item.interval);
    let overall = normalizeFreshnessOverall(source.overall || {});
    if (!overall.has_aggregate_counts && intervals.some((item) => item.has_aggregate_counts)) {
        overall = aggregateFreshnessIntervals(intervals);
    }
    return {
        ...source,
        name: String(source.name || fallbackName || '').trim(),
        label: String(source.label || source.name || fallbackName || '').trim(),
        status: String(source.status || overall.status || '').trim().toLowerCase(),
        critical: source.critical !== false,
        best_effort: source.best_effort === true,
        symbols_total: Math.max(0, Math.round(normalizeFreshnessNumber(source.symbols_total, overall.total_symbols || 0))),
        intervals,
        overall,
    };
}

function normalizeFreshnessScopes(scopesPayload = {}) {
    if (!scopesPayload || typeof scopesPayload !== 'object' || Array.isArray(scopesPayload)) return {};
    return Object.entries(scopesPayload).reduce((acc, [name, scope]) => {
        const normalized = normalizeFreshnessScope(scope, name);
        if (normalized.name) acc[normalized.name] = normalized;
        return acc;
    }, {});
}

function normalizeFreshnessPayload(freshnessPayload = []) {
    if (Array.isArray(freshnessPayload)) {
        const intervals = freshnessPayload
            .filter((item) => item && item.interval)
            .map((item) => normalizeFreshnessIntervalItem(item, item.interval));
        return {
            overall: aggregateFreshnessIntervals(intervals),
            intervals,
            scopes: {},
            aggregate: intervals.some((item) => item.has_aggregate_counts),
        };
    }
    if (!freshnessPayload || typeof freshnessPayload !== 'object') {
        return { overall: normalizeFreshnessOverall({}), intervals: [], scopes: {}, aggregate: false };
    }

    const scopes = normalizeFreshnessScopes(freshnessPayload.scopes);
    let intervalEntries = [];
    if (Array.isArray(freshnessPayload.intervals)) {
        intervalEntries = freshnessPayload.intervals.map((item) => [item?.interval, item]);
    } else if (freshnessPayload.intervals && typeof freshnessPayload.intervals === 'object') {
        intervalEntries = Object.entries(freshnessPayload.intervals);
    } else {
        const excluded = new Set([
            'overall',
            'display_overall',
            'thresholds',
            'checked_at_ms',
            'scopes',
            'scope_symbols',
            'primary_scope',
            'primary_status',
        ]);
        intervalEntries = Object.entries(freshnessPayload)
            .filter(([key]) => !excluded.has(String(key || '').toLowerCase()));
    }

    const intervals = intervalEntries
        .map(([interval, item]) => normalizeFreshnessIntervalItem(item, interval))
        .filter((item) => item.interval);
    const primaryScope = String(freshnessPayload.primary_scope || '').trim();
    const displayScope = scopes[primaryScope] || scopes.active_trading || scopes.realtime_5m || null;
    const displayOverallSource = freshnessPayload.display_overall || displayScope?.overall || freshnessPayload.overall || {};
    let overall = normalizeFreshnessOverall(displayOverallSource);
    if (!overall.has_aggregate_counts && intervals.some((item) => item.has_aggregate_counts)) {
        overall = aggregateFreshnessIntervals(intervals);
    }
    const aggregate = Boolean(freshnessPayload.overall || freshnessPayload.intervals)
        || overall.has_aggregate_counts
        || intervals.some((item) => item.has_aggregate_counts)
        || Object.keys(scopes).length > 0;

    return {
        overall,
        legacy_overall: normalizeFreshnessOverall(freshnessPayload.overall || {}),
        intervals,
        scopes,
        primary_scope: primaryScope || displayScope?.name || '',
        aggregate,
    };
}

function normalizeFreshnessItems(freshnessPayload = []) {
    return normalizeFreshnessPayload(freshnessPayload).intervals;
}

function getUsClockParts(date = new Date()) {
    const values = {};
    new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        weekday: 'short',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
    }).formatToParts(date).forEach((part) => {
        if (part.type !== 'literal') values[part.type] = part.value;
    });
    const weekdayMap = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    return {
        date: values.year && values.month && values.day ? `${values.year}-${values.month}-${values.day}` : '',
        hour: Number(values.hour || 0) || 0,
        minute: Number(values.minute || 0) || 0,
        weekday: weekdayMap[values.weekday] ?? -1,
    };
}

function formatUsDateToken(timestampMs) {
    const numeric = Number(timestampMs || 0);
    if (!Number.isFinite(numeric) || numeric <= 0) return '';
    const values = {};
    new Intl.DateTimeFormat('en-US', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
    }).formatToParts(new Date(numeric)).forEach((part) => {
        if (part.type !== 'literal') values[part.type] = part.value;
    });
    return values.year && values.month && values.day ? `${values.year}-${values.month}-${values.day}` : '';
}

function isRuntimeBarFreshnessRequired(runtime = {}, latest5m = null) {
    const clock = getUsClockParts();
    const minuteOfDay = clock.hour * 60 + clock.minute;
    const activeTargetCount = Number(runtime?.market_universe?.active_target_count || 0) || 0;
    const latestBarDate = formatUsDateToken(latest5m?.last_bar_time_ms);
    if (clock.weekday === 0 || clock.weekday === 6) return false;
    if (minuteOfDay < (9 * 60 + 40)) return false;
    if (minuteOfDay > (16 * 60 + 15)) return false;
    return activeTargetCount > 0 || latestBarDate === clock.date;
}

function normalizeBarBucketLagSeconds(lagValue, dueBucketMs, completedBucketMs) {
    const dueMs = Number(dueBucketMs || 0) || 0;
    const completedMs = Number(completedBucketMs || 0) || 0;
    if (dueMs > 0) {
        const referenceMs = completedMs > 0 && completedMs >= dueMs ? completedMs : Date.now();
        return Math.max(0, Math.round((referenceMs - dueMs) / 1000));
    }
    const rawLag = Number(lagValue || 0) || 0;
    if (rawLag > 1000000000000) {
        return Math.max(0, Math.round((Date.now() - rawLag) / 1000));
    }
    if (rawLag > 1000000000) {
        return Math.max(0, Math.round(Date.now() / 1000 - rawLag));
    }
    return rawLag;
}

function buildRuntimeBarDataHealth(runtime = {}, latest5m = null) {
    if (!isRuntimeBarFreshnessRequired(runtime, latest5m)) {
        return null;
    }
    const marketUniverse = runtime?.market_universe || {};
    const barFreshness = marketUniverse?.bar_freshness || {};
    const canonical = runtime?.canonical_5m || {};
    const pendingSymbolsTotal = Number(canonical.pending_symbols_total || barFreshness.pending_symbols_total || 0) || 0;
    const pendingSymbols = Array.isArray(canonical.pending_symbols)
        ? canonical.pending_symbols.slice(0, 8).map((item) => String(item || '').trim().toUpperCase()).filter(Boolean)
        : [];
    const dueBucketMs = Number(canonical.last_due_bucket_ms || 0) || 0;
    const completedBucketMs = Number(canonical.last_completed_bucket_ms || 0) || 0;
    const lagS = normalizeBarBucketLagSeconds(
        Number(canonical.lag_s || barFreshness.lag_s || 0) || 0,
        dueBucketMs,
        completedBucketMs
    );
    const bucketStale = String(barFreshness.status || '').trim().toLowerCase() === 'stale'
        || (dueBucketMs > 0 && (completedBucketMs <= 0 || completedBucketMs < dueBucketMs));
    if (!bucketStale) {
        return null;
    }
    return {
        status: 'offline',
        bar_bucket_status: String(barFreshness.status || 'stale'),
        pending_symbols_total: pendingSymbolsTotal,
        pending_symbols: pendingSymbols,
        lag_s: lagS,
        last_due_bucket_us: String(canonical.last_due_bucket_us || ''),
        last_completed_bucket_us: String(canonical.last_completed_bucket_us || barFreshness.last_completed_bucket_us || ''),
    };
}

function getDataHealthStatusFromFreshness(overall = {}) {
    const status = String(overall?.status || '').trim().toLowerCase();
    if (['ready', 'fresh', 'ok', 'online', 'healthy', 'not_due', 'quiet_extended', 'closed_session'].includes(status)) {
        return 'online';
    }
    if (['warn', 'warning', 'delayed', 'degraded', 'partial', 'waiting_5m'].includes(status)) {
        return 'delayed';
    }
    if (['stale', 'overdue', 'missing', 'offline', 'error', 'critical', 'rollup_lag'].includes(status)) {
        return 'offline';
    }

    const readyPct = normalizeFreshnessPercent(overall?.ready_pct);
    if (readyPct == null) return 'unknown';
    if (readyPct >= 95) return 'online';
    if (readyPct >= 80) return 'delayed';
    return 'offline';
}

function buildIbkrDataHealthFromFreshness(freshnessPayload = {}) {
    const normalized = normalizeFreshnessPayload(freshnessPayload);
    if (!normalized.aggregate) return null;
    const overall = normalized.overall || {};
    const sampleSymbols = normalizeFreshnessSamples(overall.sample_lag_symbols);
    const readyPct = normalizeFreshnessPercent(overall.ready_pct);
    const coveragePct = normalizeFreshnessPercent(overall.coverage_pct);
    const dataHealth = {
        status: getDataHealthStatusFromFreshness(overall),
        freshness_aggregate: true,
        freshness_status: String(overall.status || '').trim().toLowerCase(),
        ready_pct: readyPct,
        coverage_pct: coveragePct,
        total_symbols: Math.max(0, Math.round(normalizeFreshnessNumber(overall.total_symbols, 0))),
        due_symbols: Math.max(0, Math.round(normalizeFreshnessNumber(overall.due_symbols, 0))),
        total_checks: Math.max(0, Math.round(normalizeFreshnessNumber(overall.total_checks, overall.total_symbols || 0))),
        due_checks: Math.max(0, Math.round(normalizeFreshnessNumber(overall.due_checks, overall.due_symbols || 0))),
        ready: Math.max(0, Math.round(normalizeFreshnessNumber(overall.ready, 0))),
        overdue: Math.max(0, Math.round(normalizeFreshnessNumber(overall.overdue, 0))),
        missing: Math.max(0, Math.round(normalizeFreshnessNumber(overall.missing, 0))),
        waiting_5m: Math.max(0, Math.round(normalizeFreshnessNumber(overall.waiting_5m, 0))),
        not_due: Math.max(0, Math.round(normalizeFreshnessNumber(overall.not_due, 0))),
        quiet_extended: Math.max(0, Math.round(normalizeFreshnessNumber(overall.quiet_extended, 0))),
        expected_close_ms: normalizeFreshnessNumber(overall.expected_close_ms, 0) || 0,
        expected_close_us: String(overall.expected_close_us || '').trim(),
        checked_at_ms: normalizeFreshnessNumber(overall.checked_at_ms, 0) || 0,
        sample_lag_symbols: sampleSymbols,
    };
    if (!dataHealth.total_symbols && !dataHealth.due_symbols && !normalized.intervals.length) {
        dataHealth.status = 'unknown';
    }
    return dataHealth;
}

function buildSystemHealthSnapshot(healthPayload = {}, statusPayload = {}, freshnessItems = [], fallbackCompute = {}, schedulerPayload = {}, options = {}) {
    const runtime = statusPayload?.runtime || healthPayload?.runtime || {};
    const compute = buildSystemComputeSummary(healthPayload, statusPayload, fallbackCompute);
    const freshnessList = normalizeFreshnessItems(freshnessItems);
    const latest5m = freshnessList.find((item) => item && item.interval === '5m' && Number(item.last_bar_time_ms || 0) > 0);
    const aggregateDataHealth = buildIbkrDataHealthFromFreshness(freshnessItems);

    let dataHealth = healthPayload?.ibkr_data || {};
    if (aggregateDataHealth) {
        dataHealth = aggregateDataHealth;
    } else if (latest5m) {
        dataHealth = buildIbkrDataHealth(latest5m.last_bar_time_ms, {
            symbol: latest5m.symbol || '',
            noDataStatus: 'unknown'
        });
    } else if (isStableIbkrDataHealth(dataHealth)) {
        dataHealth = cloneIbkrDataHealth(dataHealth) || dataHealth;
    } else if (options?.preserveIbkrData && lastStableIbkrDataHealth) {
        dataHealth = cloneIbkrDataHealth(lastStableIbkrDataHealth) || { status: 'loading' };
    } else if (options?.preserveIbkrData && options?.loadingOnMissingIbkrData) {
        dataHealth = { status: 'loading' };
    } else if (!dataHealth || !dataHealth.status) {
        dataHealth = { status: 'unknown' };
    }
    const runtimeBarDataHealth = aggregateDataHealth ? null : buildRuntimeBarDataHealth(runtime, latest5m);
    if (runtimeBarDataHealth) {
        dataHealth = {
            ...dataHealth,
            ...runtimeBarDataHealth,
        };
    }
    dataHealth = rememberStableIbkrDataHealth(dataHealth);

    return {
        ibkr_data: dataHealth,
        ibkr_compute: compute,
        runtime: runtime,
        scheduler: getIbkrSchedulerSummary(schedulerPayload, currentEnvironment),
        service_topology: statusPayload?.service_topology || healthPayload?.service_topology || {},
    };
}
