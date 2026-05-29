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
                    `正在拉取 Broker ${getEnvironmentLabel(currentBrokerMode)} / Shared Data 的 runtime、2FA、配置与最近链路数据。`
                );
            }
            try {
                const brokerEnvFilter = buildBrokerEnvironmentFilter();
                const readApiFetch = (collection, params, cacheOptions = {}) => (
                    typeof cachedApiFetch === 'function'
                        ? cachedApiFetch(collection, params, cacheOptions)
                        : apiFetch(collection, params)
                );
                const readCustomJson = (path, options = {}, cacheOptions = {}) => (
                    typeof cachedPageJson === 'function'
                        ? cachedPageJson(path, options, {
                            environment: currentBrokerMode,
                            ...cacheOptions,
                          })
                        : typeof cachedCustomJson === 'function'
                        ? cachedCustomJson(path, currentBrokerMode, options, cacheOptions)
                        : requestIbkrEnvironmentJson(path, currentBrokerMode, options)
                );
                const coreCache = { ttlMs: 10000, ttl: 10000, force: Boolean(showToastOnSuccess) };
                const listCache = { ttlMs: 15000, ttl: 15000, force: Boolean(showToastOnSuccess) };
                const dataEnvFilter = buildDataEnvironmentFilter();
                const [health, status, summary, monitorResp, cronResp, twoFactorResp, startupResp, runtimeConfigResp, brokerModeSwitchResp, signalsResp, ordersResp, eventsResp] = await Promise.all([
                    readCustomJson('/api/custom/ibkr/healthz', { retryAttempts: 3 }, coreCache),
                    readCustomJson('/api/custom/ibkr/statusz?lite=1', { retryAttempts: 3 }, coreCache),
                    readCustomJson('/api/custom/system/summaryz?lite=1', { retryAttempts: 3 }, coreCache),
                    withTimeout(
                        readCustomJson('/api/custom/system/monitorz?lite=1', { retryAttempts: 2 }, coreCache),
                        9000,
                        'system/monitorz?lite=1'
                    ).catch(() => ({ service_monitor: { services: {} }, lite: true })),
                    readCustomJson('/api/custom/system/cronz', { retryAttempts: 3 }, { ttlMs: 300000, ttl: 300000, force: Boolean(showToastOnSuccess) }).catch(() => ({ items: [] })),
                    readCustomJson('/api/custom/ibkr/2fa/status', { retryAttempts: 3 }, coreCache),
                    readCustomJson('/api/custom/ibkr/startup/status', { retryAttempts: 3 }, coreCache).catch(() => ({ state: {} })),
                    readCustomJson('/api/custom/ibkr/runtime/config', { retryAttempts: 3 }, { ttlMs: 300000, ttl: 300000, force: Boolean(showToastOnSuccess) }),
                    readCustomJson('/api/custom/ibkr/broker-mode/switch/preview', { retryAttempts: 2 }, coreCache).catch((error) => ({
                        ok: false,
                        allowed: false,
                        error: error?.message || String(error || 'broker_mode_switch_preview_failed'),
                        blockers: [
                            {
                                code: 'preview_unavailable',
                                message: `无法读取 Paper / Live 切换预检：${error?.message || error}`,
                                severity: 'blocker',
                            },
                        ],
                    })),
                    readApiFetch('ibkr_signals', { filter: dataEnvFilter, sort: '-created', perPage: 8 }, listCache),
                    readApiFetch('orders', { filter: brokerEnvFilter, sort: '-created', perPage: 8 }, listCache),
                    readApiFetch('system_events', { filter: brokerEnvFilter, sort: '-created', perPage: 8 }, listCache)
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
                latestBrokerModeSwitchPreview = brokerModeSwitchResp || {};
                const signalItems = toArray(signalsResp);
                const latestSignal = signalItems[0] || null;

                const renderRuntimeSnapshot = (resolvedSummary) => {
                    renderHero(resolvedSummary, health, status, runtimeConfig, twoFactorState, latestStartupState, null);
                    renderOpsGrid(resolvedSummary, status, twoFactorState, latestStartupState, null, null, latestSignal);
                    renderMetricCards(resolvedSummary, health, status, twoFactorState, null);
                    renderRuntimeDetail(resolvedSummary, health, status, twoFactorState, latestStartupState, null, null, latestSignal);
                    renderConfigDetail(resolvedSummary, runtimeConfig, cronResp || {});
                    renderRuntimeFlowPrimaryAction(status, latestTwoFactorState);
                    renderBrokerModeSwitchPanel(latestBrokerModeSwitchPreview, status, twoFactorState);
                    renderServiceControlPanel(status, latestServiceMonitorPayload);
                };

                renderRuntimeSnapshot(baseSummary);
                renderTwoFactorPanel(twoFactorState);
                syncActionLocks();
                if (refreshMode === 'boost' && !shouldKeepBoostRefresh(status, latestTwoFactorState, latestStartupState)) {
                    refreshMode = 'steady';
                    refreshBoostStartedAt = 0;
                }
                renderServiceTopology(status);
                renderSignalsTable(signalItems);
                renderOrdersTable(toArray(ordersResp));
                renderEventsTable(toArray(eventsResp));

                if (showToastOnSuccess) showToast('Runtime 数据已刷新');

                void loadRuntimeTodayCounts(status).then((todayCounts) => {
                    if (loadId !== latestRuntimeLoadId) return;
                    const resolvedSummary = {
                        ...baseSummary,
                        today: {
                            ...(baseSummary.today || {}),
                            ...(todayCounts || {}),
                        }
                    };
                    renderRuntimeSnapshot(resolvedSummary);
                    syncActionLocks();
                }).catch(() => null);
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
