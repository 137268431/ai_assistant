let refreshTimer = null;
let currentEnvironment = getCurrentRuntimeEnvironment();
let currentFocus = '';
let hasLoadedSystemData = false;
let latestSystemLoadId = 0;

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

function normalizeFreshnessItems(freshnessPayload = []) {
    if (Array.isArray(freshnessPayload)) {
        return freshnessPayload
            .filter((item) => item && item.interval)
            .map((item) => ({
                ...item,
                interval: normalizeIbkrInterval(item.interval, item.interval),
            }));
    }
    if (!freshnessPayload || typeof freshnessPayload !== 'object') {
        return [];
    }
    return Object.entries(freshnessPayload).map(([interval, item]) => ({
        interval: normalizeIbkrInterval(interval, interval),
        last_bar_time_ms: Number(item?.last_bar_time_ms || 0) || 0,
        age_min: Number.isFinite(Number(item?.age_min)) ? Number(item.age_min) : null,
        symbol: item?.symbol || '',
    }));
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

function buildSystemHealthSnapshot(healthPayload = {}, statusPayload = {}, freshnessItems = [], fallbackCompute = {}, schedulerPayload = {}) {
    const runtime = statusPayload?.runtime || healthPayload?.runtime || {};
    const compute = buildSystemComputeSummary(healthPayload, statusPayload, fallbackCompute);
    const freshnessList = normalizeFreshnessItems(freshnessItems);
    const latest5m = freshnessList.find((item) => item && item.interval === '5m' && Number(item.last_bar_time_ms || 0) > 0);

    let dataHealth = healthPayload?.ibkr_data || {};
    if (latest5m) {
        dataHealth = buildIbkrDataHealth(latest5m.last_bar_time_ms, {
            symbol: latest5m.symbol || '',
            noDataStatus: 'unknown'
        });
    } else if (!dataHealth || !dataHealth.status) {
        dataHealth = { status: 'unknown' };
    }
    const runtimeBarDataHealth = buildRuntimeBarDataHealth(runtime, latest5m);
    if (runtimeBarDataHealth) {
        dataHealth = {
            ...dataHealth,
            ...runtimeBarDataHealth,
        };
    }

    return {
        ibkr_data: dataHealth,
        ibkr_compute: compute,
        runtime: runtime,
        scheduler: getIbkrSchedulerSummary(schedulerPayload, currentEnvironment),
        service_topology: statusPayload?.service_topology || healthPayload?.service_topology || {},
    };
}
