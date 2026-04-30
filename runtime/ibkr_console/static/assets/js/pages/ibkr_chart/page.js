const SUPPORTED_INTERVALS = ['5m', '15m', '30m', '1h', '4h', '1d'];
        const CHART_UI_PREFS_KEY = 'ibkr_chart_ui_prefs_v2';
        const CHART_MOBILE_HINT_KEY = 'ibkr_chart_mobile_hint_seen_v1';
        const RANGE_PRESETS = [
            { key: '1d', shortLabel: '1D', longLabel: '最近1天', durationMs: 24 * 60 * 60 * 1000 },
            { key: '3d', shortLabel: '3D', longLabel: '最近3天', durationMs: 3 * 24 * 60 * 60 * 1000 },
            { key: '1w', shortLabel: '1W', longLabel: '最近1周', durationMs: 7 * 24 * 60 * 60 * 1000 },
            { key: '1m', shortLabel: '1M', longLabel: '最近1月', durationMs: 31 * 24 * 60 * 60 * 1000 },
            { key: 'custom', shortLabel: '自定义', longLabel: '自定义范围', durationMs: null },
            { key: 'all', shortLabel: 'ALL', longLabel: '全部历史', durationMs: 0 },
        ];
        let currentEnvironment = getCurrentRuntimeEnvironment();
        let currentSymbol = '';
        let currentInterval = '5m';
        let currentRangeKey = '1d';
        let currentAnchorMs = 0;
        let customRangeStartMs = 0;
        let customRangeEndMs = 0;
        let pendingFocusBarTimeMs = 0;
        let currentIndicatorId = '';
        let chartInstance = null;
        let lastPayload = null;
        let chartDisplayPayload = null;
        let chartWorkspaceState = 'idle';
        let realtimeQuoteSnapshot = null;
        let formingBarSnapshot = null;
        let chartRealtimeQuoteError = '';
        let chartRealtimeQuoteFailureCount = 0;
        let chartRealtimePreviewError = '';
        let chartRealtimePollTimer = 0;
        let comparePayload = null;
        let compareLoading = false;
        let compareError = '';
        let realtimeComputedPayload = null;
        let selectedBarIndex = -1;
        let selectedSignalId = '';
        let hoverBarIndex = -1;
        let activeDrawerSignalId = '';
        let chartZoomState = null;
        let chartViewportState = { start: 0, end: 100, startIndex: 0, endIndex: 0, visibleBars: 0, totalBars: 0 };
        let activeRailCardIndex = 0;
        let mobileQuickPanelOpen = false;
        let chartFocusMode = false;
        let chartPointerLocked = false;
        let inspectorDrawerOpen = false;
        let chartTracePanelOpen = false;
        let chartLegendCollapsed = true;
        let chartMarkerDensityTier = '';
        let chartTooltipSyncRaf = 0;
        let chartTooltipSyncTimer = 0;
        let chartTooltipPinned = false;
        let chartFocusMarkerSyncRaf = 0;
        let mobileGestureHintSeen = false;
        let suppressNextChartClick = false;
        let chartTouchSession = {
            timer: 0,
            startX: 0,
            startY: 0,
            moved: false,
            lastTapAt: 0,
            lastTapX: 0,
            lastTapY: 0,
        };
        let chartLayerState = {
            ema: true,
            vwap: true,
            sdChannel: true,
            fractal: true,
            emaTouch: true,
            divergence: true,
            tradeSignals: true,
            volume: true,
        };
        const CHART_REALTIME_WARN_INTERVAL_MS = 60 * 1000;
        const chartRealtimeWarnState = {
            quote: { key: '', at: 0 },
            preview: { key: '', at: 0 },
        };

        function escapeHtml(value) {
            return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function normalizeInterval(value, fallback = '5m') {
            const text = String(value || '').trim().toLowerCase();
            const map = {
                '5': '5m',
                '5m': '5m',
                '15': '15m',
                '15m': '15m',
                '30': '30m',
                '30m': '30m',
                '60': '1h',
                '60m': '1h',
                '1h': '1h',
                '1hr': '1h',
                '240': '4h',
                '240m': '4h',
                '4h': '4h',
                '1d': '1d',
                'd': '1d',
                'day': '1d'
            };
            const normalized = map[text] || text;
            return SUPPORTED_INTERVALS.includes(normalized) ? normalized : fallback;
        }

        function getDefaultRangeKey(interval) {
            const normalized = normalizeInterval(interval || '5m');
            const defaults = {
                '5m': '1d',
                '15m': '3d',
                '30m': '1w',
                '1h': '1m',
                '4h': '1m',
                '1d': '1m',
            };
            return defaults[normalized] || '1d';
        }

        function isCustomRange(rangeKey = currentRangeKey) {
            return String(rangeKey || '').trim().toLowerCase() === 'custom';
        }

        function normalizeRangeKey(value, interval = currentInterval) {
            const key = String(value || '').trim().toLowerCase();
            const exists = RANGE_PRESETS.some((item) => item.key === key);
            return exists ? key : getDefaultRangeKey(interval);
        }

        function getRangePreset(rangeKey) {
            return RANGE_PRESETS.find((item) => item.key === normalizeRangeKey(rangeKey, currentInterval)) || RANGE_PRESETS[0];
        }

        function getRangeLabel(rangeKey) {
            if (isCustomRange(rangeKey)) {
                if (customRangeStartMs && customRangeEndMs) {
                    return `自定义 ${formatCustomRangeTime(customRangeStartMs)} → ${formatCustomRangeTime(customRangeEndMs)}`;
                }
                return '自定义范围';
            }
            return getRangePreset(rangeKey)?.longLabel || '最近1天';
        }

        function getRangeShortLabel(rangeKey) {
            if (isCustomRange(rangeKey)) return '自定义';
            return getRangePreset(rangeKey)?.shortLabel || String(rangeKey || '--').toUpperCase();
        }

        function getRangeStartMs(endMs, rangeKey) {
            if (isCustomRange(rangeKey)) return Math.max(0, Number(customRangeStartMs || 0));
            const durationMs = Number(getRangePreset(rangeKey)?.durationMs || 0);
            return durationMs > 0 && endMs > 0 ? Math.max(0, endMs - durationMs) : 0;
        }

        function padDatePart(value) {
            return String(value).padStart(2, '0');
        }

        function formatDateTimeLocalValue(ms) {
            const num = Number(ms || 0);
            if (!Number.isFinite(num) || num <= 0) return '';
            const date = new Date(num);
            return `${date.getFullYear()}-${padDatePart(date.getMonth() + 1)}-${padDatePart(date.getDate())}T${padDatePart(date.getHours())}:${padDatePart(date.getMinutes())}`;
        }

        function parseDateTimeLocalValue(value) {
            const text = String(value || '').trim();
            if (!text) return 0;
            const ms = new Date(text).getTime();
            return Number.isFinite(ms) ? ms : 0;
        }

        function parseQueryTimeMs(value) {
            const text = String(value || '').trim();
            if (!text) return 0;
            if (/^\d{10,13}$/.test(text)) {
                const num = Number(text);
                return text.length === 10 ? num * 1000 : num;
            }
            const parsed = new Date(text).getTime();
            return Number.isFinite(parsed) ? parsed : 0;
        }

        function formatCustomRangeTime(ms) {
            const num = Number(ms || 0);
            if (!Number.isFinite(num) || num <= 0) return '--';
            if (typeof formatBarTimeMsToET === 'function') return formatBarTimeMsToET(num);
            return new Date(num).toISOString().slice(0, 16).replace('T', ' ');
        }

        function getChartRequestBounds({ previewBar = null } = {}) {
            if (isCustomRange()) {
                const fallbackEndMs = Number(customRangeEndMs || currentAnchorMs || Date.now()) || Date.now();
                return {
                    startMs: Math.max(0, Number(customRangeStartMs || 0)),
                    endMs: fallbackEndMs,
                    previewBar: null,
                };
            }
            const previewBarMs = Number(previewBar?.bar_time_ms || 0) || 0;
            const endMs = Math.max(Number(currentAnchorMs || 0), previewBarMs, Date.now());
            return {
                startMs: getRangeStartMs(endMs, currentRangeKey),
                endMs,
                previewBar,
            };
        }

        function buildIntervalAliases(interval) {
            const normalized = normalizeInterval(interval || '5m');
            const map = {
                '5m': ['5m', '5'],
                '15m': ['15m', '15'],
                '30m': ['30m', '30'],
                '1h': ['1h', '60', '60m', '1hr'],
                '4h': ['4h', '240', '240m'],
                '1d': ['1d', 'd', 'day'],
            };
            return map[normalized] || [normalized];
        }

        function quoteFilterValue(value) {
            return `'${String(value ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
        }

        function buildIntervalFilterExpression(interval, fields = ['interval']) {
            const conditions = [];
            buildIntervalAliases(interval).forEach((alias) => {
                fields.forEach((field) => {
                    conditions.push(`${field} = ${quoteFilterValue(alias)}`);
                });
            });
            return `(${conditions.join(' || ')})`;
        }

        function toChartValue(value) {
            if (value === null || value === undefined || value === '') return null;
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        }

        function parseRecordExtra(extra) {
            if (!extra) return {};
            if (typeof extra === 'object') return extra;
            if (typeof extra === 'string') {
                try {
                    const parsed = JSON.parse(extra);
                    return parsed && typeof parsed === 'object' ? parsed : {};
                } catch (_) {
                    return {};
                }
            }
            return {};
        }

        function normalizeIndicatorRecord(record) {
            if (!record || typeof record !== 'object') return record;
            const extra = parseRecordExtra(record.extra);
            const normalized = { ...record };
            Object.entries(extra).forEach(([key, value]) => {
                if (normalized[key] === undefined || normalized[key] === null || normalized[key] === '') {
                    normalized[key] = value;
                }
            });
            normalized._extra = extra;
            return normalized;
        }

        function getInitialZoom(rangeKey, barsCount) {
            if (!barsCount) return { start: 0, end: 100 };
            if (normalizeRangeKey(rangeKey, currentInterval) !== 'all') return { start: 0, end: 100 };
            const visibleBars = Math.min(240, barsCount);
            const start = Math.max(0, 100 - (visibleBars / barsCount) * 100);
            return { start, end: 100 };
        }

        function clampNumber(value, min, max) {
            const num = Number(value);
            if (!Number.isFinite(num)) return min;
            return Math.min(Math.max(num, min), max);
        }

        function clampIndex(index, barsCount) {
            if (!barsCount) return 0;
            return Math.min(Math.max(Math.round(Number(index) || 0), 0), barsCount - 1);
        }

        function percentToIndex(percent, barsCount) {
            if (!barsCount) return 0;
            if (barsCount === 1) return 0;
            return clampIndex((clampNumber(percent, 0, 100) / 100) * (barsCount - 1), barsCount);
        }

        function indexToPercent(index, barsCount) {
            if (!barsCount || barsCount === 1) return 0;
            return clampNumber((clampIndex(index, barsCount) / (barsCount - 1)) * 100, 0, 100);
        }

        function applyChartViewportState(zoom, barsCount) {
            const fallback = getInitialZoom(currentRangeKey, barsCount);
            const start = clampNumber(zoom?.start ?? fallback.start, 0, 100);
            const end = clampNumber(zoom?.end ?? fallback.end, 0, 100);
            const safeEnd = end <= start ? Math.min(100, start + 6) : end;
            const startIndex = percentToIndex(start, barsCount);
            const endIndex = percentToIndex(safeEnd, barsCount);
            chartZoomState = { start, end: safeEnd };
            chartViewportState = {
                start,
                end: safeEnd,
                startIndex,
                endIndex,
                visibleBars: Math.max(1, endIndex - startIndex + 1),
                totalBars: barsCount,
            };
            return chartViewportState;
        }

        function getCurrentZoomWindow(barsCount) {
            return applyChartViewportState(chartZoomState || getInitialZoom(currentRangeKey, barsCount), barsCount);
        }

        function isCompactViewport() {
            return Number(window.innerWidth || 0) <= 768;
        }

        function isPhoneViewport() {
            return Number(window.innerWidth || 0) <= 520;
        }

        function isDesktopFinePointer() {
            return typeof window.matchMedia === 'function'
                && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
        }

        function loadChartUiPrefs() {
            try {
                const raw = localStorage.getItem(CHART_UI_PREFS_KEY);
                if (!raw) return;
                const prefs = JSON.parse(raw);
                if (prefs && typeof prefs === 'object') {
                    if (prefs.layerState && typeof prefs.layerState === 'object') {
                        chartLayerState = {
                            ...chartLayerState,
                            ...Object.fromEntries(
                                Object.entries(prefs.layerState).map(([key, value]) => [key, Boolean(value)])
                            )
                        };
                    }
                    if (typeof prefs.focusMode === 'boolean') {
                        chartFocusMode = prefs.focusMode;
                    }
                    if (typeof prefs.legendCollapsed === 'boolean') {
                        chartLegendCollapsed = prefs.legendCollapsed;
                    }
                    if (typeof prefs.tracePanelOpen === 'boolean') {
                        chartTracePanelOpen = prefs.tracePanelOpen;
                    }
                }
            } catch (_) {}
        }

        function saveChartUiPrefs() {
            try {
                localStorage.setItem(CHART_UI_PREFS_KEY, JSON.stringify({
                    layerState: chartLayerState,
                    focusMode: chartFocusMode,
                    legendCollapsed: chartLegendCollapsed,
                    tracePanelOpen: chartTracePanelOpen,
                }));
            } catch (_) {}
        }

        function loadMobileGestureHintState() {
            try {
                mobileGestureHintSeen = localStorage.getItem(CHART_MOBILE_HINT_KEY) === '1';
            } catch (_) {
                mobileGestureHintSeen = false;
            }
        }

        function applyChartFocusMode() {
            document.body.classList.toggle('chart-focus-mode', chartFocusMode);
            if (lastPayload) renderCursorStrip(getChartDisplayPayload());
            else renderChartWorkspaceChrome(null);
            window.requestAnimationFrame(() => {
                if (chartInstance) chartInstance.resize();
            });
        }

        function setChartFocusMode(enabled) {
            chartFocusMode = Boolean(enabled);
            saveChartUiPrefs();
            applyChartFocusMode();
        }

        function toggleChartFocusMode() {
            setChartFocusMode(!chartFocusMode);
        }

        function getIntervalLabel(interval) {
            const map = {
                '5m': '5分钟',
                '15m': '15分钟',
                '30m': '30分钟',
                '1h': '1小时',
                '4h': '4小时',
                '1d': '1天',
            };
            return map[String(interval || '').trim().toLowerCase()] || String(interval || '--');
        }

        function isTradeSignalInterval(interval = currentInterval) {
            return normalizeInterval(interval || currentInterval) === '5m';
        }

        function getChartLayerDefs() {
            return [
                { key: 'ema', label: 'EMA', shortLabel: 'EMA', disabled: false, swatches: ['#38BDF8', '#F59E0B', '#A78BFA'], description: 'EMA20 / EMA50 / EMA100' },
                { key: 'vwap', label: 'VWAP', shortLabel: 'VWAP', disabled: false, swatches: ['#34D399'], description: 'VWAP 主线' },
                { key: 'sdChannel', label: 'SD Channel', shortLabel: 'SD', disabled: false, swatches: ['#7DD3FC', '#F87171', '#4ADE80'], description: '标准差回归线与上下轨' },
                { key: 'fractal', label: 'Fractal', shortLabel: 'Frac', disabled: false, swatches: ['#14B8A6', '#F44336'], description: '上下分形标记' },
                { key: 'emaTouch', label: 'EMA Touch', shortLabel: 'Touch', disabled: false, swatches: ['#00C853', '#D50000'], description: 'EMA touch 多空提示' },
                { key: 'divergence', label: 'Divergence', shortLabel: 'Div', disabled: false, swatches: ['#2196F3', '#9C27B0'], description: '背离提示标记' },
                { key: 'tradeSignals', label: 'Trade Signals', shortLabel: 'Signal', disabled: !isTradeSignalInterval(), swatches: ['#48BB78', '#FC8181'], description: '多空交易信号' },
                { key: 'volume', label: 'Volume', shortLabel: 'Vol', disabled: false, swatches: ['#38BDF8'], description: '成交量柱体' },
            ].filter((item) => !item.disabled);
        }

        function formatPrice(value) {
            const num = Number(value);
            return Number.isFinite(num) ? `$${num.toFixed(2)}` : '--';
        }

        function formatNumber(value, digits = 2) {
            const num = Number(value);
            return Number.isFinite(num) ? num.toFixed(digits) : '--';
        }

        function formatPercent(value, digits = 2) {
            const num = Number(value);
            return Number.isFinite(num) ? `${num > 0 ? '+' : ''}${num.toFixed(digits)}%` : '--';
        }

        function formatSignedPercent(value, digits = 2) {
            return formatPercent(value, digits);
        }

        function formatCompactAxisNumber(value) {
            const num = Number(value);
            if (!Number.isFinite(num)) return '--';
            const abs = Math.abs(num);
            const formatScaled = (scaled, suffix) => {
                const precision = Math.abs(scaled) >= 100 ? 0 : (Math.abs(scaled) >= 10 ? 1 : 2);
                return `${scaled.toFixed(precision).replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1')}${suffix}`;
            };
            if (abs >= 1e9) return formatScaled(num / 1e9, 'B');
            if (abs >= 1e6) return formatScaled(num / 1e6, 'M');
            if (abs >= 1e3) return formatScaled(num / 1e3, 'K');
            return `${Math.round(num)}`;
        }

        function signedColor(value) {
            const num = Number(value);
            if (!Number.isFinite(num) || num === 0) return 'var(--text)';
            return num > 0 ? 'var(--long)' : 'var(--short)';
        }

        function formatSignedPriceDelta(value) {
            const num = Number(value);
            return Number.isFinite(num) ? `${num > 0 ? '+' : ''}${formatPrice(num).replace('$', '$')}` : '--';
        }

        function getTrendText(value) {
            const num = Number(value || 0);
            if (num > 0) return '多头';
            if (num < 0) return '空头';
            return '中性';
        }

        function getSdZoneText(value) {
            const num = Number(value);
            if (!Number.isFinite(num)) return '--';
            if (num === 1) return '超买';
            if (num === -1) return '超卖';
            return '正常';
        }

        function getSdTrendText(value) {
            const num = Number(value);
            if (!Number.isFinite(num)) return '--';
            if (num > 0) return '上升';
            if (num < 0) return '下降';
            return '平坦';
        }

        function getFractalSummary(indicator) {
            if (!indicator) return '--';
            const tags = [];
            if (indicator.fractal_bull) tags.push('F↑');
            if (indicator.fractal_bear) tags.push('F↓');
            return tags.length ? tags.join(' / ') : '无';
        }

        function getFractalStateText(indicator) {
            if (!indicator) return '--';
            const tags = [];
            if (indicator.fractal_bull) tags.push('分形多');
            if (indicator.fractal_bear) tags.push('分形空');
            return tags.length ? tags.join(' / ') : '无';
        }

        function getEmaStructureText(indicator) {
            if (!indicator) return '--';
            if (indicator.ema_bullish) return '多头';
            if (indicator.ema_bearish) return '空头';
            return '中性';
        }

        function getTouchSummary(indicator) {
            if (!indicator) return '--';
            const tags = [];
            if (indicator.bull_touch_fast) tags.push('E↑F');
            if (indicator.bull_touch_slow) tags.push('E↑S');
            if (indicator.bear_touch_fast) tags.push('E↓F');
            if (indicator.bear_touch_slow) tags.push('E↓S');
            return tags.length ? tags.join(' / ') : '无';
        }

        function getTouchDetailText(indicator) {
            if (!indicator) return '--';
            const tags = [];
            if (indicator.bull_touch_fast) tags.push('多头快线');
            if (indicator.bull_touch_slow) tags.push('多头慢线');
            if (indicator.bear_touch_fast) tags.push('空头快线');
            if (indicator.bear_touch_slow) tags.push('空头慢线');
            return tags.length ? tags.join(' / ') : '无';
        }

        function getIndicatorDivergenceTokens(indicator) {
            if (!indicator) return [];
            const tokens = [];
            if (indicator.crsi_reg_bull_div) tokens.push('cR↑');
            if (indicator.crsi_wide_bull_div) tokens.push('cW↑');
            if (indicator.obv_reg_bull_div) tokens.push('oR↑');
            if (indicator.obv_wide_bull_div) tokens.push('oW↑');
            if (indicator.crsi_reg_hid_bull) tokens.push('cH↑');
            if (indicator.crsi_wide_hid_bull) tokens.push('cWH↑');
            if (indicator.obv_reg_hid_bull) tokens.push('oH↑');
            if (indicator.obv_wide_hid_bull) tokens.push('oWH↑');
            if (indicator.crsi_reg_bear_div) tokens.push('cR↓');
            if (indicator.crsi_wide_bear_div) tokens.push('cW↓');
            if (indicator.obv_reg_bear_div) tokens.push('oR↓');
            if (indicator.obv_wide_bear_div) tokens.push('oW↓');
            if (indicator.crsi_reg_hid_bear) tokens.push('cH↓');
            if (indicator.crsi_wide_hid_bear) tokens.push('cWH↓');
            if (indicator.obv_reg_hid_bear) tokens.push('oH↓');
            if (indicator.obv_wide_hid_bear) tokens.push('oWH↓');
            return tokens;
        }

        function getDivergenceSummary(indicator, maxItems = 4) {
            if (!indicator) return '--';
            const tokens = getIndicatorDivergenceTokens(indicator);
            if (!tokens.length) return '无';
            if (tokens.length <= maxItems) return tokens.join(' / ');
            return `${tokens.slice(0, maxItems).join(' / ')} +${tokens.length - maxItems}`;
        }

        function getDivergenceDetailText(indicator) {
            if (!indicator) return '--';
            const tokens = getIndicatorDivergenceTokens(indicator);
            return tokens.length ? tokens.join(' / ') : '无';
        }

        function getSignalExtra(signal) {
            if (!signal || typeof signal !== 'object') return {};
            return parseRecordExtra(signal.extra);
        }

        function buildTradeSignalLabel(signal) {
            if (!signal || typeof signal !== 'object') return 'Signal';
            const extra = getSignalExtra(signal);
            const direction = String(signal.direction || '').trim().toLowerCase();
            const signalWindow = String(extra.signal_window || '').trim().toLowerCase();
            const signalMode = String(extra.signal_mode || '').trim().toLowerCase();
            const touchLine = String(extra.ema_touch_line || '').trim().toLowerCase();
            const touchSuffix = touchLine === 'fast'
                ? ' · 快线触及'
                : touchLine === 'slow'
                ? ' · 慢线触及'
                : '';
            if (direction === 'long') {
                if (signalMode === 'trend' && signalWindow === 'sd_upper') return `顺势多${touchSuffix}`;
                if (signalMode === 'mr' && signalWindow === 'sd_lower') return '回归多';
            }
            if (direction === 'short') {
                if (signalMode === 'trend' && signalWindow === 'sd_lower') return `顺势空${touchSuffix}`;
                if (signalMode === 'mr' && signalWindow === 'sd_upper') return '回归空';
            }
            return String(signal.signal || signal.direction || 'Signal');
        }

        function getTraceSignalState(trace) {
            return trace && typeof trace === 'object' && trace.signal_state && typeof trace.signal_state === 'object'
                ? trace.signal_state
                : {};
        }

        function getTraceSignalPayload(trace) {
            const signalState = getTraceSignalState(trace);
            return signalState && typeof signalState.signal_payload === 'object' ? signalState.signal_payload : null;
        }

        function getDecisionSignalContext(context) {
            if (!context) return { signal: null, traceSignal: null, signalState: {} };
            const signalState = getTraceSignalState(context.trace);
            const traceSignal = getTraceSignalPayload(context.trace);
            return {
                signal: context.activeSignal || null,
                traceSignal,
                signalState,
            };
        }

        function buildTraceDecisionLabel(trace, fallbackSignal = null) {
            const signalState = getTraceSignalState(trace);
            const label = String(signalState.label || '').trim();
            if (label) return label;
            if (fallbackSignal) return buildTradeSignalLabel(fallbackSignal);
            return '无信号';
        }

        function getTraceStageBadgeClass(stage) {
            const key = String(stage || '').trim().toLowerCase();
            if (key === 'confirmed') return 'positive';
            if (key === 'blocked') return 'negative';
            if (key === 'candidate') return 'warning';
            return '';
        }

        function getChartMarkerDensityTier(barsCount) {
            const viewport = chartViewportState.totalBars === barsCount
                ? chartViewportState
                : getCurrentZoomWindow(barsCount);
            const visibleBars = Math.max(1, Number(viewport.visibleBars || barsCount || 0));
            if (visibleBars > 180) return 'wide';
            if (visibleBars > 90) return 'mid';
            return 'tight';
        }

        function toggleChartLegendCollapsed() {
            chartLegendCollapsed = !chartLegendCollapsed;
            saveChartUiPrefs();
            renderChartFloatingLegend(getChartDisplayPayload());
        }

        function formatSignalTime(item) {
            if (item?.us_time) return String(item.us_time);
            const ms = Number(item?.bar_time_ms || 0);
            return ms ? formatBarTimeMsToET(ms) : '--';
        }

        function deriveItemDate(item) {
            const fromUs = String(item?.us_time || '').trim();
            if (/^\d{4}-\d{2}-\d{2}/.test(fromUs)) return fromUs.slice(0, 10);
            const ms = Number(item?.bar_time_ms || 0);
            if (ms > 0) return new Date(ms).toISOString().slice(0, 10);
            return new Date().toISOString().slice(0, 10);
        }

        function getSignalBadgeClass(signal) {
            return String(signal?.direction || '').toLowerCase() === 'short' ? 'short' : 'long';
        }

        function buildOrderDetailsUrl(signal, fallbackDate = '') {
            if (!signal || typeof signal !== 'object') return '';
            const extra = getSignalExtra(signal);
            const signalId = String(signal.signal_id || extra.signal_id || '').trim();
            const tradeGroupId = String(
                signal.trade_group_id
                || extra.trade_group_id
                || extra.entry_order_unique_id
                || ''
            ).trim();
            const orderId = String(
                signal.order_id
                || extra.order_id
                || extra.broker_order_id
                || ''
            ).trim();
            if (isComputedSignal(signal) && !tradeGroupId && !orderId) return '';
            if (!signalId && !tradeGroupId && !orderId) return '';
            const params = { date: fallbackDate || deriveItemDate(signal) };
            if (signalId) params.signal_id = signalId;
            if (tradeGroupId) params.trade_group_id = tradeGroupId;
            else if (orderId) params.order_id = orderId;
            return buildPageUrl('/ibkr_order_details.html', params, { environment: currentEnvironment });
        }

        function parseQueryContext() {
            const params = new URLSearchParams(window.location.search || '');
            const startMs = parseQueryTimeMs(params.get('start_ms') || params.get('from') || '');
            const endMs = parseQueryTimeMs(params.get('end_ms') || params.get('to') || '');
            return {
                symbol: String(params.get('symbol') || '').trim().toUpperCase(),
                interval: normalizeInterval(params.get('interval') || '5m'),
                range: String(params.get('range') || (startMs || endMs ? 'custom' : '')).trim().toLowerCase(),
                customStartMs: startMs,
                customEndMs: endMs,
                barTimeMs: Number(params.get('bar_time_ms') || 0) || 0,
                indicatorId: String(params.get('indicator_id') || '').trim(),
                traceOpen: ['1', 'true', 'yes'].includes(String(params.get('trace') || '').trim().toLowerCase()),
            };
        }

        function updateQueryState() {
            const params = {
                symbol: currentSymbol,
                interval: currentInterval,
                range: currentRangeKey,
            };
            if (isCustomRange() && customRangeStartMs && customRangeEndMs) {
                params.start_ms = Math.round(customRangeStartMs);
                params.end_ms = Math.round(customRangeEndMs);
            }
            if (chartTracePanelOpen) params.trace = 1;
            if (currentIndicatorId) params.indicator_id = currentIndicatorId;
            const url = buildPageUrl('/ibkr_chart.html', params, { environment: currentEnvironment });
            window.history.replaceState({}, '', url);
        }

        async function fetchWithRetry(collection, params, maxRetries = 2) {
            let lastError = null;
            for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
                try {
                    return await apiFetch(collection, params);
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
            const latestIndicator = normalizeIndicatorRecord(response?.latest_indicator || null) || (indicators.length ? indicators[indicators.length - 1] : null);
            return {
                latestIndicator,
                bars,
                indicators,
                signals,
                traceTimeline,
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
            if (!previewBarTimeMs || (latestFormalBarMs && previewBarTimeMs <= latestFormalBarMs)) {
                return payload;
            }

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
                return payload;
            }

            previewBar.close = previewClose;
            if (previewOpen > 0) {
                previewBar.high = Math.max(previewHigh || previewOpen, previewOpen, previewClose);
                previewBar.low = Math.min(previewLow || previewOpen || previewClose, previewOpen, previewClose);
            }
            previewBar.volume = Number(previewBar.volume || 0);
            return {
                ...payload,
                bars: [...baseBars, previewBar],
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
                secondary: `100 ${formatPrice(indicator?.ema_trend)} · VWAP ${formatPrice(indicator?.vwap)}`,
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
            const chips = [
                buildStripChip('hero-chip', `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)}`, currentSymbol ? '' : 'placeholder', '当前查看的标的与周期。'),
                buildStripChip('hero-chip', getEnvironmentLabel(currentEnvironment), currentEnvironment ? '' : 'placeholder', '当前运行环境。'),
                buildStripChip('hero-chip', stateBadge.text, stateBadge.className, '图表工作区当前加载状态。'),
                buildStripChip('hero-chip', `焦点 bar ${focusTime}`, focusTime === '--' ? 'placeholder' : '', '当前决策上下文使用的 focus bar 时间。'),
                buildStripChip('hero-chip', `最后收盘 bar ${lastBarTime}`, lastBarTime === '--' ? 'placeholder' : '', '当前窗口最后一根已收盘 bar 的时间。'),
                buildStripChip('hero-chip', realtimeState.text, realtimeState.className, realtimeState.title),
                buildStripChip('hero-chip', previewState.text, previewState.className, previewState.title),
                buildStripChip('hero-chip', compareState.text, compareState.className, compareState.title),
            ];
            document.getElementById('heroStatus').innerHTML = chips.join('');
        }

        function renderTimeframeGroup() {
            document.getElementById('timeframeGroup').innerHTML = SUPPORTED_INTERVALS.map((interval) => `
                <button class="tf-btn ${interval === currentInterval ? 'active' : ''}" type="button" onclick="selectChartInterval('${interval}')">${escapeHtml(getIntervalLabel(interval))}</button>
            `).join('');
        }

        function renderRangeGroup() {
            document.getElementById('rangeGroup').innerHTML = RANGE_PRESETS.map((preset) => `
                <button class="range-btn ${preset.key === currentRangeKey ? 'active' : ''}" type="button" onclick="selectChartRange('${preset.key}')">${escapeHtml(preset.shortLabel)}</button>
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
            const show = isCustomRange();
            form.hidden = !show;
            if (!show) return;
            seedCustomRangeFromPayload();
            startInput.value = formatDateTimeLocalValue(customRangeStartMs);
            endInput.value = formatDateTimeLocalValue(customRangeEndMs);
        }

        function renderSummaryStrip(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const indicators = Array.isArray(payload?.indicators) ? payload.indicators : [];
            const signals = Array.isArray(payload?.signals) ? payload.signals : [];
            const focus = bars.length ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            const focusBar = focus?.bar || null;
            const latestBar = bars.length ? bars[bars.length - 1] : null;
            const compareSummary = comparePayload?.comparison?.summary || {};
            const realtimeState = getRealtimeChipState(payload);
            const previewState = getPreviewChipState();
            const compareState = getCompareChipState(compareSummary, { compact: true });
            const stateBadge = getWorkspaceStateBadge();
            const chips = [
                { text: `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)} · ${getRangeShortLabel(currentRangeKey)}`, className: currentSymbol ? '' : 'placeholder', title: getRangeLabel(currentRangeKey) },
                { text: `${bars.length} bars · ${indicators.length} ind · ${signals.length} sig`, className: chartWorkspaceState === 'loading' ? 'loading' : '', title: '当前图表窗口实际绘制的数据量。' },
                { text: `Focus ${focusBar ? String(focusBar.us_time || '--').slice(5) : '--'}`, className: focusBar ? '' : 'placeholder', title: '当前复盘焦点时间。' },
                { text: `Latest ${latestBar ? String(latestBar.us_time || '--').slice(5) : '--'}`, className: latestBar ? '' : 'placeholder', title: '当前窗口最后一根 bar。' },
                { text: stateBadge.text.replace('工作区 ', ''), className: stateBadge.className, title: '图表工作区状态。' },
                { text: realtimeState.text, className: realtimeState.className, title: realtimeState.title },
                { text: previewState.text, className: previewState.className, title: previewState.title },
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
                { text: `范围 ${getRangeShortLabel(currentRangeKey)}`, title: getRangeLabel(currentRangeKey) },
                { text: `焦点 ${focusBar ? String(focusBar.us_time || '--').slice(5) : '--'}`, title: '当前 focus bar 时间。' },
                { text: `趋势 ${focusIndicator ? getTrendText(focusIndicator.trend_dir) : '--'}`, title: '趋势方向。' },
                { text: `EMA ${focusIndicator ? getEmaStructureText(focusIndicator) : '--'}`, title: 'EMA 结构。' },
                { text: `VWAP ${focusIndicator ? formatPercent(focusIndicator.vwap_dist) : '--'}`, title: 'VWAP 偏离。' },
                { text: `ATR ${focusIndicator ? formatPercent(focusIndicator.atr_pct) : '--'}`, title: 'ATR 波动。' },
                { text: `SD ${focusIndicator ? getSdZoneText(focusIndicator.sd_zone) : '--'} / ${focusIndicator ? getSdTrendText(focusIndicator.sd_trend) : '--'}`, title: 'SD 区间与斜率。' },
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
            const safeIndex = Math.min(Math.max(Number(index) || 0, 0), bars.length - 1);
            const bar = bars[safeIndex] || null;
            const signalMatches = signals.filter((item) => Number(item?.bar_time_ms || 0) === Number(bar?.bar_time_ms || 0));
            const activeSignal = signalMatches.find((item) => String(item?.signal_id || item?.id || '') === String(signalId || ''))
                || signalMatches[0]
                || null;
            const exactIndicator = indicatorMap.get(Number(bar?.bar_time_ms || 0)) || null;
            const exactTrace = traceMap.get(Number(bar?.bar_time_ms || 0)) || null;
            const isPreviewBar = Boolean(bar?.preview || bar?.is_preview);
            return {
                index: safeIndex,
                bar,
                isPreviewBar,
                indicator: exactIndicator || (isPreviewBar ? null : payload?.latestIndicator || null),
                signalMatches,
                activeSignal,
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
            if (!bars.length || !signals.length) return [];
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar?.bar_time_ms || 0), index]));
            return Array.from(new Set(
                signals
                    .map((signal) => indexByMs.get(Number(signal?.bar_time_ms || 0)))
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
            const signalLabel = signal ? buildTradeSignalLabel(signal) : (isTradeSignalInterval() ? 'No trade signal' : 'Labels 仅 5m');
            const decisionSignal = getDecisionSignalContext(context);
            const traceStage = String(decisionSignal.signalState?.stage || '').trim().toLowerCase();
            const traceLabel = buildTraceDecisionLabel(context?.trace, signal || decisionSignal.traceSignal);
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

        function getRailNavLabel(kicker) {
            const text = String(kicker || '').trim();
            const map = {
                'IBKR Compare': 'Compare',
                'Focus': 'Focus',
                'Stored Bars Chain': 'Stored',
                'IBKR API Chain': 'IBKR',
                'Comparison': 'Diff',
                'Snapshot': 'Snapshot',
                'Structure': 'Structure',
                'Signal Flow': 'Signals',
                'Recent Signals': 'Recent',
                'Load Error': 'Error',
            };
            return map[text] || text || 'Card';
        }

        function renderRailNav() {
            const nav = document.getElementById('railNav');
            const rail = document.getElementById('infoRail');
            if (!nav || !rail) return;
            const cards = Array.from(rail.querySelectorAll('.rail-card'));
            if (!cards.length) {
                nav.innerHTML = '';
                activeRailCardIndex = 0;
                return;
            }
            nav.innerHTML = cards.map((card, index) => {
                const kicker = card.querySelector('.rail-kicker')?.textContent || '';
                card.dataset.railIndex = String(index);
                return `<button class="rail-nav-chip" type="button" onclick="scrollInfoRailCard(${index})">${escapeHtml(getRailNavLabel(kicker))}</button>`;
            }).join('');
            setActiveRailCard(activeRailCardIndex, { syncScroll: false });
        }

        function setActiveRailCard(index, { syncScroll = true } = {}) {
            const rail = document.getElementById('infoRail');
            const nav = document.getElementById('railNav');
            const cards = rail ? Array.from(rail.querySelectorAll('.rail-card')) : [];
            const chips = nav ? Array.from(nav.querySelectorAll('.rail-nav-chip')) : [];
            if (!cards.length) return;
            const safeIndex = Math.max(0, Math.min(Number(index) || 0, cards.length - 1));
            activeRailCardIndex = safeIndex;
            const compact = isCompactViewport();
            if (rail) rail.classList.toggle('compact-tabs', compact);
            cards.forEach((card, cardIndex) => {
                card.classList.toggle('active', cardIndex === safeIndex);
            });
            chips.forEach((chip, chipIndex) => {
                chip.classList.toggle('active', chipIndex === safeIndex);
            });
            if (!syncScroll) return;
            const target = cards[safeIndex];
            if (!target) return;
            if (compact) {
                target.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
                return;
            }
            target.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'start' });
        }

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

        function renderInspectorDrawer(payload = getChartDisplayPayload()) {
            const drawer = document.getElementById('inspectorDrawer');
            const title = document.getElementById('inspectorDrawerTitle');
            const sub = document.getElementById('inspectorDrawerSub');
            const body = document.getElementById('inspectorDrawerBody');
            const rail = document.getElementById('infoRail');
            if (!drawer || !title || !sub || !body) return;
            const focus = payload ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            title.textContent = `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)} Inspector`;
            const stateText = comparePayload
                ? 'Compare 已联动'
                : chartFocusMode
                ? '焦点模式'
                : '工作台模式';
            sub.innerHTML = `${escapeHtml(focus?.bar?.us_time || '等待图表数据')}<br>${escapeHtml(stateText)}`;
            const traceBody = buildTracePanelBody(payload, { embedded: true });
            const traceCard = chartTracePanelOpen ? `
                <div class="rail-card">
                    <div class="rail-kicker">Trace</div>
                    <div class="rail-sub">Trace 简版；完整表在下方。</div>
                    <div class="trace-panel-summary">${traceBody.chips.join('')}</div>
                    ${traceBody.tableHtml}
                </div>
            ` : '';
            body.innerHTML = `${rail?.innerHTML || '<div class="rail-card"><div class="rail-kicker">Inspector</div><div class="rail-sub">等待图表数据...</div></div>'}${traceCard}`;
            drawer.classList.toggle('show', inspectorDrawerOpen);
            drawer.setAttribute('aria-hidden', inspectorDrawerOpen ? 'false' : 'true');
        }

        function setInspectorDrawerOpen(enabled) {
            inspectorDrawerOpen = Boolean(enabled);
            if (inspectorDrawerOpen) {
                mobileQuickPanelOpen = false;
                closeSignalDrawer();
            }
            renderMobileQuickPanel(getChartDisplayPayload());
            renderInspectorDrawer(getChartDisplayPayload());
            renderMobileGestureHint(getChartDisplayPayload());
        }

        function toggleInspectorDrawer() {
            dismissMobileGestureHint();
            setInspectorDrawerOpen(!inspectorDrawerOpen);
        }

        function closeInspectorDrawer() {
            setInspectorDrawerOpen(false);
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

        function renderChartWorkspaceChrome(payload = getChartDisplayPayload()) {
            renderChartToolbar(payload);
            renderChartFloatingLegend(payload);
            renderChartBottomBar(payload);
            renderMobileDock(payload);
            renderMobileQuickPanel(payload);
            renderInspectorDrawer(payload);
            renderMobileGestureHint(payload);
            renderTracePanel(payload);
        }

        function buildTraceToken(text, className = '') {
            return `<span class="trace-token ${escapeHtml(className)}">${escapeHtml(text || '--')}</span>`;
        }

        function getTraceRows(payload) {
            return Array.isArray(payload?.traceTimeline) ? payload.traceTimeline : [];
        }

        function buildTracePanelBody(payload, { embedded = false } = {}) {
            const traceRows = getTraceRows(payload);
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const focus = bars.length ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            const activeBarMs = Number(focus?.bar?.bar_time_ms || 0) || 0;
            if (!traceRows.length) {
                return {
                    chips: [
                        buildTraceToken(`Trace ${chartTracePanelOpen ? 'On' : 'Off'}`),
                    ],
                    tableHtml: '<div class="trace-empty">当前窗口暂无可展示的 trace 记录。</div>',
                };
            }
            const candidateCount = traceRows.filter((item) => String(item?.signal_state?.stage || '') === 'candidate').length;
            const blockedCount = traceRows.filter((item) => String(item?.signal_state?.stage || '') === 'blocked').length;
            const confirmedCount = traceRows.filter((item) => String(item?.signal_state?.stage || '') === 'confirmed').length;
            const chips = [
                buildTraceToken(`${traceRows.length} bars`),
                buildTraceToken(`${confirmedCount} confirmed`, confirmedCount ? 'positive' : ''),
                buildTraceToken(`${candidateCount} candidate`, candidateCount ? 'warning' : ''),
                buildTraceToken(`${blockedCount} blocked`, blockedCount ? 'negative' : ''),
                focus?.trace?.is_preview || focus?.isPreviewBar ? buildTraceToken('当前焦点含预估 bar', 'preview') : '',
            ].filter(Boolean);
            const rowsHtml = traceRows.map((item) => {
                const barTimeMs = Number(item?.bar_time_ms || 0) || 0;
                const stage = String(item?.signal_state?.stage || '').trim().toLowerCase();
                const active = activeBarMs > 0 && activeBarMs === barTimeMs;
                const isPreview = Boolean(item?.is_preview);
                const structure = [
                    item?.structure?.ema_bullish ? 'EMA 多头' : item?.structure?.ema_bearish ? 'EMA 空头' : 'EMA 中性',
                    item?.structure?.dtp_phase ? `DTP ${item.structure.dtp_phase}` : '',
                    ...(Array.isArray(item?.structure?.fractal_tokens) ? item.structure.fractal_tokens : []),
                ].filter(Boolean);
                const position = [
                    `VWAP ${formatPercent(item?.position?.vwap_dist)}`,
                    `SD ${getSdZoneText(item?.position?.sd_zone)}`,
                    `Trend ${getSdTrendText(item?.position?.sd_trend)}`,
                ];
                const volatility = [
                    `ATR ${formatPrice(item?.volatility?.atr)}`,
                    `ATR% ${formatPercent(item?.volatility?.atr_pct)}`,
                ];
                const momentum = [
                    `CRSI ${formatNumber(item?.momentum?.crsi)}`,
                    `OBV ${formatNumber(item?.momentum?.obv_rsi)}`,
                    ...((item?.momentum?.divergence_tokens || []).slice(0, 3)),
                ].filter(Boolean);
                const events = Array.isArray(item?.event_chain) && item.event_chain.length
                    ? item.event_chain.slice(0, embedded ? 2 : 3)
                    : ['无新增事件'];
                const filters = Array.isArray(item?.filters) && item.filters.length
                    ? item.filters.slice(0, embedded ? 2 : 3)
                    : ['未触发过滤'];
                const signalLabel = buildTraceDecisionLabel(item, null);
                return `
                    <tr class="${active ? 'active ' : ''}${isPreview ? 'preview' : ''}" data-trace-bar-ms="${barTimeMs}" onclick="focusTraceBar('${barTimeMs}')">
                        <td>
                            <div class="trace-cell-main">${escapeHtml(item?.us_time || '--')}</div>
                            <div class="trace-cell-sub">${escapeHtml(item?.cn_time || '--')}${isPreview ? ' · 预估' : ''}</div>
                        </td>
                        <td>
                            <div class="trace-cell-main">${escapeHtml(formatPrice(item?.close))}</div>
                            <div class="trace-cell-sub">bar #${escapeHtml(String(item?.bar_index || '--'))}</div>
                        </td>
                        <td><div class="trace-stack"><div class="trace-token-row">${structure.map((text) => buildTraceToken(text)).join('')}</div><div class="trace-token-row">${(item?.structure?.touch_tokens || []).map((text) => buildTraceToken(text, 'warning')).join('') || buildTraceToken('无触及')}</div></div></td>
                        <td><div class="trace-stack"><div class="trace-token-row">${position.map((text) => buildTraceToken(text)).join('')}</div></div></td>
                        <td><div class="trace-stack"><div class="trace-token-row">${volatility.map((text) => buildTraceToken(text)).join('')}</div></div></td>
                        <td><div class="trace-stack"><div class="trace-token-row">${momentum.map((text) => buildTraceToken(text)).join('')}</div></div></td>
                        <td><div class="trace-stack"><div class="trace-token-row">${events.map((text) => buildTraceToken(text, 'warning')).join('')}</div></div></td>
                        <td><div class="trace-stack"><div class="trace-token-row">${filters.map((text) => buildTraceToken(text, filters[0] === '未触发过滤' ? '' : 'negative')).join('')}</div></div></td>
                        <td><div class="trace-stack"><div class="trace-token-row">${buildTraceToken(signalLabel, getTraceStageBadgeClass(stage))}${isPreview ? buildTraceToken('预估', 'preview') : ''}</div><div class="trace-cell-sub">${escapeHtml(item?.signal_state?.reason || item?.signal_state?.filter_reason || 'bars 实时推演')}</div></div></td>
                    </tr>
                `;
            }).join('');
            return {
                chips,
                tableHtml: `
                    <div class="trace-panel-table-wrap">
                        <table class="trace-panel-table">
                            <thead>
                                <tr>
                                    <th>Time</th>
                                    <th>Close</th>
                                    <th>Structure</th>
                                    <th>Position</th>
                                    <th>Volatility</th>
                                    <th>Momentum</th>
                                    <th>Events</th>
                                    <th>Filters</th>
                                    <th>Signal</th>
                                </tr>
                            </thead>
                            <tbody>${rowsHtml}</tbody>
                        </table>
                    </div>
                `,
            };
        }

        function scrollTraceRowIntoView(barTimeMs) {
            const shell = document.getElementById('tracePanelShell');
            const target = shell?.querySelector?.(`[data-trace-bar-ms="${Number(barTimeMs || 0)}"]`);
            if (!target || typeof target.scrollIntoView !== 'function') return;
            window.requestAnimationFrame(() => {
                target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            });
        }

        function renderTracePanel(payload = getChartDisplayPayload()) {
            const shell = document.getElementById('tracePanelShell');
            if (!shell) return;
            const body = buildTracePanelBody(payload);
            const hidden = !chartTracePanelOpen;
            shell.classList.toggle('hidden', hidden);
            if (hidden) {
                shell.innerHTML = '';
                return;
            }
            shell.innerHTML = `
                <div class="trace-panel-head">
                    <div>
                        <div class="trace-panel-title">Bar Trace</div>
                        <div class="trace-panel-copy">每根 bar 一行，串起结构、位置、动量、过滤与信号状态；预估 bar 只显示候选/过滤，不并入确认信号。</div>
                    </div>
                    <div class="trace-panel-actions">
                        <button class="tv-tool-btn" type="button" onclick="toggleChartTracePanel()">隐藏 Trace</button>
                        <button class="tv-tool-btn" type="button" onclick="focusLatestChartBar()">回到最新</button>
                    </div>
                </div>
                <div class="trace-panel-summary">${body.chips.join('')}</div>
                ${body.tableHtml}
            `;
            const focus = payload ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            if (focus?.bar?.bar_time_ms) {
                scrollTraceRowIntoView(focus.bar.bar_time_ms);
            }
        }

        window.toggleChartTracePanel = function() {
            chartTracePanelOpen = !chartTracePanelOpen;
            saveChartUiPrefs();
            renderTracePanel(getChartDisplayPayload());
            renderChartToolbar(getChartDisplayPayload());
            renderMobileDock(getChartDisplayPayload());
            updateQueryState();
        };

        window.focusTraceBar = function(barTimeMs) {
            const payload = getChartDisplayPayload();
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const targetIndex = findBarIndexByTime(bars, Number(barTimeMs || 0));
            if (targetIndex < 0) return;
            focusBarIndex(targetIndex);
            if (chartTracePanelOpen) {
                scrollTraceRowIntoView(barTimeMs);
            }
        };

        function renderCursorStrip(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) {
                document.getElementById('cursorStrip').innerHTML = [
                    buildCursorCard('Cursor Time', '--', chartWorkspaceState === 'loading' ? '等待 bars / indicators' : '暂无游标上下文'),
                    buildCursorCard('OHLC', '--', '--'),
                    buildCursorCard('EMA / VWAP', '--', '--'),
                    buildCursorCard('SD / Fractal', '--', '--'),
                    buildCursorCard('Touch / Divergence', '--', '--'),
                    buildCursorCard('Signal / Osc', chartWorkspaceState === 'error' ? '加载失败' : '--', chartWorkspaceState === 'error' ? '请检查 ibkr_bars / 网络状态' : '--'),
                ].join('');
                renderChartWorkspaceChrome(payload);
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
            const traceLabel = buildTraceDecisionLabel(context?.trace, signal);
            const traceStage = String(decisionSignal.signalState?.stage || '').trim().toLowerCase();
            const compareRow = getCompareRowByBarTime(bar?.bar_time_ms);
            if (comparePayload && compareRow) {
                const storedBar = formatInlineOHLC(compareRow?.stored?.bar);
                const ibkrBar = formatInlineOHLC(compareRow?.ibkr?.bar);
                const storedChain = formatInlineCompareChain(compareRow?.stored?.indicator);
                const ibkrChain = formatInlineCompareChain(compareRow?.ibkr?.indicator);
                const diff = formatInlineCompareDiff(compareRow);
                document.getElementById('cursorStrip').innerHTML = [
                    buildCursorCard('Cursor Time', bar?.us_time || '--', summarizeCompareStatusLine(compareRow?.status)),
                    buildCursorCard('Stored Bars', storedBar.primary, storedBar.secondary, { valueClass: 'compact' }),
                    buildCursorCard('IBKR API Bars', ibkrBar.primary, ibkrBar.secondary, { valueClass: 'compact' }),
                    buildCursorCard('Stored Chain', storedChain.primary, storedChain.secondary, { valueClass: 'compact' }),
                    buildCursorCard('IBKR API Chain', ibkrChain.primary, ibkrChain.secondary, { valueClass: 'compact' }),
                    buildCursorCard('Diff', diff.primary, diff.secondary, { valueClass: 'compact' }),
                ].join('');
                renderChartWorkspaceChrome(payload);
                return;
            }
            const ohlc = formatInlineOHLC(bar);
            const chain = formatInlineChain(indicator);
            const cursorState = chartPointerLocked ? 'Locked cursor' : (hoverBarIndex >= 0 ? 'Hover cursor' : 'Latest focus');
            const signalPrimary = signal
                ? traceLabel
                : (isTradeSignalInterval() ? '暂无信号' : '标签仅 5m');
            const signalSecondary = indicator
                ? `CRSI ${formatNumber(indicator.crsi)} · OBV ${formatNumber(indicator.obv_rsi)} · ATR ${formatPercent(indicator.atr_pct)}`
                : (isPreviewBar ? '预览 bar 收盘后生成 Osc 指标' : '--');
            document.getElementById('cursorStrip').innerHTML = [
                buildCursorCard('Cursor Time', bar?.us_time || '--', cursorState),
                buildCursorCard('OHLC', ohlc.primary, ohlc.secondary),
                buildCursorCard('EMA / VWAP', isPreviewBar && !indicator ? '预览 bar 暂无正式均线' : chain.primary, isPreviewBar && !indicator ? '收盘后生成 EMA / VWAP' : chain.secondary),
                buildCursorCard('SD / Fractal', isPreviewBar && !indicator ? '预览 bar 暂无正式指标' : `Zone ${getSdZoneText(indicator?.sd_zone)} · ${getSdTrendText(indicator?.sd_trend)}`, isPreviewBar && !indicator ? '收盘后计算 SD / Fractal' : `Frac ${getFractalSummary(indicator)}`),
                buildCursorCard('Touch / Divergence', isPreviewBar && !indicator ? '预览中' : getTouchDetailText(indicator), isPreviewBar && !indicator ? 'Touch / Div 待收盘确认' : getDivergenceDetailText(indicator)),
                buildCursorCard('Signal / Osc', isPreviewBar && !indicator ? '未收盘预览' : signalPrimary, `${traceStage ? `Stage ${traceStage} · ` : ''}${signalSecondary}`),
            ].join('');
            renderChartWorkspaceChrome(payload);
        }

        function renderLayerStrip() {
            const defs = getChartLayerDefs();
            document.getElementById('layerStrip').innerHTML = defs.map((item) => `
                <button class="layer-btn ${chartLayerState[item.key] ? 'active' : ''} ${item.disabled ? 'disabled' : ''}" type="button" onclick="toggleChartLayer('${item.key}')" ${item.disabled ? 'disabled' : ''}>
                    ${escapeHtml(item.label)}${item.disabled ? ' · 5m' : ''}
                </button>
            `).join('');
        }

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
            renderCursorStrip(payload);
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
            renderCursorStrip(payload);
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

        function lockChartPointerAtIndex(index, signalId = '') {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
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

        function closeSignalDrawer() {
            renderSignalDrawer(null);
        }

        function openSignalDrawer(signal, context = null) {
            renderSignalDrawer(signal, context);
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
            const viewport = chartViewportState.totalBars === bars.length
                ? chartViewportState
                : getCurrentZoomWindow(bars.length);
            const currentVisible = Math.max(8, viewport.visibleBars || Math.min(120, bars.length));
            const nextVisible = Math.max(12, Math.min(bars.length, Math.round(currentVisible * factor)));
            const anchorIndex = getEffectiveCursorIndex(payload);
            let startIndex = clampIndex(anchorIndex - Math.floor(nextVisible / 2), bars.length);
            if (startIndex + nextVisible > bars.length) {
                startIndex = Math.max(0, bars.length - nextVisible);
            }
            const endIndex = Math.min(bars.length - 1, startIndex + nextVisible - 1);
            setChartZoomWindow({
                start: indexToPercent(startIndex, bars.length),
                end: indexToPercent(endIndex, bars.length),
            }, payload);
            syncChartTooltip(anchorIndex);
        }

        function focusRelativeBar(delta) {
            const payload = getChartDisplayPayload();
            if (!payload || !Array.isArray(payload.bars) || !payload.bars.length) return;
            const bars = payload.bars;
            const activeIndex = getEffectiveCursorIndex(payload);
            const targetIndex = clampIndex(activeIndex + Number(delta || 0), bars.length);
            focusBarIndex(targetIndex);
            hoverBarIndex = targetIndex;
            ensureBarVisible(targetIndex, payload);
            renderCursorStrip(payload);
        }

        function focusSignalByStep(step) {
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
            const focusTraceLabel = buildTraceDecisionLabel(focus?.trace, focusSignal || focusTraceSignal);

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

        function formatChartLabel(item) {
            if (item?.us_time) return String(item.us_time).slice(5, 16);
            const ms = Number(item?.bar_time_ms || 0);
            if (!ms) return '--';
            const date = new Date(ms);
            if (Number.isNaN(date.getTime())) return '--';
            return `${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')} ${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}`;
        }

        function supportsSessionDividers(interval = currentInterval) {
            return ['5m', '15m', '30m'].includes(normalizeInterval(interval || currentInterval));
        }

        function getBarEtParts(item) {
            const source = String(item?.us_time || '').trim()
                || formatBarTimeMsToET(Number(item?.bar_time_ms || 0));
            const match = source.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/);
            if (!match) return null;
            return {
                date: `${match[1]}-${match[2]}-${match[3]}`,
                hour: Number(match[4]),
                minute: Number(match[5]),
            };
        }

        function getUsSessionKey(item) {
            const parts = getBarEtParts(item);
            if (!parts) return '';
            const minuteOfDay = parts.hour * 60 + parts.minute;
            if (minuteOfDay >= 4 * 60 && minuteOfDay < 9 * 60 + 30) return 'pre';
            if (minuteOfDay >= 9 * 60 + 30 && minuteOfDay < 16 * 60) return 'regular';
            if (minuteOfDay >= 16 * 60 && minuteOfDay < 20 * 60) return 'post';
            return '';
        }

        function getUsSessionMeta(sessionKey) {
            const map = {
                pre: {
                    label: '盘前',
                    color: '#38BDF8',
                    borderColor: 'rgba(56, 189, 248, 0.56)',
                    dash: 'dashed',
                    opacity: 0.74,
                },
                regular: {
                    label: '盘中',
                    color: '#F59E0B',
                    borderColor: 'rgba(245, 158, 11, 0.60)',
                    dash: 'solid',
                    opacity: 0.82,
                },
                post: {
                    label: '盘后',
                    color: '#FB7185',
                    borderColor: 'rgba(251, 113, 133, 0.58)',
                    dash: 'dashed',
                    opacity: 0.78,
                },
            };
            return map[sessionKey] || null;
        }

        function buildSessionDividerMarkLineItems(bars, options = {}) {
            if (!supportsSessionDividers() || !Array.isArray(bars) || !bars.length) return [];
            const showLabels = Boolean(options.showLabels);
            const items = [];
            let previousSessionKey = '';
            let previousDate = '';
            bars.forEach((bar, index) => {
                const parts = getBarEtParts(bar);
                const sessionKey = getUsSessionKey(bar);
                if (!parts || !sessionKey) return;
                const dateChanged = parts.date !== previousDate;
                const sessionChanged = sessionKey !== previousSessionKey;
                if (!dateChanged && !sessionChanged) {
                    previousDate = parts.date;
                    previousSessionKey = sessionKey;
                    return;
                }
                const meta = getUsSessionMeta(sessionKey);
                if (!meta) {
                    previousDate = parts.date;
                    previousSessionKey = sessionKey;
                    return;
                }
                items.push({
                    xAxis: index,
                    name: meta.label,
                    lineStyle: {
                        color: meta.color,
                        type: meta.dash,
                        width: sessionKey === 'regular' ? 1.2 : 1,
                        opacity: dateChanged ? meta.opacity : Math.max(meta.opacity - 0.1, 0.45),
                    },
                    label: showLabels ? {
                        show: true,
                        formatter: meta.label,
                        position: 'insideEndTop',
                        distance: 8,
                        color: meta.color,
                        fontSize: 10,
                        fontWeight: 700,
                        fontFamily: 'JetBrains Mono, monospace',
                        backgroundColor: 'rgba(7,12,20,0.88)',
                        borderColor: meta.borderColor,
                        borderWidth: 1,
                        borderRadius: 6,
                        padding: [3, 6],
                    } : { show: false },
                });
                previousDate = parts.date;
                previousSessionKey = sessionKey;
            });
            return items;
        }

        function buildLatestPriceMarkLineItem(latestClose, latestLineColor) {
            if (!(Number.isFinite(latestClose) && latestClose > 0)) return null;
            return {
                yAxis: latestClose,
                lineStyle: {
                    color: latestLineColor,
                    type: 'dashed',
                    opacity: 0.78,
                    width: 1,
                },
                label: {
                    show: true,
                    position: 'insideEndTop',
                    distance: 8,
                    formatter: ` 最新 ${formatPrice(latestClose)} `,
                    color: latestLineColor,
                    backgroundColor: 'rgba(7,12,20,0.88)',
                    borderRadius: 6,
                    padding: [4, 6],
                },
            };
        }

        function buildMarkLineConfig(items) {
            if (!Array.isArray(items) || !items.length) return undefined;
            return {
                symbol: ['none', 'none'],
                silent: true,
                animation: false,
                emphasis: { disabled: true },
                data: items,
            };
        }

        function buildFocusMarkPointConfig(index, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length || !Number.isInteger(index) || index < 0) return undefined;
            const context = buildContext(payload, index, selectedSignalId);
            const closeValue = Number(context?.bar?.close || 0);
            if (!context?.bar || !Number.isFinite(closeValue) || closeValue <= 0) return undefined;
            return {
                symbol: 'circle',
                symbolSize: 13,
                silent: true,
                label: { show: false },
                itemStyle: {
                    color: '#7DD3FC',
                    borderColor: 'rgba(8,12,20,0.96)',
                    borderWidth: 2,
                    shadowBlur: 12,
                    shadowColor: 'rgba(125,211,252,0.38)'
                },
                data: [{
                    coord: [context.index, closeValue],
                    value: 'focus'
                }],
            };
        }

        function buildSignalScatter(signals, bars, priceResolver, color, labelBuilder = null) {
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar.bar_time_ms || 0), index]));
            return signals.map((signal) => {
                const barMs = Number(signal.bar_time_ms || 0);
                const xIndex = indexByMs.get(barMs);
                if (xIndex === undefined) return null;
                const yValue = Number(priceResolver(signal) || 0);
                if (!Number.isFinite(yValue) || yValue <= 0) return null;
                return {
                    value: [xIndex, yValue],
                    itemStyle: { color },
                    name: buildTradeSignalLabel(signal),
                    signal_id: signal.signal_id || '',
                    labelText: typeof labelBuilder === 'function' ? String(labelBuilder(signal) || '') : '',
                };
            }).filter(Boolean);
        }

        function buildIndicatorMarkerPoints(bars, indicatorMap, predicate, yResolver, labelBuilder) {
            return bars.map((bar, index) => {
                const indicator = indicatorMap.get(Number(bar?.bar_time_ms || 0));
                if (!indicator || !predicate(indicator, bar, index)) return null;
                const yValue = Number(yResolver(indicator, bar, index));
                if (!Number.isFinite(yValue) || yValue <= 0) return null;
                return {
                    value: [index, yValue],
                    labelText: typeof labelBuilder === 'function' ? String(labelBuilder(indicator, bar, index) || '') : '',
                };
            }).filter(Boolean);
        }

        function buildMarkerScatterSeries(name, data, options = {}) {
            const color = options.color || '#7DD3FC';
            const showLabel = Boolean(options.showLabel);
            const densityTier = chartMarkerDensityTier || '';
            const defaultLabelDistance = densityTier === 'mid' ? 3 : 4;
            const defaultLabelFontSize = densityTier === 'mid' ? 9 : 10;
            const defaultLabelPadding = densityTier === 'mid' ? [1, 3] : [2, 4];
            return {
                name,
                type: 'scatter',
                xAxisIndex: 0,
                yAxisIndex: 0,
                data,
                symbol: options.symbol || 'triangle',
                symbolRotate: Number(options.symbolRotate || 0),
                symbolSize: Number(options.symbolSize || 12),
                z: Number(options.z || 8),
                animation: false,
                tooltip: { show: false },
                label: {
                    show: showLabel,
                    formatter(params) {
                        return params?.data?.labelText || '';
                    },
                    position: options.labelPosition || 'top',
                    distance: Number(options.labelDistance ?? defaultLabelDistance),
                    color: options.labelColor || color,
                    fontSize: Number(options.labelFontSize ?? defaultLabelFontSize),
                    fontWeight: 700,
                    fontFamily: 'JetBrains Mono, monospace',
                    backgroundColor: showLabel ? (densityTier === 'mid' ? 'rgba(8,12,20,0.82)' : 'rgba(8,12,20,0.92)') : 'transparent',
                    borderColor: color,
                    borderWidth: showLabel ? 1 : 0,
                    borderRadius: 6,
                    padding: showLabel ? (options.labelPadding || defaultLabelPadding) : 0,
                },
                labelLayout: {
                    hideOverlap: true,
                    moveOverlap: 'shiftY',
                },
                itemStyle: {
                    color,
                    borderColor: 'rgba(8,12,20,0.96)',
                    borderWidth: 1.4,
                    shadowBlur: Number(options.shadowBlur || 0),
                    shadowColor: options.shadowColor || 'transparent',
                },
                emphasis: {
                    disabled: true,
                },
            };
        }

        function buildTooltipHtml(payload, index) {
            const context = buildContext(payload, index, '');
            const bar = context?.bar || null;
            if (!bar) return '';
            const indicator = context?.indicator || null;
            const isPreviewBar = Boolean(context?.isPreviewBar);
            const signal = context?.activeSignal || null;
            const direction = String(signal?.direction || '').toLowerCase();
            const signalColor = direction === 'short' ? '#FC8181' : '#48BB78';
            const signalText = bar?.preview || bar?.is_preview
                ? 'Forming preview bar · 正式 5m 仍以收盘写入为准'
                : signal
                ? `${escapeHtml(buildTradeSignalLabel(signal))} · ${escapeHtml(String(signal.direction || '--').toUpperCase())}`
                : 'No signal on this bar';
            return `
                <div style="font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.75;min-width:240px;">
                    <div style="font-size:12px;font-weight:700;color:#E2EAF4;margin-bottom:6px;">${escapeHtml(bar.us_time || '--')}</div>
                    <div>O ${escapeHtml(formatPrice(bar.open))} · H ${escapeHtml(formatPrice(bar.high))}</div>
                    <div>L ${escapeHtml(formatPrice(bar.low))} · C ${escapeHtml(formatPrice(bar.close))}</div>
                    <div>Vol ${escapeHtml(formatNumber(bar.volume || 0, 0))}</div>
                    <div style="margin-top:6px;color:#8BA4C4;">${isPreviewBar && !indicator ? 'EMA / VWAP 待收盘' : `EMA20 ${escapeHtml(formatPrice(indicator?.ema_fast))} · EMA50 ${escapeHtml(formatPrice(indicator?.ema_slow))}`}</div>
                    <div style="color:#8BA4C4;">${isPreviewBar && !indicator ? '指标待收盘确认' : `EMA100 ${escapeHtml(formatPrice(indicator?.ema_trend))} · VWAP ${escapeHtml(formatPrice(indicator?.vwap))}`}</div>
                    <div style="color:#8BA4C4;">${isPreviewBar && !indicator ? '预览 bar 仅 OHLC / Volume' : `CRSI ${escapeHtml(formatNumber(indicator?.crsi))} · OBV ${escapeHtml(formatNumber(indicator?.obv_rsi))} · ATR ${escapeHtml(formatPercent(indicator?.atr_pct))}`}</div>
                    <div style="margin-top:6px;color:#8BA4C4;">${isPreviewBar && !indicator ? '收盘入库后补齐指标' : `SD ${escapeHtml(getSdZoneText(indicator?.sd_zone))} · ${escapeHtml(getSdTrendText(indicator?.sd_trend))} · Fractal ${escapeHtml(getFractalSummary(indicator))}`}</div>
                    <div style="color:#8BA4C4;">${isPreviewBar && !indicator ? 'Preview bar 仅用于盘中参考' : `Touch ${escapeHtml(getTouchSummary(indicator))} · Div ${escapeHtml(getDivergenceSummary(indicator, 6))}`}</div>
                    <div style="margin-top:6px;color:${signal ? signalColor : bar?.preview || bar?.is_preview ? '#7DD3FC' : '#8BA4C4'};">${signalText}</div>
                </div>
            `;
        }

        function resolveChartTooltipPosition(point, size) {
            const viewWidth = Number(size?.viewSize?.[0] || 0);
            const viewHeight = Number(size?.viewSize?.[1] || 0);
            const contentWidth = Number(size?.contentSize?.[0] || 0);
            const contentHeight = Number(size?.contentSize?.[1] || 0);
            const padding = isCompactViewport() ? 12 : 16;
            if (!Array.isArray(point) || point.length < 2 || !viewWidth || !viewHeight) {
                return [padding, padding];
            }
            if (isCompactViewport()) {
                return [Math.max(padding, viewWidth - contentWidth - padding), padding];
            }
            const anchorX = Number(point[0] || 0);
            let left = anchorX + padding;
            if (left + contentWidth > viewWidth - padding) {
                left = Math.max(padding, anchorX - contentWidth - padding);
            }
            const top = Math.max(
                padding,
                Math.min(
                    viewHeight - contentHeight - padding,
                    Math.round(viewHeight * 0.5)
                )
            );
            return [left, top];
        }

        function renderChart(payload) {
            const displayPayload = buildChartDisplayPayload(payload);
            chartDisplayPayload = displayPayload || payload || null;
            const bars = Array.isArray(displayPayload?.bars) ? displayPayload.bars.slice() : [];
            const formalBars = Array.isArray(payload?.bars) ? payload.bars.slice() : [];
            const indicators = Array.isArray(payload?.indicators) ? payload.indicators.slice() : [];
            const signals = Array.isArray(payload?.signals) ? payload.signals.slice() : [];
            const latest = payload?.latestIndicator || null;
            const canvas = document.getElementById('chartCanvas');
            const note = document.getElementById('chartNote');

            if (!bars.length) {
                if (chartInstance) {
                    chartInstance.dispose();
                    chartInstance = null;
                }
                chartMarkerDensityTier = '';
                resetChartZoomState();
                canvas.innerHTML = '<div class="chart-empty">暂无可绘制 bars。</div>';
                note.textContent = '图表基于 ibkr_bars 重算。';
                renderChartWorkspaceChrome(displayPayload);
                return;
            }

            const sortedBars = bars.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0));
            const sortedFormalBars = formalBars.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0));
            const indicatorMap = new Map(indicators.map((item) => [Number(item.bar_time_ms || 0), item]));
            const barMap = new Map(sortedFormalBars.map((item) => [Number(item.bar_time_ms || 0), item]));
            const previewBar = sortedBars.find((item) => item?.preview || item?.is_preview) || null;
            const categories = sortedBars.map((bar) => formatChartLabel(bar));
            const candle = sortedBars.map((bar) => [Number(bar.open || 0), Number(bar.close || 0), Number(bar.low || 0), Number(bar.high || 0)]);
            const emaFast = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.ema_fast));
            const emaSlow = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.ema_slow));
            const emaTrend = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.ema_trend));
            const vwap = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.vwap));
            const sdReg = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_reg));
            const sdSignalUpper = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_signal_upper));
            const sdSignalLower = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_signal_lower));
            const sdFilterUpper = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_filter_upper));
            const sdFilterLower = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_filter_lower));
            const sdChannelSpan = sortedBars.map((bar) => {
                const indicator = indicatorMap.get(Number(bar.bar_time_ms || 0));
                const upper = Number(indicator?.sd_signal_upper);
                const lower = Number(indicator?.sd_signal_lower);
                if (!Number.isFinite(upper) || !Number.isFinite(lower)) return null;
                return Math.max(upper - lower, 0);
            });
            const crsi = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.crsi));
            const crsiUpperBand = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.crsi_ub));
            const crsiLowerBand = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.crsi_db));
            const obvRsi = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.obv_rsi));
            const atrPct = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.atr_pct));
            const volume = sortedBars.map((bar) => Number(bar.volume || 0));
            const markerOffset = (indicator, bar, multiplier = 1) => {
                const atrBase = Number(indicator?.atr_raw || indicator?.atr || 0);
                const rangeBase = Math.abs(Number(bar?.high || 0) - Number(bar?.low || 0));
                const closeBase = Math.abs(Number(bar?.close || 0)) * 0.0008;
                return Math.max(atrBase, rangeBase, closeBase, 0.05) * multiplier;
            };
            const densityTier = getChartMarkerDensityTier(sortedBars.length);
            chartMarkerDensityTier = densityTier;
            const showContextMarkers = densityTier !== 'wide';
            const showMarkerLabels = densityTier !== 'wide';
            const showTradeLabels = densityTier !== 'wide';
            const sessionDividerItemsMain = buildSessionDividerMarkLineItems(sortedBars, { showLabels: densityTier !== 'wide' });
            const sessionDividerItemsSub = buildSessionDividerMarkLineItems(sortedBars, { showLabels: false });
            const longSignals = buildSignalScatter(
                signals.filter((item) => String(item.direction || '').toLowerCase() === 'long'),
                sortedFormalBars,
                (signal) => signal.entry || signal.limit_price || barMap.get(Number(signal.bar_time_ms || 0))?.close,
                '#48BB78',
                buildTradeSignalLabel
            );
            const shortSignals = buildSignalScatter(
                signals.filter((item) => String(item.direction || '').toLowerCase() === 'short'),
                sortedFormalBars,
                (signal) => signal.entry || signal.limit_price || barMap.get(Number(signal.bar_time_ms || 0))?.close,
                '#FC8181',
                buildTradeSignalLabel
            );
            const traceTimeline = Array.isArray(displayPayload?.traceTimeline) ? displayPayload.traceTimeline : [];
            const previewCandidateSignals = buildSignalScatter(
                traceTimeline
                    .filter((item) => Boolean(item?.is_preview) && ['candidate', 'blocked'].includes(String(item?.signal_state?.stage || '').trim().toLowerCase()))
                    .map((item) => ({
                        ...(item?.signal_state?.signal_payload || {}),
                        bar_time_ms: item?.bar_time_ms,
                        direction: item?.signal_state?.direction || item?.signal_state?.signal_payload?.direction || '',
                        signal: item?.signal_state?.signal || item?.signal_state?.signal_payload?.signal || '',
                        trace_stage: item?.signal_state?.stage || '',
                    })),
                sortedBars,
                (signal) => signal.entry || signal.limit_price || signal.close || sortedBars[sortedBars.length - 1]?.close,
                '#FBBF24',
                () => '候选'
            );
            const fractalBullMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.fractal_bull),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 0.45),
                () => 'F↑'
            );
            const fractalBearMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.fractal_bear),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 0.45),
                () => 'F↓'
            );
            const sdLowerMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.sd_lower),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 0.8),
                () => 'SD↑'
            );
            const sdUpperMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.sd_upper),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 0.8),
                () => 'SD↓'
            );
            const bullTouchFastMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bull_touch_fast),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.05),
                () => 'E↑F'
            );
            const bullTouchSlowMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bull_touch_slow),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.3),
                () => 'E↑S'
            );
            const bearTouchFastMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bear_touch_fast),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.05),
                () => 'E↓F'
            );
            const bearTouchSlowMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bear_touch_slow),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.3),
                () => 'E↓S'
            );
            const crsiRegBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.6), () => 'cR↑');
            const crsiWideBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.9), () => 'cW↑');
            const obvRegBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 2.2), () => 'oR↑');
            const obvWideBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 2.5), () => 'oW↑');
            const crsiRegHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 2.8), () => 'cH↑');
            const crsiWideHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 3.1), () => 'cWH↑');
            const obvRegHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 3.4), () => 'oH↑');
            const obvWideHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 3.7), () => 'oWH↑');
            const crsiRegBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.6), () => 'cR↓');
            const crsiWideBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.9), () => 'cW↓');
            const obvRegBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 2.2), () => 'oR↓');
            const obvWideBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 2.5), () => 'oW↓');
            const crsiRegHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 2.8), () => 'cH↓');
            const crsiWideHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 3.1), () => 'cWH↓');
            const obvRegHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 3.4), () => 'oH↓');
            const obvWideHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 3.7), () => 'oWH↓');
            const preserveCursorIndex = hoverBarIndex >= 0;
            const preferredFocusIndex = getEffectiveCursorIndex(displayPayload);
            const focus = Number.isInteger(preferredFocusIndex) && preferredFocusIndex >= 0
                ? buildContext(displayPayload, preferredFocusIndex, selectedSignalId)
                : buildContext(displayPayload, sortedBars.length - 1, selectedSignalId);
            const latestClose = Number(realtimeQuoteSnapshot?.last_price ?? previewBar?.close ?? latest?.close ?? sortedBars[sortedBars.length - 1]?.close ?? 0);
            const previousClose = Number(sortedBars.length > 1 ? sortedBars[sortedBars.length - 2]?.close : latestClose);
            const latestLineColor = latestClose >= previousClose ? '#4ADE80' : '#FB7185';
            const latestPriceLineItem = buildLatestPriceMarkLineItem(latestClose, latestLineColor);
            const gridLeft = isCompactViewport() ? 66 : 78;
            const gridRight = isCompactViewport() ? 96 : 116;
            const subplotTitles = [
                {
                    text: 'CRSI / OBV RSI',
                    subtext: '优先展示 cRSI 动态带，80 / 20 仅作弱参考',
                    left: gridLeft,
                    top: '59.3%',
                },
                {
                    text: chartLayerState.volume ? '成交量 / ATR%' : 'ATR%',
                    subtext: chartLayerState.volume ? '柱体=成交量，折线=ATR%' : '当前只显示 ATR% 折线',
                    left: gridLeft,
                    top: '77.2%',
                }
            ];
            const focusMarkPoint = buildFocusMarkPointConfig(focus?.index ?? -1, displayPayload);
            const zoomWindow = getCurrentZoomWindow(sortedBars.length);

            document.getElementById('chartPanelTitle').textContent = `${currentSymbol} · ${getIntervalLabel(currentInterval)}`;
            const compareSummary = comparePayload?.comparison?.summary || null;
            const barsMetaText = `${sortedFormalBars.length} bars${previewBar ? ' + live preview' : ''}`;
            document.getElementById('chartMeta').textContent = compareSummary
                ? `${barsMetaText} · ${indicators.length} ind · ${signals.length} signals · compare bar ${compareSummary.bar_mismatch_count || 0} / ind ${compareSummary.indicator_mismatch_count || 0} / sig ${compareSummary.signal_mismatch_count || 0}`
                : `${barsMetaText} · ${indicators.length} ind · ${signals.length} signals · ${latest?.us_time || sortedFormalBars[sortedFormalBars.length - 1]?.us_time || '--'}`;
            note.textContent = !indicators.length
                ? '当前窗口的 bars 尚未形成可展示的指标快照；EMA / VWAP / Osc 将暂时不可见。'
                : currentInterval === '5m'
                ? `工具条/键盘导航；交易标签${chartLayerState.tradeSignals ? '已开' : '已关'}。`
                : '当前为非 5m 周期，主图仍展示 bars 实时重算出的价格结构与技术图层，但不叠加交易标签。';
            if (realtimeQuoteSnapshot?.last_price != null) {
                note.textContent += ' 价格与日内涨幅来自 WS 实时快照。';
            }
            if (previewBar) {
                note.textContent += ' 当前 5m 叠加了一根未收盘 preview bar；正式写库与指标计算仍只认官方收盘 bar。';
            }
            if (comparePayload) {
                note.textContent += ' 当前主图仍展示 stored bars 链路；右侧 Compare 与上方 Cursor 显示 IBKR API 临时对比结果。';
            } else if (compareError) {
                note.textContent += ' 最近一次 IBKR 对比失败，可查看右侧错误卡。';
            }
            if (supportsSessionDividers()) {
                note.textContent += ' 竖线按美股 ET 时段分隔盘前 / 盘中 / 盘后。';
            }
            note.textContent += isCompactViewport()
                ? ' 手机端会把状态卡和信息卡改成横向滑动区；支持长按锁定光标、轻扫切换 bar、双击回到最新。'
                : ' 支持滚轮缩放、拖拽平移、双击回到最新，以及 ←/→ 与 N/P 键快速导航。';

            if (chartInstance) {
                chartInstance.dispose();
            }
            canvas.innerHTML = '';
            chartInstance = echarts.init(canvas);
            const overlaySeries = [
                ...(chartLayerState.sdChannel ? [
                    {
                        name: 'SD Channel Base',
                        type: 'line',
                        data: sdSignalLower,
                        stack: 'sd-channel',
                        symbol: 'none',
                        connectNulls: true,
                        smooth: false,
                        silent: true,
                        tooltip: { show: false },
                        z: 1,
                        lineStyle: { width: 0, opacity: 0 },
                        areaStyle: { opacity: 0 },
                        emphasis: { disabled: true },
                    },
                    {
                        name: 'SD Channel Fill',
                        type: 'line',
                        data: sdChannelSpan,
                        stack: 'sd-channel',
                        symbol: 'none',
                        connectNulls: true,
                        smooth: false,
                        silent: true,
                        tooltip: { show: false },
                        z: 1,
                        lineStyle: { width: 0, opacity: 0 },
                        areaStyle: {
                            color: 'rgba(56, 189, 248, 0.12)',
                            shadowBlur: 18,
                            shadowColor: 'rgba(56, 189, 248, 0.08)',
                        },
                        emphasis: { disabled: true },
                    },
                    { name: 'SD Reg', type: 'line', data: sdReg, symbol: 'none', connectNulls: true, smooth: false, z: 4, lineStyle: { width: 1.25, color: 'rgba(125,211,252,0.68)', type: 'dashed' } },
                    { name: 'SD Signal Upper', type: 'line', data: sdSignalUpper, symbol: 'none', connectNulls: true, smooth: false, z: 5, lineStyle: { width: 1.55, color: 'rgba(248,113,113,0.84)' } },
                    { name: 'SD Signal Lower', type: 'line', data: sdSignalLower, symbol: 'none', connectNulls: true, smooth: false, z: 5, lineStyle: { width: 1.55, color: 'rgba(74,222,128,0.84)' } },
                    { name: 'SD Filter Upper', type: 'line', data: sdFilterUpper, symbol: 'none', connectNulls: true, smooth: false, z: 4, lineStyle: { width: 1.15, color: 'rgba(248,113,113,0.46)', type: 'dashed' } },
                    { name: 'SD Filter Lower', type: 'line', data: sdFilterLower, symbol: 'none', connectNulls: true, smooth: false, z: 4, lineStyle: { width: 1.15, color: 'rgba(74,222,128,0.46)', type: 'dashed' } },
                    buildMarkerScatterSeries('SD MR Bull', sdLowerMarkers, { color: '#4CAF50', symbol: 'triangle', symbolSize: 13, showLabel: showMarkerLabels, labelPosition: 'bottom', shadowBlur: 10, shadowColor: 'rgba(76,175,80,0.24)' }),
                    buildMarkerScatterSeries('SD MR Bear', sdUpperMarkers, { color: '#FF8A00', symbol: 'triangle', symbolRotate: 180, symbolSize: 13, showLabel: showMarkerLabels, labelPosition: 'top', shadowBlur: 10, shadowColor: 'rgba(255,138,0,0.24)' }),
                ] : []),
                ...(chartLayerState.fractal ? [
                    buildMarkerScatterSeries('Fractal Bull', fractalBullMarkers, { color: '#14B8A6', symbol: 'triangle', symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('Fractal Bear', fractalBearMarkers, { color: '#F44336', symbol: 'triangle', symbolRotate: 180, symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'top' }),
                ] : []),
                ...(chartLayerState.emaTouch && showContextMarkers ? [
                    buildMarkerScatterSeries('EMA Touch Bull Fast', bullTouchFastMarkers, { color: '#00C853', symbol: 'triangle', symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('EMA Touch Bull Slow', bullTouchSlowMarkers, { color: '#64DD17', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('EMA Touch Bear Fast', bearTouchFastMarkers, { color: '#FF1744', symbol: 'triangle', symbolRotate: 180, symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('EMA Touch Bear Slow', bearTouchSlowMarkers, { color: '#D50000', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                ] : []),
                ...(chartLayerState.divergence && showContextMarkers ? [
                    buildMarkerScatterSeries('cRSI Reg Bull Div', crsiRegBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('cRSI Wide Bull Div', crsiWideBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('OBV Reg Bull Div', obvRegBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('OBV Wide Bull Div', obvWideBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('cRSI Hid Bull', crsiRegHidBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('cRSI Wide Hid Bull', crsiWideHidBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('OBV Hid Bull', obvRegHidBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('OBV Wide Hid Bull', obvWideHidBullMarkers, { color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' }),
                    buildMarkerScatterSeries('cRSI Reg Bear Div', crsiRegBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('cRSI Wide Bear Div', crsiWideBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('OBV Reg Bear Div', obvRegBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('OBV Wide Bear Div', obvWideBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('cRSI Hid Bear', crsiRegHidBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('cRSI Wide Hid Bear', crsiWideHidBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('OBV Hid Bear', obvRegHidBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                    buildMarkerScatterSeries('OBV Wide Hid Bear', obvWideHidBearMarkers, { color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' }),
                ] : []),
                ...(isTradeSignalInterval() && chartLayerState.tradeSignals ? [
                    buildMarkerScatterSeries('LONG Signal', longSignals, { color: '#48BB78', symbol: 'circle', symbolSize: 12, showLabel: showTradeLabels, labelPosition: 'bottom', shadowBlur: 14, shadowColor: 'rgba(72,187,120,0.28)' }),
                    buildMarkerScatterSeries('SHORT Signal', shortSignals, { color: '#FC8181', symbol: 'circle', symbolSize: 12, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 14, shadowColor: 'rgba(252,129,129,0.28)' }),
                    buildMarkerScatterSeries('Preview Signal', previewCandidateSignals, { color: '#FBBF24', symbol: 'diamond', symbolSize: 13, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 12, shadowColor: 'rgba(251,191,36,0.26)' }),
                ] : []),
            ];
            const series = [
                {
                    id: 'price-main',
                    name: 'Price',
                    type: 'candlestick',
                    data: candle,
                    barMaxWidth: 16,
                    barMinWidth: 6,
                    itemStyle: {
                        color: '#22C55E',
                        color0: '#FB7185',
                        borderColor: '#4ADE80',
                        borderColor0: '#FB7185'
                    },
                    emphasis: {
                        itemStyle: {
                            borderWidth: 1.4
                        }
                    },
                    markLine: buildMarkLineConfig([
                        ...(latestPriceLineItem ? [latestPriceLineItem] : []),
                        ...sessionDividerItemsMain,
                    ]),
                    markPoint: focusMarkPoint,
                },
                ...(chartLayerState.ema ? [
                    { name: 'EMA 20', type: 'line', data: emaFast, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.35, color: '#38BDF8' } },
                    { name: 'EMA 50', type: 'line', data: emaSlow, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.25, color: '#F59E0B' } },
                    { name: 'EMA 100', type: 'line', data: emaTrend, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.15, color: '#A78BFA' } },
                ] : []),
                ...(chartLayerState.vwap ? [
                    { name: 'VWAP', type: 'line', data: vwap, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.15, color: '#34D399' } },
                ] : []),
                ...overlaySeries,
                {
                    name: 'cRSI Upper Band',
                    type: 'line',
                    xAxisIndex: 1,
                    yAxisIndex: 1,
                    data: crsiUpperBand,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1, color: 'rgba(251,191,36,0.72)', type: 'dashed' },
                },
                {
                    name: 'cRSI Lower Band',
                    type: 'line',
                    xAxisIndex: 1,
                    yAxisIndex: 1,
                    data: crsiLowerBand,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1, color: 'rgba(251,191,36,0.72)', type: 'dashed' },
                },
                {
                    name: 'CRSI',
                    type: 'line',
                    xAxisIndex: 1,
                    yAxisIndex: 1,
                    data: crsi,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1.2, color: '#22C55E' },
                    markLine: buildMarkLineConfig([
                        { yAxis: 80, label: { show: false }, lineStyle: { color: 'rgba(245,158,11,0.24)', type: 'dashed' } },
                        { yAxis: 20, label: { show: false }, lineStyle: { color: 'rgba(245,158,11,0.24)', type: 'dashed' } },
                        ...sessionDividerItemsSub,
                    ])
                },
                { name: 'OBV RSI', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: obvRsi, symbol: 'none', connectNulls: true, lineStyle: { width: 1.2, color: '#F59E0B' } },
                ...(chartLayerState.volume ? [
                    { name: 'Volume', type: 'bar', xAxisIndex: 2, yAxisIndex: 2, data: volume, itemStyle: { color: 'rgba(56,189,248,0.34)' } },
                ] : []),
                {
                    name: 'ATR %',
                    type: 'line',
                    xAxisIndex: 2,
                    yAxisIndex: 3,
                    data: atrPct,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1.2, color: '#FB7185' },
                    markLine: buildMarkLineConfig(sessionDividerItemsSub)
                }
            ];
            chartTooltipPinned = Boolean(chartPointerLocked);
            chartInstance.setOption({
                animation: false,
                backgroundColor: 'transparent',
                toolbox: { show: false },
                title: subplotTitles.map((item) => ({
                    text: item.text,
                    subtext: item.subtext,
                    left: item.left,
                    top: item.top,
                    padding: [4, 8, 4, 8],
                    backgroundColor: 'rgba(7, 12, 20, 0.74)',
                    borderColor: 'rgba(99,179,237,0.14)',
                    borderWidth: 1,
                    itemGap: 2,
                    textStyle: {
                        color: '#d7e2f0',
                        fontSize: 10,
                        fontWeight: 700,
                        fontFamily: 'JetBrains Mono',
                    },
                    subtextStyle: {
                        color: '#8BA4C4',
                        fontSize: 9,
                        lineHeight: 12,
                        fontFamily: 'JetBrains Mono',
                    }
                })),
                legend: {
                    show: false
                },
                tooltip: {
                    trigger: 'axis',
                    axisPointer: { type: 'cross', snap: true },
                    alwaysShowContent: chartTooltipPinned,
                    transitionDuration: 0,
                    backgroundColor: 'rgba(8,12,20,0.96)',
                    borderColor: 'rgba(99,179,237,0.16)',
                    textStyle: { color: '#E2EAF4' },
                    enterable: false,
                    confine: true,
                    position(point, params, dom, rect, size) {
                        return resolveChartTooltipPosition(point, size);
                    },
                    formatter(params) {
                        const list = Array.isArray(params) ? params : [params];
                        const axisParam = list.find((item) => Number.isInteger(item?.dataIndex));
                        const index = Number(axisParam?.dataIndex);
                        if (!Number.isInteger(index) || index < 0) return '';
                        return buildTooltipHtml(displayPayload, index);
                    }
                },
                axisPointer: {
                    link: [{ xAxisIndex: 'all' }],
                    label: {
                        backgroundColor: 'rgba(15, 23, 38, 0.96)',
                        color: '#E2EAF4',
                        borderColor: 'rgba(99,179,237,0.24)',
                        borderWidth: 1,
                    },
                    lineStyle: {
                        color: 'rgba(148, 163, 184, 0.42)',
                        width: 1,
                    }
                },
                grid: [
                    { left: gridLeft, right: gridRight, top: 68, height: '49%' },
                    { left: gridLeft, right: gridRight, top: '59%', height: '12%' },
                    { left: gridLeft, right: gridRight, top: '77%', height: '12%' }
                ],
                dataZoom: [
                    {
                        type: 'inside',
                        xAxisIndex: [0, 1, 2],
                        start: zoomWindow.start,
                        end: zoomWindow.end,
                        // Let desktop trackpad two-finger scroll move the page instead of hijacking it for zoom.
                        zoomOnMouseWheel: !isDesktopFinePointer(),
                        moveOnMouseMove: true,
                        moveOnMouseWheel: false,
                    },
                    {
                        type: 'slider',
                        xAxisIndex: [0, 1, 2],
                        start: zoomWindow.start,
                        end: zoomWindow.end,
                        bottom: 10,
                        height: 20,
                        borderColor: 'rgba(99,179,237,0.08)',
                        backgroundColor: 'rgba(8,12,20,0.76)',
                        fillerColor: 'rgba(56,189,248,0.16)',
                        dataBackground: {
                            lineStyle: { color: 'rgba(99,179,237,0.28)' },
                            areaStyle: { color: 'rgba(99,179,237,0.10)' },
                        },
                        handleStyle: {
                            color: '#0B1728',
                            borderColor: '#7DD3FC',
                        },
                        textStyle: { color: '#8BA4C4' }
                    }
                ],
                xAxis: [
                    {
                        type: 'category',
                        data: categories,
                        scale: true,
                        boundaryGap: true,
                        axisLine: { lineStyle: { color: '#30435E' } },
                        axisTick: { show: false },
                        axisLabel: { color: '#8BA4C4', fontSize: 10, hideOverlap: true },
                        splitLine: { show: false }
                    },
                    {
                        type: 'category',
                        gridIndex: 1,
                        data: categories,
                        axisLabel: { show: false },
                        axisTick: { show: false },
                        axisLine: { lineStyle: { color: '#30435E' } },
                        splitLine: { show: false }
                    },
                    {
                        type: 'category',
                        gridIndex: 2,
                        data: categories,
                        axisLabel: { color: '#8BA4C4', fontSize: 10, hideOverlap: true },
                        axisTick: { show: false },
                        axisLine: { lineStyle: { color: '#30435E' } },
                        splitLine: { show: false }
                    }
                ],
                yAxis: [
                    {
                        scale: true,
                        position: 'right',
                        splitNumber: 5,
                        splitLine: { lineStyle: { color: 'rgba(99,179,237,0.06)' } },
                        axisLabel: { color: '#8BA4C4', fontSize: 10 },
                    },
                    {
                        gridIndex: 1,
                        min: 0,
                        max: 100,
                        position: 'right',
                        splitLine: { lineStyle: { color: 'rgba(99,179,237,0.06)' } },
                        axisLabel: { color: '#8BA4C4', fontSize: 10 }
                    },
                    {
                        gridIndex: 2,
                        splitLine: { show: false },
                        axisLabel: { color: '#8BA4C4', fontSize: 10, formatter: (value) => formatCompactAxisNumber(value) },
                    },
                    {
                        gridIndex: 2,
                        position: 'right',
                        splitLine: { show: false },
                        axisLabel: { color: '#8BA4C4', fontSize: 10, formatter: '{value}%' }
                    }
                ],
                series
            });
            chartInstance.off('updateAxisPointer');
            chartInstance.off('dataZoom');
            chartInstance.off('click');
            chartInstance.off('dblclick');
            chartInstance.on('updateAxisPointer', (params) => {
                const axisInfo = Array.isArray(params?.axesInfo) ? params.axesInfo[0] : null;
                const axisValue = Number(axisInfo?.value);
                if (Number.isInteger(axisValue) && axisValue >= 0) {
                    syncCursorIndex(axisValue);
                }
            });
            chartInstance.on('dataZoom', (params) => {
                const batch = Array.isArray(params?.batch) && params.batch.length ? params.batch[0] : params;
                const start = Number(batch?.start);
                const end = Number(batch?.end);
                if (!Number.isFinite(start) || !Number.isFinite(end)) return;
                applyChartViewportState({ start, end }, sortedBars.length);
                renderChartBottomBar(displayPayload);
                const nextDensityTier = getChartMarkerDensityTier(sortedBars.length);
                if (nextDensityTier !== chartMarkerDensityTier && lastPayload) {
                    window.requestAnimationFrame(() => renderChart(lastPayload));
                }
            });
            chartInstance.on('click', (params) => {
                if (suppressNextChartClick) {
                    suppressNextChartClick = false;
                    return;
                }
                const index = Number.isInteger(params?.dataIndex)
                    ? params.dataIndex
                    : (Array.isArray(params?.value) && Number.isInteger(params.value[0]) ? params.value[0] : -1);
                if (index < 0) return;
                dismissMobileGestureHint();
                const signalKey = params?.data?.signal_id ? String(params.data.signal_id) : '';
                if (isCompactViewport() && chartPointerLocked) {
                    lockChartPointerAtIndex(index, signalKey);
                    const lockedContext = getFocusContext(getChartDisplayPayload());
                    if (lockedContext?.activeSignal) {
                        openSignalDrawer(lockedContext.activeSignal, lockedContext);
                    }
                    return;
                }
                chartPointerLocked = false;
                focusBarIndex(index, signalKey);
                syncCursorIndex(index);
                ensureBarVisible(index, displayPayload);
                const context = buildContext(displayPayload, index, signalKey);
                if (context?.activeSignal) {
                    openSignalDrawer(context.activeSignal, context);
                }
            });
            chartInstance.on('dblclick', () => {
                chartPointerLocked = false;
                focusLatestBarAction();
                resetChartViewAction();
            });
            const zr = chartInstance.getZr();
            if (zr) {
                zr.off('globalout');
                zr.on('globalout', () => {
                    clearTransientChartCursor();
                });
            }
            registerChartTouchInteractions();

            if (focus) {
                if (!preserveCursorIndex || chartPointerLocked) {
                    selectedBarIndex = focus.index;
                }
                if ((!preserveCursorIndex || chartPointerLocked) && !selectedSignalId && focus.activeSignal) {
                    selectedSignalId = String(focus.activeSignal.signal_id || focus.activeSignal.id || '');
                }
                renderInfoRail(displayPayload);
                hoverBarIndex = preserveCursorIndex ? focus.index : -1;
                renderCursorStrip(displayPayload);
                if (chartPointerLocked || hoverBarIndex >= 0) {
                    scheduleChartTooltipSync(focus.index);
                } else {
                    hideChartTooltip();
                }
                if (shouldKeepViewportFollowingFocus(focus.index, displayPayload)) {
                    ensureBarVisible(focus.index, displayPayload);
                }
            }
        }

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
                        environment: currentEnvironment,
                        symbol: currentSymbol,
                        interval: currentInterval,
                        start_ms: requestBounds.startMs,
                        end_ms: requestBounds.endMs,
                        include_signals: currentInterval === '5m',
                        include_trace: true,
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
                        environment: currentEnvironment,
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
            resetChartZoomState();
            closeMobileQuickPanel();
            await loadChartWorkspace();
        };

        window.selectChartRange = async function(rangeKey) {
            currentRangeKey = normalizeRangeKey(rangeKey, currentInterval);
            if (isCustomRange()) {
                seedCustomRangeFromPayload();
                currentAnchorMs = customRangeEndMs || currentAnchorMs;
                renderRangeGroup();
            }
            pendingFocusBarTimeMs = 0;
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            resetChartZoomState();
            closeMobileQuickPanel();
            await loadChartWorkspace();
        };

        window.applyCustomChartRange = async function() {
            const startInput = document.getElementById('customRangeStart');
            const endInput = document.getElementById('customRangeEnd');
            const startMs = parseDateTimeLocalValue(startInput?.value || '');
            const endMs = parseDateTimeLocalValue(endInput?.value || '');
            if (!startMs || !endMs) {
                showToast('请填写自定义开始和结束时间');
                return;
            }
            if (endMs <= startMs) {
                showToast('结束时间必须晚于开始时间');
                return;
            }
            currentRangeKey = 'custom';
            customRangeStartMs = startMs;
            customRangeEndMs = endMs;
            currentAnchorMs = endMs;
            pendingFocusBarTimeMs = 0;
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
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
                    closeInspectorDrawer();
                    closeMobileQuickPanel();
                    closeSignalDrawer();
                }
                return;
            }
            const displayPayload = getChartDisplayPayload();
            if (!displayPayload || !Array.isArray(displayPayload.bars) || !displayPayload.bars.length) {
                if (event.key === 'Escape') {
                    closeInspectorDrawer();
                    closeMobileQuickPanel();
                    closeSignalDrawer();
                }
                return;
            }
            if (event.metaKey || event.ctrlKey || event.altKey) {
                if (event.key === 'Escape') {
                    closeInspectorDrawer();
                    closeMobileQuickPanel();
                    closeSignalDrawer();
                }
                return;
            }
            if (event.key === 'Escape') {
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
            currentEnvironment = environment;
            currentAnchorMs = 0;
            pendingFocusBarTimeMs = 0;
            currentIndicatorId = '';
            selectedBarIndex = -1;
            selectedSignalId = '';
            hoverBarIndex = -1;
            activeDrawerSignalId = '';
            resetChartZoomState();
            closeMobileQuickPanel();
            await resolveInitialContext();
            await loadChartWorkspace();
        };

        document.addEventListener('DOMContentLoaded', async () => {
            document.getElementById('nav').innerHTML = renderNav('/ibkr_chart.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('📉 IBKR 图表', { subtitle: '全屏分析 / 信号叠加' });
            document.getElementById('pageBridge').innerHTML = '';

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
                const chartShell = document.querySelector('.chart-stage-shell');
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
