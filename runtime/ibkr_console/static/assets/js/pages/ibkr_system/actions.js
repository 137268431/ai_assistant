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
                const cacheOptions = { ttlMs: 10000, ttl: 10000, force: Boolean(showToastOnSuccess) };
                const request = typeof cachedCustomJson === 'function'
                    ? cachedCustomJson(path, currentEnvironment, options, cacheOptions)
                    : requestIbkrEnvironmentJson(path, currentEnvironment, options);
                return await withTimeout(request, timeoutMs, label);
            } catch (error) {
                bucket.push(`${label}: ${error.message || error}`);
                return fallback;
            }
        };
        const safeApiFetch = async (bucket, label, collection, params, fallback = { items: [] }, timeoutMs = secondaryTimeoutMs) => {
            try {
                const request = typeof cachedApiFetch === 'function'
                    ? cachedApiFetch(collection, params, { ttlMs: 15000, ttl: 15000, force: Boolean(showToastOnSuccess) })
                    : apiFetch(collection, params);
                return await withTimeout(request, timeoutMs, label);
            } catch (error) {
                bucket.push(`${label}: ${error.message || error}`);
                return fallback;
            }
        };
        const safeCountFetch = async (bucket, label, collection, filter, timeoutMs = secondaryTimeoutMs) => {
            try {
                const request = typeof cachedCountFetch === 'function'
                    ? cachedCountFetch(collection, filter, { ttlMs: 30000, ttl: 30000, force: Boolean(showToastOnSuccess) })
                    : apiFetch(collection, {
                        filter,
                        perPage: 1,
                        page: 1
                    });
                const payload = await withTimeout(request, timeoutMs, label);
                if (typeof payload === 'number') return payload;
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
        renderStorageHealth(summaryLite?.storage_health || computeStatus?.storage_health || {});

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
        const summaryToday = summaryLite?.today && typeof summaryLite.today === 'object' ? summaryLite.today : {};
        const mainOrderCount = summaryToday.main_orders == null
            ? (summaryToday.order_groups == null ? orderCount : Number(summaryToday.order_groups || 0))
            : Number(summaryToday.main_orders || 0);
        const todayStats = {
            orders: mainOrderCount,
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
        renderStorageHealth(summaryLite?.storage_health || computeStatus?.storage_health || {});
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
