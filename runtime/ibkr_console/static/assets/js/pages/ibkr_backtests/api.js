        const BACKTEST_RUN_LIST_FIELDS = [
            'id',
            'name',
            'status',
            'source_environment',
            'environment',
            'date_from',
            'date_to',
            'symbols',
            'symbol_source',
            'session_mode',
            'created',
            'updated',
            'started_at',
            'finished_at',
            'duration_s',
            'initial_capital',
            'trade_count',
            'net_pnl',
            'total_return_pct',
            'sharpe',
            'max_drawdown_pct',
            'win_rate',
            'progress',
            'error',
            'benchmark_symbol',
            'commission_per_share',
            'slippage_bps',
        ].join(',');
        const BACKTEST_BATCH_LIST_FIELDS = [
            'id',
            'name',
            'status',
            'source_environment',
            'environment',
            'date_from',
            'date_to',
            'symbols',
            'symbol_source',
            'session_mode',
            'created',
            'updated',
            'started_at',
            'finished_at',
            'variant_count',
            'completed_count',
            'best_run_id',
            'best_variant_label',
            'best_total_return_pct',
            'best_sharpe',
            'benchmark_symbol',
            'error',
        ].join(',');

        async function loadBacktestStatus() {
            activeStatus = await requestBacktestJson(`/api/custom/ibkr/backtest/status?environment=${encodeURIComponent(currentEnvironment)}`);
            renderStatusPanel();
        }

        async function loadBatches(preserveSelection = true) {
            const filter = `source_environment = "${escapeFilterValue(currentEnvironment)}"`;
            let payload = null;
            try {
                payload = await requestBacktestJson(`/api/custom/ibkr/backtest/batches?environment=${encodeURIComponent(currentEnvironment)}&limit=30`);
            } catch (error) {
                console.warn('fast batch list failed, falling back to PocketBase:', error);
                payload = await apiFetch('ibkr_backtest_batches', {
                    filter,
                    sort: '-created',
                    perPage: 30,
                    page: 1,
                    fields: BACKTEST_BATCH_LIST_FIELDS,
                    skipTotal: 1,
                });
            }
            batchList = toItems(payload).map(normalizeBatchRecord);
            if (!preserveSelection || !batchList.some((batch) => batch.id === selectedBatchId)) {
                selectedBatchId = batchList[0]?.id || '';
            }
            renderBatches();
            buildHeroNotes();
        }

        async function loadRuns(preserveSelection = true) {
            const filter = `source_environment = "${escapeFilterValue(currentEnvironment)}"`;
            let payload = null;
            try {
                payload = await requestBacktestJson(`/api/custom/ibkr/backtest/runs?environment=${encodeURIComponent(currentEnvironment)}&limit=40`);
            } catch (error) {
                console.warn('fast run list failed, falling back to PocketBase:', error);
                payload = await apiFetch('ibkr_backtest_runs', {
                    filter,
                    sort: '-created',
                    perPage: 40,
                    page: 1,
                    fields: BACKTEST_RUN_LIST_FIELDS,
                    skipTotal: 1,
                });
            }
            runList = toItems(payload).map(normalizeRunRecord);
            if (!preserveSelection || !runList.some((run) => run.id === selectedRunId)) {
                selectedRunId = runList[0]?.id || '';
            }
            renderRuns();
            buildHeroNotes();
        }

        function replaceListRecord(list, record) {
            if (!record?.id) return list;
            const index = list.findIndex((item) => item.id === record.id);
            if (index >= 0) {
                const next = list.slice();
                next[index] = record;
                return next;
            }
            return [record, ...list];
        }

        async function loadBacktestRunDetail(runId) {
            if (!runId) return null;
            const payload = await requestBacktestJson(`/api/custom/ibkr/backtest/run?environment=${encodeURIComponent(currentEnvironment)}&run_id=${encodeURIComponent(runId)}`);
            return normalizeRunRecord(payload.item || {});
        }

        async function hydrateSelectedRun(runId) {
            if (!runId) return null;
            try {
                const record = await loadBacktestRunDetail(runId);
                if (!record || selectedRunId !== runId) return selectedRun;
                selectedRun = record;
                runList = replaceListRecord(runList, record);
                return selectedRun;
            } catch (error) {
                console.warn('加载 run 详情失败:', error);
                return selectedRun;
            }
        }

        async function loadBacktestBatchDetail(batchId) {
            if (!batchId) return null;
            const payload = await requestBacktestJson(`/api/custom/ibkr/backtest/batch?environment=${encodeURIComponent(currentEnvironment)}&batch_id=${encodeURIComponent(batchId)}`);
            return normalizeBatchRecord(payload.item || {});
        }

        async function hydrateSelectedBatch(batchId) {
            if (!batchId) return null;
            try {
                const record = await loadBacktestBatchDetail(batchId);
                if (!record || selectedBatchId !== batchId) return selectedBatch;
                selectedBatch = record;
                batchList = replaceListRecord(batchList, record);
                return selectedBatch;
            } catch (error) {
                console.warn('加载 batch 详情失败:', error);
                return selectedBatch;
            }
        }

        async function refreshSelectedBatch(showToastOnSuccess = false) {
            if (!selectedBatchId) {
                selectedBatch = null;
                renderBatchDetail();
                return;
            }
            selectedBatch = batchList.find((batch) => batch.id === selectedBatchId) || null;
            renderBatchDetail();
            await hydrateSelectedBatch(selectedBatchId);
            renderBatches();
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
            selectedTargetsLoading = Boolean(selectedRunId);
            renderMetrics();
            renderRunDetail();
            const activeRunId = selectedRunId;
            const hydratePromise = hydrateSelectedRun(activeRunId).then(() => {
                if (selectedRunId !== activeRunId) return;
                renderRuns();
                renderMetrics();
                renderRunDetail();
            });
            await Promise.all([
                hydratePromise,
                loadRunTrades(activeRunId),
                loadRunTargets(activeRunId),
            ]);
            if (showToastOnSuccess && selectedRun) {
                showToast(`已刷新 ${selectedRun.name || selectedRun.id}`);
            }
        }
