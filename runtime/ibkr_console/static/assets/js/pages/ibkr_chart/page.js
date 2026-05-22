        async function loadChartWorkspace() {
            requireAuth(`${window.location.pathname}${window.location.search}`);
            if (!currentSymbol) {
                await resolveInitialContext();
            }
            chartWorkspaceState = 'loading';
            resetCompareState();
            closeMobileQuickPanel();
            chartPointerLocked = false;
            suppressNextChartClick = false;
            clearChartTouchTimer();
            stopChartRealtimePolling();
            realtimeQuoteSnapshot = null;
            formingBarSnapshot = null;
            chartRealtimeQuoteError = '';
            chartRealtimeQuoteFailureCount = 0;
            chartRealtimePreviewError = '';
            resetChartRealtimeWarnings();
            chartDisplayPayload = null;
            realtimeComputedPayload = null;
            chartMarkerDensityTier = '';

            const input = document.getElementById('chartSymbolInput');
            if (input && currentSymbol) input.value = currentSymbol;
            renderTimeframeGroup();
            renderRangeGroup();
            renderLayerStrip();
            renderCompareButtons();
            closeSignalDrawer();
            if (chartInstance) {
                chartInstance.dispose();
                chartInstance = null;
            }
            lastPayload = null;
            document.getElementById('chartPanelTitle').textContent = `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)}`;
            document.getElementById('chartMeta').textContent = '数据加载中';
            document.getElementById('chartNote').textContent = '正在重算指标 / 信号 ...';
            document.getElementById('chartCanvas').innerHTML = '<div class="chart-empty">图表数据加载中...</div>';
            document.getElementById('infoRail').innerHTML = '<div class="rail-card"><div class="rail-kicker">Loading</div><div class="rail-sub">正在重算图表 ...</div></div>';
            renderRailNav();
            renderHeroStatus(null);
            renderSummaryStrip(null);
            renderCursorStrip(null);

            const requestBounds = getChartRequestBounds();

            try {
                const timelineResp = await requestChartJson('/api/custom/ibkr/proxy', {
                    method: 'POST',
                    body: {
                        action: 'chart/timeline',
                        ...buildModePayload({}, { brokerMode: currentBrokerMode, dataEnvironment: currentEnvironment }),
                        symbol: currentSymbol,
                        interval: currentInterval,
                        start_ms: requestBounds.startMs,
                        end_ms: requestBounds.endMs,
                        include_signals: currentInterval === '5m',
                        include_trace: true,
                        backtest_run_id: currentBacktestRunId,
                    }
                }, 2);
                const timelinePayload = buildTimelinePayloadFromResponse(timelineResp);
                const bars = timelinePayload.bars;
                const indicators = timelinePayload.indicators;
                const signals = timelinePayload.signals;
                const latestIndicator = timelinePayload.latestIndicator;
                let initialFocusIndex = -1;

                if (latestIndicator) {
                    currentAnchorMs = Number(latestIndicator.bar_time_ms || currentAnchorMs) || currentAnchorMs;
                } else if (bars.length) {
                    currentAnchorMs = Number(bars[bars.length - 1]?.bar_time_ms || currentAnchorMs) || currentAnchorMs;
                }
                if (pendingFocusBarTimeMs > 0 && bars.length) {
                    initialFocusIndex = findBarIndexByTime(bars, pendingFocusBarTimeMs);
                    if (initialFocusIndex >= 0) {
                        selectedBarIndex = initialFocusIndex;
                        selectedSignalId = '';
                        hoverBarIndex = -1;
                    }
                }
                currentIndicatorId = '';

                lastPayload = timelinePayload;
                await refreshChartRealtimeState({ render: false });
                chartWorkspaceState = 'ready';
                renderChart(lastPayload);
                const activePayload = getChartDisplayPayload() || lastPayload;
                renderHeroStatus(activePayload);
                renderSummaryStrip(activePayload);
                if (initialFocusIndex >= 0) {
                    const displayPayload = getChartDisplayPayload() || lastPayload;
                    ensureBarVisible(initialFocusIndex, displayPayload, { center: true });
                }
                pendingFocusBarTimeMs = 0;
                renderCompareButtons();
                updateQueryState();
                setPageContextMeta([
                    { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
                    { label: '标的', value: currentSymbol || '--' },
                    { label: '周期', value: getIntervalLabel(currentInterval) },
                    { label: '范围', value: currentRangeKey.toUpperCase() },
                ]);
                setPageRefreshTime();
                startChartRealtimePolling();
            } catch (error) {
                stopChartRealtimePolling();
                chartWorkspaceState = 'error';
                chartDisplayPayload = null;
                if (chartInstance) {
                    chartInstance.dispose();
                    chartInstance = null;
                }
                document.getElementById('chartMeta').textContent = '加载失败';
                document.getElementById('chartCanvas').innerHTML = `<div class="chart-empty">加载失败：${escapeHtml(error.message || 'unknown error')}<br>请确认当前环境已有 <code>ibkr_bars</code>。</div>`;
                document.getElementById('chartNote').textContent = '图表只依赖 ibkr_bars。';
                document.getElementById('infoRail').innerHTML = `<div class="rail-card"><div class="rail-kicker">Load Error</div><div class="rail-sub">${escapeHtml(error.message || 'unknown error')}</div></div>`;
                renderRailNav();
                renderHeroStatus(null);
                renderSummaryStrip(null);
                renderCursorStrip(null);
                renderCompareButtons();
            }
        }

        async function runIbkrCompareInternal() {
            if (!lastPayload || !Array.isArray(lastPayload.bars) || !lastPayload.bars.length) {
                showToast('当前没有可对比的 bars');
                return;
            }
            if (!isCompareSupportedRange()) {
                showToast('当前 v1 不支持 ALL 范围的 IBKR 对比');
                return;
            }
            compareLoading = true;
            compareError = '';
            comparePayload = null;
            renderCompareButtons();
            const initialPayload = getChartDisplayPayload() || lastPayload;
            renderHeroStatus(initialPayload);
            renderSummaryStrip(initialPayload);
            renderInfoRail(initialPayload);
            renderCursorStrip(initialPayload);

            const requestBounds = getChartRequestBounds();

            try {
                const response = await requestChartJson('/api/custom/ibkr/proxy', {
                    method: 'POST',
                    body: {
                        action: 'chart/compare',
                        ...buildModePayload({}, { brokerMode: currentBrokerMode, dataEnvironment: currentEnvironment }),
                        symbol: currentSymbol,
                        interval: currentInterval,
                        start_ms: requestBounds.startMs,
                        end_ms: requestBounds.endMs,
                        include_signals: currentInterval === '5m'
                    }
                }, 1);
                comparePayload = buildComparePayloadFromResponse(response);
                compareError = '';
                if (comparePayload?.storedTimeline?.bars?.length) {
                    lastPayload = {
                        ...comparePayload.storedTimeline,
                        traceTimeline: Array.isArray(comparePayload.storedTimeline?.traceTimeline) && comparePayload.storedTimeline.traceTimeline.length
                            ? comparePayload.storedTimeline.traceTimeline
                            : (Array.isArray(lastPayload?.traceTimeline) ? lastPayload.traceTimeline : []),
                    };
                }
                renderChart(lastPayload);
                const activePayload = getChartDisplayPayload() || lastPayload;
                renderHeroStatus(activePayload);
                renderSummaryStrip(activePayload);
                renderCompareButtons();
            } catch (error) {
                compareError = error?.message || 'IBKR compare failed';
                comparePayload = null;
                const activePayload = getChartDisplayPayload() || lastPayload;
                renderHeroStatus(activePayload);
                renderSummaryStrip(activePayload);
                renderInfoRail(activePayload);
                renderCursorStrip(activePayload);
                renderCompareButtons();
            } finally {
                compareLoading = false;
                const activePayload = getChartDisplayPayload() || lastPayload;
                renderHeroStatus(activePayload);
                renderSummaryStrip(activePayload);
                renderInfoRail(activePayload);
                renderCursorStrip(activePayload);
                renderCompareButtons();
            }
        }

        window.selectChartInterval = async function(interval) {
            currentInterval = normalizeInterval(interval, currentInterval);
            currentRangeKey = normalizeRangeKey(currentRangeKey, currentInterval);
            currentAnchorMs = 0;
            pendingFocusBarTimeMs = 0;
            currentIndicatorId = '';
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            cancelChartSelectionMode({ render: false });
            resetChartZoomState();
            closeMobileQuickPanel();
            await loadChartWorkspace();
        };

        window.selectChartRange = async function(rangeKey) {
            const nextRangeKey = normalizeRangeKey(rangeKey, currentInterval);
            if (isCustomRange(nextRangeKey)) {
                customRangeFormOpen = true;
                seedCustomRangeFromPayload();
                currentAnchorMs = customRangeEndMs || currentAnchorMs;
                renderRangeGroup();
                return;
            }
            currentRangeKey = nextRangeKey;
            customRangeFormOpen = false;
            pendingFocusBarTimeMs = 0;
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            cancelChartSelectionMode({ render: false });
            resetChartZoomState();
            closeMobileQuickPanel();
            await loadChartWorkspace();
        };

        window.applyCustomChartRange = async function() {
            const startInput = document.getElementById('customRangeStart');
            const endInput = document.getElementById('customRangeEnd');
            const startMs = parseDateTimeEtInputValue(startInput?.value || '');
            const endMs = parseDateTimeEtInputValue(endInput?.value || '');
            if (!startMs || !endMs) {
                showToast('请填写自定义开始和结束时间');
                return;
            }
            if (endMs <= startMs) {
                showToast('结束时间必须晚于开始时间');
                return;
            }
            currentRangeKey = 'custom';
            customRangeFormOpen = false;
            customRangeStartMs = startMs;
            customRangeEndMs = endMs;
            currentAnchorMs = endMs;
            pendingFocusBarTimeMs = 0;
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            cancelChartSelectionMode({ render: false });
            resetChartZoomState();
            closeMobileQuickPanel();
            await loadChartWorkspace();
        };

        window.toggleChartLayer = function(layer) {
            if (!Object.prototype.hasOwnProperty.call(chartLayerState, layer)) return;
            const layerDef = getChartLayerDefs().find((item) => item.key === layer);
            if (!layerDef || layerDef.disabled) return;
            chartLayerState[layer] = !chartLayerState[layer];
            saveChartUiPrefs();
            renderLayerStrip();
            if (lastPayload) renderChart(lastPayload);
        };

        window.focusSignalBar = function(barTimeMs, signalId = '') {
            if (!lastPayload || !Array.isArray(lastPayload.bars) || !lastPayload.bars.length) return;
            const index = findBarIndexByTime(lastPayload.bars, barTimeMs);
            if (index < 0) {
                showToast('当前窗口未包含该信号');
                return;
            }
            const displayPayload = getChartDisplayPayload();
            clearTraceManualPage();
            focusBarIndex(index, signalId);
            ensureBarVisible(index, displayPayload || lastPayload, { center: true });
            const context = buildContext(displayPayload || lastPayload, index, signalId ? decodeURIComponent(signalId) : '');
            if (context?.activeSignal) {
                openSignalDrawer(context.activeSignal, context);
            }
        };

        window.loadSymbolFromInput = async function() {
            const value = String(document.getElementById('chartSymbolInput')?.value || '').trim().toUpperCase();
            if (!value) {
                showToast('先输入 symbol');
                return;
            }
            currentSymbol = value;
            currentAnchorMs = 0;
            pendingFocusBarTimeMs = 0;
            currentIndicatorId = '';
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            cancelChartSelectionMode({ render: false });
            resetChartZoomState();
            closeMobileQuickPanel();
            await loadChartWorkspace();
        };

        window.reloadChartPage = async function() {
            await loadChartWorkspace();
        };

        window.runIbkrCompare = async function() {
            await runIbkrCompareInternal();
        };

        window.clearIbkrCompare = function() {
            resetCompareState();
            if (lastPayload) {
                renderChart(lastPayload);
                const activePayload = getChartDisplayPayload() || lastPayload;
                renderHeroStatus(activePayload);
                renderSummaryStrip(activePayload);
            }
        };

        window.focusCompareBar = function(barTimeMs) {
            if (!lastPayload || !Array.isArray(lastPayload.bars) || !lastPayload.bars.length) return;
            const index = findBarIndexByTime(lastPayload.bars, barTimeMs);
            if (index < 0) {
                showToast('该异常 bar 不在当前 stored bars 主图中');
                return;
            }
            const displayPayload = getChartDisplayPayload();
            clearTraceManualPage();
            focusBarIndex(index, '');
            ensureBarVisible(index, displayPayload || lastPayload, { center: true });
        };

        window.scrollInfoRailCard = function(index) {
            setActiveRailCard(index, { syncScroll: true });
        };

        window.scrollInfoRailCardByKicker = function(kicker) {
            const rail = document.getElementById('infoRail');
            const cards = rail ? Array.from(rail.querySelectorAll('.rail-card')) : [];
            const targetIndex = cards.findIndex((card) => String(card.querySelector('.rail-kicker')?.textContent || '').trim() === String(kicker || '').trim());
            if (targetIndex < 0) return;
            setActiveRailCard(targetIndex, { syncScroll: true });
        };

        window.focusPreviousChartBar = function() {
            focusRelativeBar(-1);
        };

        window.focusNextChartBar = function() {
            focusRelativeBar(1);
        };

        window.focusPreviousChartSignal = function() {
            focusSignalByStep(-1);
        };

        window.focusNextChartSignal = function() {
            focusSignalByStep(1);
        };

        window.focusLatestChartBar = function() {
            focusLatestBarAction();
        };

        window.centerChartOnFocusBar = function() {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            const focusIndex = getEffectiveCursorIndex(payload);
            ensureBarVisible(focusIndex, payload, { center: true });
            syncChartTooltip(focusIndex);
        };

        window.zoomInChartView = function() {
            zoomChartAroundFocus(0.72);
        };

        window.zoomOutChartView = function() {
            zoomChartAroundFocus(1.38);
        };

        window.resetChartView = function() {
            resetChartViewAction();
            if (lastPayload) renderCursorStrip(getChartDisplayPayload());
        };

        window.downloadChartImage = function() {
            if (!chartInstance) {
                showToast('暂无可导出的图表');
                return;
            }
            const url = chartInstance.getDataURL({
                type: 'png',
                pixelRatio: 2,
                backgroundColor: '#07101a',
            });
            const link = document.createElement('a');
            link.href = url;
            link.download = `${currentSymbol || 'ibkr'}-${currentInterval}-chart.png`;
            document.body.appendChild(link);
            link.click();
            link.remove();
        };

        window.toggleChartFocusMode = toggleChartFocusMode;
        window.lockChartPointerAtIndex = function(index) {
            lockChartPointerAtIndex(index);
        };
        window.unlockChartPointer = unlockChartPointer;
        window.toggleChartLegendCollapsed = toggleChartLegendCollapsed;
        window.toggleInspectorDrawer = toggleInspectorDrawer;
        window.closeInspectorDrawer = closeInspectorDrawer;
        window.openMobileQuickPanel = openMobileQuickPanel;
        window.closeSignalDrawer = closeSignalDrawer;
        window.openFocusedSignalDrawer = openFocusedSignalDrawer;
        window.closeMobileQuickPanel = closeMobileQuickPanel;
        window.dismissMobileGestureHint = dismissMobileGestureHint;

        function handleChartHotkeys(event) {
            const targetTag = String(event?.target?.tagName || '').toLowerCase();
            const isTyping = ['input', 'textarea', 'select'].includes(targetTag) || Boolean(event?.target?.isContentEditable);
            if (isTyping) {
                if (event.key === 'Escape') {
                    cancelChartSelectionMode();
                    closeInspectorDrawer();
                    closeMobileQuickPanel();
                    closeSignalDrawer();
                }
                return;
            }
            const displayPayload = getChartDisplayPayload();
            if (!displayPayload || !Array.isArray(displayPayload.bars) || !displayPayload.bars.length) {
                if (event.key === 'Escape') {
                    cancelChartSelectionMode();
                    closeInspectorDrawer();
                    closeMobileQuickPanel();
                    closeSignalDrawer();
                }
                return;
            }
            if (event.metaKey || event.ctrlKey || event.altKey) {
                if (event.key === 'Escape') {
                    cancelChartSelectionMode();
                    closeInspectorDrawer();
                    closeMobileQuickPanel();
                    closeSignalDrawer();
                }
                return;
            }
            if (event.key === 'Escape') {
                cancelChartSelectionMode();
                closeInspectorDrawer();
                closeMobileQuickPanel();
                closeSignalDrawer();
                return;
            }
            if (event.key === 'ArrowLeft') {
                event.preventDefault();
                focusRelativeBar(event.shiftKey ? -10 : -1);
                return;
            }
            if (event.key === 'ArrowRight') {
                event.preventDefault();
                focusRelativeBar(event.shiftKey ? 10 : 1);
                return;
            }
            if (event.key === 'Home') {
                event.preventDefault();
                clearTraceManualPage();
                focusBarIndex(0);
                hoverBarIndex = 0;
                ensureBarVisible(0, displayPayload, { center: true });
                renderCursorStrip(displayPayload);
                return;
            }
            if (event.key === 'End') {
                event.preventDefault();
                focusLatestBarAction();
                return;
            }
            if (event.key === 'n' || event.key === 'N') {
                event.preventDefault();
                focusSignalByStep(1);
                return;
            }
            if (event.key === 'p' || event.key === 'P') {
                event.preventDefault();
                focusSignalByStep(-1);
                return;
            }
            if (event.key === '+' || event.key === '=') {
                event.preventDefault();
                zoomChartAroundFocus(0.72);
                return;
            }
            if (event.key === '-' || event.key === '_') {
                event.preventDefault();
                zoomChartAroundFocus(1.38);
                return;
            }
            if (event.key === '0') {
                event.preventDefault();
                resetChartViewAction();
                if (lastPayload) renderCursorStrip(getChartDisplayPayload());
                return;
            }
            if (event.key === 'c' || event.key === 'C') {
                event.preventDefault();
                const focusIndex = getEffectiveCursorIndex(displayPayload);
                ensureBarVisible(focusIndex, displayPayload, { center: true });
                syncChartTooltip(focusIndex);
                return;
            }
            if (event.key === 'f' || event.key === 'F') {
                event.preventDefault();
                toggleChartFocusMode();
                return;
            }
            if (event.key === 'i' || event.key === 'I') {
                event.preventDefault();
                toggleInspectorDrawer();
            }
        }

        window.onEnvironmentChange = async function(environment) {
            currentEnvironment = normalizeRuntimeEnvironment(environment, getSharedDataEnvironment());
            currentBrokerMode = getCurrentBrokerMode();
            currentAnchorMs = 0;
            pendingFocusBarTimeMs = 0;
            currentIndicatorId = '';
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            activeDrawerSignalId = '';
            cancelChartSelectionMode({ render: false });
            resetChartZoomState();
            closeMobileQuickPanel();
            await resolveInitialContext();
            await loadChartWorkspace();
        };

        document.addEventListener('DOMContentLoaded', async () => {
            document.getElementById('nav').innerHTML = renderNav('/ibkr_chart.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('📉 IBKR 图表', { subtitle: '全屏分析 / 信号叠加' });
            document.getElementById('pageBridge').innerHTML = renderAnalyticsBridge('/ibkr_chart.html');

            document.getElementById('chartSymbolInput').addEventListener('keydown', async (event) => {
                if (event.key !== 'Enter') return;
                event.preventDefault();
                await loadSymbolFromInput();
            });

            loadChartUiPrefs();
            loadMobileGestureHintState();
            renderLayerStrip();
            renderCompareButtons();
            applyChartFocusMode();
            renderHeroStatus(null);
            renderSummaryStrip(null);
            renderCursorStrip(null);
            await resolveInitialContext();
            await loadChartWorkspace();

            document.addEventListener('keydown', handleChartHotkeys);
            document.addEventListener('pointerdown', (event) => {
                const chartShell = document.querySelector('.chart-shell');
                if (chartShell && !chartShell.contains(event.target)) {
                    clearTransientChartCursor();
                }
            }, true);
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) clearTransientChartCursor();
            });
            window.addEventListener('blur', clearTransientChartCursor);

            window.addEventListener('resize', () => {
                if (lastPayload) renderCursorStrip(getChartDisplayPayload());
                else renderChartWorkspaceChrome(null);
                if (chartInstance) chartInstance.resize();
            });
            window.addEventListener('beforeunload', stopChartRealtimePolling);
        });
