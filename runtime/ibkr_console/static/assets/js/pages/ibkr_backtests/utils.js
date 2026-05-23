        function toIsoDate(date) {
            return getCurrentEtDateString(date);
        }

        function getLatestCompleteBacktestDate() {
            return shiftDateString(getCurrentEtDateString(), -1);
        }

        function syncBacktestDateLimits() {
            const latestCompleteDate = getLatestCompleteBacktestDate();
            const dateFrom = document.getElementById('dateFrom');
            const dateTo = document.getElementById('dateTo');
            if (dateFrom) dateFrom.max = latestCompleteDate;
            if (dateTo) dateTo.max = latestCompleteDate;
            if (dateFrom?.value && dateFrom.value > latestCompleteDate) dateFrom.value = latestCompleteDate;
            if (dateTo?.value && dateTo.value > latestCompleteDate) dateTo.value = latestCompleteDate;
            if (dateFrom?.value && dateTo?.value && dateFrom.value > dateTo.value) dateFrom.value = dateTo.value;
            return latestCompleteDate;
        }

        function initAuth() {
            try {
                requireAuth(`${location.pathname}${location.search}`);
                return true;
            } catch (_) {
                return false;
            }
        }

        function parseMaybeJson(value, fallback = {}) {
            if (value == null || value === '') return fallback;
            if (typeof value === 'object') return value;
            try {
                return JSON.parse(value);
            } catch (_) {
                return fallback;
            }
        }

        function escapeHtml(value) {
            return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function escapeFilterValue(value) {
            return String(value ?? '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
        }

        function toItems(payload) {
            return Array.isArray(payload?.items) ? payload.items : [];
        }

        function resetBacktestRowPagination(runId = selectedRunId) {
            backtestRowPageState = {};
            Object.entries(BACKTEST_ROW_PAGE_CONFIG).forEach(([key, config]) => {
                backtestRowPageState[key] = {
                    runId: runId || '',
                    page: 0,
                    perPage: Number(config.perPage || 200),
                    loaded: 0,
                    total: null,
                    hasMore: Boolean(runId),
                    loading: false,
                    error: '',
                };
            });
        }

        function getBacktestRowPagination(key) {
            if (!backtestRowPageState[key] || backtestRowPageState[key].runId !== selectedRunId) {
                resetBacktestRowPagination(selectedRunId);
            }
            return backtestRowPageState[key] || {
                runId: selectedRunId || '',
                page: 0,
                perPage: Number(BACKTEST_ROW_PAGE_CONFIG[key]?.perPage || 200),
                loaded: 0,
                total: null,
                hasMore: Boolean(selectedRunId),
                loading: false,
                error: '',
            };
        }

        function updateBacktestRowPagination(key, patch = {}) {
            const current = getBacktestRowPagination(key);
            backtestRowPageState[key] = {
                ...current,
                ...patch,
                runId: selectedRunId || current.runId || '',
            };
            return backtestRowPageState[key];
        }

        function getBacktestRowList(key) {
            if (key === 'trades') return selectedTrades;
            if (key === 'targets') return selectedTargets;
            if (key === 'signals') return selectedSignals;
            if (key === 'reverseSignals') return selectedReverseSignals;
            return [];
        }

        function renderBacktestRowPager(key, options = {}) {
            const state = getBacktestRowPagination(key);
            const config = BACKTEST_ROW_PAGE_CONFIG[key] || {};
            const loaded = Number(state.loaded || getBacktestRowList(key).length || 0);
            const total = state.total != null ? Number(state.total || 0) : null;
            const noun = options.noun || config.noun || '条记录';
            const label = options.label || config.label || key;
            const moreLabel = state.loading ? '加载中...' : `加载更多 ${label}`;
            const loadAllLabel = state.loading ? '加载中...' : '补齐全部';
            const totalCopy = total != null
                ? `已加载 ${formatNumber(loaded, 0)} / ${formatNumber(total, 0)} ${noun}`
                : `已加载 ${formatNumber(loaded, 0)} ${noun}`;
            const stateCopy = state.error
                ? `读取失败：${state.error}`
                : (state.hasMore ? `${totalCopy}，还有更多数据。` : `${totalCopy}。`);
            const errorClass = state.error ? ' has-error' : '';
            if (!selectedRunId && !loaded) return '';
            return `
                <div class="row-pager${errorClass}">
                    <div class="row-pager-copy">${escapeHtml(stateCopy)}</div>
                    <div class="row-pager-actions">
                        ${state.hasMore ? `<button class="btn ghost" type="button" ${state.loading ? 'disabled' : ''} onclick="loadMoreBacktestRows('${escapeHtml(key)}')">${escapeHtml(moreLabel)}</button>` : ''}
                        ${state.hasMore ? `<button class="btn ghost" type="button" ${state.loading ? 'disabled' : ''} onclick="loadAllBacktestRows('${escapeHtml(key)}')">${escapeHtml(loadAllLabel)}</button>` : ''}
                    </div>
                </div>
            `;
        }

        async function apiFetchAll(collection, params = {}) {
            const allItems = [];
            const perPage = Number(params.perPage || 200);
            const maxPages = Number(params.maxPages || 8);
            if (typeof fetchCollectionFullListCached === 'function') {
                return fetchCollectionFullListCached(collection, { ...params, perPage, maxPages }, { ttlMs: 120000, ttl: 120000 });
            }
            for (let page = 1; page <= maxPages; page += 1) {
                const fetchParams = { ...params, page, perPage };
                const payload = typeof cachedApiFetch === 'function'
                    ? await cachedApiFetch(collection, fetchParams, { ttlMs: 120000, ttl: 120000 })
                    : await apiFetch(collection, fetchParams);
                const items = toItems(payload);
                allItems.push(...items);
                if (items.length < perPage) break;
            }
            return allItems;
        }

        function buildAuthHeaders() {
            const headers = { 'Content-Type': 'application/json' };
            const token = getToken();
            if (token) headers.Authorization = `Bearer ${token}`;
            return headers;
        }

        async function requestBacktestJson(path, { method = 'GET', body = null } = {}) {
            const res = await fetch(`${BASE_URL}${path}`, {
                method,
                headers: buildAuthHeaders(),
                body: body ? JSON.stringify(body) : null,
            });
            if (res.status === 401 || res.status === 403) {
                handleAuthError();
                throw new Error('Authentication failed');
            }
            let payload = {};
            try {
                payload = await res.json();
            } catch (_) {
                payload = {};
            }
            if (!res.ok || payload.ok === false) {
                throw new Error(payload.error || payload.message || `Request failed: ${res.status}`);
            }
            return payload;
        }

        function normalizeRunRecord(record) {
            const metrics = parseMaybeJson(record.metrics, {});
            const extra = parseMaybeJson(record.extra, {});
            const params = parseMaybeJson(record.params, {});
            return {
                ...record,
                metrics,
                extra,
                params,
                status: String(record.status || '').trim().toLowerCase() || 'queued',
                source_environment: String(record.source_environment || '').trim().toLowerCase() || 'live',
                trade_count: Number(record.trade_count || metrics.trade_count || 0),
                net_pnl: Number(record.net_pnl || metrics.net_pnl || 0),
                total_return_pct: Number(record.total_return_pct || metrics.total_return_pct || 0),
                sharpe: Number(record.sharpe || metrics.sharpe || 0),
                max_drawdown_pct: Number(record.max_drawdown_pct || metrics.max_drawdown_pct || 0),
                win_rate: Number(record.win_rate || metrics.win_rate || 0),
                duration_s: Number(record.duration_s || metrics.duration_s || extra.duration_s || 0),
                progress: Number(record.progress || 0),
            };
        }

        function normalizeBatchRecord(record) {
            const params = parseMaybeJson(record.params, {});
            const extra = parseMaybeJson(record.extra, {});
            const leaderboard = parseMaybeJson(record.leaderboard, []);
            return {
                ...record,
                params,
                extra,
                leaderboard: Array.isArray(leaderboard) ? leaderboard : [],
                status: String(record.status || '').trim().toLowerCase() || 'queued',
                source_environment: String(record.source_environment || '').trim().toLowerCase() || 'live',
                variant_count: Number(record.variant_count || 0),
                completed_count: Number(record.completed_count || 0),
                best_total_return_pct: Number(record.best_total_return_pct || 0),
                best_sharpe: Number(record.best_sharpe || 0),
            };
        }

        function normalizeTradeRecord(record) {
            const extra = parseMaybeJson(record.extra, {});
            return {
                ...record,
                extra,
                pnl: Number(record.pnl || 0),
                pnl_pct: Number(record.pnl_pct || 0),
                entry_price: Number(record.entry_price || 0),
                exit_price: Number(record.exit_price || 0),
                shares: Number(record.shares || 0),
                bars_held: Number(record.bars_held || 0),
                entry_bar_ms: Number(record.entry_bar_ms || 0),
                exit_bar_ms: Number(record.exit_bar_ms || 0),
                trade_index: Number(record.trade_index || 0),
                setup: String(record.setup || extra.setup || '').trim(),
                setup_label: String(record.setup_label || extra.setup_label || '').trim(),
                setup_family: String(record.setup_family || extra.setup_family || '').trim(),
                signal_mode: String(record.signal_mode || extra.signal_mode || '').trim(),
            };
        }

        function normalizeBacktestTargetRecord(record) {
            const extra = parseMaybeJson(record.extra, {});
            const rank = Number(record.rank || extra.selection_rank || 0);
            const score = Number(record.score || 0);
            return {
                ...record,
                extra,
                symbol: String(record.symbol || '').trim().toUpperCase(),
                exchange: String(record.exchange || '').trim().toUpperCase(),
                date: String(record.date || '').trim().slice(0, 10),
                direction_bias: String(record.direction_bias || 'neutral').trim().toLowerCase() || 'neutral',
                scan_reason: String(record.scan_reason || '').trim(),
                status: String(record.status || '').trim().toLowerCase() || 'active',
                rank: Number.isFinite(rank) ? rank : 0,
                score: Number.isFinite(score) ? score : 0,
                bar_time_ms: Number(record.bar_time_ms || 0),
                us_time: String(record.us_time || '').trim(),
                cn_time: String(record.cn_time || '').trim(),
            };
        }

        function normalizeBacktestSignalRecord(record) {
            const extra = parseMaybeJson(record.extra, {});
            const status = String(record.status || extra.status || '').trim().toLowerCase() || 'generated';
            return {
                ...record,
                extra,
                symbol: String(record.symbol || extra.symbol || '').trim().toUpperCase(),
                exchange: String(record.exchange || extra.exchange || '').trim().toUpperCase(),
                direction: String(record.direction || extra.direction || '').trim().toLowerCase(),
                signal: String(record.signal || extra.signal || '').trim(),
                signal_id: String(record.signal_id || extra.signal_id || '').trim(),
                date: String(record.date || record.us_time || extra.date || '').trim().slice(0, 10),
                status,
                reason: String(record.reason || extra.signal_status_reason || extra.reason || '').trim(),
                entry: Number(record.entry || extra.entry || 0),
                stop_loss: Number(record.stop_loss || extra.stop_loss || 0),
                take_profit: Number(record.take_profit || extra.take_profit || 0),
                shares: Number(record.shares || extra.shares || 0),
                bar_time_ms: Number(record.bar_time_ms || extra.bar_time_ms || 0),
                bar_index: Number(record.bar_index || extra.bar_index || 0),
                us_time: String(record.us_time || extra.us_time || '').trim(),
                cn_time: String(record.cn_time || extra.cn_time || '').trim(),
                setup: String(record.setup || extra.setup || '').trim(),
                setup_label: String(record.setup_label || extra.setup_label || '').trim(),
                setup_family: String(record.setup_family || extra.setup_family || '').trim(),
                signal_mode: String(record.signal_mode || extra.signal_mode || '').trim(),
            };
        }

        function normalizeBacktestReverseRecord(record) {
            const extra = parseMaybeJson(record.extra, {});
            const triggeredSignals = parseMaybeJson(record.triggered_signals, record.triggered_signals || []);
            return {
                ...record,
                extra,
                triggered_signals: Array.isArray(triggeredSignals) ? triggeredSignals : [],
                symbol: String(record.symbol || extra.symbol || '').trim().toUpperCase(),
                direction: String(record.direction || extra.direction || '').trim().toLowerCase(),
                reverse_kind: String(record.reverse_kind || extra.reverse_kind || '').trim(),
                source: String(record.source || extra.source || '').trim(),
                target_state: String(record.target_state || extra.target_state || '').trim(),
                action_type: String(record.action_type || extra.action_type || '').trim(),
                status: String(record.status || extra.status || '').trim().toLowerCase() || 'generated',
                reason: String(record.reason || extra.reason || '').trim(),
                signal_id: String(record.signal_id || extra.signal_id || '').trim(),
                origin_signal_id: String(record.origin_signal_id || extra.origin_signal_id || '').trim(),
                trade_group_id: String(record.trade_group_id || extra.trade_group_id || '').trim(),
                date: String(record.date || record.us_time || extra.date || '').trim().slice(0, 10),
                bar_time_ms: Number(record.bar_time_ms || extra.bar_time_ms || 0),
                us_time: String(record.us_time || extra.us_time || '').trim(),
                cn_time: String(record.cn_time || extra.cn_time || '').trim(),
                setup: String(record.setup || extra.setup || '').trim(),
                setup_label: String(record.setup_label || extra.setup_label || '').trim(),
                setup_family: String(record.setup_family || extra.setup_family || '').trim(),
                signal_mode: String(record.signal_mode || extra.signal_mode || '').trim(),
                score: Number(record.score || extra.score || 0),
                priority: Number(record.priority || extra.priority || 0),
            };
        }

        function normalizeTrackingDate(value) {
            const text = String(value || '').trim();
            return text.length >= 10 ? text.slice(0, 10) : '';
        }

        function coerceTrackingNumber(value, digits = 4) {
            const num = Number(value || 0);
            if (!Number.isFinite(num)) return 0;
            return Number(num.toFixed(digits));
        }

        function getTrackingDateFromMs(barTimeMs) {
            const num = Number(barTimeMs || 0);
            if (!num) return '';
            const text = typeof formatBarTimeMsToET === 'function' ? formatBarTimeMsToET(num) : '';
            return normalizeTrackingDate(text);
        }

        function getTrackingEventDate(event) {
            return normalizeTrackingDate(event?.date)
                || normalizeTrackingDate(event?.us_time)
                || getTrackingDateFromMs(event?.bar_time_ms);
        }

        function cleanTrackingDetails(details) {
            const compact = {};
            Object.entries(details || {}).forEach(([key, value]) => {
                if (value == null || value === '' || (Array.isArray(value) && !value.length)) return;
                if (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length) return;
                compact[key] = typeof value === 'number' ? coerceTrackingNumber(value) : value;
            });
            return compact;
        }

        function getBacktestSetupField(source, key, fallback = '') {
            if (!source || typeof source !== 'object') return fallback;
            const details = source.details && typeof source.details === 'object' ? source.details : {};
            const extra = source.extra && typeof source.extra === 'object' ? source.extra : {};
            const value = source[key] ?? details[key] ?? extra[key];
            return value === undefined || value === null || value === '' ? fallback : value;
        }

        function getBacktestSetupLabel(source) {
            const label = getBacktestSetupField(source, 'setup_label', '');
            if (label) return String(label).trim();
            const setup = getBacktestSetupField(source, 'setup', '');
            if (setup) return humanizeToken(String(setup));
            const signal = getBacktestSetupField(source, 'signal', '');
            return signal ? humanizeToken(String(signal)) : '';
        }

        function getBacktestSetupFilterValue(source) {
            return String(getBacktestSetupLabel(source) || getBacktestSetupField(source, 'setup', '') || '').trim();
        }

        function normalizeTrackingAuditEvent(event) {
            const source = event || {};
            const barTimeMs = Number(source.bar_time_ms || source.entry_bar_ms || source.exit_bar_ms || 0);
            const usTime = String(source.us_time || source.entry_us_time || source.exit_us_time || (barTimeMs ? formatBarTimeMsToET(barTimeMs) : '') || '').trim();
            const normalized = {
                ...source,
                event_type: String(source.event_type || 'event').trim(),
                stage: String(source.stage || '').trim().toLowerCase(),
                status: String(source.status || '').trim().toLowerCase(),
                symbol: String(source.symbol || '').trim().toUpperCase(),
                direction: String(source.direction || '').trim().toLowerCase(),
                signal: String(source.signal || '').trim(),
                signal_id: String(source.signal_id || source.signal || '').trim(),
                reason: String(source.reason || '').trim(),
                date: normalizeTrackingDate(source.date) || normalizeTrackingDate(usTime) || getTrackingDateFromMs(barTimeMs),
                bar_time_ms: barTimeMs,
                us_time: usTime,
                cn_time: String(source.cn_time || '').trim(),
                trade_index: Number(source.trade_index || 0),
                entry_price: coerceTrackingNumber(source.entry_price || source.entry || 0),
                exit_price: coerceTrackingNumber(source.exit_price || 0),
                stop_loss: coerceTrackingNumber(source.stop_loss || 0),
                take_profit: coerceTrackingNumber(source.take_profit || 0),
                old_sl: coerceTrackingNumber(source.old_sl || 0),
                new_sl: coerceTrackingNumber(source.new_sl || 0),
                old_tp: coerceTrackingNumber(source.old_tp || 0),
                new_tp: coerceTrackingNumber(source.new_tp || 0),
                pnl: coerceTrackingNumber(source.pnl || 0),
                pnl_pct: coerceTrackingNumber(source.pnl_pct || 0),
                shares: Number(source.shares || 0),
                setup: String(getBacktestSetupField(source, 'setup', '')).trim(),
                setup_label: String(getBacktestSetupField(source, 'setup_label', '')).trim(),
                setup_family: String(getBacktestSetupField(source, 'setup_family', '')).trim(),
                signal_mode: String(getBacktestSetupField(source, 'signal_mode', '')).trim(),
                details: cleanTrackingDetails(source.details || {}),
            };
            return normalized;
        }

        function getTrackingEventSortKey(event) {
            const priority = {
                target: 10,
                signal: 20,
                execution: 30,
                risk: 40,
                special: 50,
                exit: 60,
            };
            return [
                Number(event?.bar_time_ms || 0) || 9999999999999,
                String(event?.symbol || ''),
                priority[String(event?.stage || '')] || 99,
                String(event?.event_type || ''),
            ];
        }

        function sortTrackingEvents(events) {
            return (events || []).slice().sort((a, b) => {
                const left = getTrackingEventSortKey(a);
                const right = getTrackingEventSortKey(b);
                for (let index = 0; index < left.length; index += 1) {
                    if (left[index] < right[index]) return -1;
                    if (left[index] > right[index]) return 1;
                }
                return 0;
            });
        }

        function dedupeTrackingEvents(events) {
            const seen = new Set();
            const result = [];
            (events || []).forEach((event, index) => {
                const normalized = normalizeTrackingAuditEvent(event);
                const key = [
                    normalized.event_type,
                    normalized.stage,
                    normalized.symbol,
                    normalized.signal_id,
                    normalized.trade_index,
                    normalized.bar_time_ms,
                    normalized.status,
                    index > 100000 ? index : '',
                ].join('|');
                if (seen.has(key)) return;
                seen.add(key);
                result.push(normalized);
            });
            return sortTrackingEvents(result);
        }

        function countTrackingValues(events, fieldName) {
            const counts = {};
            (events || []).forEach((event) => {
                const key = String(event?.[fieldName] || '').trim();
                if (!key) return;
                counts[key] = Number(counts[key] || 0) + 1;
            });
            return counts;
        }

        function buildTrackingSummaries(events, run, audit = {}) {
            const normalizedEvents = dedupeTrackingEvents(events);
            const daily = new Map();
            const flows = new Map();
            normalizedEvents.forEach((event) => {
                const date = getTrackingEventDate(event);
                const symbol = String(event.symbol || '').trim().toUpperCase();
                if (!date) return;
                if (!daily.has(date)) {
                    daily.set(date, {
                        date,
                        target_symbols: new Set(),
                        signal_symbols: new Set(),
                        executed_symbols: new Set(),
                        trade_symbols: new Set(),
                        target_count: 0,
                        signal_count: 0,
                        executed_signal_count: 0,
                        trade_count: 0,
                        take_profit_count: 0,
                        stop_loss_count: 0,
                        risk_adjustment_count: 0,
                        special_event_count: 0,
                    });
                }
                const day = daily.get(date);
                const eventType = String(event.event_type || '');
                const stage = String(event.stage || '');
                const status = String(event.status || '');
                if (eventType === 'target_selected') {
                    day.target_count += 1;
                    if (symbol) day.target_symbols.add(symbol);
                }
                if (eventType === 'signal_generated') {
                    day.signal_count += 1;
                    if (symbol) day.signal_symbols.add(symbol);
                }
                if (eventType === 'entry_filled') {
                    day.executed_signal_count += 1;
                    if (symbol) day.executed_symbols.add(symbol);
                }
                if (eventType === 'trade_closed') {
                    day.trade_count += 1;
                    if (symbol) day.trade_symbols.add(symbol);
                    if (status === 'take_profit') day.take_profit_count += 1;
                    if (status === 'stop_loss') day.stop_loss_count += 1;
                }
                if (stage === 'risk') day.risk_adjustment_count += 1;
                if (stage === 'special') day.special_event_count += 1;
                if (!symbol) return;
                const flowKey = `${date}|${symbol}`;
                if (!flows.has(flowKey)) {
                    flows.set(flowKey, {
                        date,
                        symbol,
                        event_count: 0,
                        targeted: false,
                        signal_count: 0,
                        executed_signal_count: 0,
                        trade_count: 0,
                        risk_adjustment_count: 0,
                        special_event_count: 0,
                        setup_labels: new Set(),
                        events: [],
                    });
                }
                const flow = flows.get(flowKey);
                flow.event_count += 1;
                flow.targeted = Boolean(flow.targeted || eventType === 'target_selected');
                if (eventType === 'signal_generated') flow.signal_count += 1;
                if (eventType === 'entry_filled') flow.executed_signal_count += 1;
                if (eventType === 'trade_closed') flow.trade_count += 1;
                if (stage === 'risk') flow.risk_adjustment_count += 1;
                if (stage === 'special') flow.special_event_count += 1;
                const setupLabel = getBacktestSetupLabel(event);
                if (setupLabel) flow.setup_labels.add(setupLabel);
                if (flow.events.length < 32) flow.events.push(event);
            });
            const dailySummary = Array.from(daily.values())
                .map((day) => ({
                    ...day,
                    target_symbols: Array.from(day.target_symbols).sort(),
                    signal_symbols: Array.from(day.signal_symbols).sort(),
                    executed_symbols: Array.from(day.executed_symbols).sort(),
                    trade_symbols: Array.from(day.trade_symbols).sort(),
                }))
                .sort((a, b) => String(a.date || '').localeCompare(String(b.date || '')));
            const dates = Array.from(new Set([
                ...dailySummary.map((item) => item.date).filter(Boolean),
                ...normalizedEvents.map((event) => getTrackingEventDate(event)).filter(Boolean),
            ])).sort();
            const requestedFocusDate = normalizeTrackingDate(audit.focus_date)
                || normalizeTrackingDate(run?.date_to)
                || dates[dates.length - 1]
                || '';
            const focusDate = dates.includes(requestedFocusDate) ? requestedFocusDate : (dates[dates.length - 1] || requestedFocusDate);
            const focusEvents = normalizedEvents.filter((event) => getTrackingEventDate(event) === focusDate);
            const focusDay = dailySummary.find((item) => item.date === focusDate) || { date: focusDate };
            const flowRows = Array.from(flows.values()).map((flow) => ({
                ...flow,
                setup_labels: Array.from(flow.setup_labels || []).sort(),
            })).sort((a, b) => (
                String(a.date || '').localeCompare(String(b.date || ''))
                || (Number(Boolean(b.targeted)) - Number(Boolean(a.targeted)))
                || String(a.symbol || '').localeCompare(String(b.symbol || ''))
            ));
            return {
                timeline: normalizedEvents,
                daily_summary: dailySummary,
                dates,
                symbols: Array.from(new Set(normalizedEvents.map((event) => event.symbol).filter(Boolean))).sort(),
                eventTypes: Array.from(new Set(normalizedEvents.map((event) => event.event_type).filter(Boolean))).sort(),
                setupLabels: Array.from(new Set(normalizedEvents.map((event) => getBacktestSetupFilterValue(event)).filter(Boolean))).sort(),
                focus_date: focusDate,
                focus_day: {
                    ...focusDay,
                    timeline: focusEvents,
                    timeline_truncated: false,
                },
                focus_symbols: Array.from(new Set([
                    ...(focusDay.target_symbols || []),
                    ...focusEvents.map((event) => event.symbol).filter(Boolean),
                ])).sort(),
                symbol_day_flows: flowRows,
                event_type_counts: countTrackingValues(normalizedEvents, 'event_type'),
                stage_counts: countTrackingValues(normalizedEvents, 'stage'),
            };
        }

        function getRunAuditPayload(run) {
            const metricsAudit = run?.metrics?.backtest_audit;
            if (metricsAudit && typeof metricsAudit === 'object') return metricsAudit;
            const extraAudit = run?.extra?.backtest_audit_summary;
            if (extraAudit && typeof extraAudit === 'object') return extraAudit;
            return {};
        }

        function normalizeNativeBacktestTrackingModel(run, audit) {
            const auditPayload = audit || {};
            const timeline = dedupeTrackingEvents([
                ...(Array.isArray(auditPayload.timeline) ? auditPayload.timeline : []),
                ...(Array.isArray(auditPayload.focus_day?.timeline) ? auditPayload.focus_day.timeline : []),
            ]);
            const summaries = buildTrackingSummaries(timeline, run, auditPayload);
            const dailySummary = Array.isArray(auditPayload.daily_summary) && auditPayload.daily_summary.length
                ? auditPayload.daily_summary.map((item) => ({
                    ...item,
                    date: normalizeTrackingDate(item.date),
                    target_symbols: Array.isArray(item.target_symbols) ? item.target_symbols : [],
                    signal_symbols: Array.isArray(item.signal_symbols) ? item.signal_symbols : [],
                    executed_symbols: Array.isArray(item.executed_symbols) ? item.executed_symbols : [],
                    trade_symbols: Array.isArray(item.trade_symbols) ? item.trade_symbols : [],
                    target_count: Number(item.target_count || 0),
                    signal_count: Number(item.signal_count || 0),
                    executed_signal_count: Number(item.executed_signal_count || 0),
                    trade_count: Number(item.trade_count || 0),
                    take_profit_count: Number(item.take_profit_count || 0),
                    stop_loss_count: Number(item.stop_loss_count || 0),
                    risk_adjustment_count: Number(item.risk_adjustment_count || 0),
                    special_event_count: Number(item.special_event_count || 0),
                })).filter((item) => item.date)
                : summaries.daily_summary;
            const dates = Array.from(new Set([
                ...dailySummary.map((item) => item.date).filter(Boolean),
                ...summaries.dates,
            ])).sort();
            const focusDate = normalizeTrackingDate(auditPayload.focus_date) || summaries.focus_date || dates[dates.length - 1] || '';
            const symbolDayFlows = Array.isArray(auditPayload.symbol_day_flows) && auditPayload.symbol_day_flows.length
                ? auditPayload.symbol_day_flows.map((flow) => ({
                    ...flow,
                    date: normalizeTrackingDate(flow.date),
                    symbol: String(flow.symbol || '').trim().toUpperCase(),
                    events: dedupeTrackingEvents(flow.events || []),
                    event_count: Number(flow.event_count || (flow.events || []).length || 0),
                    signal_count: Number(flow.signal_count || 0),
                    executed_signal_count: Number(flow.executed_signal_count || 0),
                    trade_count: Number(flow.trade_count || 0),
                    risk_adjustment_count: Number(flow.risk_adjustment_count || 0),
                    special_event_count: Number(flow.special_event_count || 0),
                    targeted: Boolean(flow.targeted),
                    setup_labels: Array.isArray(flow.setup_labels) && flow.setup_labels.length
                        ? flow.setup_labels.map((item) => String(item || '').trim()).filter(Boolean)
                        : Array.from(new Set(dedupeTrackingEvents(flow.events || []).map((event) => getBacktestSetupFilterValue(event)).filter(Boolean))).sort(),
                }))
                : summaries.symbol_day_flows;
            return {
                enabled: Boolean(auditPayload.enabled || timeline.length || dailySummary.length),
                audit_mode: 'native',
                date_from: String(auditPayload.date_from || run?.date_from || ''),
                date_to: String(auditPayload.date_to || run?.date_to || ''),
                focus_date: focusDate,
                focus_symbols: Array.isArray(auditPayload.focus_symbols) ? auditPayload.focus_symbols : summaries.focus_symbols,
                event_count: Number(auditPayload.event_count || timeline.length || 0),
                event_type_counts: Object.keys(auditPayload.event_type_counts || {}).length ? auditPayload.event_type_counts : summaries.event_type_counts,
                stage_counts: Object.keys(auditPayload.stage_counts || {}).length ? auditPayload.stage_counts : summaries.stage_counts,
                timeline_limit: Number(auditPayload.timeline_limit || 0),
                timeline_truncated: Boolean(auditPayload.timeline_truncated),
                timeline,
                focus_day: auditPayload.focus_day ? {
                    ...auditPayload.focus_day,
                    date: normalizeTrackingDate(auditPayload.focus_day.date) || focusDate,
                    timeline: dedupeTrackingEvents(auditPayload.focus_day.timeline || []),
                    timeline_truncated: Boolean(auditPayload.focus_day.timeline_truncated),
                } : summaries.focus_day,
                daily_summary: dailySummary,
                symbol_day_flows: symbolDayFlows,
                symbol_day_flow_truncated: Boolean(auditPayload.symbol_day_flow_truncated),
                dates,
                symbols: Array.from(new Set([
                    ...summaries.symbols,
                    ...symbolDayFlows.map((flow) => flow.symbol).filter(Boolean),
                    ...(Array.isArray(auditPayload.focus_symbols) ? auditPayload.focus_symbols : []),
                ])).sort(),
                eventTypes: summaries.eventTypes,
                setupLabels: Array.from(new Set([
                    ...summaries.setupLabels,
                    ...symbolDayFlows.flatMap((flow) => flow.setup_labels || []),
                ])).sort(),
            };
        }

        function addDerivedTrackingEvent(events, event) {
            events.push(normalizeTrackingAuditEvent(event));
        }

        function statusToTrackingEventType(status) {
            const value = String(status || '').trim().toLowerCase();
            return {
                generated: 'signal_generated',
                pending: 'signal_pending',
                executed: 'entry_filled',
                skipped: 'signal_skipped',
                dropped: 'signal_dropped',
            }[value] || `signal_${value || 'event'}`;
        }

        function buildDerivedBacktestTrackingModel(run, targets, signals, trades, reverseRows) {
            const events = [];
            (targets || []).forEach((row) => {
                addDerivedTrackingEvent(events, {
                    event_type: 'target_selected',
                    stage: 'target',
                    status: row.status || 'selected',
                    symbol: row.symbol,
                    direction: row.direction_bias,
                    bar_time_ms: Number(row.bar_time_ms || row.extra?.cutoff_ms || 0),
                    us_time: row.us_time || row.extra?.cutoff_us_time || '',
                    cn_time: row.cn_time || row.extra?.cutoff_cn_time || '',
                    date: row.date,
                    reason: row.scan_reason || row.extra?.scan_reason || 'target_selected',
                    details: {
                        rank: Number(row.rank || 0),
                        score: Number(row.score || 0),
                        sd_admitted_at_ms: Number(row.extra?.sd_admitted_at_ms || 0),
                        sd_admitted_us_time: row.extra?.sd_admitted_us_time || '',
                    },
                });
            });
            (signals || []).forEach((row) => {
                const history = Array.isArray(row.extra?.status_history) ? row.extra.status_history : [];
                const historyItems = history.length ? history : [{ status: 'generated' }];
                const finalStatus = String(row.status || '').trim().toLowerCase();
                if (!history.length && finalStatus && finalStatus !== 'generated') {
                    historyItems.push({ status: finalStatus });
                }
                historyItems.forEach((item) => {
                    const status = String(item?.status || row.status || 'generated').trim().toLowerCase();
                    const eventType = statusToTrackingEventType(status);
                    addDerivedTrackingEvent(events, {
                        event_type: eventType,
                        stage: ['generated', 'pending', 'skipped', 'dropped'].includes(status) ? 'signal' : 'execution',
                        status,
                        symbol: row.symbol,
                        direction: row.direction,
                        signal_id: row.signal_id,
                        signal: row.signal,
                        setup: row.setup,
                        setup_label: row.setup_label,
                        setup_family: row.setup_family,
                        signal_mode: row.signal_mode,
                        bar_time_ms: Number(item?.bar_time_ms || item?.entry_bar_ms || row.bar_time_ms || 0),
                        us_time: item?.us_time || item?.entry_us_time || row.us_time || '',
                        cn_time: item?.cn_time || item?.entry_cn_time || row.cn_time || '',
                        date: row.date,
                        reason: item?.reason || row.extra?.signal_status_reason || row.reason || status,
                        entry_price: Number(item?.entry_price || 0),
                        stop_loss: Number(row.stop_loss || 0),
                        take_profit: Number(row.take_profit || 0),
                        shares: Number(row.shares || 0),
                        details: {
                            entry: Number(row.entry || 0),
                            stop_loss: Number(row.stop_loss || 0),
                            take_profit: Number(row.take_profit || 0),
                            rr: row.rr || '',
                            shares: Number(row.shares || 0),
                            confirm_ready_bar_ms: Number(item?.confirm_ready_bar_ms || 0),
                            entry_limit_price: Number(item?.entry_limit_price || row.extra?.entry_limit_price || 0),
                            portfolio_open_exposure: Number(item?.portfolio_open_exposure || 0),
                            portfolio_reserved_exposure: Number(item?.portfolio_reserved_exposure || 0),
                        },
                    });
                });
            });
            (trades || []).forEach((trade) => {
                const extra = trade.extra || {};
                addDerivedTrackingEvent(events, {
                    event_type: 'trade_opened',
                    stage: 'execution',
                    status: 'opened',
                    symbol: trade.symbol,
                    direction: trade.direction,
                    signal_id: trade.signal_id,
                    signal: trade.signal,
                    trade_index: Number(trade.trade_index || 0),
                    bar_time_ms: Number(trade.entry_bar_ms || 0),
                    us_time: trade.entry_us_time || '',
                    cn_time: trade.entry_cn_time || '',
                    entry_price: Number(trade.entry_price || 0),
                    shares: Number(trade.shares || 0),
                    reason: trade.reason || 'entry_filled',
                    details: {
                        signal_bar_ms: Number(extra.signal_bar_ms || 0),
                        signal_us_time: extra.signal_us_time || '',
                        entry_reference_price: Number(extra.entry_reference_price || 0),
                        entry_limit_price: Number(extra.entry_limit_price || 0),
                        entry_slippage_bps: Number(extra.entry_slippage_bps || 0),
                    },
                });
                (Array.isArray(extra.risk_adjustments) ? extra.risk_adjustments : []).forEach((adjustment) => {
                    if (!adjustment || typeof adjustment !== 'object') return;
                    addDerivedTrackingEvent(events, {
                        event_type: adjustment.event_type || 'risk_adjustment',
                        stage: 'risk',
                        status: 'adjusted',
                        symbol: trade.symbol,
                        direction: trade.direction,
                        signal_id: trade.signal_id || adjustment.signal_id,
                        signal: trade.signal,
                        setup: trade.setup || extra.setup || '',
                        setup_label: trade.setup_label || extra.setup_label || '',
                        setup_family: trade.setup_family || extra.setup_family || '',
                        signal_mode: trade.signal_mode || extra.signal_mode || '',
                        trade_index: Number(trade.trade_index || 0),
                        bar_time_ms: Number(adjustment.bar_time_ms || 0),
                        us_time: adjustment.us_time || '',
                        cn_time: adjustment.cn_time || '',
                        old_sl: Number(adjustment.old_sl || 0),
                        new_sl: Number(adjustment.new_sl || 0),
                        old_tp: Number(adjustment.old_tp || 0),
                        new_tp: Number(adjustment.new_tp || 0),
                        reason: adjustment.reason || adjustment.event_type || 'risk_adjustment',
                        details: adjustment,
                    });
                });
                addDerivedTrackingEvent(events, {
                    event_type: 'trade_closed',
                    stage: 'exit',
                    status: trade.exit_reason || 'closed',
                    symbol: trade.symbol,
                    direction: trade.direction,
                    signal_id: trade.signal_id,
                    signal: trade.signal,
                    setup: trade.setup || extra.setup || '',
                    setup_label: trade.setup_label || extra.setup_label || '',
                    setup_family: trade.setup_family || extra.setup_family || '',
                    signal_mode: trade.signal_mode || extra.signal_mode || '',
                    trade_index: Number(trade.trade_index || 0),
                    bar_time_ms: Number(trade.exit_bar_ms || 0),
                    us_time: trade.exit_us_time || '',
                    cn_time: trade.exit_cn_time || '',
                    entry_price: Number(trade.entry_price || 0),
                    exit_price: Number(trade.exit_price || 0),
                    stop_loss: Number(extra.stop_price || 0),
                    take_profit: Number(extra.target_price || 0),
                    pnl: Number(trade.pnl || 0),
                    pnl_pct: Number(trade.pnl_pct || 0),
                    shares: Number(trade.shares || 0),
                    reason: trade.exit_reason || 'closed',
                    details: {
                        bars_held: Number(trade.bars_held || 0),
                        initial_stop_loss: Number(extra.initial_stop_loss || 0),
                        initial_take_profit: Number(extra.initial_take_profit || 0),
                        mfe: Number(extra.mfe || 0),
                        mae: Number(extra.mae || 0),
                        total_commission: Number(extra.total_commission || 0),
                        estimated_slippage_cost: Number(extra.estimated_slippage_cost || 0),
                    },
                });
            });
            (reverseRows || []).forEach((row) => {
                addDerivedTrackingEvent(events, {
                    event_type: 'reverse_action',
                    stage: 'special',
                    status: row.action_type || row.status || 'generated',
                    symbol: row.symbol,
                    direction: row.direction,
                    signal_id: row.signal_id || row.extra?.signal_id,
                    setup: row.setup,
                    setup_label: row.setup_label,
                    setup_family: row.setup_family,
                    signal_mode: row.signal_mode,
                    bar_time_ms: Number(row.bar_time_ms || 0),
                    us_time: row.us_time || '',
                    cn_time: row.cn_time || '',
                    reason: row.reason || '',
                    old_sl: Number(row.extra?.old_sl || 0),
                    new_sl: Number(row.extra?.new_sl || 0),
                    old_tp: Number(row.extra?.old_tp || 0),
                    new_tp: Number(row.extra?.new_tp || 0),
                    details: {
                        reverse_kind: row.reverse_kind || '',
                        source: row.source || '',
                        target_state: row.target_state || '',
                        strength: row.strength || '',
                        score: Number(row.score || 0),
                        triggered_signals: row.triggered_signals || [],
                        origin_signal_id: row.origin_signal_id || row.extra?.origin_signal_id || '',
                        trade_group_id: row.trade_group_id || '',
                        new_direction: row.extra?.new_direction || '',
                    },
                });
            });
            const summaries = buildTrackingSummaries(events, run, {});
            return {
                enabled: Boolean(summaries.timeline.length || summaries.daily_summary.length),
                audit_mode: 'derived',
                date_from: String(run?.date_from || ''),
                date_to: String(run?.date_to || ''),
                focus_date: summaries.focus_date,
                focus_symbols: summaries.focus_symbols,
                event_count: summaries.timeline.length,
                event_type_counts: summaries.event_type_counts,
                stage_counts: summaries.stage_counts,
                timeline_limit: 0,
                timeline_truncated: false,
                timeline: summaries.timeline,
                focus_day: summaries.focus_day,
                daily_summary: summaries.daily_summary,
                symbol_day_flows: summaries.symbol_day_flows,
                symbol_day_flow_truncated: false,
                dates: summaries.dates,
                symbols: summaries.symbols,
                eventTypes: summaries.eventTypes,
                setupLabels: summaries.setupLabels,
            };
        }

        function buildBacktestTrackingModel(run, targets, signals, trades, reverseRows) {
            if (!run) return null;
            const nativeAudit = getRunAuditPayload(run);
            const nativeTimeline = [
                ...(Array.isArray(nativeAudit.timeline) ? nativeAudit.timeline : []),
                ...(Array.isArray(nativeAudit.focus_day?.timeline) ? nativeAudit.focus_day.timeline : []),
            ];
            if (nativeTimeline.length || (Array.isArray(nativeAudit.symbol_day_flows) && nativeAudit.symbol_day_flows.length)) {
                return normalizeNativeBacktestTrackingModel(run, nativeAudit);
            }
            return buildDerivedBacktestTrackingModel(run, targets, signals, trades, reverseRows);
        }

        function countRunSymbols(run) {
            const raw = String(run?.symbols || '').trim();
            if (!raw) return 0;
            return raw.split(',').map((item) => item.trim()).filter(Boolean).length;
        }

        function isBacktestRunMutable(run) {
            const status = String(run?.status || '').trim().toLowerCase();
            return ['queued', 'running', 'cancelling', 'pending', 'started'].includes(status);
        }

        function statusTone(status) {
            const value = String(status || '').toLowerCase();
            if (value === 'completed' || value === 'pass' || value === 'ok') return 'pill-ok';
            if (value === 'running' || value === 'queued' || value === 'cancelling' || value === 'warn' || value === 'partial') return 'pill-running';
            if (value === 'failed' || value === 'cancelled' || value === 'fail' || value === 'error') return 'pill-failed';
            return 'pill-muted';
        }

        function detailStatusTag(status) {
            const value = String(status || '').toLowerCase();
            return `<span class="pill ${statusTone(value)}"><span class="dot"></span>${escapeHtml(value || 'idle')}</span>`;
        }

        function formatMoney(value) {
            const num = Number(value || 0);
            const prefix = num > 0 ? '+' : '';
            return `${prefix}$${num.toFixed(2)}`;
        }

        function formatPct(value) {
            const num = Number(value || 0);
            const prefix = num > 0 ? '+' : '';
            return `${prefix}${num.toFixed(2)}%`;
        }

        function formatNumber(value, digits = 2) {
            const num = Number(value || 0);
            return Number.isFinite(num) ? num.toFixed(digits) : '--';
        }

        function formatBacktestDuration(value) {
            const seconds = Number(value || 0);
            if (!Number.isFinite(seconds) || seconds <= 0) return '--';
            if (seconds < 1) return `${seconds.toFixed(3)}s`;
            if (seconds < 10) return `${seconds.toFixed(2)}s`;
            if (seconds < 60) return `${seconds.toFixed(1)}s`;
            const totalSeconds = Math.round(seconds);
            const hours = Math.floor(totalSeconds / 3600);
            const minutes = Math.floor((totalSeconds % 3600) / 60);
            const remainingSeconds = totalSeconds % 60;
            if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
            return `${minutes}m ${String(remainingSeconds).padStart(2, '0')}s`;
        }

        function firstFiniteBacktestNumber(...values) {
            for (const value of values) {
                const number = Number(value);
                if (Number.isFinite(number) && number > 0) return number;
            }
            return 0;
        }

        function getBacktestPhaseRuntimeModel(run) {
            const metrics = run?.metrics || {};
            const extra = run?.extra || {};
            const historicalTargeting = metrics.historical_targeting || extra.historical_targeting || {};
            const dailySelectionCache = metrics.daily_selection_cache
                || extra.daily_selection_cache
                || historicalTargeting.daily_selection_cache
                || {};
            const dailySelectedProfile = metrics.daily_selected_profile
                || metrics.portfolio_profile
                || extra.daily_selected_profile
                || extra.portfolio_profile
                || {};
            const totalDuration = firstFiniteBacktestNumber(run?.duration_s, metrics.duration_s, extra.duration_s);
            const selectionDuration = firstFiniteBacktestNumber(
                dailySelectionCache.duration_s,
                historicalTargeting.duration_s,
                metrics.daily_scan_replay_duration_s,
                extra.daily_scan_replay_duration_s
            );
            const executionDuration = firstFiniteBacktestNumber(
                dailySelectedProfile.duration_s,
                metrics.portfolio_stream_duration_s,
                metrics.portfolio_duration_s,
                extra.portfolio_stream_duration_s,
                extra.portfolio_duration_s
            );
            const otherDuration = totalDuration > 0
                ? Math.max(0, totalDuration - selectionDuration - executionDuration)
                : 0;
            const hitDays = Math.max(0, Number(dailySelectionCache.hit_days || 0) || 0);
            const totalDays = Math.max(
                0,
                Number(dailySelectionCache.total_days || historicalTargeting.target_date_count || 0) || 0
            );
            const rawMissDays = Number(dailySelectionCache.miss_days);
            const missDays = Number.isFinite(rawMissDays)
                ? Math.max(0, rawMissDays)
                : Math.max(0, totalDays - hitDays);
            const rebuiltDays = Math.max(0, Number(dailySelectionCache.rebuilt_days || 0) || 0);
            const rawHitRate = Number(dailySelectionCache.hit_rate);
            const hitRate = Number.isFinite(rawHitRate)
                ? rawHitRate
                : (totalDays > 0 ? (hitDays / totalDays) * 100 : 0);
            const barsLoaded = Number(dailySelectedProfile.bars_loaded || 0) || 0;
            const indicatorCount = Number(dailySelectedProfile.indicator_count || metrics.indicator_count || 0) || 0;
            const tradeDates = Number(dailySelectedProfile.trade_dates || historicalTargeting.target_date_count || 0) || 0;
            const selectedSymbolDays = Number(dailySelectedProfile.selected_symbol_days || historicalTargeting.target_row_count || 0) || 0;
            const cacheEnabled = Boolean(dailySelectionCache.enabled);
            return {
                totalDuration,
                selectionDuration,
                executionDuration,
                otherDuration,
                totalLabel: formatBacktestDuration(totalDuration),
                selectionLabel: formatBacktestDuration(selectionDuration),
                executionLabel: formatBacktestDuration(executionDuration),
                otherLabel: formatBacktestDuration(otherDuration),
                cacheEnabled,
                hitDays,
                missDays,
                rebuiltDays,
                totalDays,
                hitRate,
                cacheValue: cacheEnabled ? `${formatNumber(hitDays, 0)}/${formatNumber(totalDays, 0)} hit` : 'disabled',
                cacheSummary: cacheEnabled
                    ? `miss ${formatNumber(missDays, 0)} · rebuilt ${formatNumber(rebuiltDays, 0)} · ${formatPct(hitRate)} hit`
                    : 'selection cache disabled',
                selectionSummary: cacheEnabled
                    ? `cache ${formatNumber(hitDays, 0)}/${formatNumber(totalDays, 0)} hit · miss ${formatNumber(missDays, 0)}`
                    : 'historical target rebuild',
                executionSummary: `${formatNumber(tradeDates, 0)} days · bars ${formatNumber(barsLoaded, 0)} · indicators ${formatNumber(indicatorCount, 0)}`,
                selectedSummary: `${formatNumber(selectedSymbolDays, 0)} selected symbol-days`,
            };
        }

        function getBacktestDataQualitySummary(rows) {
            const items = Array.isArray(rows) ? rows : [];
            const symbols = new Set();
            const dates = new Set();
            let okCount = 0;
            let gapCount = 0;
            items.forEach((item) => {
                const symbol = String(item?.symbol || '').trim().toUpperCase();
                const date = String(item?.date || item?.first_bar_us || '').trim().slice(0, 10);
                if (symbol) symbols.add(symbol);
                if (date) dates.add(date);
                const rowGaps = Number(item?.gap_count || 0) || 0;
                gapCount += rowGaps;
                if (String(item?.status || '').toLowerCase() === 'ok' && rowGaps === 0) okCount += 1;
            });
            return {
                rowCount: items.length,
                uniqueSymbolCount: symbols.size,
                tradeDateCount: dates.size,
                okCount,
                gapCount,
                label: `${formatNumber(items.length, 0)} symbol-days · ${formatNumber(symbols.size, 0)} symbols · ${formatNumber(dates.size, 0)} days`,
            };
        }

        function getBacktestDataQualityRowDate(item) {
            return String(item?.date || item?.first_bar_us || '').trim().slice(0, 10);
        }

        function getBacktestDataQualityTone(item) {
            const gapCount = Number(item?.gap_count || 0) || 0;
            const status = String(item?.status || '').trim().toLowerCase();
            if (status === 'ok' && gapCount === 0) return 'ok';
            if (gapCount > 0) return 'gaps';
            return 'issues';
        }

        function filterBacktestDataQualityRows(rows) {
            const items = Array.isArray(rows) ? rows : [];
            const dateFilter = String(dataQualityFilters.date || 'all').trim();
            const statusFilter = String(dataQualityFilters.status || 'all').trim();
            return items.filter((item) => {
                const rowDate = getBacktestDataQualityRowDate(item);
                if (dateFilter !== 'all' && rowDate !== dateFilter) return false;
                const tone = getBacktestDataQualityTone(item);
                if (statusFilter === 'ok') return tone === 'ok';
                if (statusFilter === 'gaps') return tone === 'gaps';
                if (statusFilter === 'issues') return tone !== 'ok';
                return true;
            });
        }

        function getBacktestDataQualityDateOptions(rows) {
            const groups = new Map();
            (Array.isArray(rows) ? rows : []).forEach((item) => {
                const date = getBacktestDataQualityRowDate(item);
                if (!date) return;
                const current = groups.get(date) || { date, count: 0, issueCount: 0 };
                current.count += 1;
                if (getBacktestDataQualityTone(item) !== 'ok') current.issueCount += 1;
                groups.set(date, current);
            });
            return Array.from(groups.values()).sort((a, b) => a.date.localeCompare(b.date));
        }

        function buildBacktestTargetLookup(rows) {
            const lookup = new Map();
            (Array.isArray(rows) ? rows : []).forEach((row) => {
                const date = String(row?.date || '').trim().slice(0, 10);
                const symbol = String(row?.symbol || '').trim().toUpperCase();
                if (!date || !symbol) return;
                const key = `${date}|${symbol}`;
                if (!lookup.has(key)) lookup.set(key, []);
                lookup.get(key).push(row);
            });
            return lookup;
        }

        function getBacktestTargetRowsForQualityRow(item, targetLookup) {
            const date = getBacktestDataQualityRowDate(item);
            const symbol = String(item?.symbol || '').trim().toUpperCase();
            if (!date || !symbol || !targetLookup) return [];
            return targetLookup.get(`${date}|${symbol}`) || [];
        }

        function getBacktestTargetExtra(target) {
            const extra = target?.extra;
            return extra && typeof extra === 'object' ? extra : parseMaybeJson(extra, {});
        }

        function getBacktestTargetEventTime(event) {
            const usTime = String(event?.us_time || '').trim();
            const fullMatch = usTime.match(/\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
            if (fullMatch) return `${fullMatch[1]} ${fullMatch[2]} ET`;
            const match = usTime.match(/\b(\d{2}:\d{2})/);
            return match ? `${match[1]} ET` : '';
        }

        function getBacktestTargetDate(target) {
            const date = String(target?.date || '').trim();
            if (/^\d{4}-\d{2}-\d{2}$/.test(date)) return date;
            const usTime = String(target?.us_time || '').trim();
            const match = usTime.match(/\b(\d{4}-\d{2}-\d{2})\b/);
            return match ? match[1] : '';
        }

        function getBacktestTargetEventDate(event) {
            const usTime = String(event?.us_time || '').trim();
            const match = usTime.match(/\b(\d{4}-\d{2}-\d{2})\b/);
            return match ? match[1] : '';
        }

        function getBacktestTargetDayEvents(timeline, targetDate) {
            const events = Array.isArray(timeline) ? timeline : [];
            const day = String(targetDate || '').trim();
            if (!day) return events;
            return events.filter((event) => getBacktestTargetEventDate(event) === day);
        }

        function formatBacktestTargetEventChip(event) {
            const label = String(event?.label || event?.key || '').trim();
            const timeText = getBacktestTargetEventTime(event);
            const valueText = String(event?.value_label || '').trim();
            return [label, timeText, valueText].filter(Boolean).join(' ');
        }

        function formatBacktestTargetReasonNumber(value, digits = 1) {
            const numeric = Number(value || 0);
            if (!Number.isFinite(numeric)) return '0';
            const rounded = Number(numeric.toFixed(digits));
            return Number.isInteger(rounded) ? String(rounded) : String(rounded);
        }

        function buildBacktestTargetDayPrimaryText({ status, dayEvents, score, activeMinScore, summary }) {
            const parts = (Array.isArray(dayEvents) ? dayEvents : [])
                .slice(0, 3)
                .map((event) => formatBacktestTargetEventChip(event))
                .filter(Boolean);
            const minScore = Number(activeMinScore || summary?.active_min_score || 0) || 0;
            if (minScore > 0) {
                const relation = Number(score || 0) >= minScore ? '>=' : '<';
                parts.push(`score ${formatBacktestTargetReasonNumber(score, 1)}${relation}${formatBacktestTargetReasonNumber(minScore, 1)}`);
            } else if (Number(score || 0) > 0) {
                parts.push(`score ${formatBacktestTargetReasonNumber(score, 1)}`);
            }
            const admissionScore = Number(summary?.admission_score || 0) || 0;
            const admissionThreshold = Number(summary?.admission_score_threshold || 0) || 0;
            if (admissionScore > 0 && admissionThreshold > 0) {
                parts.push(`admission ${formatBacktestTargetReasonNumber(admissionScore, 1)}>=${formatBacktestTargetReasonNumber(admissionThreshold, 1)}`);
            } else if (admissionScore > 0) {
                parts.push(`admission ${formatBacktestTargetReasonNumber(admissionScore, 1)}`);
            }
            const longVotes = Number(summary?.long_votes || 0) || 0;
            const shortVotes = Number(summary?.short_votes || 0) || 0;
            if (longVotes || shortVotes) parts.push(`votes L${formatNumber(longVotes, 0)}:S${formatNumber(shortVotes, 0)}`);
            return parts.length ? `入选 ${status || 'active'}: ${parts.join(' · ')}` : '';
        }

        function pushUniqueChip(chips, value) {
            const text = String(value || '').trim();
            if (text && !chips.includes(text)) chips.push(text);
        }

        function getBacktestTargetReasonModel(targetRows) {
            const chips = [];
            let primaryText = '';
            let hiddenReasons = 0;
            let totalReasons = 0;
            (Array.isArray(targetRows) ? targetRows : []).forEach((target) => {
                const extra = getBacktestTargetExtra(target);
                const summary = extra.active_reason_summary && typeof extra.active_reason_summary === 'object'
                    ? extra.active_reason_summary
                    : {};
                const timeline = Array.isArray(extra.trigger_timeline) ? extra.trigger_timeline : [];
                const targetDate = getBacktestTargetDate(target);
                const dayEvents = getBacktestTargetDayEvents(timeline, targetDate);
                const status = String(target?.status || summary.status || '').trim().toLowerCase();
                const bias = String(target?.direction_bias || summary.direction_bias || '').trim().toLowerCase();
                const rank = Number(target?.rank || summary.rank || extra.selection_rank || 0) || 0;
                const score = Number(target?.score || summary.score || 0) || 0;
                const activeMinScore = Number(summary.active_min_score || 0) || 0;
                const reasonCount = dayEvents.length;
                totalReasons += reasonCount;
                if (!primaryText) {
                    primaryText = buildBacktestTargetDayPrimaryText({
                        status,
                        dayEvents,
                        score,
                        activeMinScore,
                        summary,
                    });
                }
                if (status) {
                    pushUniqueChip(chips, reasonCount > 1 ? `${status} · ${formatNumber(reasonCount, 0)} day reasons` : status);
                }
                const visibleEvents = dayEvents.slice(0, 4);
                visibleEvents.forEach((event) => pushUniqueChip(chips, formatBacktestTargetEventChip(event)));
                hiddenReasons += Math.max(0, dayEvents.length - visibleEvents.length);
                if (bias) pushUniqueChip(chips, bias);
                if (rank > 0) pushUniqueChip(chips, `rank ${formatNumber(rank, 0)}`);
                if (score > 0) pushUniqueChip(chips, `score ${formatNumber(score, 1)}`);
                if (!dayEvents.length) {
                    String(target?.scan_reason || extra.scan_reason || summary.scan_reason || '')
                        .split(/[;,，、|]/)
                        .map((part) => part.trim())
                        .filter(Boolean)
                        .slice(0, 4)
                        .forEach((part) => pushUniqueChip(chips, part));
                }
            });
            if (hiddenReasons > 0) pushUniqueChip(chips, `+${formatNumber(hiddenReasons, 0)} day reasons`);
            return {
                chips,
                primaryText,
                totalReasons,
            };
        }

        function getBacktestTablePreview(key, rows) {
            const items = Array.isArray(rows) ? rows : [];
            const limit = Number(BACKTEST_TABLE_PREVIEW_LIMITS[key] || 0);
            const expanded = Boolean(backtestTableExpandedState[key]);
            if (!limit || items.length <= limit || expanded) {
                return {
                    rows: items,
                    total: items.length,
                    visible: items.length,
                    hidden: 0,
                    expanded,
                    truncated: Boolean(limit && items.length > limit),
                };
            }
            return {
                rows: items.slice(0, limit),
                total: items.length,
                visible: limit,
                hidden: Math.max(0, items.length - limit),
                expanded,
                truncated: true,
            };
        }

        function renderBacktestTablePreviewBar(key, preview, noun = '条记录') {
            if (!preview?.truncated) return '';
            const actionLabel = preview.expanded ? '收起预览' : '展开全部';
            const previewCopy = preview.expanded
                ? `当前显示全部 ${formatNumber(preview.total, 0)} ${noun}。`
                : `当前先展示 ${formatNumber(preview.visible, 0)} / ${formatNumber(preview.total, 0)} ${noun}，还有 ${formatNumber(preview.hidden, 0)} ${noun} 未展开。`;
            return `
                <div class="table-preview-bar">
                    <div class="table-preview-copy">${escapeHtml(previewCopy)}</div>
                    <button class="btn ghost" type="button" onclick="toggleBacktestTableExpansion('${escapeHtml(key)}')">${escapeHtml(actionLabel)}</button>
                </div>
            `;
        }

        function getBacktestTextPreview(key, text) {
            const raw = String(text ?? '');
            const limit = Number(BACKTEST_TEXT_PREVIEW_LIMITS[key] || 0);
            const expanded = Boolean(backtestTextExpandedState[key]);
            if (!limit || raw.length <= limit || expanded) {
                return {
                    text: raw,
                    total: raw.length,
                    visible: raw.length,
                    hidden: 0,
                    expanded,
                    truncated: Boolean(limit && raw.length > limit),
                };
            }
            return {
                text: `${raw.slice(0, limit).trimEnd()}\n…`,
                total: raw.length,
                visible: limit,
                hidden: Math.max(0, raw.length - limit),
                expanded,
                truncated: true,
            };
        }

        function renderBacktestTextPreviewBar(key, preview, section) {
            if (!preview?.truncated) return '';
            const actionLabel = preview.expanded ? '收起预览' : '展开全部';
            const previewCopy = preview.expanded
                ? `当前显示全部 ${formatNumber(preview.total, 0)} 个字符。`
                : `当前先展示 ${formatNumber(preview.visible, 0)} / ${formatNumber(preview.total, 0)} 个字符，剩余 ${formatNumber(preview.hidden, 0)} 个字符已折叠。`;
            return `
                <div class="table-preview-bar">
                    <div class="table-preview-copy">${escapeHtml(previewCopy)}</div>
                    <button class="btn ghost" type="button" onclick="toggleBacktestTextExpansion('${escapeHtml(key)}', '${escapeHtml(section)}')">${escapeHtml(actionLabel)}</button>
                </div>
            `;
        }

        function renderBacktestTextPreviewBox(key, text, section, extraClass = 'mono') {
            const preview = getBacktestTextPreview(key, text);
            const className = [
                'note-box',
                'text-preview-box',
                preview.expanded ? 'is-expanded' : 'is-collapsed',
                extraClass || '',
            ].filter(Boolean).join(' ');
            return `
                ${renderBacktestTextPreviewBar(key, preview, section)}
                <div class="${className}">${escapeHtml(preview.text)}</div>
            `;
        }

        function resetBacktestClientPagination(key = '') {
            if (key) {
                const config = BACKTEST_CLIENT_PAGE_CONFIG[key];
                if (!config) return;
                backtestClientPageState[key] = {
                    page: 1,
                    pageSize: Number(config.pageSize || 12),
                };
                return;
            }
            backtestClientPageState = {};
            Object.entries(BACKTEST_CLIENT_PAGE_CONFIG).forEach(([pageKey, config]) => {
                backtestClientPageState[pageKey] = {
                    page: 1,
                    pageSize: Number(config.pageSize || 12),
                };
            });
        }

        function getBacktestClientPagination(key, rows) {
            const config = BACKTEST_CLIENT_PAGE_CONFIG[key] || {};
            if (!backtestClientPageState[key]) {
                backtestClientPageState[key] = {
                    page: 1,
                    pageSize: Number(config.pageSize || 12),
                };
            }
            const pageModel = createClientPaginationModel(rows, backtestClientPageState[key], config);
            backtestClientPageState[key] = {
                page: pageModel.page,
                pageSize: pageModel.pageSize,
            };
            return pageModel;
        }

        function renderBacktestClientPaginationBar(key, rows, options = {}) {
            const config = BACKTEST_CLIENT_PAGE_CONFIG[key] || {};
            const pageModel = getBacktestClientPagination(key, rows);
            const label = options.label || config.label || key;
            const shownFrom = pageModel.total ? pageModel.start + 1 : 0;
            const shownTo = pageModel.end;
            return renderClientPaginationBar(pageModel, {
                key,
                label,
                pageAction: 'setBacktestClientPage',
                pageSizeAction: 'setBacktestClientPageSize',
                statusText: `${label} ${formatNumber(shownFrom, 0)}-${formatNumber(shownTo, 0)} / ${formatNumber(pageModel.total, 0)}`,
                rootClass: 'client-pagination page-pagination backtest-client-pagination',
            });
        }

        function formatRunDate(run) {
            const from = run?.date_from || '--';
            const to = run?.date_to || '--';
            return `${from} → ${to}`;
        }

        function formatBreakdown(breakdown) {
            if (!breakdown || typeof breakdown !== 'object') return '--';
            const entries = Object.entries(breakdown)
                .filter(([, value]) => Number(value || 0) > 0)
                .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0));
            if (!entries.length) return '--';
            return entries.map(([key, value]) => `${key}:${value}`).join(' · ');
        }

        function formatDailyCounts(counts) {
            const entries = Array.isArray(counts)
                ? counts.map((item) => [item?.date, Number(item?.open_count || item?.count || 0)])
                : Object.entries(counts || {}).map(([date, value]) => [date, Number(value || 0)]);
            const filtered = entries
                .filter(([date, value]) => String(date || '').trim() && Number(value || 0) > 0)
                .sort((a, b) => String(b[0]).localeCompare(String(a[0])));
            if (!filtered.length) return '--';
            return filtered.slice(0, 5).map(([date, value]) => `${date}:${value}`).join(' · ');
        }

        function sumDailyOpenCounts(counts) {
            if (Array.isArray(counts)) {
                return counts.reduce((sum, item) => sum + Number(item?.open_count || item?.count || 0), 0);
            }
            return Object.values(counts || {}).reduce((sum, value) => sum + Number(value || 0), 0);
        }

        function summarizeRunSymbols(run) {
            const raw = String(run?.symbols || '').trim();
            if (!raw) return '--';
            const items = raw.split(',').map((item) => item.trim()).filter(Boolean);
            if (items.length <= 4) return items.join(', ');
            return `${items.slice(0, 4).join(', ')} +${items.length - 4}`;
        }

        function classForValue(value) {
            const num = Number(value || 0);
            if (num > 0) return 'positive';
            if (num < 0) return 'negative';
            return '';
        }

        function getStrategyParamsText(run) {
            const params = run?.params?.strategy_params || {};
            return Object.keys(params).length ? JSON.stringify(params, null, 2) : '{}';
        }
