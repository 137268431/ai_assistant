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

        function parseEtDateParts(ms) {
            const num = Number(ms || 0);
            if (!Number.isFinite(num) || num <= 0) return null;
            try {
                const parts = new Intl.DateTimeFormat('en-US', {
                    timeZone: 'America/New_York',
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit',
                    hourCycle: 'h23',
                }).formatToParts(new Date(num));
                const byType = Object.fromEntries(parts.map((part) => [part.type, part.value]));
                return {
                    year: Number(byType.year || 0),
                    month: Number(byType.month || 0),
                    day: Number(byType.day || 0),
                    hour: Number(byType.hour || 0),
                    minute: Number(byType.minute || 0),
                    second: Number(byType.second || 0),
                };
            } catch (_) {
                return null;
            }
        }

        function getEtOffsetMinutes(ms) {
            const parts = parseEtDateParts(ms);
            if (!parts) return 0;
            const asUtcMs = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second || 0);
            return Math.round((asUtcMs - Number(ms || 0)) / 60000);
        }

        function formatDateTimeEtInputValue(ms) {
            const parts = parseEtDateParts(ms);
            if (!parts) return '';
            return `${parts.year}/${padDatePart(parts.month)}/${padDatePart(parts.day)} ${padDatePart(parts.hour)}:${padDatePart(parts.minute)}`;
        }

        function parseDateTimeEtInputValue(value) {
            const text = String(value || '').trim();
            if (!text) return 0;
            const match = text.match(/^(\d{4})[-/](\d{2})[-/](\d{2})(?:T|\s+)(\d{2}):(\d{2})(?::(\d{2}))?$/);
            if (!match) return 0;
            const [, yearText, monthText, dayText, hourText, minuteText, secondText] = match;
            const year = Number(yearText);
            const month = Number(monthText);
            const day = Number(dayText);
            const hour = Number(hourText);
            const minute = Number(minuteText);
            const second = Number(secondText || 0);
            if (![year, month, day, hour, minute, second].every(Number.isFinite)) return 0;
            const wallUtcMs = Date.UTC(year, month - 1, day, hour, minute, second);
            let resolvedMs = wallUtcMs - getEtOffsetMinutes(wallUtcMs) * 60000;
            const resolvedParts = parseEtDateParts(resolvedMs);
            if (
                resolvedParts
                && (resolvedParts.year !== year
                    || resolvedParts.month !== month
                    || resolvedParts.day !== day
                    || resolvedParts.hour !== hour
                    || resolvedParts.minute !== minute)
            ) {
                resolvedMs = wallUtcMs - getEtOffsetMinutes(resolvedMs) * 60000;
            }
            return Number.isFinite(resolvedMs) ? resolvedMs : 0;
        }

        function syncCustomRangePickerValue(picker, value) {
            if (picker && typeof picker.setDate === 'function') {
                picker.setDate(value || null, false, 'Y/m/d H:i');
                return;
            }
            if (picker?.input) picker.input.value = value || '';
        }

        function ensureCustomRangePickers() {
            const startInput = document.getElementById('customRangeStart');
            const endInput = document.getElementById('customRangeEnd');
            if (!startInput || !endInput || typeof flatpickr !== 'function') return;
            const locale = flatpickr.l10ns?.zh || undefined;
            const baseOptions = {
                enableTime: true,
                time_24hr: true,
                dateFormat: 'Y/m/d H:i',
                minuteIncrement: 1,
                allowInput: true,
                disableMobile: true,
                locale,
                appendTo: document.body,
            };
            if (!customRangeStartPicker) {
                customRangeStartPicker = flatpickr(startInput, {
                    ...baseOptions,
                    onReady(_, __, instance) {
                        instance.calendarContainer?.classList.add('ibkr-chart-picker');
                    },
                    onChange(selectedDates, dateStr) {
                        if (customRangeEndPicker && selectedDates?.[0]) {
                            customRangeEndPicker.set('minDate', dateStr || null);
                        }
                    },
                });
            }
            if (!customRangeEndPicker) {
                customRangeEndPicker = flatpickr(endInput, {
                    ...baseOptions,
                    onReady(_, __, instance) {
                        instance.calendarContainer?.classList.add('ibkr-chart-picker');
                    },
                    onChange(selectedDates, dateStr) {
                        if (customRangeStartPicker && selectedDates?.[0]) {
                            customRangeStartPicker.set('maxDate', dateStr || null);
                        }
                    },
                });
            }
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
                { key: 'vwap', label: 'VWAP', shortLabel: 'VWAP', disabled: false, swatches: ['#34D399', '#6EE7B7', '#A7F3D0'], description: 'VWAP 主线与 1/2 标准差带' },
                { key: 'sdChannel', label: 'SD Channel', shortLabel: 'SD', disabled: false, swatches: ['#7DD3FC', '#F87171', '#4ADE80', '#FBBF24'], description: '标准差回归线、上下轨、压缩状态与 ORB 突破' },
                { key: 'fractal', label: 'Fractal', shortLabel: 'Frac', disabled: false, swatches: ['#14B8A6', '#F44336'], description: '上下分形标记' },
                { key: 'emaTouch', label: 'EMA Touch', shortLabel: 'Touch', disabled: false, swatches: ['#00C853', '#D50000'], description: 'EMA touch 多空提示' },
                { key: 'divergence', label: 'Divergence', shortLabel: 'Div', disabled: false, swatches: ['#2196F3', '#9C27B0'], description: '背离提示标记' },
                { key: 'tradeSignals', label: 'Trade Signals', shortLabel: 'Signal', disabled: !isTradeSignalInterval(), swatches: ['#48BB78', '#FC8181'], description: '多空交易信号' },
                { key: 'riskLevels', label: 'Risk Levels', shortLabel: 'Risk', disabled: !isTradeSignalInterval(), swatches: ['#38BDF8', '#F97316'], description: '生成信号的 TP/Target 与 SL 水平线、距离和历史胜率' },
                { key: 'lifecycle', label: 'Signal Flow', shortLabel: 'Flow', disabled: !isTradeSignalInterval(), swatches: ['#4ADE80', '#F97316', '#38BDF8'], description: 'SD窗口、组件收集、候选/过滤/确认生命周期' },
                { key: 'volume', label: 'Volume', shortLabel: 'Vol', disabled: false, swatches: ['#38BDF8'], description: '成交量柱体' },
            ].filter((item) => !item.disabled);
        }

        function formatPrice(value) {
            const num = Number(value);
            return Number.isFinite(num) ? `$${num.toFixed(2)}` : '--';
        }

        function coerceFiniteNumber(...values) {
            for (const value of values) {
                if (value === null || value === undefined || value === '') continue;
                const num = Number(value);
                if (Number.isFinite(num)) return num;
            }
            return NaN;
        }

        function getRiskStatsKey(symbol, direction, signalName) {
            return [
                String(symbol || '').trim().toUpperCase(),
                String(direction || '').trim().toLowerCase(),
                String(signalName || '').trim(),
            ].join('|');
        }

        function getSignalRiskStats(payload, signal) {
            const statsMap = payload?.riskStats && typeof payload.riskStats === 'object' ? payload.riskStats : {};
            const key = getRiskStatsKey(signal?.symbol || currentSymbol, signal?.direction, signal?.signal || getSignalField(signal, 'setup', ''));
            const stats = statsMap[key];
            if (stats && typeof stats === 'object') return stats;
            if (payload?.meta?.backtest_run_id) {
                return {
                    sample_count: 0,
                    wins: 0,
                    losses: 0,
                    win_rate: null,
                    source_run_id: payload.meta.backtest_run_id,
                    status: 'insufficient_sample',
                };
            }
            return {
                sample_count: 0,
                wins: 0,
                losses: 0,
                win_rate: null,
                source_run_id: '',
                status: 'no_backtest_run',
            };
        }

        function formatRiskWinRate(stats, { compact = false } = {}) {
            const sampleCount = Number(stats?.sample_count || 0);
            if (!stats?.source_run_id) return compact ? '胜率 --' : '需指定回测 run';
            if (!sampleCount) return compact ? 'n=0' : '样本不足';
            const rate = Number(stats?.win_rate);
            const rateText = Number.isFinite(rate) ? `${rate.toFixed(rate % 1 === 0 ? 0 : 1)}%` : '--';
            const sampleText = `n=${sampleCount}`;
            const lowSample = sampleCount < 10 ? (compact ? '少' : '样本少') : '';
            return compact
                ? `W ${rateText} ${sampleText}${lowSample ? ` ${lowSample}` : ''}`
                : `历史胜率 ${rateText} · ${sampleText}${lowSample ? ` · ${lowSample}` : ''}`;
        }

        function getRiskTargetLabel(signal) {
            const extra = getSignalExtra(signal);
            const settings = extra.exit_policy_settings && typeof extra.exit_policy_settings === 'object'
                ? extra.exit_policy_settings
                : {};
            const targetState = extra.target_state && typeof extra.target_state === 'object'
                ? extra.target_state
                : {};
            const targetMode = String(settings.target_mode || targetState.target_mode || '').trim().toLowerCase();
            return targetMode && targetMode !== 'hard_rr' ? 'Target' : 'TP';
        }

        function getSignalRiskPlan(signal, bar = null) {
            if (!signal || typeof signal !== 'object') {
                return { valid: false, reason: 'missing_signal' };
            }
            const extra = getSignalExtra(signal);
            const direction = String(signal.direction || extra.direction || '').trim().toLowerCase();
            const entry = coerceFiniteNumber(signal.entry, signal.limit_price, extra.entry, extra.entry_price, bar?.close);
            const takeProfit = coerceFiniteNumber(signal.take_profit, signal.initial_take_profit, extra.take_profit, extra.initial_take_profit);
            const stopLoss = coerceFiniteNumber(signal.stop_loss, signal.initial_stop_loss, extra.stop_loss, extra.initial_stop_loss);
            let rr = coerceFiniteNumber(signal.rr, extra.rr);
            const targetLabel = getRiskTargetLabel(signal);
            const invalidBase = direction !== 'long' && direction !== 'short';
            const missingPrices = !Number.isFinite(entry) || !Number.isFinite(takeProfit) || !Number.isFinite(stopLoss)
                || entry <= 0 || takeProfit <= 0 || stopLoss <= 0;
            const validOrder = direction === 'short'
                ? takeProfit < entry && entry < stopLoss
                : stopLoss < entry && entry < takeProfit;
            if (!Number.isFinite(rr) && !missingPrices && validOrder) {
                const risk = Math.abs(entry - stopLoss);
                const reward = Math.abs(takeProfit - entry);
                rr = risk > 0 ? reward / risk : NaN;
            }
            return {
                valid: !invalidBase && !missingPrices && validOrder,
                reason: invalidBase ? 'invalid_direction' : missingPrices ? 'missing_prices' : validOrder ? '' : 'invalid_price_order',
                direction,
                entry,
                takeProfit,
                stopLoss,
                rr: Number.isFinite(rr) ? rr : null,
                targetLabel,
                targetMode: String(
                    extra.exit_policy_settings?.target_mode
                    || extra.target_state?.target_mode
                    || 'hard_rr'
                ),
            };
        }

        function getRiskReferencePrice(payload = getChartDisplayPayload(), context = null) {
            const live = coerceFiniteNumber(realtimeQuoteSnapshot?.last_price);
            if (Number.isFinite(live) && live > 0) return live;
            const bar = context?.bar || null;
            if (bar?.preview || bar?.is_preview) {
                const previewClose = coerceFiniteNumber(bar.close);
                if (Number.isFinite(previewClose) && previewClose > 0) return previewClose;
            }
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const latestClose = coerceFiniteNumber(bars[bars.length - 1]?.close);
            return Number.isFinite(latestClose) && latestClose > 0 ? latestClose : NaN;
        }

        function getRiskDistanceState(signal, referencePrice, bar = null) {
            const plan = getSignalRiskPlan(signal, bar);
            const reference = Number(referencePrice);
            if (!plan.valid || !Number.isFinite(reference) || reference <= 0) {
                return { ...plan, referencePrice: reference, hasDistances: false };
            }
            const tpRemaining = plan.direction === 'short'
                ? reference - plan.takeProfit
                : plan.takeProfit - reference;
            const slRemaining = plan.direction === 'short'
                ? plan.stopLoss - reference
                : reference - plan.stopLoss;
            return {
                ...plan,
                referencePrice: reference,
                hasDistances: true,
                tpRemaining,
                slRemaining,
                tpRemainingPct: tpRemaining / reference * 100,
                slRemainingPct: slRemaining / reference * 100,
                tpEntryPct: (plan.direction === 'short' ? plan.entry - plan.takeProfit : plan.takeProfit - plan.entry) / plan.entry * 100,
                slEntryPct: (plan.direction === 'short' ? plan.stopLoss - plan.entry : plan.entry - plan.stopLoss) / plan.entry * 100,
                tpCrossed: tpRemaining < 0,
                slCrossed: slRemaining < 0,
            };
        }

        function formatRiskDistance(valuePct, crossed = false) {
            const num = Number(valuePct);
            if (!Number.isFinite(num)) return '--';
            const absText = `${Math.abs(num).toFixed(2)}%`;
            return crossed || num < 0 ? `已越过 ${absText}` : `还差 ${absText}`;
        }

        function formatRiskLineDistance(valuePct) {
            const num = Number(valuePct);
            if (!Number.isFinite(num)) return '';
            return `${num >= 0 ? '+' : '-'}${Math.abs(num).toFixed(1)}%`;
        }

        function buildRiskSummary(signal, payload = getChartDisplayPayload(), context = null, { compact = false } = {}) {
            const risk = getRiskDistanceState(signal, getRiskReferencePrice(payload, context), context?.bar || null);
            const stats = getSignalRiskStats(payload, signal);
            if (!risk.valid) {
                return compact ? 'TP/SL --' : 'TP/SL 缺失或价格顺序异常';
            }
            const rrText = risk.rr !== null ? `RR ${Number(risk.rr).toFixed(2).replace(/\.00$/, '')}` : 'RR --';
            const priceLine = `${risk.targetLabel} ${formatPrice(risk.takeProfit)} / SL ${formatPrice(risk.stopLoss)}`;
            if (compact) {
                return `${priceLine} · ${formatRiskLineDistance(risk.tpRemainingPct)} / ${formatRiskLineDistance(risk.slRemainingPct)} · ${formatRiskWinRate(stats, { compact: true })}`;
            }
            return `${priceLine}<br>${rrText} · ${risk.targetLabel} ${formatRiskDistance(risk.tpRemainingPct, risk.tpCrossed)} · SL ${formatRiskDistance(risk.slRemainingPct, risk.slCrossed)}<br>${formatRiskWinRate(stats)} · 历史胜率不代表未来`;
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

        function humanizeToken(value) {
            return String(value ?? '')
                .trim()
                .replace(/_/g, ' ')
                .replace(/\s+/g, ' ');
        }

        function formatOptionalNumber(value, digits = 2, prefix = '') {
            const num = Number(value);
            return Number.isFinite(num) ? `${prefix}${num.toFixed(digits)}` : '--';
        }

        function getSdRegimeText(value) {
            const key = String(value ?? '').trim().toLowerCase();
            const map = {
                squeeze: '压缩',
                compressed: '压缩',
                compression: '压缩',
                expansion: '扩张',
                expanded: '扩张',
                trend: '趋势扩张',
                normal: '常态',
                neutral: '常态',
                wide: '宽轨',
                narrow: '窄轨',
            };
            return map[key] || (key ? humanizeToken(value) : '--');
        }

        function getOrderTypeText(value) {
            const key = String(value ?? '').trim().toLowerCase();
            const map = {
                market: '市价',
                limit: '限价',
                stop: '停止单',
                stop_limit: '停止限价',
                bracket: 'Bracket',
            };
            return map[key] || (key ? humanizeToken(value) : '--');
        }

        function getSignalField(signal, key, fallback = '') {
            if (!signal || typeof signal !== 'object') return fallback;
            const extra = getSignalExtra(signal);
            const value = signal[key] ?? extra[key];
            return value === undefined || value === null || value === '' ? fallback : value;
        }


        function getSignalSetupMeta(signal, trace = null) {
            const signalState = getTraceSignalState(trace);
            const traceSignal = getTraceSignalPayload(trace) || {};
            const source = signal || traceSignal || {};
            const extra = getSignalExtra(source);
            const pick = (...values) => {
                for (const value of values) {
                    if (value !== undefined && value !== null && value !== '') return value;
                }
                return '';
            };
            const setup = pick(source.setup, extra.setup, signalState.setup, traceSignal.setup, trace?.setup);
            const label = pick(source.setup_label, extra.setup_label, signalState.setup_label, traceSignal.setup_label, trace?.setup_label, setup ? humanizeToken(setup) : '');
            return {
                setup: String(setup || '').trim(),
                label: String(label || '').trim(),
                family: String(pick(source.setup_family, extra.setup_family, signalState.setup_family, traceSignal.setup_family, trace?.setup_family) || '').trim(),
                mode: String(pick(source.signal_mode, extra.signal_mode, signalState.signal_mode, traceSignal.signal_mode, trace?.signal_mode) || '').trim(),
                profile: String(pick(source.strategy_profile, source.profile, extra.strategy_profile, extra.profile, signalState.strategy_profile, traceSignal.strategy_profile) || '').trim(),
                source: String(pick(source.setup_source, source.source, extra.setup_source, extra.source, signalState.setup_source, traceSignal.setup_source, trace?.source) || '').trim(),
            };
        }

        function normalizeCheckList(value) {
            if (!value) return [];
            if (Array.isArray(value)) return value.filter((item) => item !== null && item !== undefined && String(item).trim());
            if (typeof value === 'object') {
                return Object.entries(value).map(([key, item]) => {
                    if (item && typeof item === 'object') {
                        const passed = item.passed ?? item.ok ?? item.value;
                        const text = item.label || item.name || item.reason || key;
                        return `${humanizeToken(text)} ${passed === undefined ? '' : passed ? '✓' : '×'}`.trim();
                    }
                    if (typeof item === 'boolean') return `${humanizeToken(key)} ${item ? '✓' : '×'}`;
                    return `${humanizeToken(key)}: ${String(item)}`;
                }).filter((text) => text.trim());
            }
            return String(value).split(/[;；\n]/).map((item) => item.trim()).filter(Boolean);
        }

        function buildCheckTokenHtml(value, emptyText = '未提供') {
            const checks = normalizeCheckList(value);
            return checks.length ? checks.map((text) => buildTraceToken(text, /×|fail|block|reject|过滤/i.test(text) ? 'negative' : 'positive')).join('') : buildTraceToken(emptyText);
        }

        function getOrbBreakoutText(indicator) {
            if (!indicator) return '--';
            const up = Boolean(indicator.orb_breakout_up);
            const down = Boolean(indicator.orb_breakout_down);
            if (up && down) return '上下均触发';
            if (up) return '向上突破';
            if (down) return '向下突破';
            return '未突破';
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
            const rawSignal = String(signal.signal || extra.signal || extra.setup || '').trim().toLowerCase();
            const labelMap = {
                sd_squeeze_breakout_long: 'SD压缩突破多',
                sd_squeeze_breakout_short: 'SD压缩突破空',
                vwap_trend_pullback_long: 'VWAP顺势回踩多',
                vwap_trend_pullback_short: 'VWAP顺势回踩空',
            };
            if (labelMap[rawSignal]) return labelMap[rawSignal];
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

        function getTraceSignalKey(trace) {
            const signal = getTraceSignalPayload(trace);
            const signalState = getTraceSignalState(trace);
            const barMs = Number(trace?.bar_time_ms || 0) || 0;
            return String(
                signal?.signal_id
                || signal?.id
                || signalState?.signal_id
                || (barMs ? `trace:${barMs}` : '')
            );
        }

        function getTraceStage(trace) {
            return String(getTraceSignalState(trace)?.stage || '').trim().toLowerCase();
        }

        function getTraceDirection(trace) {
            const signalState = getTraceSignalState(trace);
            return String(signalState.direction || signalState.signal_payload?.direction || '').trim().toLowerCase();
        }

        function cleanTraceReasonText(text) {
            return String(text || '')
                .replace(/^(多头|空头)?候选被过滤[:：]\s*/, '')
                .replace(/^(多头|空头)?过滤[:：]\s*/, '')
                .trim();
        }

        function getTraceFilterReason(trace) {
            const signalState = getTraceSignalState(trace);
            const direct = cleanTraceReasonText(signalState.filter_reason || '');
            if (direct) return direct;
            const filters = Array.isArray(trace?.filters) ? trace.filters : [];
            const filterMatch = filters.map(cleanTraceReasonText).find((text) => text && !/^未触发/.test(text));
            if (filterMatch) return filterMatch;
            const events = Array.isArray(trace?.event_chain) ? trace.event_chain : [];
            const eventMatch = events
                .map((text) => {
                    const match = String(text || '').match(/候选被过滤[:：]\s*(.+)$/);
                    return cleanTraceReasonText(match?.[1] || '');
                })
                .find(Boolean);
            return eventMatch || '';
        }

        function formatTraceDecisionLabel(trace, fallbackSignal = null) {
            const stage = getTraceStage(trace);
            const reason = getTraceFilterReason(trace);
            if (stage === 'blocked') return `blocked · ${reason || buildTraceDecisionLabel(trace, fallbackSignal)}`;
            return buildTraceDecisionLabel(trace, fallbackSignal);
        }

        function getDecisionSignalContext(context) {
            if (!context) return { signal: null, traceSignal: null, signalState: {} };
            const signalState = getTraceSignalState(context.trace);
            const traceSignal = getTraceSignalPayload(context.trace);
            return {
                signal: context.activeSignal || context.activeTraceSignal || null,
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

        function getTraceSignalStageText(trace) {
            const stage = getTraceStage(trace);
            if (stage && stage !== 'none') return stage;
            return 'none';
        }

        function getTraceStageBadgeClass(stage) {
            const key = String(stage || '').trim().toLowerCase();
            if (key === 'confirmed') return 'positive';
            if (key === 'blocked') return 'negative';
            if (key === 'candidate') return 'warning';
            return '';
        }

        function getTraceStageLabel(stage) {
            const key = String(stage || '').trim().toLowerCase();
            if (key === 'confirmed') return 'Confirmed';
            if (key === 'blocked') return 'Blocked';
            if (key === 'candidate') return 'Candidate';
            return 'No event';
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

        function getMarkerPointBarIndex(point) {
            const direct = Number(point?.bar_index);
            if (Number.isInteger(direct)) return direct;
            const value = Array.isArray(point?.value) ? Number(point.value[0]) : NaN;
            return Number.isInteger(value) ? value : -1;
        }

        function normalizeMarkerLabelSide(position) {
            const text = String(position || 'top').toLowerCase();
            if (text.includes('bottom')) return 'bottom';
            if (text.includes('left')) return 'left';
            if (text.includes('right')) return 'right';
            return 'top';
        }

        function estimateChartLabelWidth(text, fontSize, padding) {
            const safeText = String(text || '');
            let units = 0;
            for (const char of safeText) {
                units += /[\u2e80-\u9fff\uff00-\uffef]/.test(char) ? 1.02 : 0.62;
            }
            const pad = Array.isArray(padding)
                ? Number(padding[1] ?? padding[0] ?? 0) * 2
                : Number(padding || 0) * 2;
            return Math.max(14, units * Number(fontSize || 10) + pad + 8);
        }

        function createChartLabelLaneAllocator(options = {}) {
            const barsCount = Math.max(1, Number(options.barsCount || 0));
            const viewport = chartViewportState.totalBars === barsCount
                ? chartViewportState
                : getCurrentZoomWindow(barsCount);
            const visibleBars = Math.max(1, Number(viewport.visibleBars || barsCount || 1));
            const canvas = document.getElementById('chartCanvas');
            const chartWidth = Math.max(320, Number(canvas?.clientWidth || window.innerWidth || 1200));
            const plotWidth = Math.max(
                220,
                chartWidth - Number(options.gridLeft || 0) - Number(options.gridRight || 0)
            );
            const pxPerBar = Math.max(4, plotWidth / visibleBars);
            const densityTier = String(options.densityTier || chartMarkerDensityTier || getChartMarkerDensityTier(barsCount));
            const maxLanes = Number(options.maxLanes || (densityTier === 'mid' ? 5 : 7));
            const laneStep = Number(options.laneStep || (densityTier === 'mid' ? 15 : 20));
            const minGapPx = Number(options.minGapPx || (densityTier === 'mid' ? 10 : 14));
            const xStep = Number(options.xStep || (densityTier === 'mid' ? 8 : 12));
            const lanesBySide = {
                top: [],
                bottom: [],
                left: [],
                right: [],
            };

            function apply(data, config = {}) {
                if (!Array.isArray(data) || !data.length || !config.showLabel) return data;
                const side = normalizeMarkerLabelSide(config.position);
                const lanes = lanesBySide[side] || (lanesBySide[side] = []);
                const baseDistance = Number(config.baseDistance ?? config.labelDistance ?? 4);
                const fontSize = Number(config.fontSize || 10);
                const padding = config.padding || [2, 4];
                const laneEntries = data
                    .map((point, dataIndex) => ({ point, dataIndex, x: getMarkerPointBarIndex(point) }))
                    .filter((entry) => Number.isInteger(entry.x) && entry.x >= 0)
                    .sort((a, b) => a.x - b.x || a.dataIndex - b.dataIndex);

                laneEntries.forEach(({ point, x }) => {
                    const text = String(point?.labelText || point?.name || '');
                    if (!text) return;
                    const halfBars = Math.max(
                        0.35,
                        (estimateChartLabelWidth(text, fontSize, padding) + minGapPx) / Math.max(pxPerBar, 1) / 2
                    );
                    const interval = { left: x - halfBars, right: x + halfBars };
                    let lane = -1;
                    for (let index = 0; index < lanes.length; index += 1) {
                        const intervals = Array.isArray(lanes[index]) ? lanes[index] : [];
                        const overlaps = intervals.some((item) => interval.left < item.right && interval.right > item.left);
                        if (!overlaps) {
                            lane = index;
                            break;
                        }
                    }
                    if (lane < 0 && lanes.length < maxLanes) {
                        lane = lanes.length;
                        lanes.push([]);
                    }
                    if (lane < 0) {
                        lane = lanes
                            .map((items, index) => ({ count: Array.isArray(items) ? items.length : 0, index }))
                            .sort((a, b) => a.count - b.count)[0]?.index ?? 0;
                    }
                    if (!Array.isArray(lanes[lane])) lanes[lane] = [];
                    lanes[lane].push(interval);
                    const sideSign = side === 'bottom' ? 1 : -1;
                    const horizontalOffset = (lane % 2 === 0 ? 1 : -1) * Math.ceil(lane / 2) * xStep;
                    const verticalOffset = side === 'top' || side === 'bottom'
                        ? [horizontalOffset, Math.max(0, lane - 1) * Math.round(laneStep * 0.35) * sideSign]
                        : [0, (lane % 2 === 0 ? -1 : 1) * Math.ceil(lane / 2) * laneStep];
                    point._labelLane = lane;
                    point.label = {
                        ...(point.label || {}),
                        show: true,
                        distance: Math.max(0, baseDistance + lane * laneStep),
                        offset: verticalOffset,
                    };
                });
                return data;
            }

            return { apply };
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
                backtestRunId: String(params.get('backtest_run_id') || '').trim(),
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
            if (currentBacktestRunId) params.backtest_run_id = currentBacktestRunId;
            const url = buildPageUrl('/ibkr_chart.html', params, { environment: currentEnvironment });
            window.history.replaceState({}, '', url);
        }
