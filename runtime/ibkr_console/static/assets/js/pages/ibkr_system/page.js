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

async function loadSystemData(showToastOnSuccess = false) {
    if (!ensureIbkrPageAuth()) return;
    const loadId = ++latestSystemLoadId;
    const isInitialLoad = !hasLoadedSystemData;
    if (isInitialLoad) {
        setIbkrPageLoading(
            true,
            '系统概览加载中',
            `正在拉取 ${getEnvironmentLabel(currentEnvironment)} 环境的健康状态、freshness、配置与最近事件。`
        );
    }
    try {
        const freshnessIntervals = ['5m', '15m', '30m', '1h', '4h', '1d'];
        const envFilterBase = `environment = "${escapeQueryValue(currentEnvironment)}"`;
        const coreTimeoutMs = isInitialLoad ? 15000 : 10000;
        const secondaryTimeoutMs = isInitialLoad ? 20000 : 12000;
        const coreErrors = [];
        const secondaryErrors = [];
        const safeRequestSystemJson = async (bucket, label, path, fallback = {}, timeoutMs = coreTimeoutMs, options = {}) => {
            try {
                return await withTimeout(requestIbkrEnvironmentJson(path, currentEnvironment, options), timeoutMs, label);
            } catch (error) {
                bucket.push(`${label}: ${error.message || error}`);
                return fallback;
            }
        };
        const safeApiFetch = async (bucket, label, collection, params, fallback = { items: [] }, timeoutMs = secondaryTimeoutMs) => {
            try {
                return await withTimeout(apiFetch(collection, params), timeoutMs, label);
            } catch (error) {
                bucket.push(`${label}: ${error.message || error}`);
                return fallback;
            }
        };
        const safeCountFetch = async (bucket, label, collection, filter, timeoutMs = secondaryTimeoutMs) => {
            try {
                const payload = await withTimeout(apiFetch(collection, {
                    filter,
                    perPage: 1,
                    page: 1
                }), timeoutMs, label);
                const totalItems = Number(payload?.totalItems);
                if (Number.isFinite(totalItems)) return totalItems;
                return Array.isArray(payload?.items) ? payload.items.length : 0;
            } catch (error) {
                bucket.push(`${label}: ${error.message || error}`);
                return null;
            }
        };

        const [computeHealth, computeStatus, summaryLite, cronResp] = await Promise.all([
            safeRequestSystemJson(coreErrors, 'ibkr_healthz', '/api/custom/ibkr/healthz', {
                ok: false,
                status: 'offline',
            }, coreTimeoutMs, { retryAttempts: 3 }),
            safeRequestSystemJson(coreErrors, 'ibkr_statusz', '/api/custom/ibkr/statusz?lite=1', {
                ok: false,
                status: 'offline',
                compute: {},
                runtime: {},
            }, coreTimeoutMs, { retryAttempts: 2 }),
            safeRequestSystemJson(coreErrors, 'summaryz_lite', '/api/custom/system/summaryz?lite=1', {
                today: {},
                ibkr_compute: {},
                config: {},
                lite_mode: true,
            }, coreTimeoutMs, { retryAttempts: 2 }),
            safeRequestSystemJson(coreErrors, 'cronz', '/api/custom/system/cronz', { items: [] }, coreTimeoutMs),
        ]);

        if (loadId !== latestSystemLoadId) return;

        const cronDefinitions = Array.isArray(cronResp?.items) ? cronResp.items : [];
        const coreCompute = buildSystemComputeSummary(computeHealth, computeStatus, summaryLite?.ibkr_compute || {});
        const coreFreshnessItems = [];
        renderStatus(buildSystemHealthSnapshot(computeHealth, computeStatus, coreFreshnessItems, summaryLite?.ibkr_compute || {}, cronResp?.scheduler || {}));
        renderFreshness(coreFreshnessItems);
        renderEngines(coreCompute);
        renderConfig(summaryLite || {}, cronDefinitions);
        renderSchedulerOverview(cronResp || {}, summaryLite || {});
        renderServiceTopology(computeStatus?.service_topology || summaryLite?.service_topology || {});

        if (isInitialLoad) {
            hasLoadedSystemData = true;
            setIbkrPageLoading(false);
        }

        setPageRefreshTime();
        document.getElementById('refreshInfo').textContent = coreErrors.length
            ? '核心已加载，补充中'
            : '核心已加载';

        const marketDate = String(
            computeStatus?.runtime?.market_universe?.market_date
            || computeStatus?.market_universe?.market_date
            || ''
        ).trim() || String(summaryLite?.timestamp || '').slice(0, 10);
        const todayDate = marketDate || new Date().toISOString().slice(0, 10);
        setPageContextMeta([
            { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
            { label: '交易日', value: todayDate || '--' },
            { label: '视图', value: currentFocus === 'stats' ? '统计聚焦' : '系统总览' },
        ]);
        const todayStart = `${todayDate} 00:00:00`;
        const todayFilterBase = `created >= "${escapeQueryValue(todayStart)}" && ${envFilterBase}`;
        const targetDateFilter = `date = "${escapeQueryValue(todayDate)}" && ${envFilterBase}`;

        const [eventsResp, signalCount, indicatorCount, orderCount, barCount, targetCount, eventCount, ...restResponses] = await Promise.all([
            safeApiFetch(secondaryErrors, 'system_events', 'system_events', { filter: envFilterBase, sort: '-created', perPage: 8 }, { items: [] }, secondaryTimeoutMs),
            safeCountFetch(secondaryErrors, 'count:ibkr_signals', 'ibkr_signals', todayFilterBase),
            safeCountFetch(secondaryErrors, 'count:ibkr_indicators', 'ibkr_indicators', todayFilterBase),
            safeCountFetch(secondaryErrors, 'count:orders', 'orders', todayFilterBase),
            safeCountFetch(secondaryErrors, 'count:ibkr_bars', 'ibkr_bars', todayFilterBase),
            safeCountFetch(secondaryErrors, 'count:ibkr_targets', 'ibkr_targets', targetDateFilter),
            safeCountFetch(secondaryErrors, 'count:system_events', 'system_events', todayFilterBase),
            ...freshnessIntervals.map((interval) =>
                safeApiFetch(secondaryErrors, `freshness:${interval}`, 'ibkr_bars', {
                    filter: `${envFilterBase} && interval = "${escapeQueryValue(interval)}"`,
                    sort: '-bar_time_ms',
                    perPage: 1
                }, { items: [] }, secondaryTimeoutMs)
            ),
            safeApiFetch(secondaryErrors, 'ibkr_backtest_batches', 'ibkr_backtest_batches', { filter: envFilterBase, sort: '-updated', perPage: 1 }, { items: [] }, secondaryTimeoutMs),
            safeApiFetch(secondaryErrors, 'ibkr_backtest_runs', 'ibkr_backtest_runs', { filter: envFilterBase, sort: '-updated', perPage: 2 }, { items: [] }, secondaryTimeoutMs)
        ]);

        if (loadId !== latestSystemLoadId) return;

        const freshnessResponses = restResponses.slice(0, freshnessIntervals.length);
        const backtestBatchResp = restResponses[freshnessIntervals.length] || {};
        const backtestRunsResp = restResponses[freshnessIntervals.length + 1] || {};
        const todayStats = {
            orders: orderCount,
            ibkr_bars: barCount,
            ibkr_indicators: indicatorCount,
            ibkr_signals: signalCount,
            ibkr_targets: targetCount,
            events: eventCount,
        };
        const freshnessItems = freshnessIntervals.map((interval, index) => {
            const latest = Array.isArray(freshnessResponses[index]?.items) ? freshnessResponses[index].items[0] : null;
            const lastBarTimeMs = Number(latest?.bar_time_ms || 0) || 0;
            return {
                interval,
                last_bar_time_ms: lastBarTimeMs,
                age_min: lastBarTimeMs ? Math.max(0, Math.round((Date.now() - lastBarTimeMs) / 60000)) : null,
                symbol: latest?.symbol || ''
            };
        });

        renderStatus(buildSystemHealthSnapshot(computeHealth, computeStatus, freshnessItems, summaryLite?.ibkr_compute || {}, cronResp?.scheduler || {}));
        renderTodayStats(todayStats);
        renderFreshness(freshnessItems);
        renderEngines(buildSystemComputeSummary(computeHealth, computeStatus, summaryLite?.ibkr_compute || {}));
        renderConfig(summaryLite || {}, cronDefinitions);
        renderSchedulerOverview(cronResp || {}, summaryLite || {});
        renderServiceTopology(computeStatus?.service_topology || summaryLite?.service_topology || {});
        renderBacktests(
            Array.isArray(backtestBatchResp?.items) ? backtestBatchResp.items : [],
            Array.isArray(backtestRunsResp?.items) ? backtestRunsResp.items : []
        );
        renderEvents(Array.isArray(eventsResp?.items) ? eventsResp.items : []);

        setPageRefreshTime();
        document.getElementById('refreshInfo').textContent = '细节已加载';

        const softErrors = coreErrors.concat(secondaryErrors);
        if (softErrors.length) {
            console.warn('[ibkr_system] soft errors', softErrors);
        }
        setPageRefreshTime();
        let refreshLabel = '细节已加载';
        if (coreErrors.length) {
            refreshLabel = '核心降级';
        } else if (secondaryErrors.length) {
            refreshLabel = '核心已加载';
        }
        document.getElementById('refreshInfo').textContent = refreshLabel;
        if (showToastOnSuccess) showToast('System 数据已刷新');
    } catch (err) {
        console.error('System 加载失败:', err);
        document.getElementById('refreshInfo').textContent = '加载失败';
        showToast(`加载失败: ${err.message || err}`);
    } finally {
        if (isInitialLoad && !hasLoadedSystemData) {
            hasLoadedSystemData = true;
            setIbkrPageLoading(false);
        }
    }
}

function applyFocusTarget() {
    if (currentFocus !== 'stats') return;
    const target = document.getElementById('todayStatsSection');
    if (!target) return;
    setTimeout(() => {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 120);
}

function buildStatusCardMarkup(dotClass, mainText, subText = '') {
    const safeMain = String(mainText || '--').trim() || '--';
    const safeSub = String(subText || '').trim();
    return `
        <div class="status-main">
            <span class="status-dot ${dotClass}"></span>
            <span class="status-main-text">${escapeHtml(safeMain)}</span>
        </div>
        <div class="status-sub${safeSub ? '' : ' is-empty'}">${escapeHtml(safeSub || '--')}</div>
    `;
}

function getSchedulerStatusCardModel(summary = {}) {
    const scheduler = getIbkrSchedulerSummary(summary, currentEnvironment);
    const lagLabel = scheduler.dispatchLagLabel && scheduler.dispatchLagLabel !== '--'
        ? `lag ${scheduler.dispatchLagLabel}`
        : '等待 bar cursor';
    if (!scheduler.ok && scheduler.status === 'offline') {
        return {
            dotClass: 'dot-red',
            mainText: 'OFFLINE',
            subText: scheduler.statusCountSummary || 'scheduler status 不可用',
        };
    }
    if (scheduler.status === 'degraded' || scheduler.dispatchLagMin >= 10) {
        return {
            dotClass: 'dot-yellow',
            mainText: 'DEGRADED',
            subText: `${lagLabel} · ${scheduler.statusCountSummary}`,
        };
    }
    if (scheduler.status === 'disabled') {
        return {
            dotClass: 'dot-yellow',
            mainText: 'DISABLED',
            subText: scheduler.statusCountSummary || '调度总开关关闭',
        };
    }
    return {
        dotClass: 'dot-green',
        mainText: String(scheduler.status || 'running').toUpperCase(),
        subText: `${lagLabel} · ${scheduler.statusCountSummary}`,
    };
}

function renderStatus(health) {
    const dataEl = document.getElementById('dataStatus');
    const dataStatusModel = getIbkrDataStatusCardModel(health.ibkr_data || {});
    dataEl.innerHTML = buildStatusCardMarkup(dataStatusModel.dotClass, dataStatusModel.mainText, dataStatusModel.subText);

    const compEl = document.getElementById('computeStatus');
    const computeStatusModel = getIbkrComputeStatusCardModel(health.ibkr_compute || {});
    compEl.innerHTML = buildStatusCardMarkup(computeStatusModel.dotClass, computeStatusModel.mainText, computeStatusModel.subText);

    const runtimeEl = document.getElementById('runtimeStatus');
    const runtimeStatusModel = getIbkrRuntimeStatusCardModel(health.runtime || {});
    runtimeEl.innerHTML = buildStatusCardMarkup(runtimeStatusModel.dotClass, runtimeStatusModel.mainText, runtimeStatusModel.subText);

    const schedulerEl = document.getElementById('schedulerStatus');
    if (schedulerEl) {
        const schedulerStatusModel = getSchedulerStatusCardModel(health.scheduler || {});
        schedulerEl.innerHTML = buildStatusCardMarkup(
            schedulerStatusModel.dotClass,
            schedulerStatusModel.mainText,
            schedulerStatusModel.subText
        );
    }
}

function renderServiceTopology(topologyPayload = {}) {
    const el = document.getElementById('serviceTopologyArea');
    const topology = topologyPayload?.services && typeof topologyPayload.services === 'object'
        ? topologyPayload.services
        : {};
    const services = Object.values(topology);
    const monitorHref = buildPageUrl('/ibkr_monitor.html', {}, { environment: currentEnvironment });
    if (!services.length) {
        el.innerHTML = `
            <div class="ops-summary-shell">
                <div class="ops-summary-lead">
                    <div>
                        <div class="ops-summary-title">服务拓扑详情已移到运维大盘</div>
                        <div class="ops-summary-copy">总览页只保留入口和健康摘要；请求、订阅、主机和 PB 明细请在运维页排查。</div>
                    </div>
                    <a class="ops-summary-link" href="${monitorHref}">打开运维大盘 →</a>
                </div>
                <div class="loading-text">暂无服务拓扑摘要</div>
            </div>
        `;
        return;
    }

    const normalizedServices = services.map((service) => {
        const rawStatus = String(service?.status || 'unknown').trim().toLowerCase() || 'unknown';
        const status = ['ok', 'ready', 'healthy', 'online', 'peer', 'external'].includes(rawStatus) ? 'running' : rawStatus;
        return {
            status,
            title: String(service?.service_name || service?.kind || '--').trim() || '--',
        };
    });
    const counts = normalizedServices.reduce((acc, service) => {
        acc[service.status] = (acc[service.status] || 0) + 1;
        return acc;
    }, {});
    const issueServices = normalizedServices.filter((service) => !['running', 'unknown'].includes(service.status));
    const unknownCount = counts.unknown || 0;
    const runningCount = counts.running || 0;
    const degradedCount = (counts.degraded || 0) + (counts.warning || 0) + (counts.warn || 0);
    const offlineCount = (counts.offline || 0) + (counts.failed || 0) + (counts.error || 0);
    const attentionCount = issueServices.length + unknownCount;
    const overallTone = offlineCount > 0 ? 'danger' : (degradedCount > 0 || attentionCount > 0 ? 'warn' : 'ok');
    const overallLabel = offlineCount > 0 ? '需要排查' : (degradedCount > 0 || attentionCount > 0 ? '关注' : '正常');
    const countParts = Object.entries(counts)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([status, count]) => `${String(status).toUpperCase()} ${count}`);
    const issueText = issueServices.length
        ? issueServices.slice(0, 3).map((service) => `${service.title} ${service.status.toUpperCase()}`).join(' · ')
        : (unknownCount ? `${unknownCount} 个服务状态未知` : '未发现异常服务状态');

    const cards = [
        {
            label: 'Services',
            value: String(normalizedServices.length),
            copy: countParts.join(' · ') || '--',
            tone: overallTone,
        },
        {
            label: 'Running',
            value: String(runningCount),
            copy: '完整服务拓扑请进运维大盘',
            tone: runningCount === normalizedServices.length ? 'ok' : 'neutral',
        },
        {
            label: 'Needs Attention',
            value: String(attentionCount),
            copy: issueText,
            tone: attentionCount ? 'warn' : 'ok',
        },
    ];

    el.innerHTML = `
        <div class="ops-summary-shell">
            <div class="ops-summary-lead">
                <div>
                    <div class="ops-summary-kicker">Ops Routing</div>
                    <div class="ops-summary-title">服务状态 ${escapeHtml(overallLabel)}</div>
                    <div class="ops-summary-copy">总览页只看健康摘要；请求、订阅、主机、PB 磁盘与完整 split-stack 详情统一在运维大盘。</div>
                </div>
                <a class="ops-summary-link" href="${monitorHref}">打开运维大盘 →</a>
            </div>
            <div class="ops-summary-grid">
                ${cards.map((card) => `
                    <div class="ops-summary-card tone-${escapeHtml(card.tone)}">
                        <div class="ops-summary-label">${escapeHtml(card.label)}</div>
                        <div class="ops-summary-value">${escapeHtml(card.value)}</div>
                        <div class="ops-summary-card-copy">${escapeHtml(card.copy || '--')}</div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function renderTodayStats(today) {
    const hasData = today && typeof today === 'object' && Object.keys(today).length > 0;
    const safeToday = hasData ? today : {};
    const stats = [
        { label: 'ORDERS', value: safeToday.orders },
        { label: 'IBKR BARS', value: safeToday.ibkr_bars },
        { label: 'IBKR IND', value: safeToday.ibkr_indicators },
        { label: 'IBKR SIG', value: safeToday.ibkr_signals },
        { label: 'TARGETS', value: safeToday.ibkr_targets },
        { label: 'EVENTS', value: safeToday.events }
    ];
    document.getElementById('todayStats').innerHTML = stats.map((item) => `
        <div class="stat-mini">
            <div class="stat-mini-label">${escapeHtml(item.label)}</div>
            <div class="stat-mini-value">${escapeHtml(hasData ? (item.value == null ? '-' : String(item.value)) : '-')}</div>
        </div>
    `).join('');
}

function renderFreshness(data) {
    const el = document.getElementById('freshnessArea');
    const byInterval = Array.isArray(data)
        ? data.reduce((acc, item) => {
            if (item && item.interval) acc[item.interval] = item;
            return acc;
        }, {})
        : (data || {});
    if (!byInterval || Object.keys(byInterval).length === 0) {
        el.innerHTML = '<div class="loading-text">暂无数据</div>';
        return;
    }
    const tfs = ['5m', '15m', '30m', '1h', '4h', '1d'];
    const cards = tfs.map((tf) => {
        const item = byInterval[tf];
        if (!item) {
            const freshnessVisual = getIbkrFreshnessVisualState(null);
            return `<div class="freshness-card ${freshnessVisual.chipClass}">
                <div class="freshness-card-head">
                    <span class="freshness-label">${tf}</span>
                    <span class="freshness-chip ${freshnessVisual.chipClass}">${escapeHtml(freshnessVisual.ageLabel)}</span>
                </div>
                <div class="freshness-meta">
                    <div class="freshness-meta-top">
                        <span class="freshness-link" style="color:var(--muted)">--</span>
                        <span class="freshness-state">${freshnessVisual.stateText}</span>
                    </div>
                    <div class="freshness-time">--</div>
                </div>
                <div class="freshness-bar-bg"><div class="freshness-bar-fill" style="width:${freshnessVisual.pct}%;background:${freshnessVisual.color}"></div></div>
            </div>`;
        }
        const age = Math.max(0, Number(item.age_min || 0) || 0);
        const freshnessVisual = getIbkrFreshnessVisualState(age);
        const chartHref = item.symbol
            ? buildPageUrl('/ibkr_chart.html', { symbol: item.symbol, interval: tf }, { environment: currentEnvironment })
            : '';
        const timeLabel = item.last_bar_time_ms
            ? formatTimeLabel(item.last_bar_time_ms)
            : (item.last_bar_time ? formatTimeLabel(item.last_bar_time) : '--');

        return `<div class="freshness-card ${freshnessVisual.chipClass}">
            <div class="freshness-card-head">
                <span class="freshness-label">${tf}</span>
                <span class="freshness-chip ${freshnessVisual.chipClass}">${escapeHtml(freshnessVisual.ageLabel)}</span>
            </div>
            <div class="freshness-meta">
                <div class="freshness-meta-top">
                    ${chartHref ? `<a class="freshness-link" href="${chartHref}">${escapeHtml(item.symbol || '--')}</a>` : '<span class="freshness-link" style="color:var(--muted)">--</span>'}
                    <span class="freshness-state" style="color:${freshnessVisual.color}">${freshnessVisual.stateText}</span>
                </div>
                <div class="freshness-time">${escapeHtml(timeLabel)}</div>
            </div>
            <div class="freshness-bar-bg"><div class="freshness-bar-fill" style="width:${freshnessVisual.pct}%;background:${freshnessVisual.color}"></div></div>
        </div>`;
    });
    el.innerHTML = `<div class="freshness-grid">${cards.join('')}</div>`;
}

function renderEngines(computeData) {
    const el = document.getElementById('engineArea');
    const countEl = document.getElementById('engineCount');
    const preloadStatusLabel = buildStartupPreloadStatusLabel(computeData?.startup_preload);
    const preloadSummary = buildStartupPreloadSummary(computeData?.startup_preload);
    const preloadDetail = buildStartupPreloadDetail(computeData?.startup_preload);
    const environmentReadySummary = buildIbkrEngineEnvironmentReadySummary(computeData?.engines, {
        preferredOrder: [currentEnvironment],
    });

    if (!computeData || !computeData.engines || typeof computeData.engines !== 'object') {
        const messages = ['无引擎数据'];
        if (preloadSummary) messages.push(preloadSummary);
        el.innerHTML = messages.map((item) => `<div class="loading-text">${escapeHtml(item)}</div>`).join('');
        countEl.textContent = preloadStatusLabel || '0 engines';
        return;
    }

    const engines = getSortedEngineEntries(computeData.engines);
    const computeMeta = [];
    if (Number(computeData.last_realtime_elapsed_s || 0) > 0) computeMeta.push(`last ${Number(computeData.last_realtime_elapsed_s || 0).toFixed(2)}s`);
    if (Number(computeData.last_realtime_signals || 0) > 0) computeMeta.push(`sig ${Number(computeData.last_realtime_signals || 0)}`);
    if (Number(computeData.queue_size || 0) > 0) computeMeta.push(`queue ${Number(computeData.queue_size || 0)}`);
    const countParts = [`${computeData.ready_engines || 0}/${computeData.total_engines || engines.length} ready`];
    if (environmentReadySummary) countParts.push(environmentReadySummary);
    if (preloadStatusLabel) countParts.push(preloadStatusLabel);
    if (computeMeta.length) countParts.push(computeMeta.join(' · '));
    else if (!preloadStatusLabel) countParts.push('top 12');
    countEl.textContent = countParts.join(' · ');

    if (!engines.length) {
        const messages = ['无引擎数据'];
        if (preloadSummary) messages.push(preloadSummary);
        if (preloadDetail) messages.push(preloadDetail);
        el.innerHTML = messages.map((item) => `<div class="loading-text">${escapeHtml(item)}</div>`).join('');
        return;
    }

    let html = '<div class="engine-summary">总览页只保留最关键的 12 条引擎概况；完整排查与动作控制请切到控制台。</div>';
    if (environmentReadySummary) {
        html += `<div class="engine-summary">环境 ready：${escapeHtml(environmentReadySummary)}</div>`;
    }
    if (preloadSummary) {
        html += `<div class="engine-summary">${escapeHtml(preloadSummary)}</div>`;
    }
    if (preloadDetail) {
        html += `<div class="engine-summary">${escapeHtml(preloadDetail)}</div>`;
    }
    html += '<div class="engine-grid">';
    engines.slice(0, 12).forEach(([key, engine]) => {
        const model = getIbkrEngineViewModel(key, engine, currentEnvironment);
        html += `<div class="engine-card">
            <div class="engine-card-head">
                <div class="engine-card-title">
                    <div class="engine-card-name">${escapeHtml(model.displayName)}</div>
                    <div class="engine-card-sub">${escapeHtml(model.subtitle)}</div>
                </div>
                <span class="engine-state ${model.ready ? 'ready' : 'warming'}">${escapeHtml(model.readyLabel)}</span>
            </div>
            <div class="engine-meta-grid">
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Bars</span>
                    <span class="engine-meta-value">${escapeHtml(String(model.barCount))}</span>
                </div>
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Last Close</span>
                    <span class="engine-meta-value">${escapeHtml(model.lastCloseLabel)}</span>
                </div>
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Last Bar</span>
                    <span class="engine-meta-value">${escapeHtml(model.lastBarLabel)}</span>
                </div>
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Chart</span>
                    <span class="engine-meta-value">${model.chartHref ? `<a class="engine-link" href="${model.chartHref}">打开图表</a>` : '--'}</span>
                </div>
            </div>
        </div>`;
    });
    html += '</div>';
    el.innerHTML = html;
}

function renderSchedulerOverview(cronPayload = {}, summary = {}) {
    const el = document.getElementById('schedulerArea');
    if (!el) return;
    const definitions = Array.isArray(cronPayload?.items) ? cronPayload.items : [];
    const scheduler = getIbkrSchedulerSummary(cronPayload?.scheduler || {}, currentEnvironment);
    const configMap = buildIbkrConfigMap(summary);
    const cards = definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentEnvironment));

    const summaryCards = [
        {
            label: 'Scheduler State',
            value: String(scheduler.status || '--').toUpperCase(),
            copy: `loop ${scheduler.loopIntervalLabel} · ${scheduler.environmentLabel}`,
            tone: scheduler.tone,
        },
        {
            label: 'Dispatch Lag',
            value: scheduler.dispatchLagLabel,
            copy: `persisted ${scheduler.latestIngestedBarLabel} · dispatched ${scheduler.latestDispatchedBarLabel}`,
            tone: scheduler.dispatchLagMin >= 10 ? 'error' : (scheduler.latestIngestedBarTimeMs ? 'ok' : 'warn'),
        },
        {
            label: 'Job Coverage',
            value: `${scheduler.enabledJobCount || 0}/${scheduler.jobCount || 0}`,
            copy: `native ${scheduler.nativeJobCount || 0} · compat ${scheduler.compatibilityJobCount || 0}`,
            tone: scheduler.compatibilityJobCount > 0 ? 'warn' : 'ok',
        },
        {
            label: 'Last Dispatch',
            value: scheduler.lastDispatchLabel,
            copy: scheduler.statusCountSummary,
            tone: scheduler.ok ? 'ok' : 'error',
        },
    ];

    const toneClass = (tone) => {
        if (tone === 'error') return 'is-danger';
        if (tone === 'warn') return 'is-warn';
        if (tone === 'ok') return 'is-ok';
        return '';
    };

    const cronMarkup = cards.length
        ? `
            <div class="cron-grid">
                ${cards.map((card) => `
                    <div class="cron-card">
                        <div class="cron-card-head">
                            <div>
                                <div class="cron-card-title">${escapeHtml(card.title)}</div>
                                <div class="cron-card-key">${escapeHtml(card.configKey)}</div>
                            </div>
                            <span class="cron-state ${card.effectiveEnabled ? 'on' : 'off'}">${escapeHtml(card.statusLabel)}</span>
                        </div>
                        <div class="cron-copy">${escapeHtml(card.functionSummary)}</div>
                        <div class="cron-meta">
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">执行器</span>
                                <span class="cron-meta-value">${escapeHtml(card.modeLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">最近完成</span>
                                <span class="cron-meta-value">${escapeHtml(card.lastRunFinishedLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">最近成功</span>
                                <span class="cron-meta-value">${escapeHtml(card.lastSuccessLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">窗口</span>
                                <span class="cron-meta-value">${escapeHtml(card.windowLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">周期</span>
                                <span class="cron-meta-value">${escapeHtml(card.primaryCycleLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">结果</span>
                                <span class="cron-meta-value">${escapeHtml(card.lastError || card.resultReason || '--')}</span>
                            </div>
                        </div>
                        <div class="cron-tags">
                            <span class="cron-tag ${toneClass(card.tone)}">${escapeHtml(card.modeLabel)}</span>
                            <span class="cron-tag">GLOBAL ${card.schedulerEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">JOB ${card.cronEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">${escapeHtml(card.environmentLabel)}</span>
                        </div>
                    </div>
                `).join('')}
            </div>
        `
        : '<div class="loading-text" style="padding:10px 0 0">暂无 IBKR Scheduler job 定义</div>';

    el.innerHTML = `
        <div class="scheduler-shell">
            <div class="scheduler-summary-grid">
                ${summaryCards.map((card) => `
                    <div class="scheduler-summary-card">
                        <div class="scheduler-summary-label">${escapeHtml(card.label)}</div>
                        <div class="scheduler-summary-value">${escapeHtml(card.value || '--')}</div>
                        <div class="scheduler-summary-copy ${card.tone === 'error' ? 'is-danger' : ''}">${escapeHtml(card.copy || '--')}</div>
                    </div>
                `).join('')}
            </div>
            <div class="scheduler-inline-meta">
                <span class="scheduler-meta-tag"><strong>Global</strong>${configMap?.pb_scheduler_enabled ?? '--'}</span>
                <span class="scheduler-meta-tag"><strong>Cursor</strong>${escapeHtml(scheduler.latestIngestedBarLabel)} -> ${escapeHtml(scheduler.latestDispatchedBarLabel)}</span>
                <span class="scheduler-meta-tag"><strong>Dispatch</strong>${escapeHtml(scheduler.lastDispatchLabel)}</span>
            </div>
            ${cronMarkup}
        </div>
    `;
}

function renderCronSummary(definitions, configMap) {
    const cronCards = Array.isArray(definitions)
        ? definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentEnvironment))
        : [];
    if (!cronCards.length) {
        return '<div class="loading-text" style="padding:10px 0 0">暂无 IBKR Scheduler job 定义</div>';
    }
    return `
        <div class="cron-grid">
            ${cronCards.map((card) => {
                return `
                    <div class="cron-card">
                        <div class="cron-card-head">
                            <div>
                                <div class="cron-card-title">${escapeHtml(card.title)}</div>
                                <div class="cron-card-key">${escapeHtml(card.configKey)}</div>
                            </div>
                            <span class="cron-state ${card.effectiveEnabled ? 'on' : 'off'}">${card.effectiveEnabled ? 'ENABLED' : 'DISABLED'}</span>
                        </div>
                        <div class="cron-copy">${escapeHtml(card.functionSummary)}</div>
                        <div class="cron-meta">
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">时区</span>
                                <div class="cron-meta-value">
                                    <details class="cron-time-details">
                                        <summary class="cron-time-summary">
                                            <span class="cron-time-primary">${escapeHtml(card.primaryCycleLabel)}</span>
                                            <span class="cron-time-toggle">UTC / ET</span>
                                        </summary>
                                        <div class="cron-time-list">
                                            <div class="cron-time-item">
                                                <span class="cron-time-name">UTC</span>
                                                <span class="cron-time-text">${escapeHtml(card.utcCycleLabel)}</span>
                                            </div>
                                            <div class="cron-time-item">
                                                <span class="cron-time-name">ET</span>
                                                <span class="cron-time-text">${escapeHtml(card.etCycleLabel)}</span>
                                            </div>
                                        </div>
                                    </details>
                                </div>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">Cron</span>
                                <div class="cron-meta-value">
                                    <details class="cron-exp-details">
                                        <summary class="cron-exp-summary">查看表达式</summary>
                                        <div class="cron-exp-text">${escapeHtml(card.cronExpr)}</div>
                                    </details>
                                </div>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">执行器</span>
                                <span class="cron-meta-value">${escapeHtml(card.modeLabel)}</span>
                            </div>
                        </div>
                        <div class="cron-tags">
                            <span class="cron-tag ${card.tone === 'error' ? 'is-danger' : (card.tone === 'warn' ? 'is-warn' : 'is-ok')}">${escapeHtml(card.statusLabel)}</span>
                            <span class="cron-tag">SCHED ${card.schedulerEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">JOB ${card.cronEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">${escapeHtml(card.environmentLabel)}</span>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderConfig(summary, cronDefinitions) {
    const el = document.getElementById('configArea');
    const configMap = buildIbkrConfigMap(summary);
    const primaryItems = getIbkrSystemPrimaryConfigItems(summary, configMap);
    const secondaryItems = getIbkrSystemSecondaryConfigItems(configMap);

    const renderConfigGrid = (items, className = '') => `<div class="config-grid ${className}">${items.map((item) => {
        const displayState = getIbkrConfigDisplayState(item.value);
        let valClass = 'val-neutral';
        if (displayState.tone === 'on') valClass = 'val-on';
        else if (displayState.tone === 'off') valClass = 'val-off';
        return `<div class="config-item">
                <span class="config-key">${escapeHtml(item.label)}</span>
                <span class="config-val ${valClass}">${escapeHtml(displayState.display)}</span>
            </div>`;
    }).join('')}</div>`;
    const primaryGrid = renderConfigGrid(primaryItems, 'config-grid-primary');
    const secondaryGrid = secondaryItems.length
        ? `<details class="config-more-details">
            <summary class="config-more-summary">
                <span>更多配置 ${secondaryItems.length} 项</span>
                <span class="config-more-copy">展开查看长尾参数</span>
            </summary>
            <div class="config-more-body">
                ${renderConfigGrid(secondaryItems, 'config-grid-secondary')}
            </div>
        </details>`
        : '';

    el.innerHTML = `
        <div class="config-stack">
            <div class="config-block-label">关键配置</div>
            ${primaryGrid}
            ${secondaryGrid}
            <div class="config-divider"></div>
            <div class="config-block-label">IBKR Scheduler 摘要</div>
            ${renderCronSummary(cronDefinitions, configMap)}
        </div>
    `;
}

function renderEvents(events) {
    const el = document.getElementById('eventArea');
    const countEl = document.getElementById('eventCount');

    if (!events || events.length === 0) {
        el.innerHTML = '<div class="loading-text">暂无事件</div>';
        countEl.textContent = '0';
        return;
    }

    countEl.textContent = String(events.length);
    const levelColors = { info: '#64748b', warning: '#eab308', error: '#ef4444' };
    const sourceLabels = { ibkr_compute: 'COMPUTE', pb: 'PB', manual: 'MANUAL', tradingview: 'TV', ibkr: 'IBKR' };

    el.innerHTML = `<div class="event-list">${events.map((event) => {
        const dotColor = levelColors[event.level] || '#64748b';
        const source = sourceLabels[event.source] || event.source || '--';
        const time = event.us_time ? String(event.us_time).slice(11, 16) : '--';
        return `<div class="event-item">
            <div class="event-dot" style="background:${dotColor}"></div>
            <div class="event-content">
                <div class="event-title">${escapeHtml(event.title || '--')}</div>
                <div class="event-meta">${escapeHtml(time)} · ${escapeHtml(source)} · ${escapeHtml(event.event_type || '')}</div>
            </div>
        </div>`;
    }).join('')}</div>`;
}

function parseMaybeJson(value, fallback) {
    if (!value) return fallback;
    if (typeof value === 'object') return value;
    try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' ? parsed : fallback;
    } catch (_) {
        return fallback;
    }
}

function formatPct(value) {
    const num = Number(value || 0);
    const prefix = num > 0 ? '+' : '';
    return `${prefix}${num.toFixed(2)}%`;
}

function formatNum(value) {
    const num = Number(value || 0);
    return Number.isFinite(num) ? num.toFixed(2) : '--';
}

function renderBacktests(batches, runs) {
    const area = document.getElementById('backtestArea');
    const countEl = document.getElementById('backtestCount');
    const batch = batches && batches[0] ? batches[0] : null;
    const run = runs && runs[0] ? runs[0] : null;
    const cards = [];

    if (batch) {
        cards.push(`
            <div class="backtest-card">
                <div class="backtest-kicker">Latest Batch</div>
                <div class="backtest-title">${escapeHtml(String(batch.name || batch.batch_id || batch.id || 'batch'))}</div>
                <div class="backtest-meta">
                    状态 ${escapeHtml(String(batch.status || '--'))}<br>
                    变体 ${escapeHtml(String(batch.completed_count || 0))}/${escapeHtml(String(batch.variant_count || 0))}<br>
                    Best Return ${escapeHtml(formatPct(batch.best_total_return_pct || 0))}<br>
                    Best Sharpe ${escapeHtml(formatNum(batch.best_sharpe || 0))}
                </div>
            </div>
        `);
    }

    if (run) {
        const metrics = parseMaybeJson(run.metrics, {});
        cards.push(`
            <div class="backtest-card">
                <div class="backtest-kicker">Latest Run</div>
                <div class="backtest-title">${escapeHtml(String(run.name || run.run_id || run.id || 'run'))}</div>
                <div class="backtest-meta">
                    状态 ${escapeHtml(String(run.status || '--'))}<br>
                    Return ${escapeHtml(formatPct(run.total_return_pct || metrics.total_return_pct || 0))}<br>
                    Sharpe ${escapeHtml(formatNum(run.sharpe || metrics.sharpe || 0))}<br>
                    Max DD ${escapeHtml(formatPct(-Math.abs(Number(run.max_drawdown_pct || metrics.max_drawdown_pct || 0))))}
                </div>
            </div>
        `);
    }

    countEl.textContent = String((batch ? 1 : 0) + (run ? 1 : 0));
    if (!cards.length) {
        area.innerHTML = '<div class="loading-text">当前环境暂无回测统计</div>';
        return;
    }
    area.innerHTML = `<div class="backtest-grid">${cards.join('')}</div>`;
}

document.addEventListener('DOMContentLoaded', async () => {
    if (!ensureIbkrPageAuth()) return;
    currentFocus = String(new URLSearchParams(window.location.search).get('focus') || '').trim().toLowerCase();
    document.getElementById('nav').innerHTML = renderNav('/ibkr_system.html');
    document.getElementById('contextBar').innerHTML = renderPageContextBar('🖥️ IBKR 总览', {
        subtitle: '健康 / 数据新鲜度 / 配置',
    });
    document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_system.html');
    ensureSystemPageTopSection();
    document.getElementById('configLink').href = buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true, environment: currentEnvironment });
    await loadSystemData(false);
    applyFocusTarget();
    refreshTimer = setInterval(() => loadSystemData(false), 60000);
});

window.onEnvironmentChange = function(environment) {
    currentEnvironment = environment;
    const params = currentFocus === 'stats' ? { focus: 'stats' } : {};
    window.location.href = buildPageUrl('/ibkr_system.html', params, { environment: currentEnvironment });
};
