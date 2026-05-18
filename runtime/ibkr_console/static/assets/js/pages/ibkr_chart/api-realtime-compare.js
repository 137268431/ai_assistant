        async function fetchWithRetry(collection, params, maxRetries = 2) {
            let lastError = null;
            for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
                try {
                    return typeof cachedApiFetch === 'function'
                        ? await cachedApiFetch(collection, params, { ttlMs: 15000, ttl: 15000 })
                        : await apiFetch(collection, params);
                } catch (error) {
                    lastError = error;
                    const message = String(error?.message || error || '');
                    const retryable = message.includes('502') || message.includes('Failed to fetch');
                    if (!retryable || attempt === maxRetries) break;
                    await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
                }
            }
            throw lastError || new Error(`Failed to fetch ${collection}`);
        }

        function getAuthHeaders(extra = {}) {
            const headers = { ...extra };
            const token = getToken();
            if (token) headers.Authorization = `Bearer ${token}`;
            return headers;
        }

        async function requestChartJson(path, { method = 'GET', body = null } = {}, maxRetries = 1) {
            let lastError = null;
            for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
                try {
                    const options = {
                        method,
                        headers: getAuthHeaders(body ? { 'Content-Type': 'application/json' } : {})
                    };
                    if (body) options.body = JSON.stringify(body);
                    const response = await fetch(buildPageUrl(path, {}, { environment: currentEnvironment }), options);
                    if (response.status === 401 || response.status === 403) {
                        clearToken();
                        location.href = `/login.html?from=${encodeURIComponent(`${location.pathname}${location.search}`)}`;
                        throw new Error('Authentication failed');
                    }
                    const text = await response.text();
                    let data = {};
                    try {
                        data = text ? JSON.parse(text) : {};
                    } catch (_) {
                        data = { ok: false, raw: text };
                    }
                    if (!response.ok) {
                        throw new Error(data.message || data.error || `Request failed (${response.status})`);
                    }
                    if (data && data.ok === false) {
                        throw new Error(data.error || data.message || 'Request failed');
                    }
                    return data;
                } catch (error) {
                    lastError = error;
                    const message = String(error?.message || error || '');
                    const retryable = message.includes('502') || message.includes('Failed to fetch');
                    if (!retryable || attempt === maxRetries) break;
                    await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
                }
            }
            throw lastError || new Error(`Failed to request ${path}`);
        }

        async function requestChartRuntimeJson(path, params = {}, maxRetries = 1) {
            let lastError = null;
            const url = `${BASE_URL}${buildPageUrl(path, params, { environment: currentEnvironment })}`;
            for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
                try {
                    const response = await fetch(url, {
                        method: 'GET',
                        cache: 'no-store',
                        headers: getAuthHeaders()
                    });
                    if (response.status === 401 || response.status === 403) {
                        clearToken();
                        location.href = `/login.html?from=${encodeURIComponent(`${location.pathname}${location.search}`)}`;
                        throw new Error('Authentication failed');
                    }
                    const text = await response.text();
                    let data = {};
                    try {
                        data = text ? JSON.parse(text) : {};
                    } catch (_) {
                        data = { ok: false, raw: text };
                    }
                    if (!response.ok || data.ok === false) {
                        throw new Error(data.error || data.message || `Request failed (${response.status})`);
                    }
                    return data;
                } catch (error) {
                    lastError = error;
                    const message = String(error?.message || error || '');
                    const retryable = message.includes('502') || message.includes('Failed to fetch');
                    if (!retryable || attempt === maxRetries) break;
                    await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
                }
            }
            throw lastError || new Error(`Failed to request ${path}`);
        }

        function buildPreviewBarForTimeline() {
            if (currentInterval !== '5m' || !formingBarSnapshot?.bar_time_ms) return null;
            const previewBar = {
                ...formingBarSnapshot,
                symbol: formingBarSnapshot?.symbol || currentSymbol || '',
                preview: true,
                is_preview: true,
            };
            const livePrice = Number(realtimeQuoteSnapshot?.last_price);
            const previewOpen = Number(previewBar.open || 0);
            const previewHigh = Number(previewBar.high || 0);
            const previewLow = Number(previewBar.low || 0);
            const previewClose = Number.isFinite(livePrice) && livePrice > 0
                ? livePrice
                : Number(previewBar.close || 0);
            if (!Number.isFinite(previewClose) || previewClose <= 0) {
                return null;
            }
            previewBar.close = previewClose;
            if (previewOpen > 0) {
                previewBar.high = Math.max(previewHigh || previewOpen, previewOpen, previewClose);
                previewBar.low = Math.min(previewLow || previewOpen || previewClose, previewOpen, previewClose);
            }
            previewBar.volume = Number(previewBar.volume || 0);
            return previewBar;
        }

        async function fetchChartTimelinePayload({ previewBar = null } = {}) {
            const requestBounds = getChartRequestBounds({ previewBar });
            const response = await requestChartJson('/api/custom/ibkr/proxy', {
                method: 'POST',
                body: {
                    action: 'chart/timeline',
                    environment: currentEnvironment,
                    symbol: currentSymbol,
                    interval: currentInterval,
                    start_ms: requestBounds.startMs,
                    end_ms: requestBounds.endMs,
                    include_signals: currentInterval === '5m',
                    include_trace: true,
                    backtest_run_id: currentBacktestRunId,
                    preview_bar: requestBounds.previewBar || undefined,
                }
            }, 2);
            return buildTimelinePayloadFromResponse(response);
        }

        async function refreshComputedDisplayPayload() {
            if (!lastPayload) {
                realtimeComputedPayload = null;
                return;
            }
            if (isCustomRange()) {
                realtimeComputedPayload = null;
                return;
            }
            const previewBar = buildPreviewBarForTimeline();
            if (!previewBar) {
                realtimeComputedPayload = null;
                return;
            }
            try {
                realtimeComputedPayload = await fetchChartTimelinePayload({ previewBar });
            } catch (error) {
                realtimeComputedPayload = null;
                warnChartRealtimeOnce('preview', '重算实时 preview 指标失败', error);
            }
        }

        function stopChartRealtimePolling() {
            if (chartRealtimePollTimer) {
                clearInterval(chartRealtimePollTimer);
                chartRealtimePollTimer = 0;
            }
        }

        function resetChartRealtimeWarnings() {
            chartRealtimeWarnState.quote = { key: '', at: 0 };
            chartRealtimeWarnState.preview = { key: '', at: 0 };
        }

        function warnChartRealtimeOnce(channel, label, error) {
            const state = chartRealtimeWarnState[channel];
            if (!state) {
                console.warn(`${label}:`, error);
                return;
            }
            const message = String(error?.message || error || 'unknown error');
            const key = `${label}:${message}`;
            const now = Date.now();
            if (state.key === key && now - state.at < CHART_REALTIME_WARN_INTERVAL_MS) {
                return;
            }
            state.key = key;
            state.at = now;
            if (message.includes('Failed to fetch')) {
                return;
            }
            console.warn(`${label}:`, error);
        }

        async function refreshChartRealtimeState({ render = false } = {}) {
            if (!currentSymbol) return;
            const previousQuote = realtimeQuoteSnapshot && Number.isFinite(Number(realtimeQuoteSnapshot?.last_price))
                ? { ...realtimeQuoteSnapshot }
                : null;
            try {
                await fetchRealtimeQuotes([currentSymbol], { reset: false });
                let nextQuote = getRealtimeQuote(currentSymbol);
                if (!(Number.isFinite(Number(nextQuote?.last_price)) && Number(nextQuote.last_price) > 0)) {
                    await fetchRealtimeQuotes([currentSymbol], { reset: true });
                    nextQuote = getRealtimeQuote(currentSymbol);
                }
                if (Number.isFinite(Number(nextQuote?.last_price)) && Number(nextQuote.last_price) > 0) {
                    realtimeQuoteSnapshot = nextQuote;
                    chartRealtimeQuoteError = '';
                    chartRealtimeQuoteFailureCount = 0;
                } else if (previousQuote) {
                    realtimeQuoteSnapshot = previousQuote;
                    chartRealtimeQuoteError = `quotes response missing ${currentSymbol}`;
                    chartRealtimeQuoteFailureCount += 1;
                    warnChartRealtimeOnce('quote', '当前 symbol 未返回实时报价，沿用上次快照', chartRealtimeQuoteError);
                } else {
                    realtimeQuoteSnapshot = null;
                    chartRealtimeQuoteError = `quotes response missing ${currentSymbol}`;
                    chartRealtimeQuoteFailureCount += 1;
                    warnChartRealtimeOnce('quote', '当前 symbol 未返回实时报价', chartRealtimeQuoteError);
                }
            } catch (error) {
                if (previousQuote) {
                    realtimeQuoteSnapshot = previousQuote;
                }
                chartRealtimeQuoteError = String(error?.message || error || 'unknown error');
                chartRealtimeQuoteFailureCount += 1;
                warnChartRealtimeOnce('quote', '加载实时价格失败', error);
            }

            formingBarSnapshot = null;
            chartRealtimePreviewError = '';
            if (currentInterval === '5m') {
                try {
                    const payload = await requestChartRuntimeJson('/api/custom/ibkr/quotes/forming_bar', {
                        symbol: currentSymbol,
                    });
                    formingBarSnapshot = payload?.preview ? payload.bar || null : null;
                    chartRealtimePreviewError = '';
                } catch (error) {
                    chartRealtimePreviewError = String(error?.message || error || 'unknown error');
                    warnChartRealtimeOnce('preview', '加载未收盘预览 bar 失败', error);
                }
            }

            await refreshComputedDisplayPayload();

            if (render && lastPayload) {
                renderChart(lastPayload);
                const activePayload = getChartDisplayPayload() || lastPayload;
                renderHeroStatus(activePayload);
                renderSummaryStrip(activePayload);
            }
        }

        function startChartRealtimePolling() {
            stopChartRealtimePolling();
            if (!currentSymbol) return;
            refreshChartRealtimeState({ render: true });
            chartRealtimePollTimer = window.setInterval(() => {
                if (document.hidden) return;
                refreshChartRealtimeState({ render: true });
            }, 3000);
        }

        function buildTimelinePayloadFromResponse(response) {
            const bars = Array.isArray(response?.bars)
                ? response.bars.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0))
                : [];
            const indicators = Array.isArray(response?.indicator_timeline)
                ? response.indicator_timeline.map((item) => normalizeIndicatorRecord(item)).sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0))
                : [];
            const signals = Array.isArray(response?.signals)
                ? response.signals.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0))
                : [];
            const traceTimeline = Array.isArray(response?.trace_timeline)
                ? response.trace_timeline.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0))
                : [];
            const backtestEvents = Array.isArray(response?.backtest_events)
                ? response.backtest_events.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0))
                : [];
            const riskStats = response?.risk_stats && typeof response.risk_stats === 'object'
                ? response.risk_stats
                : {};
            const latestIndicator = normalizeIndicatorRecord(response?.latest_indicator || null) || (indicators.length ? indicators[indicators.length - 1] : null);
            return {
                latestIndicator,
                bars,
                indicators,
                signals,
                traceTimeline,
                backtestEvents,
                riskStats,
                meta: response?.meta && typeof response.meta === 'object' ? response.meta : {}
            };
        }

        function getChartDisplayPayload() {
            return chartDisplayPayload || lastPayload;
        }

        function buildChartDisplayPayload(payload) {
            if (!payload || typeof payload !== 'object') return null;
            if (realtimeComputedPayload && Array.isArray(realtimeComputedPayload?.bars) && realtimeComputedPayload.bars.length) {
                return realtimeComputedPayload;
            }
            const baseBars = Array.isArray(payload?.bars) ? payload.bars.slice() : [];
            if (!baseBars.length || isCustomRange() || currentInterval !== '5m' || !formingBarSnapshot?.bar_time_ms) {
                return payload;
            }
            const previewBarTimeMs = Number(formingBarSnapshot.bar_time_ms || 0);
            const latestFormalBarMs = Number(baseBars[baseBars.length - 1]?.bar_time_ms || 0);
            if (!previewBarTimeMs || (latestFormalBarMs && previewBarTimeMs < latestFormalBarMs)) {
                return payload;
            }

            const matchedBarIndex = baseBars.findIndex((bar) => Number(bar?.bar_time_ms || 0) === previewBarTimeMs);
            const baseBar = matchedBarIndex >= 0 ? baseBars[matchedBarIndex] : null;
            const previewBar = {
                ...(baseBar || {}),
                ...formingBarSnapshot,
                symbol: formingBarSnapshot?.symbol || currentSymbol || '',
                preview: true,
                is_preview: true,
            };
            const livePrice = Number(realtimeQuoteSnapshot?.last_price);
            const previewOpen = Number(previewBar.open || 0);
            const previewHigh = Number(previewBar.high || 0);
            const previewLow = Number(previewBar.low || 0);
            const previewClose = Number.isFinite(livePrice) && livePrice > 0
                ? livePrice
                : Number(previewBar.close || 0);
            if (!Number.isFinite(previewClose) || previewClose <= 0) {
                return payload;
            }

            previewBar.close = previewClose;
            if (previewOpen > 0) {
                previewBar.high = Math.max(previewHigh || previewOpen, previewOpen, previewClose);
                previewBar.low = Math.min(previewLow || previewOpen || previewClose, previewOpen, previewClose);
            }
            previewBar.volume = Number(previewBar.volume || 0);
            const nextBars = baseBars.slice();
            if (matchedBarIndex >= 0) {
                nextBars[matchedBarIndex] = previewBar;
            } else {
                nextBars.push(previewBar);
            }
            return {
                ...payload,
                bars: nextBars,
                preview_bar: previewBar,
            };
        }

        function buildComparePayloadFromResponse(response) {
            const comparison = response?.comparison && typeof response.comparison === 'object'
                ? response.comparison
                : { summary: {}, timeline: [], mismatch_examples: [] };
            const timeline = Array.isArray(comparison.timeline)
                ? comparison.timeline.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0))
                : [];
            return {
                storedTimeline: buildTimelinePayloadFromResponse(response?.stored_timeline || {}),
                ibkrTimeline: buildTimelinePayloadFromResponse(response?.ibkr_timeline || {}),
                comparison: {
                    summary: comparison.summary || {},
                    timeline,
                    mismatch_examples: Array.isArray(comparison.mismatch_examples) ? comparison.mismatch_examples : []
                },
                meta: response?.meta && typeof response.meta === 'object' ? response.meta : {},
                rowByMs: new Map(
                    timeline
                        .map((item) => [Number(item?.bar_time_ms || 0), item])
                        .filter(([barTimeMs]) => barTimeMs > 0)
                )
            };
        }

        function isCompareSupportedRange() {
            if (normalizeRangeKey(currentRangeKey, currentInterval) === 'all') return false;
            if (isCustomRange()) return Boolean(customRangeStartMs && customRangeEndMs && customRangeEndMs > customRangeStartMs);
            return true;
        }

        function renderCompareButtons() {
            const compareBtn = document.getElementById('compareBtn');
            const clearCompareBtn = document.getElementById('clearCompareBtn');
            const hasBars = Boolean(
                currentSymbol
                && lastPayload
                && Array.isArray(lastPayload.bars)
                && lastPayload.bars.length
            );
            const canCompare = Boolean(hasBars && isCompareSupportedRange() && !compareLoading);
            if (compareBtn) {
                compareBtn.textContent = compareLoading ? 'IBKR 对比中...' : (comparePayload ? '重新对比' : 'IBKR 对比');
                compareBtn.hidden = !compareLoading && !canCompare;
                compareBtn.disabled = compareLoading || !canCompare;
                compareBtn.title = isCompareSupportedRange() ? '' : '当前 v1 不支持 ALL 范围的 IBKR 对比';
            }
            if (clearCompareBtn) {
                clearCompareBtn.hidden = !comparePayload && !compareError;
                clearCompareBtn.disabled = compareLoading;
            }
        }

        function resetCompareState({ keepError = false } = {}) {
            comparePayload = null;
            compareLoading = false;
            if (!keepError) compareError = '';
            renderCompareButtons();
        }

        function getCompareRowByBarTime(barTimeMs) {
            const target = Number(barTimeMs || 0);
            if (!comparePayload || !target) return null;
            return comparePayload.rowByMs.get(target) || null;
        }

        function getCompareStatusLabel(status) {
            const key = String(status || '').trim().toLowerCase();
            if (key === 'match') return '一致';
            if (key === 'mismatch') return '不一致';
            if (key === 'missing_stored') return 'Stored 缺失';
            if (key === 'missing_ibkr') return 'IBKR 缺失';
            if (key === 'loading') return '对比中';
            return '未对比';
        }

        function getCompareStatusClass(status) {
            const key = String(status || '').trim().toLowerCase();
            if (['match', 'mismatch', 'missing_stored', 'missing_ibkr'].includes(key)) return key;
            return '';
        }

        function buildCompareStatusPills(statusMap) {
            if (!statusMap || typeof statusMap !== 'object') return '';
            const defs = [
                { key: 'bar', label: 'Bars' },
                { key: 'indicator', label: 'Ind' },
                { key: 'signal', label: 'Signal' },
            ];
            return defs.map((item) => `
                <span class="compare-pill ${getCompareStatusClass(statusMap[item.key])}">
                    ${escapeHtml(item.label)} ${escapeHtml(getCompareStatusLabel(statusMap[item.key]))}
                </span>
            `).join('');
        }

        function summarizeCompareFields(group, emptyText = '一致') {
            if (!group || typeof group !== 'object') return emptyText;
            const status = String(group.status || '').trim().toLowerCase();
            if (status === 'match') return emptyText;
            if (status === 'missing_stored') return 'Stored 缺失';
            if (status === 'missing_ibkr') return 'IBKR 缺失';
            if (status === 'absent') return '无数据';
            const fields = Array.isArray(group.fields) ? group.fields : [];
            return fields.length ? fields.join(', ') : emptyText;
        }

        function buildCompareDiffCopy(compareRow) {
            if (!compareRow || typeof compareRow !== 'object') return '尚未运行 IBKR 对比。';
            const barText = summarizeCompareFields(compareRow?.diff?.bar, 'Bars 一致');
            const indicatorText = summarizeCompareFields(compareRow?.diff?.indicator, 'Indicators 一致');
            const signalText = summarizeCompareFields(compareRow?.diff?.signal, 'Signals 一致');
            return `Bars: ${barText}<br>Indicators: ${indicatorText}<br>Signals: ${signalText}`;
        }

        function formatCompareBarSummary(bar) {
            if (!bar) return '--';
            return `${escapeHtml(formatPrice(bar.open))} / ${escapeHtml(formatPrice(bar.high))}<br>${escapeHtml(formatPrice(bar.low))} / ${escapeHtml(formatPrice(bar.close))}<br>Vol ${escapeHtml(formatNumber(bar.volume || 0, 0))}`;
        }

        function formatCompareChainSummary(indicator, signal) {
            const line1 = indicator
                ? `EMA20 ${escapeHtml(formatPrice(indicator.ema_fast))} · EMA50 ${escapeHtml(formatPrice(indicator.ema_slow))}`
                : 'EMA --';
            const line2 = indicator
                ? `VWAP ${escapeHtml(formatPrice(indicator.vwap))} · CRSI ${escapeHtml(formatNumber(indicator.crsi))}`
                : 'VWAP / CRSI --';
            const line3 = indicator
                ? `OBV ${escapeHtml(formatNumber(indicator.obv_rsi))} · ATR ${escapeHtml(formatPercent(indicator.atr_pct))}`
                : 'OBV / ATR --';
            const line4 = signal
                ? `${escapeHtml(buildTradeSignalLabel(signal))} · ${escapeHtml(String(signal.direction || '--').toUpperCase())}`
                : '无信号';
            return `${line1}<br>${line2}<br>${line3}<br>${line4}`;
        }

        function buildCompareErrorCard() {
            if (!compareError) return '';
            return `
                <div class="rail-card">
                    <div class="rail-kicker">IBKR Compare</div>
                    <div class="rail-value" style="font-size:18px;color:#fecaca;">对比失败</div>
                    <div class="rail-sub">${escapeHtml(compareError)}</div>
                </div>
            `;
        }

        function isComputedSignal(signal) {
            const status = String(signal?.status || '').trim().toLowerCase();
            const extra = getSignalExtra(signal);
            const sourceKind = String(signal?.source_kind || extra?.source_kind || '').trim().toLowerCase();
            return status === 'computed' || sourceKind === 'computed';
        }

        async function resolveInitialContext() {
            const query = parseQueryContext();
            const hasExplicitRange = RANGE_PRESETS.some((item) => item.key === query.range);
            currentIndicatorId = query.indicatorId;
            currentSymbol = query.symbol;
            currentInterval = query.interval;
            currentBacktestRunId = query.backtestRunId;
            currentRangeKey = normalizeRangeKey(query.range, currentInterval);
            customRangeStartMs = Number(query.customStartMs || 0) || 0;
            customRangeEndMs = Number(query.customEndMs || 0) || 0;
            currentAnchorMs = isCustomRange(currentRangeKey) && customRangeEndMs ? customRangeEndMs : 0;
            pendingFocusBarTimeMs = query.barTimeMs;
            if (query.traceOpen) {
                chartTracePanelOpen = true;
            }

            if (currentIndicatorId) {
                try {
                    const indicatorResp = await fetchWithRetry('ibkr_indicators', {
                        filter: `id = ${quoteFilterValue(currentIndicatorId)} && environment = ${quoteFilterValue(currentEnvironment)}`,
                        perPage: 1
                    });
                    const indicator = normalizeIndicatorRecord(Array.isArray(indicatorResp?.items) ? indicatorResp.items[0] : null);
                    if (indicator) {
                        currentSymbol = String(indicator.symbol || currentSymbol).trim().toUpperCase();
                        currentInterval = normalizeInterval(indicator.interval || currentInterval, currentInterval || '5m');
                        if (!hasExplicitRange) currentRangeKey = getDefaultRangeKey(currentInterval);
                        pendingFocusBarTimeMs = Number(indicator.bar_time_ms || pendingFocusBarTimeMs) || pendingFocusBarTimeMs;
                        return;
                    }
                } catch (_) {}
            }

            if (currentSymbol) return;

            const latestBarResp = await fetchWithRetry('ibkr_bars', {
                filter: `environment = ${quoteFilterValue(currentEnvironment)} && ${buildIntervalFilterExpression('5m', ['interval'])}`,
                sort: '-bar_time_ms',
                perPage: 1
            }).catch(() => ({ items: [] }));
            const latestBar = Array.isArray(latestBarResp?.items) ? latestBarResp.items[0] : null;
            currentSymbol = String(latestBar?.symbol || 'SPY').trim().toUpperCase();
            currentInterval = normalizeInterval(latestBar?.interval || '5m');
            if (!hasExplicitRange) currentRangeKey = getDefaultRangeKey(currentInterval);
            currentAnchorMs = Number(latestBar?.bar_time_ms || 0) || 0;
        }
