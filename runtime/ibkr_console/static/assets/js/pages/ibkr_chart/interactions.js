        function findBarIndexByTime(bars, barTimeMs) {
            const target = Number(barTimeMs || 0);
            if (!target) return -1;
            return bars.findIndex((item) => Number(item?.bar_time_ms || 0) === target);
        }

        function getFocusContext(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) return null;
            const safeIndex = Math.min(Math.max(selectedBarIndex >= 0 ? selectedBarIndex : bars.length - 1, 0), bars.length - 1);
            return buildContext(payload, safeIndex, selectedSignalId);
        }

        function cancelChartTooltipSync() {
            if (chartTooltipSyncRaf) {
                window.cancelAnimationFrame(chartTooltipSyncRaf);
                chartTooltipSyncRaf = 0;
            }
            if (chartTooltipSyncTimer) {
                window.clearTimeout(chartTooltipSyncTimer);
                chartTooltipSyncTimer = 0;
            }
        }

        function syncChartTooltip(index) {
            if (!chartInstance || !Number.isInteger(index) || index < 0) return;
            setChartTooltipPinned(chartPointerLocked);
            chartInstance.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: index });
        }

        function scheduleChartTooltipSync(index) {
            if (!Number.isInteger(index) || index < 0) return;
            cancelChartTooltipSync();
            chartTooltipSyncRaf = window.requestAnimationFrame(() => {
                chartTooltipSyncRaf = 0;
                syncChartTooltip(index);
                chartTooltipSyncTimer = window.setTimeout(() => {
                    chartTooltipSyncTimer = 0;
                    syncChartTooltip(index);
                }, 60);
            });
        }

        function hideChartTooltip() {
            cancelChartTooltipSync();
            if (!chartInstance) return;
            setChartTooltipPinned(false);
            chartInstance.dispatchAction({ type: 'hideTip' });
        }

        function setChartTooltipPinned(pinned) {
            if (!chartInstance) return;
            const nextPinned = Boolean(pinned);
            if (chartTooltipPinned === nextPinned) return;
            chartTooltipPinned = nextPinned;
            chartInstance.setOption({ tooltip: { alwaysShowContent: nextPinned } });
        }

        function clearTransientChartCursor() {
            if (chartPointerLocked) return;
            const payload = getChartDisplayPayload();
            hoverBarIndex = -1;
            hideChartTooltip();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            renderCursorStrip(payload, { includeTrace: false });
            const focusIndex = getEffectiveCursorIndex(payload);
            if (focusIndex >= 0) {
                scheduleChartFocusMarkerSync(focusIndex, payload);
            }
        }

        function scheduleChartFocusMarkerSync(index, payload = getChartDisplayPayload()) {
            if (!chartInstance || !Number.isInteger(index) || index < 0) return;
            if (chartFocusMarkerSyncRaf) {
                window.cancelAnimationFrame(chartFocusMarkerSyncRaf);
                chartFocusMarkerSyncRaf = 0;
            }
            chartFocusMarkerSyncRaf = window.requestAnimationFrame(() => {
                chartFocusMarkerSyncRaf = 0;
                if (!chartInstance) return;
                const markPoint = buildFocusMarkPointConfig(index, payload);
                chartInstance.setOption({
                    series: [{
                        id: 'price-main',
                        markPoint: markPoint || { data: [] },
                    }]
                });
            });
        }

        function syncCursorIndex(index) {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            if (chartPointerLocked) return;
            const safeIndex = Math.min(Math.max(Number(index) || 0, 0), payload.bars.length - 1);
            hoverBarIndex = safeIndex;
            renderCursorStrip(payload, { includeTrace: false });
            scheduleChartFocusMarkerSync(safeIndex, payload);
        }

        function clearChartTouchTimer() {
            if (chartTouchSession.timer) {
                window.clearTimeout(chartTouchSession.timer);
                chartTouchSession.timer = 0;
            }
        }

        function unlockChartPointer({ preserveHover = true } = {}) {
            chartPointerLocked = false;
            if (!preserveHover) hoverBarIndex = -1;
            if (lastPayload) renderCursorStrip(getChartDisplayPayload());
        }

        function getBarIndexFromClientPoint(clientX, clientY) {
            const canvas = document.getElementById('chartCanvas');
            if (!chartInstance || !canvas || clientX == null || clientY == null) return -1;
            const rect = canvas.getBoundingClientRect();
            const x = Number(clientX) - rect.left;
            const y = Number(clientY) - rect.top;
            if (x < 0 || y < 0 || x > rect.width || y > rect.height) return -1;
            const point = [x, y];
            let result = null;
            try {
                result = chartInstance.convertFromPixel({ seriesIndex: 0 }, point);
            } catch (_) {
                result = null;
            }
            const rawIndex = Array.isArray(result) ? result[0] : result;
            const index = Math.round(Number(rawIndex));
            if (!Number.isInteger(index)) return -1;
            const bars = Array.isArray(getChartDisplayPayload()?.bars) ? getChartDisplayPayload().bars : [];
            return bars.length ? clampIndex(index, bars.length) : -1;
        }

        function getChartSelectionOverlay() {
            const shell = document.querySelector('.chart-stage-shell');
            if (!shell) return null;
            let overlay = shell.querySelector('.chart-selection-box');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.className = 'chart-selection-box';
                overlay.setAttribute('aria-hidden', 'true');
                shell.appendChild(overlay);
            }
            return overlay;
        }

        function hideChartSelectionOverlay() {
            const overlay = document.querySelector('.chart-selection-box');
            if (!overlay) return;
            overlay.classList.remove('show');
            overlay.style.left = '';
            overlay.style.top = '';
            overlay.style.width = '';
            overlay.style.height = '';
        }

        function renderChartSelectionOverlay(startClientX, currentClientX) {
            const canvas = document.getElementById('chartCanvas');
            const shell = document.querySelector('.chart-stage-shell');
            const overlay = getChartSelectionOverlay();
            if (!canvas || !shell || !overlay) return;
            const canvasRect = canvas.getBoundingClientRect();
            const shellRect = shell.getBoundingClientRect();
            const startX = clampNumber(Number(startClientX) - canvasRect.left, 0, canvasRect.width);
            const currentX = clampNumber(Number(currentClientX) - canvasRect.left, 0, canvasRect.width);
            const left = Math.min(startX, currentX) + canvasRect.left - shellRect.left;
            const width = Math.max(1, Math.abs(currentX - startX));
            overlay.style.left = `${left}px`;
            overlay.style.top = `${canvasRect.top - shellRect.top}px`;
            overlay.style.width = `${width}px`;
            overlay.style.height = `${canvasRect.height}px`;
            overlay.classList.add('show');
        }

        function resetChartSelectionSession() {
            chartSelectionSession = null;
            hideChartSelectionOverlay();
        }

        function cancelChartSelectionMode({ render = true } = {}) {
            chartSelectionMode = false;
            resetChartSelectionSession();
            if (render) renderChartToolbar(getChartDisplayPayload());
        }

        function setChartSelectionMode(enabled) {
            chartSelectionMode = Boolean(enabled) && !isCompactViewport();
            resetChartSelectionSession();
            renderChartToolbar(getChartDisplayPayload());
        }

        function toggleChartSelectionMode() {
            setChartSelectionMode(!chartSelectionMode);
        }

        function getSelectionIndexFromClientX(clientX) {
            const canvas = document.getElementById('chartCanvas');
            const payload = getChartDisplayPayload();
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !canvas || !bars.length) return -1;
            const rect = canvas.getBoundingClientRect();
            const x = clampNumber(Number(clientX) - rect.left, 0, rect.width);
            const point = [x, Math.max(0, Math.min(rect.height, rect.height * 0.35))];
            const resolveIndex = (finder) => {
                try {
                    const converted = chartInstance.convertFromPixel(finder, point);
                    const rawIndex = Array.isArray(converted) ? converted[0] : converted;
                    const index = Math.round(Number(rawIndex));
                    return Number.isInteger(index) ? clampIndex(index, bars.length) : -1;
                } catch (_) {
                    return -1;
                }
            };
            const finders = [
                { xAxisIndex: 0, yAxisIndex: 0 },
                { gridIndex: 0 },
                { seriesIndex: 0 },
                { xAxisIndex: 0 },
            ];
            for (const finder of finders) {
                const index = resolveIndex(finder);
                if (index >= 0) return index;
            }
            return -1;
        }

        function applyChartTimeSelection(startClientX, endClientX) {
            const payload = getChartDisplayPayload();
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length) return false;
            const startIndex = getSelectionIndexFromClientX(startClientX);
            const endIndex = getSelectionIndexFromClientX(endClientX);
            if (startIndex < 0 || endIndex < 0) return false;
            const leftIndex = Math.min(startIndex, endIndex);
            const rightIndex = Math.max(startIndex, endIndex);
            if (rightIndex - leftIndex < 1) {
                showToast('圈选范围太窄');
                return false;
            }
            clearTraceManualPage();
            chartPointerLocked = false;
            selectedSignalId = '';
            setChartZoomWindow({
                start: indexToPercent(leftIndex, bars.length),
                end: indexToPercent(rightIndex, bars.length),
            }, payload);
            focusBarIndex(rightIndex, '');
            hoverBarIndex = rightIndex;
            syncChartTooltip(rightIndex);
            renderCursorStrip(payload);
            return true;
        }

        function shouldStartChartSelection(event) {
            if (!event || isCompactViewport()) return false;
            if (event.pointerType === 'touch') return false;
            if (event.button !== 0) return false;
            if (!chartSelectionMode && !event.shiftKey) return false;
            const payload = getChartDisplayPayload();
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length) return false;
            const canvas = document.getElementById('chartCanvas');
            if (!canvas || !canvas.contains(event.target)) return false;
            const rect = canvas.getBoundingClientRect();
            const x = Number(event.clientX) - rect.left;
            const y = Number(event.clientY) - rect.top;
            return x >= 0 && y >= 0 && x <= rect.width && y <= rect.height;
        }

        function handleChartSelectionPointerDown(event) {
            if (!shouldStartChartSelection(event)) return;
            const canvas = document.getElementById('chartCanvas');
            chartSelectionSession = {
                pointerId: event.pointerId,
                startX: Number(event.clientX),
                currentX: Number(event.clientX),
                moved: false,
            };
            suppressNextChartClick = true;
            hideChartTooltip();
            renderChartSelectionOverlay(chartSelectionSession.startX, chartSelectionSession.currentX);
            try {
                canvas?.setPointerCapture?.(event.pointerId);
            } catch (_) {}
            event.preventDefault();
            event.stopPropagation();
        }

        function handleChartSelectionPointerMove(event) {
            if (!chartSelectionSession || event.pointerId !== chartSelectionSession.pointerId) return;
            chartSelectionSession.currentX = Number(event.clientX);
            if (Math.abs(chartSelectionSession.currentX - chartSelectionSession.startX) >= 8) {
                chartSelectionSession.moved = true;
            }
            renderChartSelectionOverlay(chartSelectionSession.startX, chartSelectionSession.currentX);
            event.preventDefault();
            event.stopPropagation();
        }

        function handleChartSelectionPointerUp(event) {
            if (!chartSelectionSession || event.pointerId !== chartSelectionSession.pointerId) return;
            const session = chartSelectionSession;
            const endX = Number(event.clientX);
            const shouldApply = session.moved && Math.abs(endX - session.startX) >= 12;
            resetChartSelectionSession();
            if (chartSelectionMode) chartSelectionMode = false;
            if (shouldApply) {
                applyChartTimeSelection(session.startX, endX);
            }
            window.setTimeout(() => {
                suppressNextChartClick = false;
            }, 80);
            renderChartToolbar(getChartDisplayPayload());
            event.preventDefault();
            event.stopPropagation();
        }

        function handleChartSelectionPointerCancel(event) {
            if (chartSelectionSession && event?.pointerId === chartSelectionSession.pointerId) {
                resetChartSelectionSession();
                if (chartSelectionMode) {
                    chartSelectionMode = false;
                    renderChartToolbar(getChartDisplayPayload());
                }
            }
        }

        function registerChartSelectionInteractions() {
            const canvas = document.getElementById('chartCanvas');
            if (!canvas) return;
            canvas.removeEventListener('pointerdown', handleChartSelectionPointerDown, true);
            canvas.removeEventListener('pointermove', handleChartSelectionPointerMove, true);
            canvas.removeEventListener('pointerup', handleChartSelectionPointerUp, true);
            canvas.removeEventListener('pointercancel', handleChartSelectionPointerCancel, true);
            canvas.addEventListener('pointerdown', handleChartSelectionPointerDown, true);
            canvas.addEventListener('pointermove', handleChartSelectionPointerMove, true);
            canvas.addEventListener('pointerup', handleChartSelectionPointerUp, true);
            canvas.addEventListener('pointercancel', handleChartSelectionPointerCancel, true);
        }

        function lockChartPointerAtIndex(index, signalId = '') {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            clearTraceManualPage();
            const bars = payload.bars;
            let safeIndex = clampIndex(index, bars.length);
            let safeSignalId = String(signalId || '');
            if (!safeSignalId) {
                const snapped = findNearestSignalAnchor(lastPayload, safeIndex, isCompactViewport() ? 1 : 0);
                if (snapped) {
                    safeIndex = snapped.index;
                    safeSignalId = snapped.signalId;
                }
            }
            dismissMobileGestureHint();
            chartPointerLocked = true;
            hoverBarIndex = safeIndex;
            focusBarIndex(safeIndex, safeSignalId);
            ensureBarVisible(safeIndex, payload);
            renderCursorStrip(payload);
            syncChartTooltip(safeIndex);
        }

        function navigateTouchSwipe(deltaX) {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            const magnitude = Math.abs(Number(deltaX) || 0);
            if (magnitude < 40) return;
            const step = magnitude >= 120 ? 5 : magnitude >= 70 ? 3 : 1;
            dismissMobileGestureHint();
            const directionStep = deltaX < 0 ? step : -step;
            if (chartPointerLocked) {
                const activeIndex = getEffectiveCursorIndex(payload);
                lockChartPointerAtIndex(activeIndex + directionStep);
                return;
            }
            focusRelativeBar(directionStep);
        }

        function renderSignalDrawer(signal, context = null) {
            const drawer = document.getElementById('signalDetailDrawer');
            const content = document.getElementById('signalDetailContent');
            if (!drawer || !content) return;
            if (!signal) {
                drawer.classList.remove('show');
                drawer.setAttribute('aria-hidden', 'true');
                content.innerHTML = '';
                activeDrawerSignalId = '';
                renderMobileGestureHint(getChartDisplayPayload());
                return;
            }
            const ctx = context || buildContext(getChartDisplayPayload(), selectedBarIndex >= 0 ? selectedBarIndex : 0, String(signal.signal_id || signal.id || ''));
            const bar = ctx?.bar || null;
            const indicator = ctx?.indicator || null;
            const signalDate = deriveItemDate(signal);
            const signalUrl = buildPageUrl('/ibkr_signals.html', { search: currentSymbol, date: signalDate }, { environment: currentEnvironment });
            const orderUrl = buildPageUrl('/orders.html', { search: currentSymbol, date: signalDate }, { environment: currentEnvironment });
            const orderDetailsUrl = buildOrderDetailsUrl(signal, signalDate);
            const accountUrl = buildPageUrl('/ibkr_account.html', {}, { environment: currentEnvironment });
            const extra = getSignalExtra(signal);
            const reason = signal.reason || signal.note || extra.reason || '暂无原因说明';
            const computedAt = extra.computed_at_us || extra.computed_at_cn || signal.updated || signal.created || '--';
            const statusText = isComputedSignal(signal) ? 'COMPUTED' : String(signal.status || '--').toUpperCase();
            const sourceNote = isComputedSignal(signal)
                ? '当前信号点由 ibkr_bars 实时重算，未必对应历史信号台账。'
                : '当前信号来自历史台账记录。';

            content.innerHTML = `
                <div class="drawer-head">
                    <div>
                        <div class="drawer-kicker">Signal Detail</div>
                        <div class="drawer-title">${escapeHtml(signal.symbol || currentSymbol || '--')} · ${escapeHtml(buildTradeSignalLabel(signal))}</div>
                        <div class="drawer-sub">${escapeHtml(signal.signal_id || signal.id || '--')}<br>${escapeHtml(formatSignalTime(signal))} · ${escapeHtml(getIntervalLabel(currentInterval))}</div>
                    </div>
                    <button class="drawer-close" type="button" onclick="closeSignalDrawer()">×</button>
                </div>

                <div class="drawer-grid">
                    <div class="drawer-metric">
                        <div class="drawer-label">Direction / Status</div>
                        <div class="drawer-value"><span class="signal-badge ${getSignalBadgeClass(signal)}">${escapeHtml(String(signal.direction || '--').toUpperCase())}</span><br>${escapeHtml(statusText)}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">Entry / RR</div>
                        <div class="drawer-value">${escapeHtml(formatPrice(signal.entry || signal.limit_price))}<br>${escapeHtml(String(signal.rr || '--'))}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">TP / SL</div>
                        <div class="drawer-value">${escapeHtml(formatPrice(signal.take_profit))}<br>${escapeHtml(formatPrice(signal.stop_loss))}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">Shares / Industry</div>
                        <div class="drawer-value">${escapeHtml(String(signal.shares || '--'))}<br>${escapeHtml(String(extra.industry || '--'))}</div>
                    </div>
                </div>

                <div class="drawer-block">
                    <div class="drawer-block-title">Reason</div>
                    <div class="drawer-copy">${escapeHtml(reason)}</div>
                </div>

                <div class="drawer-block">
                    <div class="drawer-block-title">Source</div>
                    <div class="drawer-copy">${escapeHtml(sourceNote)}</div>
                </div>

                <div class="drawer-grid">
                    <div class="drawer-metric">
                        <div class="drawer-label">Bar Close / Volume</div>
                        <div class="drawer-value">${bar ? `${escapeHtml(formatPrice(bar.close))}<br>${escapeHtml(formatNumber(bar.volume || 0, 0))}` : '--'}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">CRSI / OBV RSI</div>
                        <div class="drawer-value">${indicator ? `${escapeHtml(formatNumber(indicator.crsi))}<br>${escapeHtml(formatNumber(indicator.obv_rsi))}` : '--'}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">VWAP Dist / ATR %</div>
                        <div class="drawer-value">${indicator ? `${escapeHtml(formatPercent(indicator.vwap_dist))}<br>${escapeHtml(formatPercent(indicator.atr_pct))}` : '--'}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">Computed</div>
                        <div class="drawer-value">${escapeHtml(String(computedAt))}</div>
                    </div>
                </div>

                <div class="drawer-actions">
                    <a class="mini-link" href="${signalUrl}">历史信号页</a>
                    <a class="mini-link" href="${orderUrl}">历史订单页</a>
                    ${orderDetailsUrl ? `<a class="mini-link" href="${orderDetailsUrl}">打开订单明细</a>` : ''}
                    <a class="mini-link" href="${accountUrl}">打开账户页</a>
                </div>
            `;
            drawer.classList.add('show');
            drawer.setAttribute('aria-hidden', 'false');
            activeDrawerSignalId = String(signal.signal_id || signal.id || '');
            renderMobileGestureHint(getChartDisplayPayload());
        }

        function renderComponentDrawer(context = null) {
            const drawer = document.getElementById('signalDetailDrawer');
            const content = document.getElementById('signalDetailContent');
            if (!drawer || !content || !context?.bar) return;
            const bar = context.bar;
            const indicator = context.indicator || {};
            const trace = context.trace || {};
            const componentTokens = getTraceFlowTokens(trace, 8);
            const eventTokens = Array.isArray(trace?.event_chain) ? trace.event_chain : [];
            const filters = Array.isArray(trace?.filters) && trace.filters.length ? trace.filters : ['未触发过滤'];
            const dtpState = getTraceDtpState(trace);
            const dtpText = dtpState.phase || dtpState.dir ? formatDtpStateLabel(dtpState) : '--';
            const windowFlags = trace?.window_flags && typeof trace.window_flags === 'object' ? trace.window_flags : {};
            const windowText = [
                windowFlags.sd_upper_valid || windowFlags.sd_upper_active ? '上轨窗口' : '',
                windowFlags.sd_lower_valid || windowFlags.sd_lower_active ? '下轨窗口' : '',
            ].filter(Boolean).join(' / ') || '无窗口';
            const signalStage = getTraceSignalStageText(trace);
            const signalState = getTraceSignalState(trace);
            const traceReason = getTraceFilterReason(trace) || signalState.reason || signalState.filter_reason || 'bars 实时推演';
            const ohlc = formatInlineOHLC(bar);
            content.innerHTML = `
                <div class="drawer-head">
                    <div>
                        <div class="drawer-kicker">Component Detail</div>
                        <div class="drawer-title">${escapeHtml(currentSymbol || bar.symbol || '--')} · 组件收集</div>
                        <div class="drawer-sub">${escapeHtml(String(bar.us_time || trace.us_time || '--'))}<br>${escapeHtml(getIntervalLabel(currentInterval))} · bar #${escapeHtml(String(trace.bar_index || bar.bar_index || context.index + 1 || '--'))}</div>
                    </div>
                    <button class="drawer-close" type="button" onclick="closeSignalDrawer()">×</button>
                </div>

                <div class="drawer-grid">
                    <div class="drawer-metric">
                        <div class="drawer-label">OHLC</div>
                        <div class="drawer-value">${escapeHtml(ohlc.primary)}<br>${escapeHtml(ohlc.secondary)}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">Volume / ATR</div>
                        <div class="drawer-value">${escapeHtml(formatNumber(bar.volume || 0, 0))}<br>${escapeHtml(formatPercent(indicator?.atr_pct))}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">DTP / Signal</div>
                        <div class="drawer-value"><span style="color:${escapeHtml(getDtpStateColor(dtpState))};">${escapeHtml(dtpText)}</span><br>${escapeHtml(signalStage)}</div>
                    </div>
                    <div class="drawer-metric">
                        <div class="drawer-label">SD Window</div>
                        <div class="drawer-value">${escapeHtml(windowText)}<br>${escapeHtml(`SD ${getSdZoneText(trace?.position?.sd_zone ?? indicator?.sd_zone)} · ${getSdTrendText(trace?.position?.sd_trend ?? indicator?.sd_trend)}`)}</div>
                    </div>
                </div>

                <div class="drawer-block">
                    <div class="drawer-block-title">Components</div>
                    <div class="drawer-token-row">${componentTokens.length ? componentTokens.map((text) => buildTraceToken(text, 'warning')).join('') : buildTraceToken('无组件')}</div>
                </div>

                <div class="drawer-block">
                    <div class="drawer-block-title">Events</div>
                    <div class="drawer-copy">${escapeHtml(eventTokens.length ? eventTokens.join(' · ') : '无新增事件')}</div>
                </div>

                <div class="drawer-block">
                    <div class="drawer-block-title">Filters / Reason</div>
                    <div class="drawer-token-row">${filters.map((text) => buildTraceToken(text, filters[0] === '未触发过滤' ? '' : 'negative')).join('')}</div>
                    <div class="drawer-copy">${escapeHtml(traceReason)}</div>
                </div>
            `;
            drawer.classList.add('show');
            drawer.setAttribute('aria-hidden', 'false');
            activeDrawerSignalId = `component:${Number(bar?.bar_time_ms || trace?.bar_time_ms || 0) || context.index}`;
            renderMobileGestureHint(getChartDisplayPayload());
        }

        function closeSignalDrawer() {
            renderSignalDrawer(null);
        }

        function openSignalDrawer(signal, context = null) {
            renderSignalDrawer(signal, context);
        }

        function openComponentDrawer(context = null) {
            renderComponentDrawer(context);
        }

        function openFocusedSignalDrawer() {
            const context = getFocusContext(getChartDisplayPayload());
            if (!context?.activeSignal) {
                showToast('当前焦点没有信号');
                return;
            }
            openSignalDrawer(context.activeSignal, context);
        }

        function focusBarIndex(index, signalId = '') {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            const safeIndex = Math.min(Math.max(Number(index) || 0, 0), payload.bars.length - 1);
            selectedBarIndex = safeIndex;
            selectedSignalId = signalId ? decodeURIComponent(signalId) : '';
            renderInfoRail(payload);
            renderCursorStrip(payload);
            const context = buildContext(payload, safeIndex, selectedSignalId);
            if (document.getElementById('signalDetailDrawer')?.classList.contains('show')) {
                if (context?.activeSignal) openSignalDrawer(context.activeSignal, context);
                else if (String(selectedSignalId || '').startsWith('component:')) openComponentDrawer(context);
                else closeSignalDrawer();
            }
            syncChartTooltip(safeIndex);
            scheduleChartFocusMarkerSync(safeIndex, payload);
        }

        function setChartZoomWindow(zoom, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length) return;
            const viewport = applyChartViewportState(zoom, bars.length);
            chartInstance.dispatchAction({
                type: 'dataZoom',
                start: viewport.start,
                end: viewport.end,
            });
            renderChartBottomBar(payload);
        }

        function resetChartZoomState() {
            chartZoomState = null;
            chartViewportState = { start: 0, end: 100, startIndex: 0, endIndex: 0, visibleBars: 0, totalBars: 0 };
        }

        function zoomChartAroundIndex(anchorIndex, factor, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length) return false;
            const viewport = chartViewportState.totalBars === bars.length
                ? chartViewportState
                : getCurrentZoomWindow(bars.length);
            const currentVisible = Math.max(8, viewport.visibleBars || Math.min(120, bars.length));
            const nextVisible = Math.max(12, Math.min(bars.length, Math.round(currentVisible * factor)));
            if (nextVisible === currentVisible) return false;
            const safeAnchor = clampIndex(anchorIndex >= 0 ? anchorIndex : getEffectiveCursorIndex(payload), bars.length);
            const anchorRatio = currentVisible > 1
                ? clampNumber((safeAnchor - viewport.startIndex) / Math.max(1, currentVisible - 1), 0, 1)
                : 0.5;
            let startIndex = Math.round(safeAnchor - ((nextVisible - 1) * anchorRatio));
            startIndex = Math.max(0, Math.min(startIndex, Math.max(0, bars.length - nextVisible)));
            const endIndex = Math.min(bars.length - 1, startIndex + nextVisible - 1);
            setChartZoomWindow({
                start: indexToPercent(startIndex, bars.length),
                end: indexToPercent(endIndex, bars.length),
            }, payload);
            syncChartTooltip(safeAnchor);
            return true;
        }

        function panChartByBars(deltaBars, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length || !Number.isFinite(Number(deltaBars))) return false;
            const viewport = chartViewportState.totalBars === bars.length
                ? chartViewportState
                : getCurrentZoomWindow(bars.length);
            const visibleBars = Math.max(1, viewport.visibleBars || bars.length);
            if (visibleBars >= bars.length) return false;
            const step = Math.trunc(Number(deltaBars));
            if (!step) return false;
            const maxStart = Math.max(0, bars.length - visibleBars);
            const startIndex = Math.max(0, Math.min(maxStart, viewport.startIndex + step));
            if (startIndex === viewport.startIndex) return false;
            const endIndex = Math.min(bars.length - 1, startIndex + visibleBars - 1);
            setChartZoomWindow({
                start: indexToPercent(startIndex, bars.length),
                end: indexToPercent(endIndex, bars.length),
            }, payload);
            return true;
        }

        function normalizeWheelDelta(delta, event) {
            const mode = Number(event?.deltaMode || 0);
            const multiplier = mode === 1 ? 16 : mode === 2 ? 240 : 1;
            return Number(delta || 0) * multiplier;
        }

        function handleChartWheelGesture(event) {
            const payload = getChartDisplayPayload();
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length || !event) return;
            const isPinchZoom = Boolean(event.ctrlKey || event.metaKey);
            if (isPinchZoom) {
                event.preventDefault();
                event.stopPropagation();
                const delta = clampNumber(normalizeWheelDelta(event.deltaY || event.deltaX, event), -600, 600);
                const factor = Math.exp(delta * 0.0024);
                const pointerIndex = getBarIndexFromClientPoint(event.clientX, event.clientY);
                zoomChartAroundIndex(pointerIndex, factor, payload);
                return;
            }

            const rawHorizontal = event.shiftKey && Math.abs(Number(event.deltaX || 0)) < 1
                ? event.deltaY
                : event.deltaX;
            const horizontalDelta = normalizeWheelDelta(rawHorizontal, event);
            if (Math.abs(horizontalDelta) < 1) return;

            const canvas = document.getElementById('chartCanvas');
            const width = Math.max(320, Number(canvas?.getBoundingClientRect?.().width || 0));
            const viewport = chartViewportState.totalBars === bars.length
                ? chartViewportState
                : getCurrentZoomWindow(bars.length);
            const visibleBars = Math.max(8, viewport.visibleBars || Math.min(120, bars.length));
            const deltaBars = Math.trunc((horizontalDelta / width) * visibleBars * 1.35)
                || (horizontalDelta > 0 ? 1 : -1);
            const moved = panChartByBars(deltaBars, payload);
            if (moved || Math.abs(Number(event.deltaX || 0)) >= Math.abs(Number(event.deltaY || 0))) {
                event.preventDefault();
                event.stopPropagation();
            }
        }

        function ensureBarVisible(index, payload = getChartDisplayPayload(), { center = false, paddingBars = 10 } = {}) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length) return;
            const targetIndex = clampIndex(index, bars.length);
            const viewport = chartViewportState.totalBars === bars.length
                ? chartViewportState
                : getCurrentZoomWindow(bars.length);
            const visibleBars = Math.max(8, viewport.visibleBars || Math.min(96, bars.length));
            const minVisible = viewport.startIndex + Math.min(paddingBars, Math.max(2, Math.floor(visibleBars / 4)));
            const maxVisible = viewport.endIndex - Math.min(paddingBars, Math.max(2, Math.floor(visibleBars / 4)));
            if (!center && targetIndex >= minVisible && targetIndex <= maxVisible) return;
            let startIndex = targetIndex - Math.floor(visibleBars / 2);
            startIndex = Math.max(0, Math.min(startIndex, Math.max(0, bars.length - visibleBars)));
            const endIndex = Math.min(bars.length - 1, startIndex + visibleBars - 1);
            setChartZoomWindow({
                start: indexToPercent(startIndex, bars.length),
                end: indexToPercent(endIndex, bars.length),
            }, payload);
        }

        function shouldKeepViewportFollowingFocus(index, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) return false;
            if (chartPointerLocked) return true;
            const targetIndex = clampIndex(index, bars.length);
            const viewport = chartViewportState.totalBars === bars.length
                ? chartViewportState
                : getCurrentZoomWindow(bars.length);
            const tailSlack = Math.max(1, Math.min(3, Math.floor(Math.max(1, viewport.visibleBars || 1) * 0.04)));
            const focusIsLatest = targetIndex >= bars.length - 1 - tailSlack;
            const viewportNearLatest = viewport.endIndex >= bars.length - 1 - tailSlack;
            return focusIsLatest && viewportNearLatest;
        }

        function zoomChartAroundFocus(factor, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!chartInstance || !bars.length) return;
            const anchorIndex = getEffectiveCursorIndex(payload);
            zoomChartAroundIndex(anchorIndex, factor, payload);
        }

        function focusRelativeBar(delta) {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            clearTraceManualPage();
            const bars = payload.bars;
            const activeIndex = getEffectiveCursorIndex(payload);
            const targetIndex = clampIndex(activeIndex + Number(delta || 0), bars.length);
            focusBarIndex(targetIndex);
            hoverBarIndex = targetIndex;
            ensureBarVisible(targetIndex, payload);
            renderCursorStrip(payload);
        }

        function focusSignalByStep(step) {
            clearTraceManualPage();
            if (!lastPayload || !Array.isArray(lastPayload.signals) || !lastPayload.signals.length) {
                showToast('当前窗口暂无信号');
                return;
            }
            const signalIndices = getSignalBarIndices(lastPayload);
            if (!signalIndices.length) {
                showToast('当前窗口暂无可定位信号');
                return;
            }
            const displayPayload = getChartDisplayPayload();
            const currentIndex = getEffectiveCursorIndex(displayPayload || lastPayload);
            let targetIndex = -1;
            if (step > 0) {
                targetIndex = signalIndices.find((index) => index > currentIndex);
                if (!Number.isInteger(targetIndex)) targetIndex = signalIndices[0];
            } else {
                targetIndex = signalIndices.slice().reverse().find((index) => index < currentIndex);
                if (!Number.isInteger(targetIndex)) targetIndex = signalIndices[signalIndices.length - 1];
            }
            focusBarIndex(targetIndex);
            hoverBarIndex = targetIndex;
            ensureBarVisible(targetIndex, displayPayload || lastPayload, { center: true });
            const context = buildContext(displayPayload || lastPayload, targetIndex, selectedSignalId);
            if (context?.activeSignal) openSignalDrawer(context.activeSignal, context);
            renderCursorStrip(displayPayload || lastPayload);
        }

        function focusLatestBarAction() {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            clearTraceManualPage();
            const latestIndex = payload.bars.length - 1;
            focusBarIndex(latestIndex);
            hoverBarIndex = latestIndex;
            ensureBarVisible(latestIndex, payload, { center: true });
            renderCursorStrip(payload);
        }

        function resetChartViewAction() {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            setChartZoomWindow(getInitialZoom(currentRangeKey, payload.bars.length), payload);
            const focusIndex = getEffectiveCursorIndex(payload);
            syncChartTooltip(focusIndex >= 0 ? focusIndex : payload.bars.length - 1);
        }

        function renderInfoRail(payload) {
            const signals = Array.isArray(payload?.signals) ? payload.signals.slice().sort((a, b) => Number(b.bar_time_ms || 0) - Number(a.bar_time_ms || 0)) : [];
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const longCount = signals.filter((item) => String(item.direction || '').toLowerCase() === 'long').length;
            const shortCount = signals.filter((item) => String(item.direction || '').toLowerCase() === 'short').length;
            const latestBar = bars.length ? bars[bars.length - 1] : null;
            const timeRange = bars.length ? `${bars[0]?.us_time || '--'} -> ${latestBar?.us_time || '--'}` : '--';
            const latestSignal = signals[0] || null;
            const focus = getFocusContext(payload);
            const focusBar = focus?.bar || latestBar || null;
            const focusIndicator = focus?.indicator || null;
            const decisionSignal = getDecisionSignalContext(focus);
            const focusSignal = decisionSignal.signal || null;
            const focusTraceSignal = decisionSignal.traceSignal || null;
            const focusSignalMatches = focus?.signalMatches || [];
            const activeSignalKey = String(focusSignal?.signal_id || focusSignal?.id || focusTraceSignal?.signal_id || focusTraceSignal?.id || '');
            const focusDate = deriveItemDate(focusSignal || focusBar || latestBar);
            const signalUrl = buildPageUrl('/ibkr_signals.html', { search: currentSymbol, date: focusDate }, { environment: currentEnvironment });
            const ordersUrl = buildPageUrl('/orders.html', { search: currentSymbol, date: focusDate }, { environment: currentEnvironment });
            const orderDetailsUrl = buildOrderDetailsUrl(focusSignal, focusDate);
            const accountUrl = buildPageUrl('/ibkr_account.html', {}, { environment: currentEnvironment });
            const compareRow = getCompareRowByBarTime(focusBar?.bar_time_ms);
            const focusTraceLabel = formatTraceDecisionLabel(focus?.trace, focusSignal || focusTraceSignal);

            if (comparePayload) {
                const compareSummary = comparePayload?.comparison?.summary || {};
                const mismatchExamples = Array.isArray(comparePayload?.comparison?.mismatch_examples)
                    ? comparePayload.comparison.mismatch_examples
                    : [];
                const compareListHtml = mismatchExamples.length
                    ? mismatchExamples.map((item) => {
                        const barTimeMs = Number(item?.bar_time_ms || 0);
                        const focusable = findBarIndexByTime(bars, barTimeMs) >= 0;
                        return `
                            <div class="compare-item ${focusable ? 'interactive' : 'disabled'}" ${focusable ? `onclick="focusCompareBar('${barTimeMs}')"` : ''}>
                                <div class="compare-side">
                                    <div class="compare-title">${escapeHtml(item?.us_time || formatBarTimeMsToET(barTimeMs) || '--')}</div>
                                    <div class="compare-meta">Bars ${escapeHtml(getCompareStatusLabel(item?.status?.bar))} · Ind ${escapeHtml(getCompareStatusLabel(item?.status?.indicator))} · Sig ${escapeHtml(getCompareStatusLabel(item?.status?.signal))}</div>
                                    <div class="compare-meta">bar ${escapeHtml((item?.bar_fields || []).join(', ') || '--')} · ind ${escapeHtml((item?.indicator_fields || []).join(', ') || '--')} · sig ${escapeHtml((item?.signal_fields || []).join(', ') || '--')}</div>
                                </div>
                                <span class="compare-pill ${getCompareStatusClass(item?.status?.bar)}">${focusable ? '聚焦' : '仅提示'}</span>
                            </div>
                        `;
                    }).join('')
                    : '<div class="rail-sub">当前窗口无差异。</div>';

                document.getElementById('infoRail').innerHTML = `
                    ${buildCompareErrorCard()}
                    <div class="rail-card">
                        <div class="rail-kicker">Focus</div>
                        <div class="rail-value">${focusBar ? formatPrice(focusBar.close) : '--'}</div>
                        <div class="rail-sub">${escapeHtml(currentSymbol || '--')} · ${escapeHtml(getIntervalLabel(currentInterval))}<br>${escapeHtml(focusBar?.us_time || '--')}</div>
                        <div class="metric-grid">
                            <div class="metric-item"><div class="metric-label">Open / High</div><div class="metric-value">${focusBar ? `${escapeHtml(formatPrice(focusBar.open))} / ${escapeHtml(formatPrice(focusBar.high))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">Low / Close</div><div class="metric-value">${focusBar ? `${escapeHtml(formatPrice(focusBar.low))} / ${escapeHtml(formatPrice(focusBar.close))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">Volume</div><div class="metric-value">${focusBar ? escapeHtml(formatNumber(focusBar.volume || 0, 0)) : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">Signal</div><div class="metric-value">${escapeHtml(focusTraceLabel || (focusSignalMatches.length ? `${focusSignalMatches.length} hits` : '--'))}</div></div>
                        </div>
                        <div class="focus-actions">
                            ${focusSignal ? `<button class="mini-link mini-link-btn" type="button" onclick="openFocusedSignalDrawer()">信号详情</button>` : ''}
                            <a class="mini-link" href="${signalUrl}">历史信号页</a>
                            <a class="mini-link" href="${ordersUrl}">历史订单页</a>
                            ${orderDetailsUrl ? `<a class="mini-link" href="${orderDetailsUrl}">订单明细</a>` : ''}
                            <a class="mini-link" href="${accountUrl}">账户页</a>
                        </div>
                    </div>
                    <div class="rail-card">
                        <div class="rail-kicker">Stored Bars Chain</div>
                        <div class="rail-value">${compareRow?.stored?.bar ? formatPrice(compareRow.stored.bar.close) : '--'}</div>
                        <div class="rail-sub">${escapeHtml(currentSymbol || '--')} · Stored bars<br>${escapeHtml(compareRow?.stored?.bar?.us_time || focusBar?.us_time || '--')}</div>
                        <div class="metric-grid">
                            <div class="metric-item"><div class="metric-label">OHLC</div><div class="metric-value">${compareRow?.stored?.bar ? `${escapeHtml(formatPrice(compareRow.stored.bar.open))} / ${escapeHtml(formatPrice(compareRow.stored.bar.high))}<br>${escapeHtml(formatPrice(compareRow.stored.bar.low))} / ${escapeHtml(formatPrice(compareRow.stored.bar.close))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">EMA / VWAP</div><div class="metric-value">${compareRow?.stored?.indicator ? `${escapeHtml(formatPrice(compareRow.stored.indicator.ema_fast))} / ${escapeHtml(formatPrice(compareRow.stored.indicator.ema_slow))}<br>${escapeHtml(formatPrice(compareRow.stored.indicator.vwap))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">CRSI / OBV</div><div class="metric-value">${compareRow?.stored?.indicator ? `${escapeHtml(formatNumber(compareRow.stored.indicator.crsi))} / ${escapeHtml(formatNumber(compareRow.stored.indicator.obv_rsi))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">Signal</div><div class="metric-value">${compareRow?.stored?.signal ? `${escapeHtml(buildTradeSignalLabel(compareRow.stored.signal))}<br>${escapeHtml(String(compareRow.stored.signal.direction || '--').toUpperCase())}` : '无信号'}</div></div>
                        </div>
                    </div>
                    <div class="rail-card">
                        <div class="rail-kicker">IBKR API Chain</div>
                        <div class="rail-value">${compareRow?.ibkr?.bar ? formatPrice(compareRow.ibkr.bar.close) : '--'}</div>
                        <div class="rail-sub">${escapeHtml(currentSymbol || '--')} · IBKR API bars<br>${escapeHtml(compareRow?.ibkr?.bar?.us_time || focusBar?.us_time || '--')}</div>
                        <div class="metric-grid">
                            <div class="metric-item"><div class="metric-label">OHLC</div><div class="metric-value">${compareRow?.ibkr?.bar ? `${escapeHtml(formatPrice(compareRow.ibkr.bar.open))} / ${escapeHtml(formatPrice(compareRow.ibkr.bar.high))}<br>${escapeHtml(formatPrice(compareRow.ibkr.bar.low))} / ${escapeHtml(formatPrice(compareRow.ibkr.bar.close))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">EMA / VWAP</div><div class="metric-value">${compareRow?.ibkr?.indicator ? `${escapeHtml(formatPrice(compareRow.ibkr.indicator.ema_fast))} / ${escapeHtml(formatPrice(compareRow.ibkr.indicator.ema_slow))}<br>${escapeHtml(formatPrice(compareRow.ibkr.indicator.vwap))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">CRSI / OBV</div><div class="metric-value">${compareRow?.ibkr?.indicator ? `${escapeHtml(formatNumber(compareRow.ibkr.indicator.crsi))} / ${escapeHtml(formatNumber(compareRow.ibkr.indicator.obv_rsi))}` : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">Signal</div><div class="metric-value">${compareRow?.ibkr?.signal ? `${escapeHtml(buildTradeSignalLabel(compareRow.ibkr.signal))}<br>${escapeHtml(String(compareRow.ibkr.signal.direction || '--').toUpperCase())}` : '无信号'}</div></div>
                        </div>
                    </div>
                    <div class="rail-card">
                        <div class="rail-kicker">Comparison</div>
                        <div class="metric-grid">
                            <div class="metric-item"><div class="metric-label">Matched Bars</div><div class="metric-value">${escapeHtml(String(compareSummary.matched_bar_count || 0))}</div></div>
                            <div class="metric-item"><div class="metric-label">Missing Bars</div><div class="metric-value">${escapeHtml(String((compareSummary.missing_stored_bar_count || 0) + (compareSummary.missing_ibkr_bar_count || 0)))}</div></div>
                            <div class="metric-item"><div class="metric-label">Bar Diff</div><div class="metric-value">${escapeHtml(String(compareSummary.bar_mismatch_count || 0))}</div></div>
                            <div class="metric-item"><div class="metric-label">Signal Diff</div><div class="metric-value">${escapeHtml(String(compareSummary.signal_mismatch_count || 0))}</div></div>
                        </div>
                        <div class="compare-status-row">${buildCompareStatusPills(compareRow?.status)}</div>
                        <div class="compare-copy">${buildCompareDiffCopy(compareRow)}</div>
                        <div class="compare-list">${compareListHtml}</div>
                    </div>
                    <div class="rail-card">
                        <div class="rail-kicker">Signal Flow</div>
                        <div class="metric-grid">
                            <div class="metric-item"><div class="metric-label">多头信号</div><div class="metric-value">${longCount}</div></div>
                            <div class="metric-item"><div class="metric-label">空头信号</div><div class="metric-value">${shortCount}</div></div>
                            <div class="metric-item"><div class="metric-label">最新信号</div><div class="metric-value">${latestSignal ? escapeHtml(buildTradeSignalLabel(latestSignal)) : '--'}</div></div>
                            <div class="metric-item"><div class="metric-label">信号时间</div><div class="metric-value">${latestSignal ? escapeHtml(formatSignalTime(latestSignal).slice(5)) : '--'}</div></div>
                        </div>
                        <div class="rail-sub">IBKR 对比不写库。</div>
                    </div>
                    <div class="rail-card">
                        <div class="rail-kicker">Recent Signals</div>
                        <div class="signal-list">
                            ${signals.slice(0, 12).map((signal) => {
                                const signalMs = Number(signal.bar_time_ms || 0) || 0;
                                const encodedSignalKey = encodeURIComponent(String(signal.signal_id || signal.id || ''));
                                const isActive = activeSignalKey && activeSignalKey === String(signal.signal_id || signal.id || '');
                                return `
                                    <div class="signal-item interactive ${isActive ? 'active' : ''}" onclick="focusSignalBar('${signalMs}', '${encodedSignalKey}')">
                                        <div class="signal-side">
                                            <div class="signal-title">${escapeHtml(buildTradeSignalLabel(signal))}</div>
                                            <div class="signal-meta">${escapeHtml(formatSignalTime(signal))}</div>
                                        </div>
                                        <span class="signal-badge ${escapeHtml(getSignalBadgeClass(signal))}">${escapeHtml(String(signal.direction || '--').toUpperCase())}</span>
                                    </div>
                                `;
                            }).join('') || '<div class="rail-sub">暂无重算信号。</div>'}
                        </div>
                    </div>
                `;
                renderRailNav();
                renderInspectorDrawer(payload);
                return;
            }

            document.getElementById('infoRail').innerHTML = `
                ${buildCompareErrorCard()}
                <div class="rail-card">
                    <div class="rail-kicker">Focus</div>
                    <div class="rail-value">${focusBar ? formatPrice(focusBar.close) : '--'}</div>
                    <div class="rail-sub">${escapeHtml(currentSymbol || '--')} · ${escapeHtml(getIntervalLabel(currentInterval))}<br>${escapeHtml(focusBar?.us_time || '--')}</div>
                    <div class="metric-grid">
                        <div class="metric-item"><div class="metric-label">Open / High</div><div class="metric-value">${focusBar ? `${escapeHtml(formatPrice(focusBar.open))} / ${escapeHtml(formatPrice(focusBar.high))}` : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">Low / Close</div><div class="metric-value">${focusBar ? `${escapeHtml(formatPrice(focusBar.low))} / ${escapeHtml(formatPrice(focusBar.close))}` : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">Volume</div><div class="metric-value">${focusBar ? escapeHtml(formatNumber(focusBar.volume || 0, 0)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">VWAP Dist</div><div class="metric-value" style="color:${signedColor(focusIndicator?.vwap_dist)}">${focusIndicator ? escapeHtml(formatPercent(focusIndicator.vwap_dist)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">CRSI / OBV RSI</div><div class="metric-value">${focusIndicator ? `${escapeHtml(formatNumber(focusIndicator.crsi))} / ${escapeHtml(formatNumber(focusIndicator.obv_rsi))}` : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">Signal</div><div class="metric-value">${focusSignal ? escapeHtml(buildTradeSignalLabel(focusSignal)) : (focusSignalMatches.length ? `${focusSignalMatches.length} hits` : '--')}</div></div>
                    </div>
                    <div class="focus-actions">
                        ${focusSignal ? `<button class="mini-link mini-link-btn" type="button" onclick="openFocusedSignalDrawer()">信号详情</button>` : ''}
                        <a class="mini-link" href="${signalUrl}">历史信号页</a>
                        <a class="mini-link" href="${ordersUrl}">历史订单页</a>
                        ${orderDetailsUrl ? `<a class="mini-link" href="${orderDetailsUrl}">订单明细</a>` : ''}
                        <a class="mini-link" href="${accountUrl}">账户页</a>
                    </div>
                    <div class="rail-sub">点 K 线或 Recent Signals 切换焦点。</div>
                </div>
                <div class="rail-card">
                    <div class="rail-kicker">Snapshot</div>
                    <div class="rail-value">${focusBar ? formatPrice(focusBar.close) : '--'}</div>
                    <div class="rail-sub">${escapeHtml(currentSymbol || '--')} · ${escapeHtml(getIntervalLabel(currentInterval))}<br>${escapeHtml(focusBar?.us_time || latestBar?.us_time || '--')}</div>
                    <div class="link-row">
                        <a class="mini-link" href="${buildPageUrl('/ibkr_indicators.html', { date: new Date().toISOString().slice(0, 10) }, { environment: currentEnvironment })}">历史指标页</a>
                        <a class="mini-link" href="${buildPageUrl('/ibkr_signals.html', {}, { environment: currentEnvironment })}">历史信号页</a>
                    </div>
                </div>
                <div class="rail-card">
                    <div class="rail-kicker">Structure</div>
                    <div class="metric-grid">
                        <div class="metric-item"><div class="metric-label">趋势</div><div class="metric-value">${focusIndicator ? escapeHtml(getTrendText(focusIndicator.trend_dir)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">EMA 状态</div><div class="metric-value">${focusIndicator?.ema_bullish ? '📈 多头' : focusIndicator?.ema_bearish ? '📉 空头' : '➡️ 中性'}</div></div>
                        <div class="metric-item"><div class="metric-label">SD</div><div class="metric-value">${focusIndicator ? `${escapeHtml(getSdZoneText(focusIndicator.sd_zone))}<br>${escapeHtml(getSdTrendText(focusIndicator.sd_trend))}` : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">Fractal</div><div class="metric-value">${focusIndicator ? escapeHtml(getFractalStateText(focusIndicator)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">VWAP 偏离</div><div class="metric-value" style="color:${signedColor(focusIndicator?.vwap_dist)}">${focusIndicator ? escapeHtml(formatPercent(focusIndicator.vwap_dist)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">ATR %</div><div class="metric-value">${focusIndicator ? escapeHtml(formatPercent(focusIndicator.atr_pct)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">CRSI</div><div class="metric-value">${focusIndicator ? escapeHtml(formatNumber(focusIndicator.crsi)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">OBV RSI</div><div class="metric-value">${focusIndicator ? escapeHtml(formatNumber(focusIndicator.obv_rsi)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">Touch</div><div class="metric-value">${focusIndicator ? escapeHtml(getTouchSummary(focusIndicator)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">Div</div><div class="metric-value">${focusIndicator ? escapeHtml(getDivergenceSummary(focusIndicator, 3)) : '--'}</div></div>
                    </div>
                    <div class="rail-sub">时间范围: ${escapeHtml(getRangeLabel(currentRangeKey))}<br>${escapeHtml(timeRange)}</div>
                </div>
                <div class="rail-card">
                    <div class="rail-kicker">Signal Flow</div>
                    <div class="metric-grid">
                        <div class="metric-item"><div class="metric-label">多头信号</div><div class="metric-value">${longCount}</div></div>
                        <div class="metric-item"><div class="metric-label">空头信号</div><div class="metric-value">${shortCount}</div></div>
                        <div class="metric-item"><div class="metric-label">最新信号</div><div class="metric-value">${latestSignal ? escapeHtml(buildTradeSignalLabel(latestSignal)) : '--'}</div></div>
                        <div class="metric-item"><div class="metric-label">信号时间</div><div class="metric-value">${latestSignal ? escapeHtml(formatSignalTime(latestSignal).slice(5)) : '--'}</div></div>
                    </div>
                    <div class="rail-sub">交易标签仅 5m 叠加。</div>
                </div>
                <div class="rail-card">
                    <div class="rail-kicker">Recent Signals</div>
                    <div class="signal-list">
                        ${signals.slice(0, 5).map((signal) => {
                            const signalKey = String(signal.signal_id || signal.id || '');
                            const encodedSignalKey = encodeURIComponent(signalKey);
                            const signalMs = Number(signal.bar_time_ms || 0) || 0;
                            const isActive = activeSignalKey && activeSignalKey === signalKey;
                            return `
                            <div class="signal-item interactive ${isActive ? 'active' : ''}" onclick="focusSignalBar('${signalMs}', '${encodedSignalKey}')">
                                <div class="signal-side">
                                    <div class="signal-title">${escapeHtml(buildTradeSignalLabel(signal))}</div>
                                    <div class="signal-meta">${escapeHtml(formatSignalTime(signal))}</div>
                                </div>
                                <span class="signal-badge ${String(signal.direction || '').toLowerCase() === 'short' ? 'short' : 'long'}">${escapeHtml(String(signal.direction || '--').toUpperCase())}</span>
                            </div>
                        `;
                        }).join('') || '<div class="signal-item"><div class="signal-side"><div class="signal-title">暂无信号</div><div class="signal-meta">当前时间窗没有实时重算出的信号</div></div></div>'}
                    </div>
                </div>
            `;
            renderRailNav();
            renderInspectorDrawer(payload);
        }

