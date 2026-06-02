        function buildStripChip(baseClass, text, extraClass = '', title = '') {
            const classes = [baseClass, extraClass].filter(Boolean).join(' ');
            const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
            return `<span class="${classes}"${titleAttr}>${escapeHtml(text || '--')}</span>`;
        }
        function getWorkspaceStateBadge() {
            if (chartWorkspaceState === 'loading') return { text: '工作区 加载中', className: 'loading' };
            if (chartWorkspaceState === 'error') return { text: '工作区 加载失败', className: 'error' };
            if (chartWorkspaceState === 'ready') return { text: '工作区 已就绪', className: '' };
            return { text: '工作区 等待数据', className: 'placeholder' };
        }
        function summarizeCompareStatusLine(statusMap) {
            const defs = [
                { key: 'bar', label: 'Bars' },
                { key: 'indicator', label: 'Ind' },
                { key: 'signal', label: 'Sig' },
            ];
            return defs.map((item) => `${item.label} ${getCompareStatusLabel(statusMap?.[item.key])}`).join(' · ');
        }
        function getChartFallbackClose(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const fallbackClose = Number(payload?.latestIndicator?.close ?? bars[bars.length - 1]?.close ?? NaN);
            return Number.isFinite(fallbackClose) && fallbackClose > 0 ? fallbackClose : null;
        }
        function getRealtimeChipState(payload) {
            const livePrice = Number(realtimeQuoteSnapshot?.last_price);
            if (Number.isFinite(livePrice) && livePrice > 0) {
                const age = Number(realtimeQuoteSnapshot?.quote_age_s);
                const ageText = Number.isFinite(age) ? `，行情延迟约 ${age.toFixed(age >= 10 ? 0 : 1)} 秒` : '';
                const isDelayed = Number.isFinite(age) && age > 15;
                return {
                    text: isDelayed
                        ? `实时价 ${formatPrice(livePrice)} · 延迟 ${age.toFixed(age >= 60 ? 0 : 1)}s`
                        : `实时价 ${formatPrice(livePrice)} · 日涨跌 ${formatSignedPercent(realtimeQuoteSnapshot.day_change_pct)}`,
                    className: isDelayed ? 'placeholder' : '',
                    title: chartRealtimeQuoteError
                        ? `当前显示的是最近一次成功拿到的 live quote${ageText}。最近一次 /quotes 刷新异常：${chartRealtimeQuoteError}`
                        : `来自实时行情快照${ageText}。`,
                };
            }
            const fallbackClose = getChartFallbackClose(payload);
            if (Number.isFinite(fallbackClose)) {
                const errorText = chartRealtimeQuoteError ? `最近一次报价请求失败：${chartRealtimeQuoteError}。` : '当前没有拿到可用实时报价。';
                return {
                    text: `实时价 暂缺 · 回退 ${formatPrice(fallbackClose)}`,
                    className: 'placeholder',
                    title: `${errorText} 页面先用最近已收盘 K 线 close ${formatPrice(fallbackClose)} 兜底展示。`,
                };
            }
            return {
                text: '实时价 --',
                className: 'placeholder',
                title: '当前还没有拿到实时价，也没有可回退的收盘价。',
            };
        }
        function getPreviewChipState() {
            if (currentInterval !== '5m') {
                return {
                    text: '未收盘预览 仅 5m',
                    className: 'placeholder',
                    title: '只有 5m 周期会叠加当前未收盘 bar 的预览。',
                };
            }
            if (formingBarSnapshot?.bar_time_ms) {
                return {
                    text: `未收盘预览 ${formatChartLabel(formingBarSnapshot)}`,
                    className: '',
                    title: '这是当前 5m 尚未收盘 bar 的临时预览，不会直接写回历史 bars。',
                };
            }
            if (chartRealtimePreviewError) {
                return {
                    text: '未收盘预览 暂缺',
                    className: 'placeholder',
                    title: `当前未收盘 5m 预览暂时不可用。最近错误：${chartRealtimePreviewError}`,
                };
            }
            return {
                text: '未收盘预览 --',
                className: 'placeholder',
                title: '等待当前 5m bar 的未收盘预览数据。',
            };
        }
        function getCompareChipState(compareSummary, { compact = false } = {}) {
            if (compareLoading) {
                return {
                    text: compact ? 'IBKR 对比 运行中' : 'IBKR 对比 正在运行',
                    className: 'loading',
                    title: '正在把当前 stored bars 链路与 IBKR API 临时拉取结果做逐 bar 对比。',
                };
            }
            if (comparePayload) {
                return {
                    text: compact
                        ? `IBKR 对比 Stored ${compareSummary.stored_visible_bars || 0} · IBKR ${compareSummary.ibkr_visible_bars || 0} · 差异 ${compareSummary.bar_mismatch_count || 0}/${compareSummary.indicator_mismatch_count || 0}/${compareSummary.signal_mismatch_count || 0}`
                        : `IBKR 对比 bars ${compareSummary.bar_mismatch_count || 0} · ind ${compareSummary.indicator_mismatch_count || 0} · sig ${compareSummary.signal_mismatch_count || 0}`,
                    className: '',
                    title: '差异顺序为 bars / indicators / signals，用于核对 stored 链路和 IBKR API 临时回放是否一致。',
                };
            }
            if (compareError) {
                return {
                    text: 'IBKR 对比 失败',
                    className: 'error',
                    title: `最近一次 IBKR 对比失败：${compareError}`,
                };
            }
            return {
                text: 'IBKR 对比 未运行',
                className: 'placeholder',
                title: '点击“IBKR 对比”后，会把当前图表窗口的 stored 数据链路和 IBKR API 回放结果进行逐 bar 对照。',
            };
        }
        function buildCursorCard(label, primary, secondary, { valueClass = '', meta = '' } = {}) {
            return `
                <div class="cursor-card">
                    <div class="cursor-label">${escapeHtml(label || '--')}</div>
                    <div class="cursor-value ${escapeHtml(valueClass)}">
                        <span class="cursor-main">${escapeHtml(primary || '--')}</span>
                        <span class="cursor-sub">${escapeHtml(secondary || '--')}</span>
                    </div>
                    <div class="cursor-meta">${meta || '&nbsp;'}</div>
                </div>
            `;
        }
        function formatInlineOHLC(bar) {
            return {
                primary: `O ${formatPrice(bar?.open)} · H ${formatPrice(bar?.high)}`,
                secondary: `L ${formatPrice(bar?.low)} · C ${formatPrice(bar?.close)}`,
            };
        }
        function formatInlineChain(indicator) {
            return {
                primary: `20 ${formatPrice(indicator?.ema_fast)} · 50 ${formatPrice(indicator?.ema_slow)}`,
                secondary: `VWAP ${formatPrice(indicator?.vwap)} · ±1 ${formatPrice(indicator?.vwap_upper1 ?? indicator?.vwap_upper)}/${formatPrice(indicator?.vwap_lower1 ?? indicator?.vwap_lower)}`,
            };
        }
        function formatInlineCompareChain(indicator) {
            return {
                primary: `EMA20 ${formatPrice(indicator?.ema_fast)} · EMA50 ${formatPrice(indicator?.ema_slow)}`,
                secondary: `VWAP ${formatPrice(indicator?.vwap)} · CRSI ${formatNumber(indicator?.crsi)}`,
            };
        }
        function formatInlineCompareDiff(compareRow) {
            return {
                primary: `Bars ${summarizeCompareFields(compareRow?.diff?.bar, '一致')}`,
                secondary: `Ind ${summarizeCompareFields(compareRow?.diff?.indicator, '一致')} · Sig ${summarizeCompareFields(compareRow?.diff?.signal, '一致')}`,
            };
        }
        function renderHeroStatus(payload) {
            const title = document.getElementById('heroTitle');
            if (title) {
                title.textContent = currentSymbol
                    ? `${currentSymbol} · ${getIntervalLabel(currentInterval)} 回测复盘图`
                    : 'IBKR 回测复盘图';
            }
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const focus = bars.length ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            const stateBadge = getWorkspaceStateBadge();
            const compareSummary = comparePayload?.comparison?.summary || {};
            const focusTime = focus?.bar?.us_time || '--';
            const lastBarTime = bars.length ? bars[bars.length - 1]?.us_time || '--' : '--';
            const realtimeState = getRealtimeChipState(payload);
            const previewState = getPreviewChipState();
            const compareState = getCompareChipState(compareSummary, { compact: false });
            const freshnessState = getChartFreshnessChipState(payload);
            const chips = [
                buildStripChip('hero-chip', `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)}`, currentSymbol ? '' : 'placeholder', '当前查看的标的与周期。'),
                buildStripChip('hero-chip', getEnvironmentLabel(currentEnvironment), currentEnvironment ? '' : 'placeholder', '当前运行环境。'),
                buildStripChip('hero-chip', stateBadge.text, stateBadge.className, '图表工作区当前加载状态。'),
                buildStripChip('hero-chip', `焦点 bar ${focusTime}`, focusTime === '--' ? 'placeholder' : '', '当前决策上下文使用的 focus bar 时间。'),
                buildStripChip('hero-chip', `最后收盘 bar ${lastBarTime}`, lastBarTime === '--' ? 'placeholder' : '', '当前窗口最后一根已收盘 bar 的时间。'),
                buildStripChip('hero-chip', realtimeState.text, realtimeState.className, realtimeState.title),
                buildStripChip('hero-chip', previewState.text, previewState.className, previewState.title),
                buildStripChip('hero-chip', freshnessState.text, freshnessState.className, freshnessState.title),
                buildStripChip('hero-chip', compareState.text, compareState.className, compareState.title),
            ];
            document.getElementById('heroStatus').innerHTML = chips.join('');
        }
        function getChartFreshnessChipState(payload) {
            const freshness = payload?.meta?.freshness || {};
            const repair = payload?.meta?.repair || {};
            const status = String(freshness.status || '').trim().toLowerCase();
            if (!status) return { text: 'Bars freshness --', className: 'placeholder', title: '后端尚未返回 bars 完整性状态。' };
            const interval = freshness?.needs_repair_intervals?.[0] || currentInterval;
            const row = freshness?.intervals?.[interval] || {};
            if (status === 'ready') {
                return { text: 'Bars Ready', className: 'good', title: `权威 bars 已到最新闭合桶 ${row.expected_closed_us || '--'}。` };
            }
            const repairText = repair.status ? ` · ${repair.status}` : '';
            return {
                text: `Bars ${status.toUpperCase()}${repairText}`,
                className: status === 'stale' ? 'warning' : 'loading',
                title: `latest ${row.latest_stored_us || '--'} / expected ${row.expected_closed_us || '--'}；已触发 IBKR API 异步补偿。`,
            };
        }
        function renderTimeframeGroup() {
            document.getElementById('timeframeGroup').innerHTML = SUPPORTED_INTERVALS.map((interval) => `
                <button class="tf-btn ${interval === currentInterval ? 'active' : ''}" type="button" onclick="selectChartInterval('${interval}')">${escapeHtml(getIntervalLabel(interval))}</button>
            `).join('');
        }
        function renderRangeGroup() {
            document.getElementById('rangeGroup').innerHTML = RANGE_PRESETS.map((preset) => `
                <button class="range-btn ${preset.key === currentRangeKey || (preset.key === 'custom' && customRangeFormOpen) ? 'active' : ''}" type="button" onclick="selectChartRange('${preset.key}')">${escapeHtml(preset.shortLabel)}</button>
            `).join('');
            renderCustomRangeForm();
        }
        function seedCustomRangeFromPayload(payload = getChartDisplayPayload()) {
            if (customRangeStartMs && customRangeEndMs) return;
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (bars.length) {
                customRangeStartMs = Number(bars[0]?.bar_time_ms || 0) || customRangeStartMs;
                customRangeEndMs = Number(bars[bars.length - 1]?.bar_time_ms || 0) || customRangeEndMs;
                return;
            }
            const endMs = Number(currentAnchorMs || Date.now()) || Date.now();
            customRangeEndMs = customRangeEndMs || endMs;
            customRangeStartMs = customRangeStartMs || getRangeStartMs(endMs, getDefaultRangeKey(currentInterval));
        }
        function renderCustomRangeForm() {
            const form = document.getElementById('customRangeForm');
            const startInput = document.getElementById('customRangeStart');
            const endInput = document.getElementById('customRangeEnd');
            if (!form || !startInput || !endInput) return;
            const show = customRangeFormOpen || isCustomRange();
            form.hidden = !show;
            if (!show) return;
            seedCustomRangeFromPayload();
            ensureCustomRangePickers();
            const startValue = formatDateTimeEtInputValue(customRangeStartMs);
            const endValue = formatDateTimeEtInputValue(customRangeEndMs);
            syncCustomRangePickerValue(customRangeStartPicker, startValue);
            syncCustomRangePickerValue(customRangeEndPicker, endValue);
            startInput.value = startValue;
            endInput.value = endValue;
        }
        function renderSummaryStrip(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const indicators = Array.isArray(payload?.indicators) ? payload.indicators : [];
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const backtestEvents = Array.isArray(payload?.backtestEvents) ? payload.backtestEvents : [];
            const tvEvents = Array.isArray(payload?.tvEvents) ? payload.tvEvents : [];
            const orderEvents = Array.isArray(payload?.orderEvents) ? payload.orderEvents : [];
            const preAlertCount = tvEvents.filter((event) => String(event?.event_type || '').toLowerCase() === 'pre_alert').length;
            const exitCount = orderEvents.filter((event) => String(event?.event_type || '').toLowerCase().startsWith('live_exit')).length;
            const focus = bars.length ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            const focusBar = focus?.bar || null;
            const latestBar = bars.length ? bars[bars.length - 1] : null;
            const compareSummary = comparePayload?.comparison?.summary || {};
            const realtimeState = getRealtimeChipState(payload);
            const previewState = getPreviewChipState();
            const compareState = getCompareChipState(compareSummary, { compact: true });
            const stateBadge = getWorkspaceStateBadge();
            const freshnessState = getChartFreshnessChipState(payload);
            const chips = [
                { text: `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)} · ${getRangeShortLabel(currentRangeKey)}`, className: currentSymbol ? '' : 'placeholder', title: getRangeLabel(currentRangeKey) },
                { text: `${bars.length} bars · ${indicators.length} ind · ${signals.length} sig${preAlertCount ? ` · PA ${preAlertCount}` : ''}${exitCount ? ` · Exit ${exitCount}` : ''}${backtestEvents.length ? ` · BT ${backtestEvents.length}` : ''}`, className: chartWorkspaceState === 'loading' ? 'loading' : '', title: '当前图表窗口实际绘制的数据量。PA 表示 TV pre_alert 入池事件。' },
                { text: `Focus ${focusBar ? String(focusBar.us_time || '--').slice(5) : '--'}`, className: focusBar ? '' : 'placeholder', title: '当前复盘焦点时间。' },
                { text: `Latest ${latestBar ? String(latestBar.us_time || '--').slice(5) : '--'}`, className: latestBar ? '' : 'placeholder', title: '当前窗口最后一根 bar。' },
                { text: stateBadge.text.replace('工作区 ', ''), className: stateBadge.className, title: '图表工作区状态。' },
                { text: realtimeState.text, className: realtimeState.className, title: realtimeState.title },
                { text: previewState.text, className: previewState.className, title: previewState.title },
                { text: freshnessState.text, className: freshnessState.className, title: freshnessState.title },
                { text: compareState.text, className: compareState.className, title: compareState.title },
            ];
            document.getElementById('summaryStrip').innerHTML = chips.map((item) => buildStripChip('summary-chip', item.text, item.className, item.title)).join('');
        }
        function buildFloatingLegendChip(text, title = '', className = '') {
            const titleAttr = title ? ` title="${escapeHtml(title)}"` : '';
            const classAttr = ['tv-floating-pill', className].filter(Boolean).join(' ');
            return `<span class="${classAttr}"${titleAttr}>${escapeHtml(text || '--')}</span>`;
        }
        function getFloatingOverviewItems(payload, { compact = false } = {}) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const indicators = Array.isArray(payload?.indicators) ? payload.indicators : [];
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const backtestEvents = Array.isArray(payload?.backtestEvents) ? payload.backtestEvents : [];
            const tvEvents = Array.isArray(payload?.tvEvents) ? payload.tvEvents : [];
            const orderEvents = Array.isArray(payload?.orderEvents) ? payload.orderEvents : [];
            const preAlertCount = tvEvents.filter((event) => String(event?.event_type || '').toLowerCase() === 'pre_alert').length;
            const exitCount = orderEvents.filter((event) => String(event?.event_type || '').toLowerCase().startsWith('live_exit')).length;
            const focus = bars.length ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            const focusIndicator = focus?.indicator || null;
            const focusBar = focus?.bar || null;
            const compareSummary = comparePayload?.comparison?.summary || {};
            const realtimeState = getRealtimeChipState(payload);
            const previewState = getPreviewChipState();
            const compareState = getCompareChipState(compareSummary, { compact: true });
            const items = [
                { text: `K线 ${bars.length}`, title: '当前图表窗口实际绘制的 bars 数量。' },
                { text: `指标 ${indicators.length}`, title: '当前窗口成功匹配到的指标快照数量。' },
                { text: `信号 ${signals.length}`, title: '当前窗口内的交易信号数量。' },
                ...(preAlertCount ? [{ text: `pre_alert ${preAlertCount}`, title: '当前窗口内 TradingView pre_alert 入池候选事件。' }] : []),
                ...(exitCount ? [{ text: `退出 ${exitCount}`, title: '当前窗口内真实订单退出事件，含 EOD 强制平仓。' }] : []),
                ...(backtestEvents.length ? [{ text: `回测 ${backtestEvents.length}`, title: '当前窗口内叠加的回测买卖事件。' }] : []),
                { text: `范围 ${getRangeShortLabel(currentRangeKey)}`, title: getRangeLabel(currentRangeKey) },
                { text: `焦点 ${focusBar ? String(focusBar.us_time || '--').slice(5) : '--'}`, title: '当前 focus bar 时间。' },
                { text: `趋势 ${focusIndicator ? getTrendText(focusIndicator.trend_dir) : '--'}`, title: '趋势方向。' },
                { text: `EMA ${focusIndicator ? getEmaStructureText(focusIndicator) : '--'}`, title: 'EMA 结构。' },
                { text: `VWAP ${focusIndicator ? formatPercent(focusIndicator.vwap_dist) : '--'}`, title: 'VWAP 偏离。' },
                { text: `ATR ${focusIndicator ? formatPercent(focusIndicator.atr_pct) : '--'}`, title: 'ATR 波动。' },
                { text: `SD ${focusIndicator ? getSdRegimeText(focusIndicator.sd_regime) : '--'} · Z ${focusIndicator ? formatOptionalNumber(focusIndicator.sd_close_z) : '--'}`, title: 'SD regime 表示压缩/扩张/常态，Z 为收盘价相对 SD 中轴的位置。' },
                { text: `ORB ${focusIndicator ? getOrbBreakoutText(focusIndicator) : '--'}`, title: '开盘区间高低点及突破方向。' },
                { text: realtimeState.text, title: realtimeState.title, className: realtimeState.className === 'placeholder' ? 'muted' : '' },
                { text: previewState.text, title: previewState.title, className: previewState.className === 'placeholder' ? 'muted' : '' },
                { text: compareState.text, title: compareState.title, className: compareState.className === 'placeholder' ? 'muted' : (compareState.className || '') },
            ];
            return compact ? items.filter((_, index) => index !== 1 && index !== 7 && index !== 11) : items;
        }
        function buildFloatingLayerButton(item) {
            const swatches = Array.isArray(item?.swatches) ? item.swatches : [];
            const title = item?.description
                ? `${item.description}${item.disabled ? '，当前仅 5m 周期可用。' : ''}`
                : (item.disabled ? '当前仅 5m 周期可用。' : '');
            return `
                <button
                    class="tv-layer-chip ${chartLayerState[item.key] ? 'active' : ''} ${item.disabled ? 'disabled' : ''}"
                    type="button"
                    onclick="toggleChartLayer('${item.key}')"
                    ${item.disabled ? 'disabled' : ''}
                    title="${escapeHtml(title)}"
                >
                    <span class="tv-layer-swatches">
                        ${swatches.map((color) => `<span class="tv-layer-swatch" style="--swatch:${escapeHtml(color)};"></span>`).join('')}
                    </span>
                    <span class="tv-layer-label">${escapeHtml(item.label)}${item.disabled ? ' · 5m' : ''}</span>
                </button>
            `;
        }
        function buildContext(payload, index, signalId = '') {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) return null;
            const indicators = Array.isArray(payload?.indicators) ? payload.indicators : [];
            const indicatorMap = new Map(indicators.map((item) => [Number(item?.bar_time_ms || 0), item]));
            const traceTimeline = Array.isArray(payload?.traceTimeline) ? payload.traceTimeline : [];
            const traceMap = new Map(traceTimeline.map((item) => [Number(item?.bar_time_ms || 0), item]));
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const tvEvents = Array.isArray(payload?.tvEvents) ? payload.tvEvents : [];
            const orderEvents = Array.isArray(payload?.orderEvents) ? payload.orderEvents : [];
            const safeIndex = Math.min(Math.max(Number(index) || 0, 0), bars.length - 1);
            const bar = bars[safeIndex] || null;
            const signalMatches = signals.filter((item) => Number(item?.bar_time_ms || 0) === Number(bar?.bar_time_ms || 0));
            const tvEventMatches = tvEvents.filter((item) => Number(item?.bar_time_ms || 0) === Number(bar?.bar_time_ms || 0));
            const orderEventMatches = orderEvents.filter((item) => Number(item?.bar_time_ms || 0) === Number(bar?.bar_time_ms || 0));
            const activeSignal = signalMatches.find((item) => String(item?.signal_id || item?.id || '') === String(signalId || ''))
                || signalMatches[0]
                || null;
            const exactIndicator = indicatorMap.get(Number(bar?.bar_time_ms || 0)) || null;
            const exactTrace = traceMap.get(Number(bar?.bar_time_ms || 0)) || null;
            const traceSignal = getTraceSignalPayload(exactTrace);
            const traceSignalKey = getTraceSignalKey(exactTrace);
            const isPreviewBar = Boolean(bar?.preview || bar?.is_preview);
            return {
                index: safeIndex,
                bar,
                isPreviewBar,
                indicator: exactIndicator || (isPreviewBar ? null : payload?.latestIndicator || null),
                signalMatches,
                activeSignal,
                tvEventMatches,
                orderEventMatches,
                activeTraceSignal: traceSignalKey && String(signalId || '') === traceSignalKey ? traceSignal : null,
                trace: exactTrace,
            };
        }
        function getEffectiveCursorIndex(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) return -1;
            if (hoverBarIndex >= 0) return clampIndex(hoverBarIndex, bars.length);
            if (selectedBarIndex >= 0) return clampIndex(selectedBarIndex, bars.length);
            return bars.length - 1;
        }
        function getSignalBarIndices(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const tvEvents = Array.isArray(payload?.tvEvents) ? payload.tvEvents : [];
            const orderEvents = Array.isArray(payload?.orderEvents) ? payload.orderEvents : [];
            if (!bars.length || (!signals.length && !tvEvents.length && !orderEvents.length)) return [];
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar?.bar_time_ms || 0), index]));
            return Array.from(new Set(
                [...signals, ...tvEvents, ...orderEvents]
                    .map((item) => indexByMs.get(Number(item?.bar_time_ms || 0)))
                    .filter((index) => Number.isInteger(index) && index >= 0)
            )).sort((a, b) => a - b);
        }
        function findNearestSignalAnchor(payload, index, maxDistance = 0) {
            const signalIndices = getSignalBarIndices(payload);
            if (!signalIndices.length) return null;
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) return null;
            const targetIndex = clampIndex(index, bars.length);
            let bestIndex = -1;
            let bestDistance = Infinity;
            signalIndices.forEach((signalIndex) => {
                const distance = Math.abs(signalIndex - targetIndex);
                if (distance < bestDistance) {
                    bestDistance = distance;
                    bestIndex = signalIndex;
                }
            });
            if (!Number.isInteger(bestIndex) || bestDistance > Number(maxDistance || 0)) return null;
            const context = buildContext(payload, bestIndex, '');
            const signal = context?.activeSignal || null;
            return {
                index: bestIndex,
                signalId: String(signal?.signal_id || signal?.id || '')
            };
        }
        function renderChartToolbar(payload = getChartDisplayPayload()) {
            const toolbar = document.getElementById('chartToolbar');
            if (!toolbar) return;
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const focus = bars.length ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            const compact = isCompactViewport();
            const phone = isPhoneViewport();
            if (!bars.length) {
                toolbar.innerHTML = `
                    <div class="tv-toolbar-status">
                        <span class="tv-copy-chip">${escapeHtml(chartWorkspaceState === 'loading' ? '正在加载图表数据' : '等待图表数据')}</span>
                    </div>
                `;
                return;
            }
            const labels = compact ? {
                prevBar: '◀',
                nextBar: '▶',
                prevSignal: 'Sig−',
                nextSignal: 'Sig+',
                latest: phone ? 'Now' : 'Latest',
                inspect: 'Info',
                trace: 'Trace',
                tools: 'Tools',
                center: 'Center',
                zoomOut: '−',
                zoomIn: '+',
                reset: 'Reset',
                select: 'Select',
            } : {
                prevBar: '◀ Bar',
                nextBar: 'Bar ▶',
                prevSignal: 'Prev Signal',
                nextSignal: 'Next Signal',
                latest: 'Latest',
                inspect: inspectorDrawerOpen ? 'Hide Inspector' : 'Inspector',
                trace: chartTracePanelOpen ? 'Hide Trace' : 'Trace',
                tools: 'Tools',
                center: 'Center',
                zoomOut: '− Zoom',
                zoomIn: '＋ Zoom',
                reset: 'Reset',
                select: 'Select',
            };
            const focusText = focus?.bar?.us_time || '等待数据';
            const statusChips = [
                `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)} · ${getRangeShortLabel(currentRangeKey)}`,
                `Focus ${compact ? String(focusText).replace(/^(\d{4}-)/, '') : focusText}`,
                signals.length ? `${signals.length} signals` : 'No signals',
                chartPointerLocked ? 'Locked' : '',
            ].filter(Boolean);
            const firstActions = [
                { label: labels.prevBar, action: 'focusPreviousChartBar()' },
                { label: labels.nextBar, action: 'focusNextChartBar()' },
                ...(signals.length ? [
                    { label: labels.prevSignal, action: 'focusPreviousChartSignal()' },
                    { label: labels.nextSignal, action: 'focusNextChartSignal()' },
                ] : []),
            ];
            const secondActions = [
                { label: labels.latest, action: 'focusLatestChartBar()', className: 'accent' },
                { label: labels.center, action: 'centerChartOnFocusBar()' },
                { label: labels.zoomOut, action: 'zoomOutChartView()' },
                { label: labels.zoomIn, action: 'zoomInChartView()' },
                { label: labels.reset, action: 'resetChartView()' },
                ...(!compact ? [
                    { label: labels.select, action: 'toggleChartSelectionMode()', className: chartSelectionMode ? 'active' : '' },
                ] : []),
                { label: labels.tools, action: 'openMobileQuickPanel()' },
                { label: labels.inspect, action: 'toggleInspectorDrawer()', className: inspectorDrawerOpen ? 'accent' : '' },
                { label: labels.trace, action: 'toggleChartTracePanel()', className: chartTracePanelOpen ? 'accent' : '' },
            ];
            const renderAction = (item) => `
                <button class="tv-tool-btn ${escapeHtml(item.className || '')}" type="button" onclick="${item.action}">${escapeHtml(item.label)}</button>
            `;
            toolbar.innerHTML = `
                <div class="tv-toolbar-cluster">
                    ${firstActions.map(renderAction).join('')}
                </div>
                <div class="tv-toolbar-cluster">
                    ${secondActions.map(renderAction).join('')}
                </div>
                <div class="tv-toolbar-copy">
                    ${statusChips.map((text) => `<span class="tv-copy-chip">${escapeHtml(text)}</span>`).join('')}
                </div>
            `;
        }
        function renderChartFloatingLegend(payload = getChartDisplayPayload()) {
            const root = document.getElementById('chartFloatingLegend');
            const watermark = document.getElementById('chartWatermarkSymbol');
            if (watermark) watermark.textContent = currentSymbol || '--';
            if (!root) return;
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) {
                root.innerHTML = '<div class="tv-floating-shell compact-focus"><div class="tv-floating-head"><span class="tv-floating-pill brand">等待图表数据</span></div></div>';
                return;
            }
            const activeIndex = getEffectiveCursorIndex(payload);
            const context = buildContext(payload, activeIndex, selectedSignalId);
            const bar = context?.bar || null;
            const signal = context?.activeSignal || null;
            const compareRow = getCompareRowByBarTime(bar?.bar_time_ms);
            const previousClose = Number(activeIndex > 0 ? bars[activeIndex - 1]?.close : bar?.open);
            const closeValue = Number(bar?.close || 0);
            const deltaValue = Number.isFinite(closeValue) && Number.isFinite(previousClose) ? closeValue - previousClose : NaN;
            const deltaPercent = Number.isFinite(deltaValue) && previousClose ? (deltaValue / previousClose) * 100 : NaN;
            const deltaClass = !Number.isFinite(deltaValue) ? '' : deltaValue >= 0 ? 'positive' : 'negative';
            const focusLabel = hoverBarIndex >= 0 ? 'Cursor' : 'Focus';
            const compact = isCompactViewport();
            const signalLabel = signal
                ? buildTradeSignalLabel(signal)
                : (context?.tvEventMatches?.[0] ? getTvEventSummary(context.tvEventMatches[0]) : context?.orderEventMatches?.[0] ? getOrderEventSummary(context.orderEventMatches[0]) : (isTradeSignalInterval() ? 'No trade signal' : 'Labels 仅 5m'));
            const decisionSignal = getDecisionSignalContext(context);
            const riskSignal = signal || decisionSignal.traceSignal || null;
            const traceStage = getTraceStage(context?.trace);
            const traceLabel = formatTraceDecisionLabel(context?.trace, signal || decisionSignal.traceSignal);
            const summaryTime = compact ? String(bar?.us_time || '--').slice(5) : (bar?.us_time || '--');
            const summaryPills = [
                buildFloatingLegendChip(`${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)}`, '当前查看的标的与周期。', 'brand'),
                buildFloatingLegendChip(`${focusLabel} ${summaryTime}`, '当前浮层焦点时间。'),
                chartPointerLocked ? buildFloatingLegendChip('Locked', '当前光标已锁定。', 'brand') : '',
                buildFloatingLegendChip(
                    `${formatPrice(bar?.close)} · ${compact ? formatPercent(deltaPercent) : formatSignedPriceDelta(deltaValue)}${compact ? '' : ` · ${formatPercent(deltaPercent)}`}`,
                    '当前焦点 K 线收盘价与相对上一根 K 线的变化。',
                    deltaClass
                ),
                buildFloatingLegendChip(traceStage ? `${traceStage} · ${traceLabel}` : signalLabel, '当前焦点 bar 的信号/trace 状态。', getTraceStageBadgeClass(traceStage) || (signal ? getSignalBadgeClass(signal) : '')),
                riskSignal ? buildFloatingLegendChip(buildRiskSummary(riskSignal, payload, context, { compact: true }), '当前参考价到 TP/Target 与 SL 的距离，以及该回测 run 内历史胜率。', 'brand') : '',
                compareRow ? buildFloatingLegendChip(getCompareStatusLabel(compareRow?.status?.bar), '当前焦点 bar 的 IBKR 对比状态。', getCompareStatusClass(compareRow?.status?.bar)) : '',
            ].filter(Boolean).join('');
            root.innerHTML = `
                <div class="tv-floating-shell compact-focus">
                    <div class="tv-floating-head">
                        <div class="tv-floating-summary">${summaryPills}</div>
                        <button class="tv-floating-toggle" type="button" onclick="toggleInspectorDrawer()">${compact ? 'Info' : 'Inspector'}</button>
                    </div>
                </div>
            `;
        }
        function renderChartBottomBar(payload = getChartDisplayPayload()) {
            const statusRoot = document.getElementById('chartBottomStatus');
            const shortcutRoot = document.getElementById('chartShortcutStrip');
            if (!statusRoot || !shortcutRoot) return;
            statusRoot.innerHTML = '';
            shortcutRoot.innerHTML = '';
        }
        function renderCursorStrip(payload, { includeTrace = true } = {}) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) {
                document.getElementById('cursorStrip').innerHTML = [
                    buildCursorCard('Bar Start (ET)', '--', chartWorkspaceState === 'loading' ? '等待 bars / indicators' : '暂无游标上下文'),
                    buildCursorCard('OHLC', '--', '--'),
                    buildCursorCard('EMA / VWAP', '--', '--'),
                    buildCursorCard('SD / Fractal', '--', '--'),
                    buildCursorCard('Touch / Divergence', '--', '--'),
                    buildCursorCard('TP / Runner / SL', '--', '等待信号风险位'),
                    buildCursorCard('Signal / Osc', chartWorkspaceState === 'error' ? '加载失败' : '--', chartWorkspaceState === 'error' ? '请检查 ibkr_bars / 网络状态' : '--'),
                ].join('');
                renderChartWorkspaceChrome(payload, { includeTrace });
                return;
            }
            const fallbackIndex = selectedBarIndex >= 0 ? selectedBarIndex : bars.length - 1;
            const activeIndex = hoverBarIndex >= 0 ? hoverBarIndex : fallbackIndex;
            const context = buildContext(payload, activeIndex, selectedSignalId);
            const bar = context?.bar || null;
            const indicator = context?.indicator || null;
            const isPreviewBar = Boolean(context?.isPreviewBar);
            const decisionSignal = getDecisionSignalContext(context);
            const signal = decisionSignal.signal || decisionSignal.traceSignal || null;
            const traceLabel = formatTraceDecisionLabel(context?.trace, signal);
            const traceStage = getTraceStage(context?.trace);
            const compareRow = getCompareRowByBarTime(bar?.bar_time_ms);
            if (comparePayload && compareRow) {
                const storedBar = formatInlineOHLC(compareRow?.stored?.bar);
                const ibkrBar = formatInlineOHLC(compareRow?.ibkr?.bar);
                const storedChain = formatInlineCompareChain(compareRow?.stored?.indicator);
                const ibkrChain = formatInlineCompareChain(compareRow?.ibkr?.indicator);
                const diff = formatInlineCompareDiff(compareRow);
                document.getElementById('cursorStrip').innerHTML = [
                    buildCursorCard('Bar Start (ET)', getIbkrBarStartLabel(bar), `Close ${getIbkrBarCloseLabel(bar)} · ${summarizeCompareStatusLine(compareRow?.status)}`),
                    buildCursorCard('Stored Bars', storedBar.primary, storedBar.secondary, { valueClass: 'compact' }),
                    buildCursorCard('IBKR API Bars', ibkrBar.primary, ibkrBar.secondary, { valueClass: 'compact' }),
                    buildCursorCard('Stored Chain', storedChain.primary, storedChain.secondary, { valueClass: 'compact' }),
                    buildCursorCard('IBKR API Chain', ibkrChain.primary, ibkrChain.secondary, { valueClass: 'compact' }),
                    buildCursorCard('Diff', diff.primary, diff.secondary, { valueClass: 'compact' }),
                ].join('');
                renderChartWorkspaceChrome(payload, { includeTrace });
                return;
            }
            const ohlc = formatInlineOHLC(bar);
            const chain = formatInlineChain(indicator);
            const cursorState = chartPointerLocked ? 'Locked cursor' : (hoverBarIndex >= 0 ? 'Hover cursor' : 'Latest focus');
            const signalPrimary = traceStage
                ? traceLabel
                : (signal ? traceLabel : (context?.tvEventMatches?.[0] ? getTvEventSummary(context.tvEventMatches[0]) : context?.orderEventMatches?.[0] ? getOrderEventSummary(context.orderEventMatches[0]) : (isTradeSignalInterval() ? '暂无信号' : '标签仅 5m')));
            const signalSecondary = indicator
                ? `CRSI ${formatNumber(indicator.crsi)} · OBV ${formatNumber(indicator.obv_rsi)} · ATR ${formatPercent(indicator.atr_pct)}`
                : (isPreviewBar ? '预览 bar 收盘后生成 Osc 指标' : '--');
            const eventSecondary = [
                ...(Array.isArray(context?.tvEventMatches) ? context.tvEventMatches.slice(0, 2).map(getTvEventSummary) : []),
                ...(Array.isArray(context?.orderEventMatches) ? context.orderEventMatches.slice(0, 2).map(getOrderEventSummary) : []),
            ].join(' · ');
            const riskState = signal ? getRiskDistanceState(signal, getRiskReferencePrice(payload, context), bar) : null;
            const riskStats = signal ? getSignalRiskStats(payload, signal) : null;
            const riskPrimary = riskState?.valid
                ? formatRiskPriceLine(riskState)
                : (signal ? 'TP/SL 缺失' : '无信号风险位');
            const riskSecondary = riskState?.valid
                ? `${formatRiskDistanceLine(riskState)} · ${formatRiskWinRate(riskStats, { compact: true })}`
                : (signal ? '无法计算距离或胜率' : '聚焦信号后显示距离/胜率');
            document.getElementById('cursorStrip').innerHTML = [
                buildCursorCard('Bar Start (ET)', getIbkrBarStartLabel(bar), `Close ${getIbkrBarCloseLabel(bar)} · ${cursorState}`),
                buildCursorCard('OHLC', ohlc.primary, ohlc.secondary),
                buildCursorCard('EMA / VWAP', isPreviewBar && !indicator ? '预览 bar 暂无正式均线' : chain.primary, isPreviewBar && !indicator ? '收盘后生成 EMA / VWAP' : chain.secondary),
                buildCursorCard('SD / ORB', isPreviewBar && !indicator ? '预览 bar 暂无正式指标' : `Regime ${getSdRegimeText(indicator?.sd_regime)} · Z ${formatOptionalNumber(indicator?.sd_close_z)}`, isPreviewBar && !indicator ? '收盘后计算 SD / ORB' : `Width ${formatOptionalNumber(indicator?.sd_width_rank)} · ORB ${getOrbBreakoutText(indicator)}`),
                buildCursorCard('RVOL / Divergence', isPreviewBar && !indicator ? '预览中' : `RVOL20 ${formatOptionalNumber(indicator?.rvol_20)} · ${getTouchDetailText(indicator)}`, isPreviewBar && !indicator ? 'Touch / Div 待收盘确认' : getDivergenceDetailText(indicator)),
                buildCursorCard('TP / Runner / SL', riskPrimary, riskSecondary),
                buildCursorCard('Signal / Osc', isPreviewBar && !indicator ? '未收盘预览' : signalPrimary, eventSecondary || `${traceStage ? `Stage ${traceStage} · ` : ''}${signalSecondary}`),
            ].join('');
            renderChartWorkspaceChrome(payload, { includeTrace });
        }
        function renderLayerStrip() {
            const defs = getChartLayerDefs();
            document.getElementById('layerStrip').innerHTML = defs.map((item) => `
                <button class="layer-btn ${chartLayerState[item.key] ? 'active' : ''} ${item.disabled ? 'disabled' : ''}" type="button" onclick="toggleChartLayer('${item.key}')" ${item.disabled ? 'disabled' : ''}>
                    ${escapeHtml(item.label)}${item.disabled ? ' · 5m' : ''}
                </button>
            `).join('');
        }
