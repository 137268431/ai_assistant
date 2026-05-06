        async function loadBacktestStatus() {
            activeStatus = await requestBacktestJson(`/api/custom/ibkr/backtest/status?environment=${encodeURIComponent(currentEnvironment)}`);
            renderStatusPanel();
        }

        async function loadBatches(preserveSelection = true) {
            const filter = `source_environment = "${escapeFilterValue(currentEnvironment)}"`;
            const payload = await apiFetch('ibkr_backtest_batches', {
                filter,
                sort: '-created',
                perPage: 30,
                page: 1,
            });
            batchList = toItems(payload).map(normalizeBatchRecord);
            if (!preserveSelection || !batchList.some((batch) => batch.id === selectedBatchId)) {
                selectedBatchId = batchList[0]?.id || '';
            }
            renderBatches();
            buildHeroNotes();
        }

        async function loadRuns(preserveSelection = true) {
            const filter = `source_environment = "${escapeFilterValue(currentEnvironment)}"`;
            const payload = await apiFetch('ibkr_backtest_runs', {
                filter,
                sort: '-created',
                perPage: 40,
                page: 1,
            });
            runList = toItems(payload).map(normalizeRunRecord);
            if (!preserveSelection || !runList.some((run) => run.id === selectedRunId)) {
                selectedRunId = runList[0]?.id || '';
            }
            renderRuns();
            buildHeroNotes();
        }

        async function refreshSelectedBatch(showToastOnSuccess = false) {
            if (!selectedBatchId) {
                selectedBatch = null;
                renderBatchDetail();
                return;
            }
            selectedBatch = batchList.find((batch) => batch.id === selectedBatchId) || null;
            renderBatchDetail();
            if (showToastOnSuccess && selectedBatch) {
                showToast(`已刷新 ${selectedBatch.name || selectedBatch.id}`);
            }
        }

        async function loadRunTrades(runId) {
            if (!runId) {
                selectedTrades = [];
                backtestTableExpandedState.trades = false;
                renderTrades();
                return;
            }
            const activeRunId = runId;
            const filter = `run_id = "${escapeFilterValue(runId)}"`;
            const items = await apiFetchAll('ibkr_backtest_trades', {
                filter,
                sort: 'trade_index',
                perPage: 200,
                maxPages: 12,
            });
            if (selectedRunId !== activeRunId) {
                return;
            }
            selectedTrades = items.map(normalizeTradeRecord);
            backtestTableExpandedState.trades = false;
            renderTrades();
        }

        async function loadRunTargets(runId, { showToastOnError = false } = {}) {
            if (!runId) {
                selectedTargets = [];
                selectedTargetsLoading = false;
                buildReplaySymbolOptions();
                renderRunDetail();
                return;
            }
            const activeRunId = runId;
            selectedTargets = [];
            selectedTargetsLoading = true;
            renderRunDetail();
            try {
                const filter = `run_id = "${escapeFilterValue(runId)}"`;
                const items = await apiFetchAll('ibkr_backtest_targets', {
                    filter,
                    sort: '-date,rank,symbol',
                    perPage: 200,
                    maxPages: 20,
                });
                if (selectedRunId !== activeRunId) {
                    return;
                }
                selectedTargets = items.map(normalizeBacktestTargetRecord);
            } catch (error) {
                console.error('loadRunTargets failed:', error);
                if (selectedRunId === activeRunId) {
                    selectedTargets = [];
                    if (showToastOnError) {
                        showToast(`读取回测 targets 失败: ${error.message || error}`);
                    }
                }
            } finally {
                if (selectedRunId === activeRunId) {
                    selectedTargetsLoading = false;
                    buildReplaySymbolOptions();
                    renderRunDetail();
                }
            }
        }

        async function refreshSelectedRun(showToastOnSuccess = false) {
            if (!selectedRunId) {
                selectedRun = null;
                selectedTrades = [];
                selectedTargets = [];
                selectedTargetsLoading = false;
                renderMetrics();
                renderRunDetail();
                renderTrades();
                renderReplay([]);
                return;
            }
            const found = runList.find((run) => run.id === selectedRunId);
            selectedRun = found || null;
            selectedTargets = [];
            selectedTargetsLoading = Boolean(selectedRun);
            renderMetrics();
            renderRunDetail();
            await Promise.all([
                loadRunTrades(selectedRunId),
                loadRunTargets(selectedRunId),
            ]);
            if (showToastOnSuccess && selectedRun) {
                showToast(`已刷新 ${selectedRun.name || selectedRun.id}`);
            }
        }
