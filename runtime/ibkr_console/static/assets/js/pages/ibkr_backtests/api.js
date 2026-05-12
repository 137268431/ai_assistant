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

        function clearSelectedTrackingRows() {
            selectedTrades = [];
            selectedTargets = [];
            selectedSignals = [];
            selectedReverseSignals = [];
            selectedTrackingModel = null;
            selectedTargetsLoading = false;
            trackingLoading = false;
            trackingFilterRunId = '';
            trackingFilters.date = '';
            trackingFilters.symbol = '';
            trackingFilters.eventType = '';
            backtestTableExpandedState.trades = false;
            backtestTableExpandedState.trackingTimeline = false;
            backtestTableExpandedState.trackingFlows = false;
            if (typeof renderTracking === 'function') renderTracking();
        }

        function refreshTrackingModel() {
            selectedTrackingModel = buildBacktestTrackingModel(
                selectedRun,
                selectedTargets,
                selectedSignals,
                selectedTrades,
                selectedReverseSignals,
            );
            trackingLoading = false;
            if (typeof renderTracking === 'function') renderTracking();
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

        function mergeBacktestRows(existingRows, nextRows) {
            const merged = [];
            const seen = new Set();
            [...(Array.isArray(existingRows) ? existingRows : []), ...(Array.isArray(nextRows) ? nextRows : [])].forEach((row) => {
                const key = row?.id || JSON.stringify(row);
                if (seen.has(key)) return;
                seen.add(key);
                merged.push(row);
            });
            return merged;
        }

        function assignBacktestRows(key, rows, { append = false } = {}) {
            if (key === 'trades') {
                selectedTrades = append ? mergeBacktestRows(selectedTrades, rows) : rows;
                selectedTrades.sort((a, b) => Number(a.trade_index || 0) - Number(b.trade_index || 0));
                return;
            }
            if (key === 'targets') {
                selectedTargets = append ? mergeBacktestRows(selectedTargets, rows) : rows;
                return;
            }
            if (key === 'signals') {
                selectedSignals = append ? mergeBacktestRows(selectedSignals, rows) : rows;
                return;
            }
            if (key === 'reverseSignals') {
                selectedReverseSignals = append ? mergeBacktestRows(selectedReverseSignals, rows) : rows;
            }
        }

        function renderBacktestRowsForKey(key, { renderPanel = true, renderDetail = true } = {}) {
            buildReplaySymbolOptions();
            refreshTrackingModel();
            if (key === 'trades' && renderPanel) renderTrades();
            if (key === 'targets') {
                selectedTargetsLoading = false;
                if (renderDetail) renderRunDetail();
            }
            if ((key === 'signals' || key === 'reverseSignals') && typeof renderTracking === 'function') {
                renderTracking();
            }
        }

        async function loadBacktestRowPage(key, runId, options = {}) {
            const config = BACKTEST_ROW_PAGE_CONFIG[key] || {};
            const collection = options.collection || config.collection;
            if (!collection) throw new Error(`Missing collection for ${key}`);
            if (!runId) {
                assignBacktestRows(key, [], { append: false });
                updateBacktestRowPagination(key, { page: 0, loaded: 0, total: null, hasMore: false, loading: false, error: '' });
                renderBacktestRowsForKey(key, options);
                return [];
            }
            const activeRunId = runId;
            const current = getBacktestRowPagination(key);
            const append = Boolean(options.append);
            const page = append ? Number(current.page || 0) + 1 : 1;
            const perPage = Number(options.perPage || current.perPage || config.perPage || 200);
            updateBacktestRowPagination(key, { loading: true, error: '', perPage });
            if (key === 'targets') selectedTargetsLoading = true;
            if ((key === 'signals' || key === 'reverseSignals') && !selectedTrackingModel) trackingLoading = true;
            if (options.renderBeforeLoad) renderBacktestRowsForKey(key, options);
            try {
                const filter = `run_id = "${escapeFilterValue(runId)}"`;
                const fetchParams = {
                    filter,
                    sort: options.sort,
                    perPage,
                    page,
                };
                if (options.skipTotal) fetchParams.skipTotal = 1;
                const payload = await apiFetch(collection, fetchParams);
                if (selectedRunId !== activeRunId) return [];
                const normalizedRows = toItems(payload).map(options.normalize || ((row) => row));
                assignBacktestRows(key, normalizedRows, { append });
                const loaded = getBacktestRowList(key).length;
                const totalItems = payload.totalItems != null ? Number(payload.totalItems || 0) : null;
                const hasMore = totalItems != null
                    ? loaded < totalItems
                    : normalizedRows.length >= perPage;
                updateBacktestRowPagination(key, {
                    page,
                    loaded,
                    total: totalItems,
                    hasMore,
                    loading: false,
                    error: '',
                });
                renderBacktestRowsForKey(key, options);
                return normalizedRows;
            } catch (error) {
                console.error(`loadBacktestRowPage ${key} failed:`, error);
                if (selectedRunId === activeRunId) {
                    if (!append) assignBacktestRows(key, [], { append: false });
                    updateBacktestRowPagination(key, {
                        loading: false,
                        hasMore: false,
                        error: error.message || String(error),
                    });
                    renderBacktestRowsForKey(key, options);
                    if (options.showToastOnError) {
                        showToast(`读取回测 ${config.label || key} 失败: ${error.message || error}`);
                    }
                }
                return [];
            } finally {
                if (selectedRunId === activeRunId) {
                    if (key === 'targets') selectedTargetsLoading = false;
                    if (key === 'signals' || key === 'reverseSignals') trackingLoading = false;
                }
            }
        }

        async function loadMoreBacktestRows(key) {
            if (!selectedRunId) {
                showToast('先选择一个 run');
                return;
            }
            const state = getBacktestRowPagination(key);
            if (state.loading || !state.hasMore) return;
            await loadBacktestRowsByKey(key, selectedRunId, { append: true, showToastOnError: true });
        }

        async function loadAllBacktestRows(key) {
            if (!selectedRunId) {
                showToast('先选择一个 run');
                return;
            }
            let guard = 0;
            while (selectedRunId && getBacktestRowPagination(key).hasMore && !getBacktestRowPagination(key).loading && guard < 80) {
                guard += 1;
                await loadBacktestRowsByKey(key, selectedRunId, { append: true, showToastOnError: true });
            }
        }

        async function loadBacktestRowsByKey(key, runId, options = {}) {
            if (key === 'trades') return loadRunTrades(runId, options);
            if (key === 'targets') return loadRunTargets(runId, options);
            if (key === 'signals') return loadRunSignals(runId, options);
            if (key === 'reverseSignals') return loadRunReverseSignals(runId, options);
            throw new Error(`Unknown row key: ${key}`);
        }

        function shouldLoadBacktestRowsForTab(key, tabKey = activeBacktestTab) {
            if (key === 'targets') return tabKey === 'runs' || tabKey === 'tracking';
            if (key === 'trades') return tabKey === 'trades' || tabKey === 'tracking';
            if (key === 'signals' || key === 'reverseSignals') return tabKey === 'tracking';
            return false;
        }

        async function ensureBacktestRowsForActiveTab(tabKey = activeBacktestTab) {
            if (!selectedRunId) return;
            const tasks = [];
            if (shouldLoadBacktestRowsForTab('targets', tabKey) && getBacktestRowPagination('targets').page === 0) {
                tasks.push(loadRunTargets(selectedRunId, { showToastOnError: true, renderDetail: tabKey === 'runs' }));
            }
            if (shouldLoadBacktestRowsForTab('trades', tabKey) && getBacktestRowPagination('trades').page === 0) {
                tasks.push(loadRunTrades(selectedRunId, { showToastOnError: true, renderPanel: tabKey === 'trades' }));
            }
            if (shouldLoadBacktestRowsForTab('signals', tabKey) && getBacktestRowPagination('signals').page === 0) {
                tasks.push(loadRunSignals(selectedRunId, { showToastOnError: true }));
            }
            if (shouldLoadBacktestRowsForTab('reverseSignals', tabKey) && getBacktestRowPagination('reverseSignals').page === 0) {
                tasks.push(loadRunReverseSignals(selectedRunId, { showToastOnError: true }));
            }
            await Promise.all(tasks);
        }

        async function loadRunTrades(runId, { renderPanel = true, append = false, showToastOnError = false } = {}) {
            if (!runId) {
                selectedTrades = [];
                updateBacktestRowPagination('trades', { page: 0, loaded: 0, total: null, hasMore: false, loading: false, error: '' });
                backtestTableExpandedState.trades = false;
                refreshTrackingModel();
                if (renderPanel) renderTrades();
                return;
            }
            return loadBacktestRowPage('trades', runId, {
                append,
                renderPanel,
                showToastOnError,
                collection: 'ibkr_backtest_trades',
                sort: 'trade_index',
                perPage: 200,
                skipTotal: true,
                normalize: normalizeTradeRecord,
            });
        }

        async function loadRunTargets(runId, { showToastOnError = false, preserveExisting = false, renderDetail = true, append = false } = {}) {
            if (!runId) {
                selectedTargets = [];
                selectedTargetsLoading = false;
                updateBacktestRowPagination('targets', { page: 0, loaded: 0, total: null, hasMore: false, loading: false, error: '' });
                buildReplaySymbolOptions();
                refreshTrackingModel();
                if (renderDetail) renderRunDetail();
                return;
            }
            if (!preserveExisting && !append) {
                selectedTargets = [];
            }
            selectedTargetsLoading = !preserveExisting || !selectedTargets.length;
            if (renderDetail) renderRunDetail();
            return loadBacktestRowPage('targets', runId, {
                append: append || preserveExisting,
                renderDetail,
                showToastOnError,
                collection: 'ibkr_backtest_targets',
                sort: '-date,rank,symbol',
                perPage: 200,
                skipTotal: true,
                normalize: normalizeBacktestTargetRecord,
            });
        }

        async function loadRunSignals(runId, { showToastOnError = false, preserveExisting = false, append = false } = {}) {
            if (!runId) {
                selectedSignals = [];
                updateBacktestRowPagination('signals', { page: 0, loaded: 0, total: null, hasMore: false, loading: false, error: '' });
                refreshTrackingModel();
                return;
            }
            if (!preserveExisting && !selectedTrackingModel) {
                trackingLoading = true;
                if (typeof renderTracking === 'function') renderTracking();
            }
            return loadBacktestRowPage('signals', runId, {
                append: append || preserveExisting,
                showToastOnError,
                collection: 'ibkr_backtest_signals',
                sort: 'bar_time_ms,symbol',
                perPage: 200,
                skipTotal: true,
                normalize: normalizeBacktestSignalRecord,
            });
        }

        async function loadRunReverseSignals(runId, { showToastOnError = false, preserveExisting = false, append = false } = {}) {
            if (!runId) {
                selectedReverseSignals = [];
                updateBacktestRowPagination('reverseSignals', { page: 0, loaded: 0, total: null, hasMore: false, loading: false, error: '' });
                refreshTrackingModel();
                return;
            }
            if (!preserveExisting && !selectedTrackingModel) {
                trackingLoading = true;
                if (typeof renderTracking === 'function') renderTracking();
            }
            return loadBacktestRowPage('reverseSignals', runId, {
                append: append || preserveExisting,
                showToastOnError,
                collection: 'ibkr_backtest_reverse_signals',
                sort: 'bar_time_ms,symbol',
                perPage: 200,
                skipTotal: true,
                normalize: normalizeBacktestReverseRecord,
            });
        }

        function scheduleBacktestRowsForActiveTab(tabKey = activeBacktestTab) {
            setTimeout(() => {
                ensureBacktestRowsForActiveTab(tabKey).catch((error) => {
                    console.warn('background backtest row load failed:', error);
                });
            }, 0);
        }

        async function refreshSelectedRun(showToastOnSuccess = false, { loadRows = true } = {}) {
            if (!selectedRunId) {
                selectedRun = null;
                clearSelectedTrackingRows();
                resetBacktestClientPagination();
                renderMetrics();
                renderRunDetail();
                renderTrades();
                renderReplay([]);
                return;
            }
            const activeRunId = selectedRunId;
            const hadSelectedRun = selectedRun?.id === activeRunId;
            const found = runList.find((run) => run.id === activeRunId);
            selectedRun = found || selectedRun || null;
            const renderDetailDuringRefresh = showToastOnSuccess || !hadSelectedRun || isBacktestRunMutable(selectedRun);
            const preserveRows = hadSelectedRun && !showToastOnSuccess;
            if (!preserveRows) {
                selectedTargets = [];
                selectedSignals = [];
                selectedReverseSignals = [];
                selectedTrades = [];
                resetBacktestRowPagination(activeRunId);
                resetBacktestClientPagination();
                selectedTrackingModel = null;
                selectedTargetsLoading = shouldLoadBacktestRowsForTab('targets', activeBacktestTab);
                trackingLoading = activeBacktestTab === 'tracking';
                backtestTableExpandedState.trackingTimeline = false;
                backtestTableExpandedState.trackingFlows = false;
            }
            if (renderDetailDuringRefresh) {
                renderMetrics();
                renderRunDetail();
            }
            if (!selectedTrackingModel && typeof renderTracking === 'function') renderTracking();
            const hydratePromise = hydrateSelectedRun(activeRunId).then(() => {
                if (selectedRunId !== activeRunId) return;
                renderRuns();
                if (renderDetailDuringRefresh || isBacktestRunMutable(selectedRun)) {
                    renderMetrics();
                    renderRunDetail();
                }
                refreshTrackingModel();
            });
            await Promise.all([
                hydratePromise,
                loadRows && !preserveRows ? ensureBacktestRowsForActiveTab(activeBacktestTab) : Promise.resolve(),
            ]);
            if (!loadRows && !preserveRows) {
                scheduleBacktestRowsForActiveTab(activeBacktestTab);
            }
            if (showToastOnSuccess && selectedRun) {
                showToast(`已刷新 ${selectedRun.name || selectedRun.id}`);
            }
        }
