// Shared IBKR engine, data health, compute, and runtime status card models.

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

function getIbkrEngineEnvironmentReadyStats(engines, preferredOrder = []) {
    if (!engines || typeof engines !== 'object') return [];
    const stats = new Map();
    Object.entries(engines).forEach(([key, engine]) => {
        const fallbackEnvironment = String(key || '').split(/[/:]/)[0] || '';
        const environment = String(engine?.environment || fallbackEnvironment).trim().toLowerCase();
        if (!environment) return;
        const current = stats.get(environment) || {
            environment,
            ready: 0,
            total: 0,
        };
        current.total += 1;
        if (engine?.is_ready) current.ready += 1;
        stats.set(environment, current);
    });

    const ordered = [];
    const seen = new Set();
    [
        ...(Array.isArray(preferredOrder) ? preferredOrder : []),
        'live',
        'paper',
        'backtest',
        ...Array.from(stats.keys()).sort(),
    ].forEach((environment) => {
        const normalized = String(environment || '').trim().toLowerCase();
        if (!normalized || seen.has(normalized) || !stats.has(normalized)) return;
        seen.add(normalized);
        ordered.push(stats.get(normalized));
    });
    return ordered;
}

function buildIbkrEngineEnvironmentReadySummary(engines, options = {}) {
    const { preferredOrder = [], maxItems = 3 } = options || {};
    const stats = getIbkrEngineEnvironmentReadyStats(engines, preferredOrder);
    if (stats.length <= 1) return '';
    return stats
        .slice(0, Math.max(1, Number(maxItems || 3) || 3))
        .map((item) => `${getEnvironmentLabel(item.environment)} ${item.ready}/${item.total}`)
        .join(' · ');
}

const IBKR_DATA_ONLINE_MAX_AGE_MIN = 10;
const IBKR_DATA_DELAYED_MAX_AGE_MIN = 30;

function getIbkrDataHealthStatus(ageMin, { noDataStatus = 'no_data' } = {}) {
    const numericAge = Number(ageMin);
    if (!Number.isFinite(numericAge) || numericAge < 0) return noDataStatus;
    if (numericAge <= IBKR_DATA_ONLINE_MAX_AGE_MIN) return 'online';
    if (numericAge <= IBKR_DATA_DELAYED_MAX_AGE_MIN) return 'delayed';
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

    if (numericAge <= 5) {
        return {
            color: '#22c55e',
            pct: 100,
            chipClass: 'is-fresh',
            stateText: '正常',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= IBKR_DATA_ONLINE_MAX_AGE_MIN) {
        return {
            color: '#22c55e',
            pct: 84,
            chipClass: 'is-fresh',
            stateText: '正常',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= 20) {
        return {
            color: '#eab308',
            pct: 72,
            chipClass: 'is-warn',
            stateText: '延迟',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= IBKR_DATA_DELAYED_MAX_AGE_MIN) {
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
    const normalizedInterval = normalizeIbkrInterval(interval, '5m') || '5m';
    const intervalLabel = formatIbkrIntervalLabel(interval, interval || '5m');
    return {
        key: safeKey,
        environment: effectiveEnvironment,
        environmentLabel,
        symbol,
        interval,
        displayName: hasSymbol ? `${symbol}${interval ? ` · ${intervalLabel}` : ''}` : safeKey,
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
        uptime_s: health?.ibkr_compute?.uptime_s ?? health?.uptime_s ?? 0,
        startup_preload: health?.ibkr_compute?.startup_preload
            || health?.ibkr_compute?.compute_startup_preload
            || health?.startup_preload
            || health?.compute_startup_preload
            || null
    };
}

function getIbkrComputeStartupPreloadMeta(compute = {}) {
    const preload = compute?.startup_preload || compute?.compute_startup_preload || {};
    if (!preload || typeof preload !== 'object') return '';

    const status = String(preload.status || '').trim().toLowerCase();
    const symbolTotal = Number(preload.symbol_total || 0) || 0;
    const symbolCompleted = Number(preload.symbol_completed || 0) || 0;
    const readyCount = Number(preload.ready_count || 0) || 0;
    const envCompleted = Number(preload.env_completed || 0) || 0;
    const envTotal = Number(preload.env_total || 0) || 0;

    if (status === 'running') return `preload ${symbolCompleted}/${symbolTotal || '--'}`;
    if (status === 'scheduled') return `preload queued ${envCompleted}/${envTotal || '--'}`;
    if (status === 'completed') return 'preload done';
    if (status === 'failed') return 'preload failed';
    if (status === 'skipped') return 'preload skipped';
    return '';
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
    } else if (dataHealth?.status === 'loading') {
        mainText = '加载中';
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
    if (Array.isArray(dataHealth?.pending_symbols) && dataHealth.pending_symbols.length) {
        bucketMeta.push(`symbols ${formatSymbolPreview(dataHealth.pending_symbols, 4)}`);
    }
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
    const preloadMeta = getIbkrComputeStartupPreloadMeta(compute);
    if (preloadMeta) metaParts.push(preloadMeta);
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

