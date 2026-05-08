function applyDefaultDates() {
            const latestCompleteDate = syncBacktestDateLimits();
            const toDate = new Date(`${latestCompleteDate}T12:00:00Z`);
            const from = new Date(toDate);
            from.setUTCDate(toDate.getUTCDate() - 14);
            document.getElementById('dateFrom').value = toIsoDate(from);
            document.getElementById('dateTo').value = latestCompleteDate;
            syncBacktestDateLimits();
        }

        window.handleStartBacktest = handleStartBacktest;
        window.cancelActiveRun = cancelActiveRun;
        window.cleanupSelectedRun = cleanupSelectedRun;
        window.cleanupSelectedBatch = cleanupSelectedBatch;
        window.selectBatch = selectBatch;
        window.openRunFromBatch = openRunFromBatch;
        window.selectRun = selectRun;
        window.toggleBacktestTableExpansion = toggleBacktestTableExpansion;
        window.toggleBacktestTextExpansion = toggleBacktestTextExpansion;
        window.refreshDashboard = refreshDashboard;
        window.refreshSelectedRun = refreshSelectedRun;
        window.refreshSelectedBatch = refreshSelectedBatch;
        window.setBacktestTab = setBacktestTab;
        window.loadReplayForSelection = loadReplayForSelection;
        window.replayTrade = replayTrade;
        window.openTradeChart = openTradeChart;
        window.replayTarget = replayTarget;
        window.syncSymbolSourceUI = syncSymbolSourceUI;
        window.syncTvCompareUI = syncTvCompareUI;
        window.syncBacktestPresetUI = syncBacktestPresetUI;
        window.onEnvironmentChange = function(environment) {
            currentEnvironment = environment;
            window.location.href = buildPageUrl('/ibkr_backtests.html', {}, { environment: currentEnvironment });
        };

        document.addEventListener('DOMContentLoaded', async () => {
            if (!initAuth()) return;
            document.getElementById('nav').innerHTML = renderNav('/ibkr_backtests.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('🧪 IBKR 回测工坊', {
                subtitle: '结果 / 参数 / replay',
            });
            document.getElementById('pageBridge').innerHTML = renderBacktestsBridge('/ibkr_backtests.html');
            document.getElementById('overviewChartToggle')?.addEventListener('toggle', resizeBacktestCharts);
            applyDefaultDates();
            document.getElementById('dateFrom')?.addEventListener('change', syncBacktestDateLimits);
            document.getElementById('dateTo')?.addEventListener('change', syncBacktestDateLimits);
            document.getElementById('backtestMachinePreset')?.addEventListener('change', () => syncBacktestPresetUI(true));
            syncBacktestPresetUI(true);
            syncSymbolSourceUI();
            syncTvCompareUI();
            setBacktestTab(activeBacktestTab);
            renderReplay([]);
            await withPageLoading(
                () => refreshDashboard(false),
                {
                    title: '回测页加载中',
                    copy: '正在同步回测数据。',
                }
            );
            refreshTimer = setInterval(() => refreshDashboard(false), 10000);
        });
