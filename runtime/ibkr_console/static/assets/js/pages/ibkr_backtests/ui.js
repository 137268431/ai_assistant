        function setActionState(isPending) {
            actionPending = Boolean(isPending);
            const buttons = document.querySelectorAll('button');
            buttons.forEach((button) => {
                if (button.id === 'startButton') {
                    button.disabled = actionPending;
                }
            });
        }

        function resizeBacktestCharts() {
            requestAnimationFrame(() => {
                if (equityChart) equityChart.resize();
                if (replayChart) replayChart.resize();
            });
        }

        function setBacktestTab(tabKey) {
            const panels = Array.from(document.querySelectorAll('[data-backtest-tab-panel]'));
            if (!panels.length) return;
            const targetKey = panels.some((panel) => panel.dataset.backtestTabPanel === tabKey) ? tabKey : 'runs';
            activeBacktestTab = targetKey;
            panels.forEach((panel) => {
                const isActive = panel.dataset.backtestTabPanel === targetKey;
                panel.hidden = !isActive;
                panel.classList.toggle('is-active', isActive);
            });
            document.querySelectorAll('[data-backtest-tab]').forEach((button) => {
                const isActive = button.dataset.backtestTab === targetKey;
                button.classList.toggle('is-active', isActive);
                button.setAttribute('aria-selected', isActive ? 'true' : 'false');
            });
            resizeBacktestCharts();
        }

        function syncSymbolSourceUI() {
            const source = document.getElementById('symbolSource').value;
            const symbolsInput = document.getElementById('symbolsText');
            const help = document.getElementById('symbolsHelp');
            const supportsManualUniverse = source === 'manual' || source === 'daily_scan_replay';
            symbolsInput.disabled = !supportsManualUniverse;
            if (source === 'manual') {
                help.textContent = '手动模式下必填；其它模式会忽略这个输入。';
                symbolsInput.placeholder = 'AAPL,NVDA,MSFT';
            } else if (source === 'targets') {
                help.textContent = '当日回测会读取当前 active ibkr_targets；历史日期会自动回放 daily_scan_replay。';
                symbolsInput.placeholder = 'targets 模式会自动解析';
            } else if (source === 'daily_scan_replay') {
                help.textContent = '可选：填写后会作为历史盘前选股的底池；留空则按 watchlist 快照逐日回放。';
                symbolsInput.placeholder = '可选：限制历史盘前扫描底池';
            } else {
                help.textContent = '会读取 watchlist 股票池，并按 max_symbols 截断。';
                symbolsInput.placeholder = 'watchlist 模式会自动解析';
            }
        }

        function syncTvCompareUI() {
            const compareWithTv = Boolean(document.getElementById('compareWithTv')?.checked);
            const compareSignalsInput = document.getElementById('compareTvSignals');
            if (!compareSignalsInput) return;
            compareSignalsInput.disabled = !compareWithTv;
            if (!compareWithTv) {
                compareSignalsInput.checked = false;
            }
        }
