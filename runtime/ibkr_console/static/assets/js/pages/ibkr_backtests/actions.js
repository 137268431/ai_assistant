        async function refreshDashboard(showToastOnSuccess = false) {
            if (!initAuth()) return;
            try {
                await Promise.all([
                    loadBacktestStatus(),
                    loadBatches(true),
                    loadRuns(true),
                ]);
                await Promise.all([
                    refreshSelectedBatch(false),
                    refreshSelectedRun(false, { loadRows: false }),
                ]);
                setPageContextMeta([
                    { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
                    { label: '批次', value: selectedBatchId || '未选择' },
                    { label: 'Run', value: selectedRunId || '未选择' },
                ]);
                setPageRefreshTime();
                scheduleBacktestRefresh();
                if (showToastOnSuccess) showToast('回测面板已刷新');
            } catch (error) {
                console.error('refreshDashboard failed:', error);
                showToast(`刷新失败: ${error.message || error}`);
            } finally {
                setPageLoading(false, {
                    title: '回测页加载中',
                    copy: '正在同步回测数据。',
                });
            }
        }

        async function handleStartBacktest() {
            if (actionPending) return;
            if (!initAuth()) return;
            const symbolSource = document.getElementById('symbolSource').value;
            const symbolsText = String(document.getElementById('symbolsText').value || '').trim();
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const strategyParamsText = String(document.getElementById('strategyParams').value || '').trim();
            const variantsText = String(document.getElementById('variantsJson').value || '').trim();
            const executionCostProfileText = String(document.getElementById('executionCostProfile')?.value || '').trim();
            let strategyParams = {};
            let variants = [];
            let executionCostProfile = {};
            if (symbolSource === 'manual' && !symbolsText) {
                showToast('manual 模式需要填写 symbols');
                return;
            }
            if (!dateFrom || !dateTo) {
                showToast('请选择完整的回测日期范围');
                return;
            }
            if (dateFrom > dateTo) {
                showToast('Date From 不能大于 Date To');
                return;
            }
            const latestCompleteDate = syncBacktestDateLimits();
            if (dateTo > latestCompleteDate) {
                showToast(`Date To 最晚只能选到 ${latestCompleteDate}（美东昨天），不能包含今天。`);
                return;
            }
            if (strategyParamsText) {
                try {
                    strategyParams = JSON.parse(strategyParamsText);
                } catch (_) {
                    showToast('Strategy Params JSON 解析失败');
                    return;
                }
            }
            if (variantsText) {
                try {
                    variants = JSON.parse(variantsText);
                } catch (_) {
                    showToast('Variants JSON Array 解析失败');
                    return;
                }
                if (!Array.isArray(variants) || !variants.length) {
                    showToast('Variants JSON Array 需要是非空数组');
                    return;
                }
            }
            if (executionCostProfileText) {
                try {
                    executionCostProfile = JSON.parse(executionCostProfileText);
                } catch (_) {
                    showToast('Execution Cost Profile JSON 解析失败');
                    return;
                }
            }
            const resourceGuardPayload = getBacktestResourceGuardPayload();
            const dailySelectionPayload = getBacktestDailySelectionPayload(symbolSource);
            const payload = {
                environment: currentEnvironment,
                name: String(document.getElementById('runName').value || '').trim(),
                machine_profile: getBacktestMachinePresetKey(),
                symbol_source: symbolSource,
                symbols: symbolsText,
                date_from: dateFrom,
                date_to: dateTo,
                session_mode: document.getElementById('sessionMode').value,
                benchmark_symbol: String(document.getElementById('benchmarkSymbol').value || '').trim().toUpperCase(),
                initial_capital: Number(document.getElementById('initialCapital').value || 10000),
                execution_model: document.getElementById('executionModel').value,
                borrow_limit_mode: document.getElementById('borrowLimitMode').value,
                max_borrow_amount: Number(document.getElementById('maxBorrowAmount').value || 0),
                position_limit_max: Number(document.getElementById('positionLimitMax').value || 0),
                max_strategy_open_positions: Number(document.getElementById('maxStrategyOpenPositions')?.value || 5),
                consecutive_stop_loss_limit: Number(document.getElementById('consecutiveStopLossLimit')?.value || 3),
                signal_validity_minutes: Number(document.getElementById('signalValidityMinutes').value || 30),
                trade_window_start_time: String(document.getElementById('tradeWindowStart').value || '09:35').trim(),
                trade_window_end_time: String(document.getElementById('tradeWindowEnd').value || '15:30').trim(),
                order_window_end_time: String(document.getElementById('orderWindowEnd').value || '15:00').trim(),
                simultaneous_signal_priority: document.getElementById('signalPriority').value,
                manual_confirm_mode: 'auto',
                confirm_delay_minutes: 0,
                commission_per_share: Number(document.getElementById('commissionPerShare').value || 0.005),
                slippage_bps: Number(document.getElementById('slippageBps').value || 2),
                account_model_mode: document.getElementById('accountModelMode')?.value || 'current_snapshot',
                fee_model: document.getElementById('feeModel')?.value || 'ibkr_us_equity_fixed_v1',
                slippage_model: document.getElementById('slippageModel')?.value || 'bar_capped_bps_v1',
                execution_cost_profile: executionCostProfile,
                slippage_cap_to_bar: Boolean(document.getElementById('slippageCapToBar')?.checked || document.getElementById('slippageModel')?.value !== 'fixed_bps_v1'),
                limit_price_protection: Boolean(document.getElementById('slippageCapToBar')?.checked || document.getElementById('slippageModel')?.value !== 'fixed_bps_v1'),
                stop_gap_to_open: Boolean(document.getElementById('slippageCapToBar')?.checked || document.getElementById('slippageModel')?.value !== 'fixed_bps_v1'),
                warmup_bars: Number(document.getElementById('warmupBars').value || 320),
                scan_warmup_bars: Number(document.getElementById('warmupBars').value || 320),
                premarket_cutoff_time: String(document.getElementById('premarketCutoff').value || '09:20').trim(),
                compare_with_tv: Boolean(document.getElementById('compareWithTv')?.checked),
                compare_tv_signals: Boolean(document.getElementById('compareWithTv')?.checked && document.getElementById('compareTvSignals')?.checked),
                persist_backtest_indicators: Boolean(document.getElementById('persistBacktestIndicators')?.checked),
                exclude_market_monitors: true,
                exclude_symbols: 'QQQ,SPY,VIX,BOXX,IBKR',
                max_symbols: Number(document.getElementById('maxSymbols').value || 20),
                source_environment: currentEnvironment,
                strategy_params: strategyParams,
                variants,
                ...resourceGuardPayload,
                ...dailySelectionPayload,
            };
            if (shouldConfirmHeavyBacktest(payload)) {
                const spanDays = getBacktestDateSpanDays(dateFrom, dateTo);
                if (!window.confirm(`当前是 full watchlist + ${spanDays} 天 + extended，大概率会占用 4核8G 机器较多内存。建议分段或 safe 档；确认仍要启动吗？`)) {
                    return;
                }
            }
            setActionState(true);
            try {
                const result = await requestBacktestJson('/api/custom/ibkr/backtest/run', {
                    method: 'POST',
                    body: payload,
                });
                selectedRunId = result.run_id || selectedRunId;
                selectedBatchId = result.batch_id || selectedBatchId;
                document.getElementById('runName').value = '';
                if (result.batch_id) {
                    document.getElementById('variantsJson').value = '';
                }
                showToast(`回测已启动: ${result.run_id || '--'}`);
                await refreshDashboard(false);
            } catch (error) {
                showToast(`启动失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function loadExecutionCostProfile() {
            if (actionPending) return;
            if (!initAuth()) return;
            const symbolsText = String(document.getElementById('symbolsText')?.value || '').trim();
            const params = new URLSearchParams({
                environment: currentEnvironment,
                limit: '5000',
                persist_state: '1',
            });
            if (symbolsText) params.set('symbols', symbolsText);
            setActionState(true);
            try {
                const payload = await requestBacktestJson(`/api/custom/ibkr/backtest/execution-cost/profile?${params.toString()}`);
                if (!payload.profile_available) {
                    showToast(`暂无可用成交样本: ${payload.error || 'no fills'}`);
                    return;
                }
                const profile = payload.execution_cost_profile || payload.backtest_payload_patch?.execution_cost_profile || {};
                document.getElementById('executionCostProfile').value = JSON.stringify(profile, null, 2);
                document.getElementById('feeModel').value = 'calibrated_v1';
                if (payload.backtest_payload_patch?.commission_per_share != null) {
                    document.getElementById('commissionPerShare').value = payload.backtest_payload_patch.commission_per_share;
                }
                if (payload.backtest_payload_patch?.slippage_bps != null) {
                    document.getElementById('slippageBps').value = payload.backtest_payload_patch.slippage_bps;
                }
                const count = payload.sample?.fill_count || payload.fill_query?.summary?.fill_count || 0;
                showToast(`已加载成交校准 Profile，样本 ${count} 笔`);
            } catch (error) {
                showToast(`加载校准 Profile 失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function importRecentExecutionFills() {
            if (actionPending) return;
            if (!initAuth()) return;
            setActionState(true);
            try {
                const payload = await requestBacktestJson('/api/custom/ibkr/backtest/execution-cost/import-recent-fills', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        days: 1,
                    },
                });
                showToast(`最近成交导入完成: ${payload.imported || 0}/${payload.raw_count || 0}`);
                setActionState(false);
                await loadExecutionCostProfile();
            } catch (error) {
                showToast(`导入最近成交失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function cancelActiveRun() {
            if (actionPending) return;
            if (!activeStatus?.run_id) {
                showToast('当前没有运行中的回测');
                return;
            }
            if (!window.confirm(`确认停止运行中的回测 ${activeStatus.run_id} 吗？`)) return;
            setActionState(true);
            try {
                await requestBacktestJson('/api/custom/ibkr/backtest/cancel', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        run_id: activeStatus.run_id,
                    },
                });
                showToast('已发送停止请求');
                await refreshDashboard(false);
            } catch (error) {
                showToast(`停止失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function cleanupSelectedRun() {
            if (actionPending) return;
            if (!selectedRunId) {
                showToast('先选择一个 run');
                return;
            }
            if (!window.confirm(`确认清理回测 ${selectedRunId} 吗？这会删除 run 和所有关联 backtest rows。`)) return;
            setActionState(true);
            try {
                await requestBacktestJson('/api/custom/ibkr/backtest/cleanup', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        run_id: selectedRunId,
                    },
                });
                showToast('回测结果已清理');
                selectedRunId = '';
                selectedRun = null;
                clearSelectedTrackingRows();
                renderReplay([]);
                await refreshDashboard(false);
            } catch (error) {
                showToast(`清理失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function cleanupSelectedBatch() {
            if (actionPending) return;
            if (!selectedBatchId) {
                showToast('先选择一个 experiment');
                return;
            }
            if (!window.confirm(`确认清理 experiment ${selectedBatchId} 吗？这会删除该批次下所有 run 和关联 backtest rows。`)) return;
            setActionState(true);
            try {
                await requestBacktestJson('/api/custom/ibkr/backtest/cleanup', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        batch_id: selectedBatchId,
                    },
                });
                showToast('Experiment 已清理');
                if (selectedRun?.extra?.batch_id === selectedBatchId) {
                    selectedRunId = '';
                    selectedRun = null;
                    clearSelectedTrackingRows();
                    renderReplay([]);
                }
                selectedBatchId = '';
                selectedBatch = null;
                await refreshDashboard(false);
            } catch (error) {
                showToast(`清理 experiment 失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function selectBatch(batchId) {
            setBacktestTab('experiments');
            selectedBatchId = batchId;
            selectedBatch = batchList.find((batch) => batch.id === batchId) || null;
            backtestTableExpandedState.leaderboard = false;
            backtestTextExpandedState.batchVariants = false;
            renderBatches();
            renderBatchDetail();
            await hydrateSelectedBatch(batchId);
            renderBatches();
            renderBatchDetail();
        }

        async function openRunFromBatch(runId) {
            if (!runId) return;
            setBacktestTab('runs');
            await selectRun(runId);
        }

        async function selectRun(runId) {
            selectedRunId = runId;
            selectedRun = runList.find((run) => run.id === runId) || null;
            selectedTargets = [];
            selectedTrades = [];
            selectedSignals = [];
            selectedReverseSignals = [];
            resetBacktestRowPagination(runId);
            resetBacktestClientPagination();
            selectedTrackingModel = null;
            selectedTargetsLoading = Boolean(runId) && shouldLoadBacktestRowsForTab('targets', activeBacktestTab);
            trackingLoading = Boolean(runId) && activeBacktestTab === 'tracking';
            trackingFilterRunId = '';
            trackingFilters.date = '';
            trackingFilters.symbol = '';
            trackingFilters.eventType = '';
            trackingFilters.setup = '';
            dataQualityFilters.date = 'all';
            dataQualityFilters.status = 'all';
            backtestTextExpandedState.strategyParams = false;
            backtestTextExpandedState.runtimeExtra = false;
            backtestTableExpandedState.trackingTimeline = false;
            backtestTableExpandedState.trackingFlows = false;
            renderRuns();
            renderMetrics();
            renderRunDetail();
            if (typeof renderTracking === 'function') renderTracking();
            const hydratePromise = hydrateSelectedRun(runId).then(() => {
                if (selectedRunId !== runId) return;
                renderRuns();
                renderMetrics();
                renderRunDetail();
                refreshTrackingModel();
            });
            await hydratePromise;
            scheduleBacktestRowsForActiveTab(activeBacktestTab);
            renderReplay([]);
            document.getElementById('replayCenterBar').value = '';
        }

        async function replayTarget(symbol, barTimeMs) {
            setBacktestTab('trades');
            buildReplaySymbolOptions();
            document.getElementById('replaySymbol').value = symbol || document.getElementById('replaySymbol').value;
            document.getElementById('replayCenterBar').value = String(barTimeMs || '');
            await loadReplayForSelection();
        }

        function getBacktestClientPaginationRows(key) {
            if (key === 'dailyFunnel') {
                return Array.isArray(selectedRun?.metrics?.daily_funnel)
                    ? selectedRun.metrics.daily_funnel
                    : [];
            }
            if (key === 'historicalTargets') {
                return selectedTargets;
            }
            if (key === 'auditDailySummary') {
                const audit = selectedRun?.metrics?.backtest_audit || selectedRun?.extra?.backtest_audit_summary || {};
                return Array.isArray(audit.daily_summary) ? audit.daily_summary : [];
            }
            if (key === 'auditFocusTimeline') {
                const audit = selectedRun?.metrics?.backtest_audit || selectedRun?.extra?.backtest_audit_summary || {};
                const focusDay = audit.focus_day || {};
                const focusTimeline = Array.isArray(focusDay.timeline) ? focusDay.timeline : [];
                if (focusTimeline.length) return focusTimeline;
                return Array.isArray(audit.timeline)
                    ? audit.timeline.filter((item) => String(item?.date || '') === String(audit.focus_date || ''))
                    : [];
            }
            if (key === 'trackingFlows') {
                return typeof getFilteredTrackingFlows === 'function' ? getFilteredTrackingFlows(selectedTrackingModel) : [];
            }
            if (key === 'trackingTimeline') {
                return typeof getFilteredTrackingEvents === 'function' ? getFilteredTrackingEvents(selectedTrackingModel) : [];
            }
            return [];
        }

        function renderBacktestClientPaginationPanel(key) {
            if (key === 'dailyFunnel' || key === 'historicalTargets' || key === 'auditDailySummary' || key === 'auditFocusTimeline') {
                renderRunDetail();
                return;
            }
            if (key === 'trackingFlows' || key === 'trackingTimeline') {
                renderTracking();
            }
        }

        function setDataQualityDateFilter(date) {
            dataQualityFilters.date = String(date || 'all').trim() || 'all';
            backtestTableExpandedState.dataQuality = false;
            renderRunDetail();
        }

        function setDataQualityStatusFilter(status) {
            dataQualityFilters.status = String(status || 'all').trim() || 'all';
            backtestTableExpandedState.dataQuality = false;
            renderRunDetail();
        }

        function setBacktestClientPage(key, page) {
            if (!Object.prototype.hasOwnProperty.call(BACKTEST_CLIENT_PAGE_CONFIG, key)) return;
            const current = getBacktestClientPagination(key, getBacktestClientPaginationRows(key));
            const nextPage = Math.min(
                Math.max(1, Number(page || 1)),
                Math.max(1, Number(current.totalPages || 1))
            );
            backtestClientPageState[key] = {
                page: nextPage,
                pageSize: Number(current.pageSize || BACKTEST_CLIENT_PAGE_CONFIG[key].pageSize || 12),
            };
            renderBacktestClientPaginationPanel(key);
        }

        function setBacktestClientPageSize(key, value) {
            const config = BACKTEST_CLIENT_PAGE_CONFIG[key];
            if (!config) return;
            const pageSizeOptions = Array.isArray(config.pageSizeOptions) && config.pageSizeOptions.length
                ? config.pageSizeOptions.map((item) => Number(item)).filter((item) => Number.isFinite(item) && item > 0)
                : [Number(config.pageSize || 12)];
            const requestedPageSize = Number(value || 0);
            const nextPageSize = pageSizeOptions.includes(requestedPageSize)
                ? requestedPageSize
                : Number(config.pageSize || pageSizeOptions[0] || 12);
            backtestClientPageState[key] = {
                page: 1,
                pageSize: nextPageSize,
            };
            renderBacktestClientPaginationPanel(key);
        }

        async function loadReplayForSelection() {
            if (!selectedRunId) {
                showToast('先选择一个 run');
                return;
            }
            const symbol = String(document.getElementById('replaySymbol').value || '').trim();
            if (!symbol) {
                showToast('请选择一个 symbol');
                return;
            }
            const centerBarMs = Number(document.getElementById('replayCenterBar').value || 0);
            const windowSize = Number(document.getElementById('replayWindow').value || 80);
            try {
                const payload = await requestBacktestJson(`/api/custom/ibkr/backtest/replay?environment=${encodeURIComponent(currentEnvironment)}&run_id=${encodeURIComponent(selectedRunId)}&symbol=${encodeURIComponent(symbol)}&center_bar_ms=${encodeURIComponent(centerBarMs || 0)}&window=${encodeURIComponent(windowSize || 80)}`);
                selectedReplayRows = Array.isArray(payload.rows) ? payload.rows : [];
                backtestTableExpandedState.replay = false;
                renderReplay(selectedReplayRows);
            } catch (error) {
                showToast(`Replay 失败: ${error.message || error}`);
            }
        }

        function toggleBacktestTableExpansion(key) {
            if (!Object.prototype.hasOwnProperty.call(backtestTableExpandedState, key)) return;
            backtestTableExpandedState[key] = !backtestTableExpandedState[key];
            if (key === 'leaderboard') {
                renderBatchDetail();
                return;
            }
            if (key === 'runs') {
                renderRuns();
                return;
            }
            if (key === 'trades') {
                renderTrades();
                return;
            }
            if (key === 'targets') {
                renderRunDetail();
                return;
            }
            if (key === 'dataQuality') {
                renderRunDetail();
                return;
            }
            if (key === 'replay') {
                renderReplay(selectedReplayRows);
                return;
            }
            if (key === 'trackingTimeline' || key === 'trackingFlows') {
                renderTracking();
            }
        }

        function toggleBacktestTextExpansion(key, section) {
            if (!Object.prototype.hasOwnProperty.call(backtestTextExpandedState, key)) return;
            backtestTextExpandedState[key] = !backtestTextExpandedState[key];
            if (section === 'batchDetail') {
                renderBatchDetail();
                return;
            }
            if (section === 'runDetail') {
                renderRunDetail();
            }
        }

        function onRunFilterChange() {
            runFilters.search = String(document.getElementById('runSearch')?.value || '').trim();
            runFilters.status = String(document.getElementById('runStatusFilter')?.value || 'all').trim() || 'all';
            runFilters.source = String(document.getElementById('runSourceFilter')?.value || 'all').trim() || 'all';
            backtestTableExpandedState.runs = false;
            renderRuns();
        }

        function resetRunFilters() {
            runFilters.search = '';
            runFilters.status = 'all';
            runFilters.source = 'all';
            const searchInput = document.getElementById('runSearch');
            const statusInput = document.getElementById('runStatusFilter');
            const sourceInput = document.getElementById('runSourceFilter');
            if (searchInput) searchInput.value = '';
            if (statusInput) statusInput.value = 'all';
            if (sourceInput) sourceInput.value = 'all';
            backtestTableExpandedState.runs = false;
            renderRuns();
        }

        async function replayTrade(symbol, entryBarMs) {
            setBacktestTab('trades');
            document.getElementById('replayCenterBar').value = String(entryBarMs || '');
            buildReplaySymbolOptions();
            document.getElementById('replaySymbol').value = symbol || document.getElementById('replaySymbol').value;
            await loadReplayForSelection();
        }

        function openTradeChart(symbol, entryBarMs, exitBarMs) {
            const safeSymbol = String(symbol || '').trim().toUpperCase();
            if (!safeSymbol || !selectedRunId) return;
            const entryMs = Number(entryBarMs || 0);
            const exitMs = Number(exitBarMs || entryMs || 0);
            const padMs = 90 * 60 * 1000;
            const params = {
                symbol: safeSymbol,
                interval: '5m',
                backtest_run_id: selectedRunId,
            };
            if (entryMs > 0) {
                params.range = 'custom';
                params.bar_time_ms = entryMs;
                params.start_ms = Math.max(0, entryMs - padMs);
                params.end_ms = Math.max(exitMs, entryMs) + padMs;
            } else {
                params.range = '1d';
            }
            window.location.href = buildPageUrl('/ibkr_chart.html', params, { environment: selectedRun?.source_environment || currentEnvironment });
        }
