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

        const BACKTEST_MACHINE_PRESETS = Object.freeze({
            safe_4c8g: {
                label: '4C8G safe',
                maxSymbols: { manual: 20, targets: 20, daily_scan_replay: 0, watchlist: 80 },
                warmupBars: 160,
                sessionMode: 'extended',
                resourceGuard: {
                    resource_guard_enabled: true,
                    resource_guard_max_load: 3.5,
                    resource_guard_min_available_mb: 1800,
                    resource_guard_max_rss_mb: 4200,
                    resource_guard_sleep_s: 0.35,
                    resource_guard_check_steps: 80,
                },
                help: '4核8G 安全档：先限制 watchlist 标的数和 warmup，保护实盘同机资源。',
            },
            fast_sample: {
                label: 'fast sample',
                maxSymbols: { manual: 10, targets: 10, daily_scan_replay: 0, watchlist: 30 },
                warmupBars: 120,
                sessionMode: 'regular',
                resourceGuard: {
                    resource_guard_enabled: true,
                    resource_guard_max_load: 3.2,
                    resource_guard_min_available_mb: 2200,
                    resource_guard_max_rss_mb: 3200,
                    resource_guard_sleep_s: 0.25,
                    resource_guard_check_steps: 100,
                },
                help: '快速抽样档：regular + 小标的池，适合快速验证参数方向。',
            },
            full_watchlist: {
                label: 'full watchlist',
                maxSymbols: { manual: 200, targets: 200, daily_scan_replay: 0, watchlist: 200 },
                warmupBars: 320,
                sessionMode: 'extended',
                resourceGuard: {
                    resource_guard_enabled: true,
                    resource_guard_max_load: 3.8,
                    resource_guard_min_available_mb: 1400,
                    resource_guard_max_rss_mb: 5200,
                    resource_guard_sleep_s: 0.2,
                    resource_guard_check_steps: 120,
                },
                help: '全量档：适合非开盘时段或分段回测；4核8G 上近一年会更慢。',
            },
            daily_selected_fast: {
                label: 'Daily selected fast + live SD',
                symbolSource: 'daily_scan_replay',
                executionModel: 'portfolio_stream',
                maxSymbols: { manual: 12, targets: 12, daily_scan_replay: 0, watchlist: 12 },
                warmupBars: 320,
                sessionMode: 'extended',
                dailySelection: {
                    daily_selected_only: true,
                    daily_selection_require_sd_trigger: true,
                    daily_selection_reuse_live_admission: true,
                    daily_selection_candidate_limit: 80,
                    daily_selection_cache_enabled: true,
                    daily_selection_cache_mode: 'use_or_build',
                    daily_selection_cache_force_rebuild: false,
                },
                resourceGuard: {
                    resource_guard_enabled: true,
                    resource_guard_max_load: 3.5,
                    resource_guard_min_available_mb: 1800,
                    resource_guard_max_rss_mb: 4200,
                    resource_guard_sleep_s: 0.25,
                    resource_guard_check_steps: 100,
                },
                help: '证明实盘链路用：逐日日筛 + 复用实盘 SD 窗口准入，只计算每日通过准入的标的。',
            },
        });

        function getBacktestMachinePresetKey() {
            const value = String(document.getElementById('backtestMachinePreset')?.value || 'daily_selected_fast').trim();
            return BACKTEST_MACHINE_PRESETS[value] ? value : 'daily_selected_fast';
        }

        function getBacktestMachinePreset() {
            return BACKTEST_MACHINE_PRESETS[getBacktestMachinePresetKey()];
        }

        function syncBacktestPresetUI(force = false) {
            const preset = getBacktestMachinePreset();
            const sourceInput = document.getElementById('symbolSource');
            const executionInput = document.getElementById('executionModel');
            if (force && preset.symbolSource && sourceInput) sourceInput.value = preset.symbolSource;
            if (force && preset.executionModel && executionInput) executionInput.value = preset.executionModel;
            const source = String(sourceInput?.value || 'manual').trim() || 'manual';
            const maxSymbolsInput = document.getElementById('maxSymbols');
            const warmupInput = document.getElementById('warmupBars');
            const sessionInput = document.getElementById('sessionMode');
            const help = document.getElementById('backtestPresetHelp');
            const nextMaxSymbols = Number(preset.maxSymbols[source] || preset.maxSymbols.manual || 20);
            if (maxSymbolsInput) {
                const previousPresetValue = Number(maxSymbolsInput.dataset.presetValue || 0);
                const currentValue = Number(maxSymbolsInput.value || 0);
                if (force || !currentValue || currentValue === previousPresetValue) {
                    maxSymbolsInput.value = String(nextMaxSymbols);
                }
                maxSymbolsInput.dataset.presetValue = String(nextMaxSymbols);
                maxSymbolsInput.dataset.defaultValue = String(nextMaxSymbols);
            }
            if (warmupInput) {
                const previousPresetValue = Number(warmupInput.dataset.presetValue || 0);
                const currentValue = Number(warmupInput.value || 0);
                if (force || !currentValue || currentValue === previousPresetValue) {
                    warmupInput.value = String(preset.warmupBars);
                }
                warmupInput.dataset.presetValue = String(preset.warmupBars);
            }
            if (sessionInput && (force || sessionInput.value === sessionInput.dataset.presetValue)) {
                sessionInput.value = preset.sessionMode;
                sessionInput.dataset.presetValue = preset.sessionMode;
            } else if (sessionInput && !sessionInput.dataset.presetValue) {
                sessionInput.dataset.presetValue = sessionInput.value;
            }
            if (help) help.textContent = preset.help;
            if (force && preset.symbolSource) syncSymbolSourceUI();
        }

        function getBacktestResourceGuardPayload() {
            return { ...getBacktestMachinePreset().resourceGuard };
        }

        function getBacktestDailySelectionPayload(symbolSource) {
            const preset = getBacktestMachinePreset();
            const isDailyScanReplay = String(symbolSource || '').trim() === 'daily_scan_replay';
            if (!isDailyScanReplay) {
                return {
                    daily_selected_only: false,
                    daily_selection_require_sd_trigger: false,
                    daily_selection_reuse_live_admission: false,
                    daily_selection_cache_enabled: false,
                    daily_selection_cache_force_rebuild: false,
                };
            }
            return {
                daily_selected_only: true,
                daily_selection_require_sd_trigger: false,
                daily_selection_reuse_live_admission: false,
                daily_selection_cache_enabled: true,
                daily_selection_cache_mode: 'use_or_build',
                daily_selection_cache_force_rebuild: false,
                ...(preset.dailySelection || {}),
                daily_selection_cache_force_rebuild: Boolean(document.getElementById('dailySelectionCacheForceRebuild')?.checked),
            };
        }

        function getBacktestDateSpanDays(dateFrom, dateTo) {
            if (!dateFrom || !dateTo) return 0;
            const start = new Date(`${dateFrom}T00:00:00Z`);
            const end = new Date(`${dateTo}T00:00:00Z`);
            return Math.max(1, Math.round((end - start) / 86400000) + 1);
        }

        function shouldConfirmHeavyBacktest(payload) {
            const presetKey = getBacktestMachinePresetKey();
            const spanDays = getBacktestDateSpanDays(payload.date_from, payload.date_to);
            return presetKey === 'full_watchlist'
                && payload.symbol_source === 'watchlist'
                && (Number(payload.max_symbols || 0) === 0 || Number(payload.max_symbols || 0) >= 150)
                && spanDays >= 180;
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
            if (typeof ensureBacktestRowsForActiveTab === 'function') {
                ensureBacktestRowsForActiveTab(targetKey).catch((error) => {
                    console.warn('ensureBacktestRowsForActiveTab failed:', error);
                });
            }
        }

        function syncSymbolSourceUI() {
            const source = document.getElementById('symbolSource').value;
            const symbolsInput = document.getElementById('symbolsText');
            const help = document.getElementById('symbolsHelp');
            const maxSymbolsInput = document.getElementById('maxSymbols');
            const supportsManualUniverse = source === 'manual' || source === 'daily_scan_replay';
            symbolsInput.disabled = !supportsManualUniverse;
            if (source === 'manual') {
                help.textContent = '手动模式下必填；其它模式会忽略这个输入。';
                symbolsInput.placeholder = 'AAPL,NVDA,MSFT';
            } else if (source === 'targets') {
                help.textContent = '当日回测会读取当前 active ibkr_targets；历史日期会自动回放 daily_scan_replay。';
                symbolsInput.placeholder = 'targets 模式会自动解析';
            } else if (source === 'daily_scan_replay') {
                help.textContent = '可选：填写后作为历史盘前选股底池；组合回测只加载每天入选标的。';
                symbolsInput.placeholder = '可选：限制历史盘前扫描底池';
            } else {
                help.textContent = '会读取 trade watchlist 股票池；QQQ/SPY/VIX 等 market_monitor 会自动排除。';
                symbolsInput.placeholder = 'watchlist 模式会自动解析';
            }
            if (maxSymbolsInput) {
                const preset = getBacktestMachinePreset();
                const nextDefault = Number(preset.maxSymbols[source] || preset.maxSymbols.manual || 20);
                const previousDefault = Number(maxSymbolsInput.dataset.defaultValue || 20);
                const currentValue = Number(maxSymbolsInput.value || 0);
                maxSymbolsInput.max = '200';
                if (!currentValue || currentValue === previousDefault) {
                    maxSymbolsInput.value = String(nextDefault);
                }
                maxSymbolsInput.dataset.defaultValue = String(nextDefault);
                maxSymbolsInput.dataset.presetValue = String(nextDefault);
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
