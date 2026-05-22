        window.onEnvironmentChange = function(environment) {
            currentBrokerMode = normalizeBrokerMode(environment, getCurrentBrokerMode());
            currentEnvironment = currentBrokerMode;
            currentDataEnvironment = getSharedDataEnvironment();
            window.location.href = buildPageUrl('/ibkr_runtime.html', {}, { environment: currentEnvironment });
        };

        document.addEventListener('DOMContentLoaded', async () => {
            if (!ensureIbkrPageAuth()) return;
            window.addEventListener('pagehide', () => {
                runtimePageClosing = true;
                latestRuntimeLoadId += 1;
                clearRefreshTimer();
            });
            window.addEventListener('beforeunload', () => {
                runtimePageClosing = true;
                latestRuntimeLoadId += 1;
                clearRefreshTimer();
            });
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'visible' && !refreshTimer && hasLoadedRuntimeData) {
                    scheduleRuntimeRefresh(2000);
                }
            });
            document.getElementById('nav').innerHTML = renderNav('/ibkr_runtime.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('🎛️ IBKR 运行时', { subtitle: '控制 / 调度 / 链路' });
            document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_runtime.html');
            document.getElementById('configLink').href = buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true });
            await loadRuntimeData(false);
        });
