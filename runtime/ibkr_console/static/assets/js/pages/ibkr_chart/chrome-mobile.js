        function renderMobileQuickPanel(payload = getChartDisplayPayload()) {
            const panel = document.getElementById('mobileQuickPanel');
            const layerRoot = document.getElementById('mobileQuickLayerGroup');
            const timeframeRoot = document.getElementById('mobileQuickTimeframeGroup');
            const rangeRoot = document.getElementById('mobileQuickRangeGroup');
            const viewRoot = document.getElementById('mobileQuickViewGroup');
            if (!panel || !layerRoot || !timeframeRoot || !rangeRoot || !viewRoot) return;
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) {
                panel.classList.remove('show');
                panel.setAttribute('aria-hidden', 'true');
                layerRoot.innerHTML = '';
                timeframeRoot.innerHTML = '';
                rangeRoot.innerHTML = '';
                viewRoot.innerHTML = '';
                return;
            }
            panel.classList.toggle('show', mobileQuickPanelOpen);
            panel.setAttribute('aria-hidden', mobileQuickPanelOpen ? 'false' : 'true');
            const layerDefs = getChartLayerDefs();
            layerRoot.innerHTML = layerDefs.map((item) => `
                <button class="mobile-quick-chip ${chartLayerState[item.key] ? 'active' : ''} ${item.disabled ? 'disabled' : ''}" type="button" onclick="toggleChartLayer('${item.key}')" ${item.disabled ? 'disabled' : ''}>${escapeHtml(item.shortLabel)}${item.disabled ? ' · 5m' : ''}</button>
            `).join('');
            timeframeRoot.innerHTML = SUPPORTED_INTERVALS.map((interval) => `
                <button class="mobile-quick-chip ${interval === currentInterval ? 'active' : ''}" type="button" onclick="selectChartInterval('${interval}')">${escapeHtml(getIntervalLabel(interval))}</button>
            `).join('');
            rangeRoot.innerHTML = RANGE_PRESETS.map((preset) => `
                <button class="mobile-quick-chip ${preset.key === currentRangeKey ? 'active' : ''}" type="button" onclick="selectChartRange('${preset.key}')">${escapeHtml(preset.shortLabel)}</button>
            `).join('');
            const canCompare = Boolean(isCompareSupportedRange() && currentSymbol && bars.length && !compareLoading);
            viewRoot.innerHTML = [
                { active: chartFocusMode, label: chartFocusMode ? 'Exit Focus' : 'Focus', action: 'toggleChartFocusMode()' },
                { active: inspectorDrawerOpen, label: inspectorDrawerOpen ? 'Hide Info' : 'Inspector', action: 'toggleInspectorDrawer()' },
                { active: chartPointerLocked, label: chartPointerLocked ? 'Unlock Cursor' : 'Lock Cursor', action: chartPointerLocked ? 'unlockChartPointer({ preserveHover: false })' : 'lockChartPointerAtIndex(getEffectiveCursorIndex(getChartDisplayPayload()))' },
                { active: chartTracePanelOpen, label: chartTracePanelOpen ? 'Hide Trace' : 'Trace', action: 'toggleChartTracePanel()' },
                { active: false, label: 'Center', action: 'centerChartOnFocusBar()' },
                { active: false, label: 'Zoom In', action: 'zoomInChartView()' },
                { active: false, label: 'Zoom Out', action: 'zoomOutChartView()' },
                { active: false, label: 'Reset Zoom', action: 'resetChartView()' },
                { active: false, label: 'Export PNG', action: 'downloadChartImage()' },
                ...(canCompare ? [{ active: Boolean(comparePayload), label: comparePayload ? 'Recompare' : 'IBKR Compare', action: 'runIbkrCompare()' }] : []),
                ...(comparePayload || compareError ? [{ active: false, label: 'Clear Compare', action: 'clearIbkrCompare()' }] : []),
            ].map((item) => `
                <button class="mobile-quick-chip ${item.active ? 'active' : ''}" type="button" onclick="${item.action}">${escapeHtml(item.label)}</button>
            `).join('');
        }
        function openMobileQuickPanel() {
            inspectorDrawerOpen = false;
            renderInspectorDrawer(getChartDisplayPayload());
            closeSignalDrawer();
            mobileQuickPanelOpen = true;
            renderMobileQuickPanel(getChartDisplayPayload());
            renderMobileGestureHint(getChartDisplayPayload());
        }
        function closeMobileQuickPanel() {
            mobileQuickPanelOpen = false;
            renderMobileQuickPanel(getChartDisplayPayload());
            renderMobileGestureHint(getChartDisplayPayload());
        }
        function dismissMobileGestureHint() {
            if (isCompactViewport() && !mobileGestureHintSeen) {
                mobileGestureHintSeen = true;
                try {
                    localStorage.setItem(CHART_MOBILE_HINT_KEY, '1');
                } catch (_) {}
            }
            renderMobileGestureHint(getChartDisplayPayload());
        }
        function renderMobileGestureHint(payload = getChartDisplayPayload()) {
            const root = document.getElementById('mobileGestureHint');
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!root) return;
            const hidden = !isCompactViewport()
                || mobileGestureHintSeen
                || !bars.length
                || mobileQuickPanelOpen
                || inspectorDrawerOpen
                || Boolean(document.getElementById('signalDetailDrawer')?.classList.contains('show'));
            root.classList.toggle('show', !hidden);
            root.setAttribute('aria-hidden', hidden ? 'true' : 'false');
        }
        function registerChartTouchInteractions() {
            const canvas = document.getElementById('chartCanvas');
            if (!canvas) return;
            const shell = document.querySelector('.chart-stage-shell');
            const handlePointerExit = (event) => {
                if (event?.pointerType === 'touch') return;
                clearTransientChartCursor();
            };
            canvas.onpointerleave = handlePointerExit;
            canvas.onmouseleave = handlePointerExit;
            if (shell) {
                shell.onpointerleave = handlePointerExit;
                shell.onmouseleave = handlePointerExit;
            }
            canvas.removeEventListener('wheel', handleChartWheelGesture, { capture: true });
            canvas.addEventListener('wheel', handleChartWheelGesture, { passive: false, capture: true });
            registerChartSelectionInteractions();
            canvas.ontouchstart = null;
            canvas.ontouchmove = null;
            canvas.ontouchend = null;
            canvas.ontouchcancel = null;
            if (!isCompactViewport()) return;

            canvas.ontouchstart = (event) => {
                const touch = event.touches && event.touches[0];
                if (!touch) return;
                const startX = touch.clientX;
                const startY = touch.clientY;
                chartTouchSession.startX = startX;
                chartTouchSession.startY = startY;
                chartTouchSession.moved = false;
                clearChartTouchTimer();
                if ((event.touches?.length || 0) > 1) return;
                chartTouchSession.timer = window.setTimeout(() => {
                    const index = getBarIndexFromClientPoint(startX, startY);
                    if (index < 0) return;
                    suppressNextChartClick = true;
                    chartTouchSession.lastTapAt = 0;
                    lockChartPointerAtIndex(index);
                    showToast('已锁定光标');
                }, 360);
            };

            canvas.ontouchmove = (event) => {
                const touch = event.touches && event.touches[0];
                if (!touch) return;
                const dx = Math.abs(touch.clientX - chartTouchSession.startX);
                const dy = Math.abs(touch.clientY - chartTouchSession.startY);
                if (dx > 10 || dy > 10) {
                    chartTouchSession.moved = true;
                    clearChartTouchTimer();
                }
            };

            canvas.ontouchend = (event) => {
                clearChartTouchTimer();
                const touch = event.changedTouches && event.changedTouches[0];
                if (!touch) return;
                const dx = touch.clientX - chartTouchSession.startX;
                const dy = touch.clientY - chartTouchSession.startY;
                const absX = Math.abs(dx);
                const absY = Math.abs(dy);
                const now = Date.now();
                if (absX > 42 && absX > absY * 1.2) {
                    suppressNextChartClick = true;
                    navigateTouchSwipe(dx);
                    chartTouchSession.lastTapAt = 0;
                    return;
                }
                if (chartPointerLocked && suppressNextChartClick) {
                    chartTouchSession.lastTapAt = 0;
                    return;
                }
                if (chartTouchSession.moved) return;
                const isDoubleTap = chartTouchSession.lastTapAt
                    && now - chartTouchSession.lastTapAt < 280
                    && Math.abs(touch.clientX - chartTouchSession.lastTapX) < 24
                    && Math.abs(touch.clientY - chartTouchSession.lastTapY) < 24;
                chartTouchSession.lastTapAt = now;
                chartTouchSession.lastTapX = touch.clientX;
                chartTouchSession.lastTapY = touch.clientY;
                if (isDoubleTap) {
                    suppressNextChartClick = true;
                    unlockChartPointer();
                    focusLatestBarAction();
                }
            };

            canvas.ontouchcancel = () => {
                clearChartTouchTimer();
            };
        }
        function renderMobileDock(payload = getChartDisplayPayload()) {
            const dock = document.getElementById('mobileDock');
            if (!dock) return;
            if (!isCompactViewport()) {
                dock.innerHTML = '';
                return;
            }
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) {
                dock.innerHTML = '';
                return;
            }
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const focus = buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId);
            const focusSignal = focus?.activeSignal || null;
            dock.innerHTML = `
                <span class="mobile-dock-label">${escapeHtml(currentSymbol || '--')} · ${escapeHtml((focus?.bar?.us_time || '--').replace(/^(\d{4}-)/, ''))}</span>
                <button class="mobile-dock-btn" type="button" onclick="focusPreviousChartBar()">◀</button>
                <button class="mobile-dock-btn" type="button" onclick="focusNextChartBar()">▶</button>
                <button class="mobile-dock-btn accent" type="button" onclick="focusLatestChartBar()">Now</button>
                <button class="mobile-dock-btn" type="button" onclick="openMobileQuickPanel()">Tools</button>
                <button class="mobile-dock-btn ${inspectorDrawerOpen ? 'accent' : ''}" type="button" onclick="toggleInspectorDrawer()">Info</button>
                ${focusSignal || signals.length ? `<button class="mobile-dock-btn" type="button" onclick="${focusSignal ? 'openFocusedSignalDrawer()' : 'focusNextChartSignal()'}">${focusSignal ? 'Signal' : 'Next Sig'}</button>` : ''}
            `;
        }
