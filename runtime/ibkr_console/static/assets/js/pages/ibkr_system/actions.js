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
        const currentBrokerMode = typeof getCurrentBrokerMode === 'function' ? getCurrentBrokerMode() : currentEnvironment;
        const dataEnvFilterBase = `environment = "${escapeQueryValue(currentEnvironment)}"`;
        const brokerEnvFilterBase = `environment = "${escapeQueryValue(currentBrokerMode)}"`;
        const coreTimeoutMs = isInitialLoad ? 15000 : 10000;
        const schedulerTimeoutMs = isInitialLoad ? 22000 : 18000;
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
        const safeCountFetch = async (bucket, label, collection, filter, timeoutMs = secondaryTimeoutMs, cacheOptions = {}) => {
            try {
                const countCacheOptions = {
                    ttlMs: 300000,
                    ttl: 300000,
                    swrMs: 300000,
                    swr: 300000,
                    ...cacheOptions,
                    force: Boolean(showToastOnSuccess) || Boolean(cacheOptions.force),
                    tags: ['system', 'today-stats', collection, currentBrokerMode, currentEnvironment].concat(cacheOptions.tags || []),
                };
                const request = typeof cachedCountFetch === 'function'
                    ? cachedCountFetch(collection, filter, countCacheOptions)
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
            safeRequestSystemJson(coreErrors, 'cronz', '/api/custom/system/cronz', { items: [] }, schedulerTimeoutMs),
        ]);

        if (loadId !== latestSystemLoadId) return;

        const cronDefinitions = Array.isArray(cronResp?.items) ? cronResp.items : [];
        const coreCompute = buildSystemComputeSummary(computeHealth, computeStatus, summaryLite?.ibkr_compute || {});
        const coreFreshnessPayload = summaryLite?.data_freshness || [];
        const summaryToday = summaryLite?.today && typeof summaryLite.today === 'object' ? summaryLite.today : {};
        const summaryTodayStats = rememberStableTodayStats(summaryToday);
        renderStatus(buildSystemHealthSnapshot(
            computeHealth,
            computeStatus,
            coreFreshnessPayload,
            summaryLite?.ibkr_compute || {},
            cronResp?.scheduler || {},
            { preserveIbkrData: true, loadingOnMissingIbkrData: true }
        ));
        renderFreshness(coreFreshnessPayload);
        renderEngines(coreCompute);
        renderConfig(summaryLite || {}, cronDefinitions);
        renderSchedulerOverview(cronResp || {}, summaryLite || {});
        renderServiceTopology(computeStatus?.service_topology || summaryLite?.service_topology || {});
        renderStorageHealth(summaryLite?.storage_health || computeStatus?.storage_health || {});
        renderTodayStats(summaryTodayStats);

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
        const todayDate = marketDate || getCurrentEtDateString();
        setPageContextMeta([
            { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
            { label: '交易日', value: todayDate || '--' },
            { label: '视图', value: currentFocus === 'stats' ? '统计聚焦' : '系统总览' },
        ]);
        const todayStart = `${todayDate} 00:00:00`;
        const dataTodayFilterBase = `created >= "${escapeQueryValue(todayStart)}" && ${dataEnvFilterBase}`;
        const brokerTodayFilterBase = `created >= "${escapeQueryValue(todayStart)}" && ${brokerEnvFilterBase}`;
        const targetDateFilter = `date = "${escapeQueryValue(todayDate)}" && ${dataEnvFilterBase}`;

        let secondarySnapshot = getLastSystemSecondarySnapshot();
        if (shouldRefreshSystemSecondary(showToastOnSuccess)) {
            const [eventsResp, signalCount, tvWebhookCount, orderCount, barCount, targetCount, eventCount, backtestBatchResp, backtestRunsResp] = await Promise.all([
                safeApiFetch(secondaryErrors, 'system_events', 'system_events', { filter: brokerEnvFilterBase, sort: '-created', perPage: 8 }, { items: [] }, secondaryTimeoutMs),
                safeCountFetch(secondaryErrors, 'count:ibkr_signals', 'ibkr_signals', dataTodayFilterBase),
                safeCountFetch(secondaryErrors, 'count:tv_webhook_events', 'tv_webhook_events', targetDateFilter),
                safeCountFetch(secondaryErrors, 'count:orders', 'orders', brokerTodayFilterBase),
                safeCountFetch(secondaryErrors, 'count:ibkr_bars', 'ibkr_bars', dataTodayFilterBase),
                safeCountFetch(secondaryErrors, 'count:ibkr_targets', 'ibkr_targets', targetDateFilter),
                safeCountFetch(secondaryErrors, 'count:system_events', 'system_events', brokerTodayFilterBase),
                safeApiFetch(secondaryErrors, 'ibkr_backtest_batches', 'ibkr_backtest_batches', { filter: dataEnvFilterBase, sort: '-updated', perPage: 1 }, { items: [] }, secondaryTimeoutMs),
                safeApiFetch(secondaryErrors, 'ibkr_backtest_runs', 'ibkr_backtest_runs', { filter: dataEnvFilterBase, sort: '-updated', perPage: 2 }, { items: [] }, secondaryTimeoutMs)
            ]);
            const mainOrderCount = summaryToday.main_orders == null
                ? (summaryToday.order_groups == null ? orderCount : Number(summaryToday.order_groups || 0))
                : Number(summaryToday.main_orders || 0);
            secondarySnapshot = rememberSystemSecondarySnapshot({
                eventsResp,
                backtestBatchResp,
                backtestRunsResp,
                todayStats: {
                    orders: mainOrderCount,
                    ibkr_bars: barCount,
                    tv_webhook_events: tvWebhookCount,
                    ibkr_signals: signalCount,
                    ibkr_targets: targetCount,
                    events: eventCount,
                },
            });
        }

        if (loadId !== latestSystemLoadId) return;

        const secondaryTodayStats = secondarySnapshot.todayStats || {};
        const eventsResp = secondarySnapshot.eventsResp || { items: [] };
        const backtestBatchResp = secondarySnapshot.backtestBatchResp || { items: [] };
        const backtestRunsResp = secondarySnapshot.backtestRunsResp || { items: [] };
        const todayStats = rememberStableTodayStats(mergeTodayStats(summaryTodayStats, secondaryTodayStats));
        const freshnessPayload = summaryLite?.data_freshness || [];

        renderStatus(buildSystemHealthSnapshot(
            computeHealth,
            computeStatus,
            freshnessPayload,
            summaryLite?.ibkr_compute || {},
            cronResp?.scheduler || {},
            { preserveIbkrData: !summaryLite?.data_freshness }
        ));
        renderTodayStats(todayStats);
        renderFreshness(freshnessPayload);
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
