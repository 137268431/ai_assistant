        async function loadRuntimeData(showToastOnSuccess = false) {
            if (!ensureIbkrPageAuth()) return;
            if (refreshInFlight) {
                scheduleRuntimeRefresh(1500);
                return;
            }
            refreshInFlight = true;
            const loadId = ++latestRuntimeLoadId;
            const isInitialLoad = !hasLoadedRuntimeData;
            if (isInitialLoad) {
                setIbkrPageLoading(
                    true,
                    '控制台加载中',
                    `正在拉取 ${getEnvironmentLabel(currentEnvironment)} 环境的 runtime、2FA、配置与最近链路数据。`
                );
            }
            try {
                const envFilter = buildEnvironmentFilter();
                const [health, status, summary, monitorResp, cronResp, twoFactorResp, startupResp, runtimeConfigResp, signalsResp, ordersResp, eventsResp] = await Promise.all([
                    requestIbkrEnvironmentJson('/api/custom/ibkr/healthz', currentEnvironment, { retryAttempts: 3 }),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/statusz?lite=1', currentEnvironment, { retryAttempts: 3 }),
                    requestIbkrEnvironmentJson('/api/custom/system/summaryz?lite=1', currentEnvironment, { retryAttempts: 3 }),
                    withTimeout(
                        requestIbkrEnvironmentJson('/api/custom/system/monitorz', currentEnvironment, { retryAttempts: 2 }),
                        9000,
                        'system/monitorz'
                    ).catch(() => ({ service_monitor: { services: {} } })),
                    requestIbkrEnvironmentJson('/api/custom/system/cronz', currentEnvironment, { retryAttempts: 3 }).catch(() => ({ items: [] })),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/2fa/status', currentEnvironment, { retryAttempts: 3 }),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/startup/status', currentEnvironment, { retryAttempts: 3 }).catch(() => ({ state: {} })),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/runtime/config', currentEnvironment, { retryAttempts: 3 }),
                    apiFetch('ibkr_signals', { filter: envFilter, sort: '-created', perPage: 8 }),
                    apiFetch('orders', { filter: envFilter, sort: '-created', perPage: 8 }),
                    apiFetch('system_events', { filter: envFilter, sort: '-created', perPage: 8 })
                ]);
                if (loadId !== latestRuntimeLoadId) return;

                const runtimeConfig = Array.isArray(runtimeConfigResp?.items) ? runtimeConfigResp.items : [];
                const baseSummary = {
                    ...(summary || {}),
                    today: {
                        ...((summary && summary.today) || {}),
                    }
                };
                latestRuntimeStatus = status || {};
                latestServiceMonitorPayload = monitorResp || {};
                const twoFactorState = twoFactorResp?.state || {};
                latestTwoFactorState = deriveTwoFactorUiState(twoFactorState);
                const startupState = startupResp?.state || {};
                latestStartupState = normalizeStartupUiState(startupState);
                const signalItems = toArray(signalsResp);
                let latestBar = latestRuntimeBarsSnapshot[0] || null;
                const latestSignal = signalItems[0] || null;

                const renderRuntimeSnapshot = (resolvedSummary, indicatorItems = latestRuntimeIndicatorSnapshot, options = {}) => {
                    const latestIndicator = indicatorItems[0] || null;
                    const previousLoading = runtimeRecentDataLoading;
                    runtimeRecentDataLoading = Boolean(options.dataLoading);
                    try {
                        renderHero(resolvedSummary, health, status, runtimeConfig, twoFactorState, latestStartupState, latestBar);
                        renderOpsGrid(resolvedSummary, status, twoFactorState, latestStartupState, latestBar, latestIndicator, latestSignal);
                        renderMetricCards(resolvedSummary, health, status, twoFactorState, latestBar);
                        renderRuntimeDetail(resolvedSummary, health, status, twoFactorState, latestStartupState, latestBar, latestIndicator, latestSignal);
                        renderConfigDetail(resolvedSummary, runtimeConfig, cronResp || {});
                        renderPipelinePanel(resolvedSummary, status, twoFactorState, latestBar, latestIndicator, latestSignal);
                        renderRuntimeFlowPrimaryAction(status, latestTwoFactorState);
                        renderServiceControlPanel(status, latestServiceMonitorPayload);
                        renderIndicatorsTable(indicatorItems, { loading: Boolean(options.dataLoading) && !indicatorItems.length });
                    } finally {
                        runtimeRecentDataLoading = previousLoading;
                    }
                };

                const recentDataLoading = !latestBar;
                renderRuntimeSnapshot(baseSummary, latestRuntimeIndicatorSnapshot, { dataLoading: recentDataLoading });
                renderTwoFactorPanel(twoFactorState);
                syncActionLocks();
                if (refreshMode === 'boost' && !shouldKeepBoostRefresh(status, latestTwoFactorState, latestStartupState)) {
                    refreshMode = 'steady';
                    refreshBoostStartedAt = 0;
                }
                renderEngineTable(status);
                renderServiceTopology(status);
                void loadEngineDetail(loadId, status);
                renderBarsTable(latestRuntimeBarsSnapshot, { loading: recentDataLoading && !latestRuntimeBarsSnapshot.length });
                renderSignalsTable(signalItems);
                renderOrdersTable(toArray(ordersResp));
                renderEventsTable(toArray(eventsResp));

                if (showToastOnSuccess) showToast('Runtime 数据已刷新');

                const recentMarketDate = resolveRuntimeMarketDate(status);
                const recentRecordFilter = `created >= "${escapeQueryValue(`${recentMarketDate} 00:00:00`)}" && ${envFilter}`;
                const barsPromise = apiFetch('ibkr_bars', {
                    filter: recentRecordFilter,
                    sort: '-bar_time_ms',
                    perPage: 8,
                }).catch((error) => {
                    console.warn('加载最近 bars 失败:', error);
                    return { items: [] };
                });
                const indicatorsPromise = apiFetch('ibkr_indicators', {
                    filter: recentRecordFilter,
                    sort: '-bar_time_ms',
                    perPage: 8,
                }).catch((error) => {
                    console.warn('加载最近 indicators 失败:', error);
                    return { items: [] };
                });

                void Promise.all([
                    barsPromise,
                    indicatorsPromise,
                    loadRuntimeTodayCounts(status).catch(() => null),
                ]).then(([barsResp, indicatorsResp, todayCounts]) => {
                    if (loadId !== latestRuntimeLoadId) return;
                    const barsItems = toArray(barsResp);
                    const indicatorItems = toArray(indicatorsResp);
                    latestRuntimeBarsSnapshot = barsItems;
                    latestRuntimeIndicatorSnapshot = indicatorItems;
                    latestBar = barsItems[0] || null;
                    const resolvedSummary = {
                        ...baseSummary,
                        today: {
                            ...(baseSummary.today || {}),
                            ...(todayCounts || {}),
                        }
                    };
                    renderRuntimeSnapshot(resolvedSummary, indicatorItems);
                    renderBarsTable(barsItems);
                    syncActionLocks();
                });
            } catch (error) {
                if (shouldIgnoreRuntimeLoadError(error, loadId)) return;
                console.error('Runtime 加载失败:', error);
                document.getElementById('refreshInfo').textContent = '加载失败';
                document.getElementById('lastAction').textContent = `加载失败：${error.message || error}`;
                showToast(`加载失败: ${error.message || error}`);
            } finally {
                refreshInFlight = false;
                if (isInitialLoad) {
                    hasLoadedRuntimeData = true;
                    setIbkrPageLoading(false);
                }
                scheduleRuntimeRefresh();
            }
        }

