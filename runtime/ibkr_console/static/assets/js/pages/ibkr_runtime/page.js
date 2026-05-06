        window.onEnvironmentChange = function(environment) {
            currentEnvironment = environment;
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
            document.getElementById('nav').innerHTML = renderNav('/ibkr_runtime.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('🎛️ IBKR 运行时', { subtitle: '控制 / 调度 / 链路' });
            document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_runtime.html');
            document.getElementById('configLink').href = buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true, environment: currentEnvironment });
            await loadRuntimeData(false);
        });
