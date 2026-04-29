let currentEnvironment = getCurrentRuntimeEnvironment();
        let activeStatus = null;
        let batchList = [];
        let selectedBatchId = '';
        let selectedBatch = null;
        let runList = [];
        let selectedRunId = '';
        let selectedRun = null;
        let selectedTrades = [];
        let selectedTargets = [];
        let selectedTargetsLoading = false;
        let selectedReplayRows = [];
        let equityChart = null;
        let replayChart = null;
        let refreshTimer = null;
        let actionPending = false;
        let activeBacktestTab = 'runs';
        const BACKTEST_TABLE_PREVIEW_LIMITS = Object.freeze({
            leaderboard: 50,
            trades: 20,
            replay: 40,
        });
        const backtestTableExpandedState = {
            leaderboard: false,
            trades: false,
            replay: false,
        };
        const BACKTEST_TEXT_PREVIEW_LIMITS = Object.freeze({
            batchVariants: 1800,
            strategyParams: 1800,
            runtimeExtra: 2200,
        });
        const backtestTextExpandedState = {
            batchVariants: false,
            strategyParams: false,
            runtimeExtra: false,
        };

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

        async function apiFetchAll(collection, params = {}) {
            const allItems = [];
            const perPage = Number(params.perPage || 200);
            const maxPages = Number(params.maxPages || 8);
            for (let page = 1; page <= maxPages; page += 1) {
                const payload = await apiFetch(collection, { ...params, page, perPage });
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
            return {
                ...record,
                extra: parseMaybeJson(record.extra, {}),
                pnl: Number(record.pnl || 0),
                pnl_pct: Number(record.pnl_pct || 0),
                entry_price: Number(record.entry_price || 0),
                exit_price: Number(record.exit_price || 0),
                shares: Number(record.shares || 0),
                bars_held: Number(record.bars_held || 0),
                entry_bar_ms: Number(record.entry_bar_ms || 0),
                exit_bar_ms: Number(record.exit_bar_ms || 0),
                trade_index: Number(record.trade_index || 0),
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
            const className = extraClass ? `note-box ${extraClass}` : 'note-box';
            return `
                ${renderBacktestTextPreviewBar(key, preview, section)}
                <div class="${className}">${escapeHtml(preview.text)}</div>
            `;
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

        function groupTargetsByDate(items, run) {
            const dailyRows = Array.isArray(run?.extra?.historical_targeting?.daily) ? run.extra.historical_targeting.daily : [];
            const dailyMap = new Map(
                dailyRows.map((item) => [String(item?.date || ''), item || {}]),
            );
            const groups = new Map();
            items.forEach((item) => {
                const date = String(item?.date || '--');
                if (!groups.has(date)) {
                    groups.set(date, {
                        date,
                        summary: dailyMap.get(date) || null,
                        items: [],
                    });
                }
                groups.get(date).items.push(item);
            });
            return Array.from(groups.values())
                .map((group) => ({
                    ...group,
                    items: group.items.sort((a, b) => {
                        const rankDelta = Number(a.rank || 9999) - Number(b.rank || 9999);
                        if (rankDelta !== 0) return rankDelta;
                        const scoreDelta = Number(b.score || 0) - Number(a.score || 0);
                        if (scoreDelta !== 0) return scoreDelta;
                        return String(a.symbol || '').localeCompare(String(b.symbol || ''));
                    }),
                }))
                .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
        }

        function buildBacktestTargetReplayCard(run) {
            const capture = run?.extra?.backtest_target_capture || run?.metrics?.backtest_target_capture || {};
            const historicalTargeting = run?.extra?.historical_targeting || run?.metrics?.historical_targeting || {};
            const scanDiagnostics = run?.metrics?.daily_scan_match_diagnostics || run?.extra?.daily_scan_match_diagnostics || {};
            const groups = groupTargetsByDate(selectedTargets, run);
            const selectedSymbolCount = Number(
                historicalTargeting.selected_symbol_count
                || new Set(selectedTargets.map((item) => String(item?.symbol || '').trim()).filter(Boolean)).size
                || 0,
            );
            const targetCount = Number(
                run?.metrics?.backtest_target_count
                || capture.saved_count
                || historicalTargeting.target_row_count
                || selectedTargets.length
                || 0,
            );
            const status = selectedTargetsLoading
                ? 'running'
                : String(capture.status || (targetCount > 0 ? 'ok' : 'empty')).toLowerCase();
            const enabled = String(run?.symbol_source || '').toLowerCase() === 'daily_scan_replay' || targetCount > 0;

            if (!enabled && !selectedTargets.length) {
                return `
                    <div class="detail-card">
                        <div class="subhead">Historical Targets</div>
                        <div class="empty-state">未启用历史选股回放。</div>
                    </div>
                `;
            }

            return `
                <div class="detail-card">
                    <div class="subhead">Historical Targets</div>
                    <div class="foot-note">展示历史选股回放结果；开仓以 selection plan 为准。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Collection</div><div class="detail-item-value mono">${escapeHtml(capture.collection || 'ibkr_backtest_targets')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Rows</div><div class="detail-item-value">${escapeHtml(String(targetCount))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Trade Dates</div><div class="detail-item-value">${escapeHtml(String(historicalTargeting.target_date_count || groups.length || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Selected Symbols</div><div class="detail-item-value">${escapeHtml(String(selectedSymbolCount))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Universe Mode</div><div class="detail-item-value">${escapeHtml(historicalTargeting.universe_mode || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Cutoff</div><div class="detail-item-value">${escapeHtml(historicalTargeting.premarket_cutoff_time || run?.extra?.premarket_cutoff_time || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scan Session</div><div class="detail-item-value">${escapeHtml(historicalTargeting.scan_session_mode || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scan Warmup</div><div class="detail-item-value">${escapeHtml(String(historicalTargeting.scan_warmup_bars || run?.extra?.scan_warmup_bars || '--'))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Saved / Errors</div><div class="detail-item-value">${escapeHtml(String(capture.saved_count || 0))} / ${escapeHtml(String(capture.error_count || 0))}</div></div>
                        ${scanDiagnostics.enabled ? `
                            <div class="detail-item"><div class="detail-item-label">Signal Match</div><div class="detail-item-value">${escapeHtml(formatPct(scanDiagnostics.selected_day_signal_rate_pct || 0))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Matched Signals</div><div class="detail-item-value">${escapeHtml(String(scanDiagnostics.selected_day_signal_count || 0))} / ${escapeHtml(String(scanDiagnostics.generated_signal_count || 0))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Not Selected</div><div class="detail-item-value">${escapeHtml(String(scanDiagnostics.not_selected_signal_count || 0))}</div></div>
                        ` : ''}
                    </div>
                    ${selectedTargetsLoading ? '<div class="empty-state" style="margin-top: 14px;">读取历史 targets ...</div>' : ''}
                    ${!selectedTargetsLoading && groups.length ? groups.map((group) => {
                        const summary = group.summary || {};
                        const first = group.items[0] || {};
                        const universeSize = Number(summary.universe_size || first?.extra?.universe_size || 0);
                        const selectedCount = Number(summary.selected_count || group.items.length || 0);
                        const readyCount = Number(summary.ready_symbol_count || 0);
                        const candidateCount = Number(summary.candidate_count || 0);
                        return `
                            <div class="subhead" style="margin-top: 18px;">${escapeHtml(group.date)}</div>
                            <div class="foot-note">
                                selected ${escapeHtml(String(selectedCount))}
                                ${candidateCount ? ` / candidates ${escapeHtml(String(candidateCount))}` : ''}
                                ${readyCount ? ` · ready ${escapeHtml(String(readyCount))}` : ''}
                                ${universeSize ? ` · universe ${escapeHtml(String(universeSize))}` : ''}
                            </div>
                            <div class="table-wrap" style="margin-top: 10px;">
                                <table class="data-table" style="min-width: 900px;">
                                    <thead>
                                        <tr>
                                            <th>Rank</th>
                                            <th>Symbol</th>
                                            <th>Score</th>
                                            <th>Bias</th>
                                            <th>Reason</th>
                                            <th>Cutoff</th>
                                            <th>Universe</th>
                                            <th>Replay</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        ${group.items.map((item) => `
                                            <tr>
                                                <td class="mono">${escapeHtml(String(item.rank || '--'))}</td>
                                                <td class="mono">${escapeHtml(item.symbol || '--')}${item.exchange ? `<br><span style="color: var(--muted);">${escapeHtml(item.exchange)}</span>` : ''}</td>
                                                <td>${escapeHtml(formatNumber(item.score || 0, 4))}</td>
                                                <td>${item.direction_bias === 'short'
                                                    ? '<span class="tag short">SHORT</span>'
                                                    : item.direction_bias === 'long'
                                                        ? '<span class="tag long">LONG</span>'
                                                        : `<span class="tag">${escapeHtml((item.direction_bias || 'neutral').toUpperCase())}</span>`}</td>
                                                <td>${escapeHtml(item.scan_reason || '--')}</td>
                                                <td class="mono">${escapeHtml(item.us_time || '--')}${item.cn_time ? `<br><span style="color: var(--muted);">${escapeHtml(item.cn_time)}</span>` : ''}</td>
                                                <td>${escapeHtml(String(item?.extra?.universe_size || universeSize || '--'))}</td>
                                                <td><button class="btn ghost" type="button" onclick="replayTarget('${escapeHtml(item.symbol || '')}', ${Number(item.bar_time_ms || 0)})">回放</button></td>
                                            </tr>
                                        `).join('')}
                                    </tbody>
                                </table>
                            </div>
                        `;
                    }).join('') : ''}
                    ${!selectedTargetsLoading && !groups.length ? '<div class="empty-state" style="margin-top: 14px;">暂无历史 target 明细。</div>' : ''}
                </div>
            `;
        }

        function buildBacktestIndicatorCaptureCard(run) {
            const capture = run?.extra?.backtest_indicator_capture || run?.metrics?.backtest_indicator_capture || {};
            const status = String(capture.status || 'empty').toLowerCase();
            const errors = Array.isArray(capture.errors) ? capture.errors : [];
            return `
                <div class="detail-card">
                    <div class="subhead">回测指标留痕</div>
                    <div class="foot-note">默认不落库：bars 可复用，指标只在内存中演算；仅勾选“保存指标明细”时写入该临时表用于排查。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Collection</div><div class="detail-item-value mono">${escapeHtml(capture.collection || 'ibkr_backtest_indicators')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Attempted</div><div class="detail-item-value">${escapeHtml(String(capture.attempted_count || run?.metrics?.backtest_indicator_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Saved</div><div class="detail-item-value">${escapeHtml(String(capture.saved_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Errors</div><div class="detail-item-value">${escapeHtml(String(capture.error_count || 0))}</div></div>
                    </div>
                    ${errors.length ? `<div class="note-box mono" style="margin-top: 14px;">${escapeHtml(JSON.stringify(errors, null, 2))}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无落库错误。</div>'}
                </div>
            `;
        }

        function buildBacktestReverseCaptureCard(run) {
            const capture = run?.extra?.backtest_reverse_capture || run?.metrics?.backtest_reverse_capture || {};
            const status = String(capture.status || 'empty').toLowerCase();
            const errors = Array.isArray(capture.errors) ? capture.errors : [];
            const metrics = run?.metrics || {};
            const samples = Array.isArray(metrics.backtest_reverse_samples) ? metrics.backtest_reverse_samples : [];
            return `
                <div class="detail-card">
                    <div class="subhead">反转验证</div>
                    <div class="foot-note">回测反转留痕，不回写 live。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Collection</div><div class="detail-item-value mono">${escapeHtml(capture.collection || 'ibkr_backtest_reverse_signals')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Attempted</div><div class="detail-item-value">${escapeHtml(String(capture.attempted_count || metrics.backtest_reverse_signal_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Saved</div><div class="detail-item-value">${escapeHtml(String(capture.saved_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Errors</div><div class="detail-item-value">${escapeHtml(String(capture.error_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Action Breakdown</div><div class="detail-item-value">${escapeHtml(formatBreakdown(metrics.backtest_reverse_action_breakdown || {}))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Strength Breakdown</div><div class="detail-item-value">${escapeHtml(formatBreakdown(metrics.backtest_reverse_strength_breakdown || {}))}</div></div>
                    </div>
                    ${samples.length ? `<div class="note-box mono" style="margin-top: 14px;">${escapeHtml(JSON.stringify(samples, null, 2))}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无反转动作。</div>'}
                    ${errors.length ? `<div class="note-box mono" style="margin-top: 14px;">${escapeHtml(JSON.stringify(errors, null, 2))}</div>` : ''}
                </div>
            `;
        }

        function buildRunAnalysisCard(run) {
            const report = run?.extra?.analysis_report || {};
            const summary = report.summary || run?.metrics?.analysis_summary || {};
            const recommendation = report.recommendation || {};
            const comparison = report.baseline_comparison || {};
            const strengths = Array.isArray(recommendation.strengths) ? recommendation.strengths : [];
            const risks = Array.isArray(recommendation.risks) ? recommendation.risks : [];
            const suggestions = Array.isArray(recommendation.suggestions) ? recommendation.suggestions : [];
            const verdict = String(summary.verdict || recommendation.verdict || 'mixed').toLowerCase();
            const baselineExists = Boolean(comparison.baseline_exists);
            const improved = Boolean(summary.improved_vs_baseline || comparison.improved_vs_baseline);
            const strengthsHtml = strengths.map((item) => escapeHtml(item)).join('<br>');
            const risksHtml = risks.map((item) => escapeHtml(item)).join('<br>');
            const suggestionsHtml = suggestions.map((item) => escapeHtml(item)).join('<br>');

            return `
                <div class="detail-card">
                    <div class="subhead">回测分析</div>
                    <div class="foot-note">系统会结合收益、回撤、成交率、反转动作和 TV 对齐结果，自动产出这期回测的判断与优化方向。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(verdict)}</div>
                    <div class="note-box">${escapeHtml(summary.headline || recommendation.headline || '当前 run 尚未产出分析摘要。')}</div>
                    <div class="detail-list" style="margin-top: 14px;">
                        <div class="detail-item"><div class="detail-item-label">Improved vs Baseline</div><div class="detail-item-value ${improved ? 'positive' : ''}">${escapeHtml(improved ? 'YES' : 'NO')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Baseline Run</div><div class="detail-item-value mono">${escapeHtml(comparison.baseline_run_id || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta Return</div><div class="detail-item-value ${classForValue(comparison.delta_return_pct || 0)}">${baselineExists ? escapeHtml(formatPct(comparison.delta_return_pct || 0)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta Sharpe</div><div class="detail-item-value ${classForValue(comparison.delta_sharpe || 0)}">${baselineExists ? escapeHtml(formatNumber(comparison.delta_sharpe || 0, 2)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scope Similarity</div><div class="detail-item-value">${baselineExists ? escapeHtml(`${formatNumber((comparison.scope_similarity?.overlap_ratio || 0) * 100, 0)}% overlap`) : '--'}</div></div>
                    </div>
                    ${strengths.length ? `<div class="note-box" style="margin-top: 14px;"><strong>优势</strong><br>${strengthsHtml}</div>` : ''}
                    ${risks.length ? `<div class="note-box" style="margin-top: 14px;"><strong>风险</strong><br>${risksHtml}</div>` : ''}
                    ${suggestions.length ? `<div class="note-box" style="margin-top: 14px;"><strong>优化建议</strong><br>${suggestionsHtml}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无优化建议。</div>'}
                </div>
            `;
        }

        function buildExperimentAnalysisCard(batch) {
            const analysis = batch?.extra?.experiment_analysis || {};
            const summary = analysis.summary || {};
            const comparison = analysis.comparison || {};
            const suggestions = Array.isArray(analysis.suggestions) ? analysis.suggestions : [];
            const improved = Boolean(summary.improved_vs_historical || comparison.improved_vs_historical);
            const suggestionsHtml = suggestions.map((item) => escapeHtml(item)).join('<br>');

            if (!batch) {
                return '';
            }

            return `
                <div class="detail-card">
                    <div class="subhead">Experiment Analysis</div>
                    <div class="foot-note">系统会在参数扫描完成后，对最佳变体、次优变体和同周期历史基准做对比，帮助判断是否值得替换当前候选方案。</div>
                    <div class="note-box" style="margin-top: 12px;">${escapeHtml(summary.headline || '当前 experiment 尚未产出分析结论。')}</div>
                    <div class="detail-list" style="margin-top: 14px;">
                        <div class="detail-item"><div class="detail-item-label">Best Variant</div><div class="detail-item-value">${escapeHtml(summary.best_variant_label || comparison.best_variant_label || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Improved vs Historical</div><div class="detail-item-value ${improved ? 'positive' : ''}">${escapeHtml(improved ? 'YES' : 'NO')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta vs Historical</div><div class="detail-item-value ${classForValue(comparison.delta_vs_historical_return_pct || 0)}">${comparison.historical_baseline_run_id ? escapeHtml(formatPct(comparison.delta_vs_historical_return_pct || 0)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta vs 2nd Best</div><div class="detail-item-value ${classForValue(comparison.delta_vs_second_best_return_pct || 0)}">${comparison.delta_vs_second_best_return_pct !== undefined ? escapeHtml(formatPct(comparison.delta_vs_second_best_return_pct || 0)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Historical Baseline</div><div class="detail-item-value mono">${escapeHtml(comparison.historical_baseline_run_id || '--')}</div></div>
                    </div>
                    ${suggestions.length ? `<div class="note-box" style="margin-top: 14px;"><strong>优化方向</strong><br>${suggestionsHtml}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无优化方向。</div>'}
                </div>
            `;
        }

        function buildTvParityCard(run) {
            const tvParity = run?.extra?.tv_parity || {};
            const summary = tvParity.summary || run?.metrics?.tv_parity || {};
            const status = String(tvParity.status || summary.status || 'disabled').toLowerCase();
            const symbolRows = Array.isArray(tvParity.symbols) ? tvParity.symbols : [];
            const signalCompareEnabled = Boolean(
                summary.signal_compare_enabled !== undefined
                    ? summary.signal_compare_enabled
                    : tvParity.signal_compare_enabled
            );

            if (!summary.enabled && !tvParity.enabled) {
                return `
                    <div class="detail-card">
                        <div class="subhead">TV 对齐校验</div>
                        <div class="empty-state">未启用 TV 对齐校验。</div>
                    </div>
                `;
            }

            const sampleItems = [];
            symbolRows.forEach((item) => {
                [
                    ['indicators', 'indicator'],
                    ['signals', 'signal'],
                ].forEach(([key, label]) => {
                    const section = item?.[key] || {};
                    if (Array.isArray(section.mismatch_samples) && section.mismatch_samples.length) {
                        sampleItems.push({ symbol: item.symbol, type: `${label}_mismatch`, sample: section.mismatch_samples[0] });
                    }
                    if (Array.isArray(section.missing_in_tv_samples) && section.missing_in_tv_samples.length) {
                        sampleItems.push({ symbol: item.symbol, type: `${label}_missing_in_tv`, sample: section.missing_in_tv_samples[0] });
                    }
                    if (Array.isArray(section.missing_in_backtest_samples) && section.missing_in_backtest_samples.length) {
                        sampleItems.push({ symbol: item.symbol, type: `${label}_missing_in_backtest`, sample: section.missing_in_backtest_samples[0] });
                    }
                });
            });

            return `
                <div class="detail-card">
                    <div class="subhead">TV 对齐校验</div>
                    <div class="foot-note">${signalCompareEnabled
                        ? '这是回测阶段的临时 accuracy audit，只用于当前对齐验证，不会作为以后生产链路的硬阻断条件。'
                        : '当前只校验技术指标与 TV reference 的对齐，TV 信号不再作为误差来源。'}</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Reference</div><div class="detail-item-value">${escapeHtml((tvParity.source_environment || run?.source_environment || '--').toUpperCase())} · ${escapeHtml(tvParity.interval || '5')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Compared Symbols</div><div class="detail-item-value">${escapeHtml(String(summary.symbol_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Symbols With Ref</div><div class="detail-item-value">${escapeHtml(String(summary.symbols_with_reference || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Indicator Match</div><div class="detail-item-value">${escapeHtml(formatPct(summary.indicator_match_rate || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Match</div><div class="detail-item-value">${signalCompareEnabled ? escapeHtml(formatPct(summary.signal_match_rate || 0)) : 'OFF'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Indicator Rows</div><div class="detail-item-value">${escapeHtml(String(summary.indicator_matched_count || 0))} / ${escapeHtml(String(summary.indicator_generated_count || 0))} generated · TV ${escapeHtml(String(summary.indicator_tv_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Indicator Drift</div><div class="detail-item-value">${escapeHtml(String(summary.indicator_mismatch_count || 0))} mismatch · ${escapeHtml(String(summary.indicator_missing_in_tv_count || 0))} missing in TV · ${escapeHtml(String(summary.indicator_missing_in_backtest_count || 0))} missing in replay</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Rows</div><div class="detail-item-value">${signalCompareEnabled ? `${escapeHtml(String(summary.signal_matched_count || 0))} / ${escapeHtml(String(summary.signal_generated_count || 0))} generated · TV ${escapeHtml(String(summary.signal_tv_count || 0))}` : 'disabled'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Drift</div><div class="detail-item-value">${signalCompareEnabled ? `${escapeHtml(String(summary.signal_mismatch_count || 0))} mismatch · ${escapeHtml(String(summary.signal_missing_in_tv_count || 0))} missing in TV · ${escapeHtml(String(summary.signal_missing_in_backtest_count || 0))} missing in replay` : 'disabled'}</div></div>
                    </div>
                    ${symbolRows.length ? `
                        <div class="table-wrap" style="margin-top: 14px;">
                            <table class="data-table" style="min-width: 760px;">
                                <thead>
                                    <tr>
                                        <th>Symbol</th>
                                        <th>Status</th>
                                        <th>Indicators</th>
                                        <th>Signals</th>
                                        <th>Ref Error</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${symbolRows.map((item) => `
                                        <tr>
                                            <td class="mono">${escapeHtml(item.symbol || '--')}</td>
                                            <td>${detailStatusTag(item.status || '--')}</td>
                                            <td>
                                                gen ${escapeHtml(String(item?.indicators?.generated_count || 0))} / tv ${escapeHtml(String(item?.indicators?.tv_count || 0))}
                                                <br>match ${escapeHtml(String(item?.indicators?.matched_count || 0))} · drift ${escapeHtml(String((item?.indicators?.mismatch_count || 0) + (item?.indicators?.missing_in_tv_count || 0) + (item?.indicators?.missing_in_backtest_count || 0)))}
                                            </td>
                                            <td>
                                                ${signalCompareEnabled
                                                    ? `gen ${escapeHtml(String(item?.signals?.generated_count || 0))} / tv ${escapeHtml(String(item?.signals?.tv_count || 0))}
                                                <br>match ${escapeHtml(String(item?.signals?.matched_count || 0))} · drift ${escapeHtml(String((item?.signals?.mismatch_count || 0) + (item?.signals?.missing_in_tv_count || 0) + (item?.signals?.missing_in_backtest_count || 0)))}`
                                                    : 'disabled'}
                                            </td>
                                            <td class="mono">${escapeHtml(item.reference_error || '--')}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state" style="margin-top: 14px;">暂无可对齐 TV rows。</div>'}
                    <div class="subhead" style="margin-top: 16px;">Sample Drift</div>
                    ${sampleItems.length ? `<div class="note-box mono">${escapeHtml(JSON.stringify(sampleItems.slice(0, 8), null, 2))}</div>` : '<div class="empty-state">暂无 sample drift。</div>'}
                </div>
            `;
        }

        function getBatchVariantsText(batch) {
            const variants = batch?.params?.variants || [];
            return Array.isArray(variants) && variants.length ? JSON.stringify(variants, null, 2) : '[]';
        }

        function setActionState(isPending) {
            actionPending = Boolean(isPending);
            const buttons = document.querySelectorAll('button');
            buttons.forEach((button) => {
                if (button.id === 'startButton') {
                    button.disabled = actionPending;
                }
            });
        }

        function resizeBacktestCharts() {
            requestAnimationFrame(() => {
                if (equityChart) equityChart.resize();
                if (replayChart) replayChart.resize();
            });
        }

        function setBacktestTab(tabKey) {
            const panels = Array.from(document.querySelectorAll('[data-backtest-tab-panel]'));
            if (!panels.length) return;
            const targetKey = panels.some((panel) => panel.dataset.backtestTabPanel === tabKey) ? tabKey : 'runs';
            activeBacktestTab = targetKey;
            panels.forEach((panel) => {
                const isActive = panel.dataset.backtestTabPanel === targetKey;
                panel.hidden = !isActive;
                panel.classList.toggle('is-active', isActive);
            });
            document.querySelectorAll('[data-backtest-tab]').forEach((button) => {
                const isActive = button.dataset.backtestTab === targetKey;
                button.classList.toggle('is-active', isActive);
                button.setAttribute('aria-selected', isActive ? 'true' : 'false');
            });
            resizeBacktestCharts();
        }

        function syncSymbolSourceUI() {
            const source = document.getElementById('symbolSource').value;
            const symbolsInput = document.getElementById('symbolsText');
            const help = document.getElementById('symbolsHelp');
            const supportsManualUniverse = source === 'manual' || source === 'daily_scan_replay';
            symbolsInput.disabled = !supportsManualUniverse;
            if (source === 'manual') {
                help.textContent = '手动模式下必填；其它模式会忽略这个输入。';
                symbolsInput.placeholder = 'AAPL,NVDA,MSFT';
            } else if (source === 'targets') {
                help.textContent = '当日回测会读取当前 active ibkr_targets；历史日期会自动回放 daily_scan_replay。';
                symbolsInput.placeholder = 'targets 模式会自动解析';
            } else if (source === 'daily_scan_replay') {
                help.textContent = '可选：填写后会作为历史盘前选股的底池；留空则按 watchlist 快照逐日回放。';
                symbolsInput.placeholder = '可选：限制历史盘前扫描底池';
            } else {
                help.textContent = '会读取 watchlist 股票池，并按 max_symbols 截断。';
                symbolsInput.placeholder = 'watchlist 模式会自动解析';
            }
        }

        function syncTvCompareUI() {
            const compareWithTv = Boolean(document.getElementById('compareWithTv')?.checked);
            const compareSignalsInput = document.getElementById('compareTvSignals');
            if (!compareSignalsInput) return;
            compareSignalsInput.disabled = !compareWithTv;
            if (!compareWithTv) {
                compareSignalsInput.checked = false;
            }
        }

        function buildHeroNotes() {
            const running = Boolean(activeStatus?.running);
            const activeRun = runList.find((item) => item.id === activeStatus?.run_id) || selectedRun;
            const activeBatch = batchList.find((item) => item.id === activeStatus?.batch_id) || selectedBatch;
            const notes = [
                {
                    label: 'Source Env',
                    value: `${getEnvironmentLabel(currentEnvironment)} · bars / targets / watchlist 均取自当前来源环境`,
                },
                {
                    label: 'Isolation',
                    value: '只写 backtest collections；运行时重新计算指标与信号，不回写实盘链路。',
                },
                {
                    label: 'Active Worker',
                    value: running
                        ? `${activeStatus.stage || 'running'} · ${activeStatus.message || 'backtest in progress'}`
                        : '当前没有运行中的 backtest job',
                },
                {
                    label: 'Active Batch',
                    value: activeBatch
                        ? `${activeBatch.name || activeBatch.id} · ${activeBatch.completed_count}/${activeBatch.variant_count} variants`
                        : '未选中 experiment 时，这里会展示参数扫描批次进度与最佳变体',
                },
                {
                    label: 'Selected Run',
                    value: activeRun
                        ? `${activeRun.name || activeRun.id} · ${formatRunDate(activeRun)}`
                        : '选中一个 run 后会显示收益曲线、成交明细和 replay timeline',
                },
            ];
            document.getElementById('heroNotes').innerHTML = notes.map((note) => `
                <div class="hero-note">
                    <div class="hero-note-label">${escapeHtml(note.label)}</div>
                    <div class="hero-note-value">${escapeHtml(note.value)}</div>
                </div>
            `).join('');
        }

        function renderStatusPanel() {
            const running = Boolean(activeStatus?.running);
            const lastStatus = activeStatus?.status || 'idle';
            const status = running ? lastStatus : 'idle';
            const progress = running ? Number(activeStatus?.progress || 0) : 0;
            document.getElementById('statusPill').innerHTML = detailStatusTag(status);
            document.getElementById('statusProgressBar').style.width = `${Math.max(0, Math.min(100, progress))}%`;
            document.getElementById('statusHeadline').textContent = running
                ? (activeStatus?.message || 'backtest running')
                : '当前没有运行中的 backtest job';
            document.getElementById('statusMeta').innerHTML = `
                <div>Stage: <span class="mono">${escapeHtml(running ? (activeStatus?.stage || 'running') : 'idle')}</span></div>
                <div>Progress: <span class="mono">${progress}%</span></div>
                <div>Run ID: <span class="mono">${escapeHtml(running ? (activeStatus?.run_id || '--') : '--')}</span></div>
                <div>Batch ID: <span class="mono">${escapeHtml(running ? (activeStatus?.batch_id || '--') : '--')}</span></div>
                <div>Updated: <span class="mono">${activeStatus?.updated_at_ms ? formatBarTimeMsToET(activeStatus.updated_at_ms) : '--'}</span></div>
            `;
            if (running) {
                const run = runList.find((item) => item.id === activeStatus.run_id);
                const batch = batchList.find((item) => item.id === activeStatus.batch_id);
                document.getElementById('statusNote').textContent = run
                    ? `正在运行：${run.name || run.id}\n日期：${formatRunDate(run)}\nSymbols：${run.symbols || '--'}${batch ? `\nExperiment：${batch.name || batch.id}` : ''}`
                    : `Worker 正在执行中，当前 stage=${activeStatus.stage || 'running'}。`;
            } else {
                const lastStateHint = ['completed', 'failed', 'cancelled'].includes(lastStatus)
                    ? `最近一次执行结果：${lastStatus} · ${activeStatus?.message || 'backtest finished'}`
                    : '暂无运行中的回测。你可以直接启动新的 run，或者点击左侧历史记录查看既有结果。';
                document.getElementById('statusNote').textContent = lastStateHint;
            }
        }

        function renderBatches() {
            document.getElementById('batchCountLabel').textContent = `${batchList.length} experiments`;
            if (!batchList.length) {
                document.getElementById('batchList').innerHTML = '<div class="empty-state">暂无参数扫描。<br>填入 Variants 后启动。</div>';
                return;
            }
            document.getElementById('batchList').innerHTML = batchList.map((batch) => `
                <div class="run-item ${batch.id === selectedBatchId ? 'active' : ''}" onclick="selectBatch('${escapeHtml(batch.id)}')">
                    <div class="run-item-top">
                        <div>
                            <div class="run-item-title">${escapeHtml(batch.name || batch.id)}</div>
                            <div class="run-item-copy mono">${escapeHtml(formatRunDate(batch))}</div>
                        </div>
                        ${detailStatusTag(batch.status)}
                    </div>
                    <div class="run-stats">
                        <span class="mini-chip">VAR ${batch.variant_count}</span>
                        <span class="mini-chip">DONE ${batch.completed_count}</span>
                        <span class="mini-chip">BEST ${formatPct(batch.best_total_return_pct)}</span>
                        <span class="mini-chip">SH ${formatNumber(batch.best_sharpe, 2)}</span>
                    </div>
                    <div class="run-item-bottom" style="margin-top: 10px;">
                        <div class="run-item-copy">${escapeHtml(summarizeRunSymbols(batch))}</div>
                        <div class="run-item-copy mono">${escapeHtml((batch.created || '--').slice(0, 19))}</div>
                    </div>
                </div>
            `).join('');
        }

        function renderBatchDetail() {
            if (!selectedBatch) {
                document.getElementById('batchDetail').innerHTML = '<div class="empty-state">选择 experiment 查看参数与排行。</div>';
                return;
            }
            const leaderboard = Array.isArray(selectedBatch.leaderboard) ? selectedBatch.leaderboard : [];
            const leaderboardPreview = getBacktestTablePreview('leaderboard', leaderboard);
            document.getElementById('batchDetail').innerHTML = `
                <div class="detail-card">
                    <div class="subhead">Experiment Meta</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Name</div><div class="detail-item-value">${escapeHtml(selectedBatch.name || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Batch ID</div><div class="detail-item-value mono">${escapeHtml(selectedBatch.id || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Progress</div><div class="detail-item-value">${escapeHtml(`${selectedBatch.completed_count}/${selectedBatch.variant_count}`)}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Best Variant</div><div class="detail-item-value">${escapeHtml(selectedBatch.best_variant_label || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Best Return</div><div class="detail-item-value ${classForValue(selectedBatch.best_total_return_pct)}">${escapeHtml(formatPct(selectedBatch.best_total_return_pct || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Best Sharpe</div><div class="detail-item-value ${classForValue(selectedBatch.best_sharpe)}">${escapeHtml(formatNumber(selectedBatch.best_sharpe, 2))}</div></div>
                    </div>
                </div>
                <div class="detail-card">
                    <div class="subhead">Variants JSON</div>
                    ${renderBacktestTextPreviewBox('batchVariants', getBatchVariantsText(selectedBatch), 'batchDetail')}
                </div>
                ${buildExperimentAnalysisCard(selectedBatch)}
                <div class="detail-card">
                    <div class="subhead">Leaderboard</div>
                    ${leaderboard.length ? `
                        ${renderBacktestTablePreviewBar('leaderboard', leaderboardPreview, '个变体')}
                        <div class="table-wrap">
                            <table class="data-table">
                                <thead>
                                    <tr>
                                        <th>#</th>
                                        <th>Variant</th>
                                        <th>Status</th>
                                        <th>Return</th>
                                        <th>Sharpe</th>
                                        <th>Trades</th>
                                        <th>Drawdown</th>
                                        <th>Run</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${leaderboardPreview.rows.map((item, index) => `
                                        <tr>
                                            <td class="mono">${index + 1}</td>
                                            <td>${escapeHtml(item.variant_label || item.name || '--')}</td>
                                            <td>${detailStatusTag(item.status || '--')}</td>
                                            <td class="${classForValue(item.total_return_pct)}">${escapeHtml(formatPct(item.total_return_pct || 0))}</td>
                                            <td class="${classForValue(item.sharpe)}">${escapeHtml(formatNumber(item.sharpe, 2))}</td>
                                            <td>${escapeHtml(String(item.trade_count || 0))}</td>
                                            <td class="negative">${escapeHtml(formatPct(-Math.abs(item.max_drawdown_pct || 0)))}</td>
                                            <td><button class="btn ghost" type="button" onclick="openRunFromBatch('${escapeHtml(item.run_id || '')}')">打开 Run</button></td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state">暂无 leaderboard。</div>'}
                </div>
            `;
        }

        function renderRuns() {
            document.getElementById('runCountLabel').textContent = `${runList.length} runs`;
            if (!runList.length) {
                document.getElementById('runList').innerHTML = '<div class="empty-state">暂无回测记录。<br>先跑一轮。</div>';
                return;
            }
            document.getElementById('runList').innerHTML = runList.map((run) => `
                <div class="run-item ${run.id === selectedRunId ? 'active' : ''}" onclick="selectRun('${escapeHtml(run.id)}')">
                    <div class="run-item-top">
                        <div>
                            <div class="run-item-title">${escapeHtml(run.name || run.id)}</div>
                            <div class="run-item-copy mono">${escapeHtml(formatRunDate(run))}</div>
                        </div>
                        ${detailStatusTag(run.status)}
                    </div>
                    <div class="run-stats">
                        <span class="mini-chip">${escapeHtml(run.source_environment.toUpperCase())}</span>
                        <span class="mini-chip">TRADES ${run.trade_count}</span>
                        <span class="mini-chip">RET ${formatPct(run.total_return_pct)}</span>
                        <span class="mini-chip">SHARPE ${formatNumber(run.sharpe, 2)}</span>
                    </div>
                    <div class="run-item-bottom" style="margin-top: 10px;">
                        <div class="run-item-copy">${escapeHtml(summarizeRunSymbols(run))}</div>
                        <div class="run-item-copy mono">${escapeHtml((run.created || '--').slice(0, 19))}</div>
                    </div>
                </div>
            `).join('');
        }

        function renderMetrics() {
            if (!selectedRun) {
                document.getElementById('metricsGrid').innerHTML = '<div class="empty-state">选择 run 查看收益与风险。</div>';
                document.getElementById('metricsMoreGrid').innerHTML = '';
                destroyEquityChart();
                return;
            }
            const metrics = selectedRun.metrics || {};
            const scanDiagnostics = metrics.daily_scan_match_diagnostics || selectedRun.extra?.daily_scan_match_diagnostics || {};
            const primaryCards = [
                ['Net PnL', formatMoney(selectedRun.net_pnl), classForValue(selectedRun.net_pnl), `${selectedRun.trade_count} trades`],
                ['Total Return', formatPct(selectedRun.total_return_pct), classForValue(selectedRun.total_return_pct), `ending ${formatMoney(metrics.ending_equity || 0)}`],
                ['Sharpe', formatNumber(selectedRun.sharpe, 2), classForValue(selectedRun.sharpe), `sortino ${formatNumber(metrics.sortino, 2)}`],
                ['Max Drawdown', formatPct(-Math.abs(selectedRun.max_drawdown_pct || 0)), 'negative', `profit factor ${formatNumber(metrics.profit_factor, 2)}`],
            ];
            const secondaryCards = [
                ['Win Rate', formatPct(selectedRun.win_rate), classForValue(selectedRun.win_rate - 50), `expectancy ${formatMoney(metrics.expectancy || 0)}`],
                ['Avg Win / Loss', `${formatMoney(metrics.avg_win || 0)} / ${formatMoney(metrics.avg_loss || 0)}`, '', `盈亏比 ${formatNumber(metrics.win_loss_ratio || 0, 2)}`],
                ['Signal Fill', formatPct(metrics.signal_fill_rate || 0), classForValue((metrics.signal_fill_rate || 0) - 50), `${metrics.executed_signal_count || 0}/${metrics.signal_count || 0} executed`],
                ['Portfolio Exposure', formatMoney(metrics.portfolio_max_gross_exposure || 0), '', `borrow max ${formatMoney(metrics.portfolio_max_borrowed_amount || 0)}`],
                ['Signal Rejects', String(Object.values(metrics.portfolio_rejection_counts || {}).reduce((sum, value) => sum + Number(value || 0), 0)), '', formatBreakdown(metrics.portfolio_rejection_counts || {})],
                ['Replay Targets', String(metrics.backtest_target_count || 0), '', `${metrics.historical_targeting?.target_date_count || 0} trade dates`],
                ['Reverse Actions', String(metrics.backtest_reverse_signal_count || 0), '', formatBreakdown(metrics.backtest_reverse_action_breakdown || {})],
            ];
            if (scanDiagnostics.enabled) {
                secondaryCards.splice(6, 0, [
                    'Scan Match',
                    formatPct(scanDiagnostics.selected_day_signal_rate_pct || 0),
                    classForValue((scanDiagnostics.selected_day_signal_rate_pct || 0) - 35),
                    `${scanDiagnostics.selected_day_signal_count || 0}/${scanDiagnostics.generated_signal_count || 0} signals`,
                ]);
            }
            const renderMetricCards = (cards) => cards.map(([label, value, tone, subtext]) => `
                <div class="metric-card">
                    <div class="metric-label">${escapeHtml(label)}</div>
                    <div class="metric-value ${tone}">${escapeHtml(value)}</div>
                    <div class="metric-subtext">${escapeHtml(subtext)}</div>
                </div>
            `).join('');
            document.getElementById('metricsGrid').innerHTML = renderMetricCards(primaryCards);
            document.getElementById('metricsMoreGrid').innerHTML = renderMetricCards(secondaryCards);
            renderEquityChart(selectedRun);
        }

        function destroyEquityChart() {
            if (equityChart) {
                equityChart.destroy();
                equityChart = null;
            }
        }

        function renderEquityChart(run) {
            destroyEquityChart();
            const extra = run.extra || {};
            const equityCurve = Array.isArray(extra.equity_curve) ? extra.equity_curve : [];
            const benchmarkCurve = Array.isArray(extra.benchmark_curve) ? extra.benchmark_curve : [];
            const labels = Array.from(new Set([...equityCurve.map((item) => item.date), ...benchmarkCurve.map((item) => item.date)])).sort();
            const equityMap = new Map(equityCurve.map((item) => [item.date, Number(item.equity || 0)]));
            const benchmarkMap = new Map(benchmarkCurve.map((item) => [item.date, Number(item.equity || 0)]));
            const ctx = document.getElementById('equityChart').getContext('2d');
            if (!labels.length) {
                ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
                ctx.font = '12px JetBrains Mono';
                ctx.fillStyle = '#8BA4C4';
                ctx.fillText('No equity curve yet for this run', 16, 28);
                return;
            }
            equityChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Strategy Equity',
                            data: labels.map((label) => equityMap.get(label) ?? null),
                            borderColor: '#F6AD55',
                            backgroundColor: 'rgba(246,173,85,0.16)',
                            borderWidth: 2.2,
                            pointRadius: 0,
                            tension: 0.24,
                        },
                        {
                            label: `${run.benchmark_symbol || 'SPY'} Benchmark`,
                            data: labels.map((label) => benchmarkMap.get(label) ?? null),
                            borderColor: '#63B3ED',
                            backgroundColor: 'rgba(99,179,237,0.12)',
                            borderDash: [6, 4],
                            borderWidth: 1.8,
                            pointRadius: 0,
                            tension: 0.18,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { labels: { color: '#E2EAF4' } },
                    },
                    scales: {
                        x: {
                            ticks: { color: '#8BA4C4', maxTicksLimit: 8 },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                        y: {
                            ticks: { color: '#8BA4C4' },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                    },
                },
            });
        }

        function renderRunDetail() {
            if (!selectedRun) {
                document.getElementById('selectedRunPill').innerHTML = '';
                document.getElementById('runDetail').innerHTML = '<div class="empty-state">选择 run 查看详情。</div>';
                return;
            }
            document.getElementById('selectedRunPill').innerHTML = detailStatusTag(selectedRun.status);
            const metrics = selectedRun.metrics || {};
            const extra = selectedRun.extra || {};
            const portfolioRisk = metrics.portfolio_risk || extra.portfolio_risk || {};
            const monthlyReturns = Array.isArray(metrics.monthly_returns) ? metrics.monthly_returns : [];
            const qualityRows = Array.isArray(metrics.data_quality) ? metrics.data_quality : [];
            const skipped = Array.isArray(metrics.skipped_symbols) ? metrics.skipped_symbols : [];
            const detailHtml = `
                <div class="detail-card">
                    <div class="subhead">Meta</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Name</div><div class="detail-item-value">${escapeHtml(selectedRun.name || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Run ID</div><div class="detail-item-value mono">${escapeHtml(selectedRun.id || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Date Range</div><div class="detail-item-value">${escapeHtml(formatRunDate(selectedRun))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Symbols</div><div class="detail-item-value">${escapeHtml(selectedRun.symbols || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Symbol Source</div><div class="detail-item-value">${escapeHtml(selectedRun.symbol_source || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Source Environment</div><div class="detail-item-value">${escapeHtml((selectedRun.source_environment || '--').toUpperCase())}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Session</div><div class="detail-item-value">${escapeHtml(selectedRun.session_mode || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Warmup</div><div class="detail-item-value">${escapeHtml(String(extra.warmup_bars || metrics.warmup_bars || '--'))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scan Cutoff</div><div class="detail-item-value">${escapeHtml(extra.premarket_cutoff_time || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Capital</div><div class="detail-item-value">${escapeHtml(formatMoney(selectedRun.initial_capital || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Execution</div><div class="detail-item-value">${escapeHtml(extra.execution_model || metrics.execution_model || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Borrow Mode</div><div class="detail-item-value">${escapeHtml(portfolioRisk.borrow_limit_mode || extra.borrow_limit_mode || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Borrow Limit</div><div class="detail-item-value">${escapeHtml(formatMoney(portfolioRisk.max_borrow_amount || extra.max_borrow_amount || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Buying Power</div><div class="detail-item-value">${escapeHtml(formatMoney(portfolioRisk.total_exposure_limit || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Validity</div><div class="detail-item-value">${escapeHtml(String(portfolioRisk.signal_validity_minutes || extra.signal_validity_minutes || '--'))}m</div></div>
                        <div class="detail-item"><div class="detail-item-label">Order Cut</div><div class="detail-item-value">${escapeHtml(portfolioRisk.order_window_end_time || extra.order_window_end_time || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Runtime</div><div class="detail-item-value">${escapeHtml(String(selectedRun.duration_s || 0))}s</div></div>
                        <div class="detail-item"><div class="detail-item-label">Started</div><div class="detail-item-value mono">${escapeHtml(selectedRun.started_at || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Finished</div><div class="detail-item-value mono">${escapeHtml(selectedRun.finished_at || '--')}</div></div>
                    </div>
                </div>
                <div class="detail-card">
                    <div class="subhead">Strategy Params</div>
                    ${renderBacktestTextPreviewBox('strategyParams', getStrategyParamsText(selectedRun), 'runDetail')}
                </div>
                <div class="detail-card">
                    <div class="subhead">Data Quality</div>
                    ${qualityRows.length ? `
                        <div class="quality-grid">
                            ${qualityRows.map((item) => {
                                const gapCount = Number(item.gap_count || 0);
                                const tone = item.status === 'ok' && gapCount === 0 ? 'good' : (item.status === 'insufficient_data' ? 'bad' : 'warn');
                                return `
                                    <div class="quality-card ${tone}">
                                        <div class="quality-title">${escapeHtml(item.symbol || '--')}</div>
                                        <div class="quality-copy">status=${escapeHtml(item.status || '--')} · bars=${escapeHtml(String(item.bar_count || 0))} · gaps=${escapeHtml(String(gapCount))}</div>
                                        <div class="quality-copy">${escapeHtml(item.first_bar_us || '--')} → ${escapeHtml(item.last_bar_us || '--')}</div>
                                    </div>
                                `;
                            }).join('')}
                        </div>
                    ` : '<div class="empty-state">暂无数据质量汇总。</div>'}
                    ${skipped.length ? `<div class="foot-note">Skipped: ${escapeHtml(skipped.join(', '))}</div>` : ''}
                </div>
                <div class="detail-card">
                    <div class="subhead">Monthly Returns</div>
                    ${monthlyReturns.length ? `
                        <div class="table-wrap">
                            <table class="data-table" style="min-width: 420px;">
                                <thead><tr><th>Month</th><th>Return</th></tr></thead>
                                <tbody>
                                    ${monthlyReturns.map((item) => `
                                        <tr>
                                            <td class="mono">${escapeHtml(item.month || '--')}</td>
                                            <td class="${classForValue(item.return_pct)}">${escapeHtml(formatPct(item.return_pct || 0))}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state">暂无月度权益。</div>'}
                </div>
                <div class="detail-card">
                    <div class="subhead">Error / Notes</div>
                    <div class="note-box">${escapeHtml(selectedRun.error || 'No error. This run completed without runtime exceptions.')}</div>
                </div>
                ${buildRunAnalysisCard(selectedRun)}
                ${buildBacktestIndicatorCaptureCard(selectedRun)}
                ${buildBacktestTargetReplayCard(selectedRun)}
                ${buildBacktestReverseCaptureCard(selectedRun)}
                ${buildTvParityCard(selectedRun)}
                <div class="detail-card">
                    <div class="subhead">Runtime Extra</div>
                    ${renderBacktestTextPreviewBox('runtimeExtra', JSON.stringify({
                        strategy_tag: extra.strategy_tag || '',
                        resolved_symbols: extra.resolved_symbols || [],
                        force_flat_eod: extra.force_flat_eod,
                        backtest_indicator_capture: extra.backtest_indicator_capture || {},
                        backtest_signal_capture: extra.backtest_signal_capture || {},
                        backtest_target_capture: extra.backtest_target_capture || {},
                        backtest_reverse_capture: extra.backtest_reverse_capture || {},
                        historical_targeting: extra.historical_targeting || {},
                        daily_scan_match_diagnostics: metrics.daily_scan_match_diagnostics || extra.daily_scan_match_diagnostics || {},
                        portfolio_risk: metrics.portfolio_risk || extra.portfolio_risk || {},
                        portfolio_rejection_counts: metrics.portfolio_rejection_counts || extra.portfolio_rejection_counts || {},
                        portfolio_candidate_samples: metrics.portfolio_candidate_samples || [],
                        analysis_report: extra.analysis_report || {},
                    }, null, 2), 'runDetail')}
                </div>
            `;
            document.getElementById('runDetail').innerHTML = detailHtml;
        }

        function buildReplaySymbolOptions() {
            const select = document.getElementById('replaySymbol');
            const symbols = new Set();
            if (selectedRun?.symbols) {
                selectedRun.symbols.split(',').map((item) => item.trim()).filter(Boolean).forEach((symbol) => symbols.add(symbol));
            }
            selectedTrades.forEach((trade) => {
                if (trade.symbol) symbols.add(String(trade.symbol).trim().toUpperCase());
            });
            selectedTargets.forEach((item) => {
                if (item.symbol) symbols.add(String(item.symbol).trim().toUpperCase());
            });
            const items = Array.from(symbols).sort();
            if (!items.length) {
                select.innerHTML = '<option value="">No symbol</option>';
                return;
            }
            const current = select.value && items.includes(select.value) ? select.value : items[0];
            select.innerHTML = items.map((symbol) => `<option value="${escapeHtml(symbol)}" ${symbol === current ? 'selected' : ''}>${escapeHtml(symbol)}</option>`).join('');
        }

        function renderTrades() {
            document.getElementById('tradeCountLabel').textContent = `${selectedTrades.length} trades`;
            buildReplaySymbolOptions();
            if (!selectedRun) {
                document.getElementById('tradesPanel').innerHTML = '<div class="empty-state">先选一个 run，才会加载对应交易明细。</div>';
                return;
            }
            if (!selectedTrades.length) {
                document.getElementById('tradesPanel').innerHTML = '<div class="empty-state">暂无成交记录。</div>';
                return;
            }
            const tradePreview = getBacktestTablePreview('trades', selectedTrades);
            document.getElementById('tradesPanel').innerHTML = `
                ${renderBacktestTablePreviewBar('trades', tradePreview, '笔交易')}
                <div class="table-wrap">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Symbol</th>
                                <th>Direction</th>
                                <th>Entry</th>
                                <th>Exit</th>
                                <th>PnL</th>
                                <th>Bars</th>
                                <th>Reason</th>
                                <th>Replay</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${tradePreview.rows.map((trade) => `
                                <tr>
                                    <td class="mono">${trade.trade_index}</td>
                                    <td>${escapeHtml(trade.symbol || '--')}</td>
                                    <td><span class="tag ${trade.direction === 'short' ? 'short' : 'long'}">${escapeHtml((trade.direction || '--').toUpperCase())}</span></td>
                                    <td class="mono">${escapeHtml((trade.entry_us_time || '--').slice(0, 16))}<br>${escapeHtml(formatMoney(trade.entry_price))}</td>
                                    <td class="mono">${escapeHtml((trade.exit_us_time || '--').slice(0, 16))}<br>${escapeHtml(formatMoney(trade.exit_price))}</td>
                                    <td class="${classForValue(trade.pnl)}">${escapeHtml(formatMoney(trade.pnl))}<br>${escapeHtml(formatPct(trade.pnl_pct))}</td>
                                    <td>${escapeHtml(String(trade.bars_held || 0))}</td>
                                    <td>${escapeHtml(trade.exit_reason || '--')}</td>
                                    <td><button class="btn ghost" type="button" onclick="replayTrade('${escapeHtml(trade.symbol || '')}', ${Number(trade.entry_bar_ms || 0)})">回放</button></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;
        }

        function destroyReplayChart() {
            if (replayChart) {
                replayChart.destroy();
                replayChart = null;
            }
        }

        function renderReplay(rows) {
            destroyReplayChart();
            const panel = document.getElementById('replayPanel');
            if (!rows.length) {
                panel.innerHTML = '<div class="empty-state">选择 symbol 后 Replay。</div>';
                const ctx = document.getElementById('replayChart').getContext('2d');
                ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
                ctx.font = '12px JetBrains Mono';
                ctx.fillStyle = '#8BA4C4';
                ctx.fillText('No replay loaded', 16, 28);
                return;
            }
            const replayPreview = getBacktestTablePreview('replay', rows);
            const labels = rows.map((row) => (row.us_time || '').slice(11, 16));
            const closes = rows.map((row) => Number(row.close || 0));
            const signalPoints = rows.map((row) => row.signal ? Number(row.close || 0) : null);
            const ctx = document.getElementById('replayChart').getContext('2d');
            replayChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Close',
                            data: closes,
                            borderColor: '#63B3ED',
                            backgroundColor: 'rgba(99,179,237,0.14)',
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.16,
                        },
                        {
                            type: 'scatter',
                            label: 'Signal Bar',
                            data: signalPoints,
                            borderColor: '#F6AD55',
                            backgroundColor: '#F6AD55',
                            pointRadius: 4,
                            pointHoverRadius: 5,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { labels: { color: '#E2EAF4' } },
                    },
                    scales: {
                        x: {
                            ticks: { color: '#8BA4C4', maxTicksLimit: 10 },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                        y: {
                            ticks: { color: '#8BA4C4' },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                    },
                },
            });
            panel.innerHTML = `
                ${renderBacktestTablePreviewBar('replay', replayPreview, '根K线')}
                <div class="table-wrap">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>US Time</th>
                                <th>Session</th>
                                <th>OHLC</th>
                                <th>ATR</th>
                                <th>SD Zone</th>
                                <th>DTP</th>
                                <th>Signal</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${replayPreview.rows.map((row) => {
                                const signal = row.signal || null;
                                return `
                                    <tr>
                                        <td class="mono">${escapeHtml((row.us_time || '--').slice(0, 16))}</td>
                                        <td>${escapeHtml(row.session_type || '--')}</td>
                                        <td class="mono">O ${formatNumber(row.open, 2)} / H ${formatNumber(row.high, 2)}<br>L ${formatNumber(row.low, 2)} / C ${formatNumber(row.close, 2)}</td>
                                        <td>${escapeHtml(formatNumber(row.atr, 3))}</td>
                                        <td>${escapeHtml(row.sd_zone || '--')}<br><span class="mono">trend ${escapeHtml(String(row.sd_trend ?? '--'))}</span></td>
                                        <td>${escapeHtml(row.dtp_phase || '--')}</td>
                                        <td>${signal ? `<span class="tag signal">${escapeHtml((signal.signal || '--').toUpperCase())}</span><br>${escapeHtml(signal.direction || '--')} · ${escapeHtml(signal.reason || '')}` : '--'}</td>
                                    </tr>
                                `;
                            }).join('')}
                        </tbody>
                    </table>
                </div>
            `;
        }

        async function loadBacktestStatus() {
            activeStatus = await requestBacktestJson(`/api/custom/ibkr/backtest/status?environment=${encodeURIComponent(currentEnvironment)}`);
            renderStatusPanel();
        }

        async function loadBatches(preserveSelection = true) {
            const filter = `source_environment = "${escapeFilterValue(currentEnvironment)}"`;
            const payload = await apiFetch('ibkr_backtest_batches', {
                filter,
                sort: '-created',
                perPage: 30,
                page: 1,
            });
            batchList = toItems(payload).map(normalizeBatchRecord);
            if (!preserveSelection || !batchList.some((batch) => batch.id === selectedBatchId)) {
                selectedBatchId = batchList[0]?.id || '';
            }
            renderBatches();
            buildHeroNotes();
        }

        async function loadRuns(preserveSelection = true) {
            const filter = `source_environment = "${escapeFilterValue(currentEnvironment)}"`;
            const payload = await apiFetch('ibkr_backtest_runs', {
                filter,
                sort: '-created',
                perPage: 40,
                page: 1,
            });
            runList = toItems(payload).map(normalizeRunRecord);
            if (!preserveSelection || !runList.some((run) => run.id === selectedRunId)) {
                selectedRunId = runList[0]?.id || '';
            }
            renderRuns();
            buildHeroNotes();
        }

        async function refreshSelectedBatch(showToastOnSuccess = false) {
            if (!selectedBatchId) {
                selectedBatch = null;
                renderBatchDetail();
                return;
            }
            selectedBatch = batchList.find((batch) => batch.id === selectedBatchId) || null;
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
            selectedTargetsLoading = Boolean(selectedRun);
            renderMetrics();
            renderRunDetail();
            await Promise.all([
                loadRunTrades(selectedRunId),
                loadRunTargets(selectedRunId),
            ]);
            if (showToastOnSuccess && selectedRun) {
                showToast(`已刷新 ${selectedRun.name || selectedRun.id}`);
            }
        }

        async function refreshDashboard(showToastOnSuccess = false) {
            if (!initAuth()) return;
            try {
                await Promise.all([
                    loadBacktestStatus(),
                    loadBatches(true),
                    loadRuns(true),
                ]);
                await refreshSelectedBatch(false);
                await refreshSelectedRun(false);
                setPageContextMeta([
                    { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
                    { label: '批次', value: selectedBatchId || '未选择' },
                    { label: 'Run', value: selectedRunId || '未选择' },
                ]);
                setPageRefreshTime();
                if (showToastOnSuccess) showToast('回测面板已刷新');
            } catch (error) {
                console.error('refreshDashboard failed:', error);
                showToast(`刷新失败: ${error.message || error}`);
            }
        }

        async function handleStartBacktest() {
            if (actionPending) return;
            if (!initAuth()) return;
            const symbolSource = document.getElementById('symbolSource').value;
            const symbolsText = String(document.getElementById('symbolsText').value || '').trim();
            const dateFrom = document.getElementById('dateFrom').value;
            const dateTo = document.getElementById('dateTo').value;
            const strategyParamsText = String(document.getElementById('strategyParams').value || '').trim();
            const variantsText = String(document.getElementById('variantsJson').value || '').trim();
            let strategyParams = {};
            let variants = [];
            if (symbolSource === 'manual' && !symbolsText) {
                showToast('manual 模式需要填写 symbols');
                return;
            }
            if (!dateFrom || !dateTo) {
                showToast('请选择完整的回测日期范围');
                return;
            }
            if (dateFrom > dateTo) {
                showToast('Date From 不能大于 Date To');
                return;
            }
            if (strategyParamsText) {
                try {
                    strategyParams = JSON.parse(strategyParamsText);
                } catch (_) {
                    showToast('Strategy Params JSON 解析失败');
                    return;
                }
            }
            if (variantsText) {
                try {
                    variants = JSON.parse(variantsText);
                } catch (_) {
                    showToast('Variants JSON Array 解析失败');
                    return;
                }
                if (!Array.isArray(variants) || !variants.length) {
                    showToast('Variants JSON Array 需要是非空数组');
                    return;
                }
            }
            const payload = {
                environment: currentEnvironment,
                name: String(document.getElementById('runName').value || '').trim(),
                symbol_source: symbolSource,
                symbols: symbolsText,
                date_from: dateFrom,
                date_to: dateTo,
                session_mode: document.getElementById('sessionMode').value,
                benchmark_symbol: String(document.getElementById('benchmarkSymbol').value || '').trim().toUpperCase(),
                initial_capital: Number(document.getElementById('initialCapital').value || 10000),
                execution_model: document.getElementById('executionModel').value,
                borrow_limit_mode: document.getElementById('borrowLimitMode').value,
                max_borrow_amount: Number(document.getElementById('maxBorrowAmount').value || 0),
                position_limit_max: Number(document.getElementById('positionLimitMax').value || 3),
                signal_validity_minutes: Number(document.getElementById('signalValidityMinutes').value || 30),
                trade_window_start_time: String(document.getElementById('tradeWindowStart').value || '09:35').trim(),
                trade_window_end_time: String(document.getElementById('tradeWindowEnd').value || '15:30').trim(),
                order_window_end_time: String(document.getElementById('orderWindowEnd').value || '15:00').trim(),
                simultaneous_signal_priority: document.getElementById('signalPriority').value,
                manual_confirm_mode: 'auto',
                confirm_delay_minutes: 0,
                commission_per_share: Number(document.getElementById('commissionPerShare').value || 0.005),
                slippage_bps: Number(document.getElementById('slippageBps').value || 2),
                warmup_bars: Number(document.getElementById('warmupBars').value || 320),
                scan_warmup_bars: Number(document.getElementById('warmupBars').value || 320),
                premarket_cutoff_time: String(document.getElementById('premarketCutoff').value || '09:20').trim(),
                compare_with_tv: Boolean(document.getElementById('compareWithTv')?.checked),
                compare_tv_signals: Boolean(document.getElementById('compareWithTv')?.checked && document.getElementById('compareTvSignals')?.checked),
                persist_backtest_indicators: Boolean(document.getElementById('persistBacktestIndicators')?.checked),
                max_symbols: Number(document.getElementById('maxSymbols').value || 12),
                source_environment: currentEnvironment,
                strategy_params: strategyParams,
                variants,
            };
            setActionState(true);
            try {
                const result = await requestBacktestJson('/api/custom/ibkr/backtest/run', {
                    method: 'POST',
                    body: payload,
                });
                selectedRunId = result.run_id || selectedRunId;
                selectedBatchId = result.batch_id || selectedBatchId;
                document.getElementById('runName').value = '';
                if (result.batch_id) {
                    document.getElementById('variantsJson').value = '';
                }
                showToast(`回测已启动: ${result.run_id || '--'}`);
                await refreshDashboard(false);
            } catch (error) {
                showToast(`启动失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function cancelActiveRun() {
            if (actionPending) return;
            if (!activeStatus?.run_id) {
                showToast('当前没有运行中的回测');
                return;
            }
            if (!window.confirm(`确认停止运行中的回测 ${activeStatus.run_id} 吗？`)) return;
            setActionState(true);
            try {
                await requestBacktestJson('/api/custom/ibkr/backtest/cancel', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        run_id: activeStatus.run_id,
                    },
                });
                showToast('已发送停止请求');
                await refreshDashboard(false);
            } catch (error) {
                showToast(`停止失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function cleanupSelectedRun() {
            if (actionPending) return;
            if (!selectedRunId) {
                showToast('先选择一个 run');
                return;
            }
            if (!window.confirm(`确认清理回测 ${selectedRunId} 吗？这会删除 run 和所有关联 backtest rows。`)) return;
            setActionState(true);
            try {
                await requestBacktestJson('/api/custom/ibkr/backtest/cleanup', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        run_id: selectedRunId,
                    },
                });
                showToast('回测结果已清理');
                selectedRunId = '';
                selectedRun = null;
                selectedTrades = [];
                selectedTargets = [];
                selectedTargetsLoading = false;
                renderReplay([]);
                await refreshDashboard(false);
            } catch (error) {
                showToast(`清理失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function cleanupSelectedBatch() {
            if (actionPending) return;
            if (!selectedBatchId) {
                showToast('先选择一个 experiment');
                return;
            }
            if (!window.confirm(`确认清理 experiment ${selectedBatchId} 吗？这会删除该批次下所有 run 和关联 backtest rows。`)) return;
            setActionState(true);
            try {
                await requestBacktestJson('/api/custom/ibkr/backtest/cleanup', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        batch_id: selectedBatchId,
                    },
                });
                showToast('Experiment 已清理');
                if (selectedRun?.extra?.batch_id === selectedBatchId) {
                    selectedRunId = '';
                    selectedRun = null;
                    selectedTrades = [];
                    selectedTargets = [];
                    selectedTargetsLoading = false;
                    renderReplay([]);
                }
                selectedBatchId = '';
                selectedBatch = null;
                await refreshDashboard(false);
            } catch (error) {
                showToast(`清理 experiment 失败: ${error.message || error}`);
            } finally {
                setActionState(false);
            }
        }

        async function selectBatch(batchId) {
            setBacktestTab('experiments');
            selectedBatchId = batchId;
            selectedBatch = batchList.find((batch) => batch.id === batchId) || null;
            backtestTableExpandedState.leaderboard = false;
            backtestTextExpandedState.batchVariants = false;
            renderBatches();
            renderBatchDetail();
        }

        async function openRunFromBatch(runId) {
            if (!runId) return;
            setBacktestTab('runs');
            await selectRun(runId);
        }

        async function selectRun(runId) {
            selectedRunId = runId;
            selectedRun = runList.find((run) => run.id === runId) || null;
            selectedTargets = [];
            selectedTargetsLoading = Boolean(selectedRun);
            backtestTextExpandedState.strategyParams = false;
            backtestTextExpandedState.runtimeExtra = false;
            renderRuns();
            renderMetrics();
            renderRunDetail();
            await Promise.all([
                loadRunTrades(runId),
                loadRunTargets(runId, { showToastOnError: true }),
            ]);
            renderReplay([]);
            document.getElementById('replayCenterBar').value = '';
        }

        async function replayTarget(symbol, barTimeMs) {
            setBacktestTab('trades');
            buildReplaySymbolOptions();
            document.getElementById('replaySymbol').value = symbol || document.getElementById('replaySymbol').value;
            document.getElementById('replayCenterBar').value = String(barTimeMs || '');
            await loadReplayForSelection();
        }

        async function loadReplayForSelection() {
            if (!selectedRunId) {
                showToast('先选择一个 run');
                return;
            }
            const symbol = String(document.getElementById('replaySymbol').value || '').trim();
            if (!symbol) {
                showToast('请选择一个 symbol');
                return;
            }
            const centerBarMs = Number(document.getElementById('replayCenterBar').value || 0);
            const windowSize = Number(document.getElementById('replayWindow').value || 80);
            try {
                const payload = await requestBacktestJson(`/api/custom/ibkr/backtest/replay?environment=${encodeURIComponent(currentEnvironment)}&run_id=${encodeURIComponent(selectedRunId)}&symbol=${encodeURIComponent(symbol)}&center_bar_ms=${encodeURIComponent(centerBarMs || 0)}&window=${encodeURIComponent(windowSize || 80)}`);
                selectedReplayRows = Array.isArray(payload.rows) ? payload.rows : [];
                backtestTableExpandedState.replay = false;
                renderReplay(selectedReplayRows);
            } catch (error) {
                showToast(`Replay 失败: ${error.message || error}`);
            }
        }

        function toggleBacktestTableExpansion(key) {
            if (!Object.prototype.hasOwnProperty.call(backtestTableExpandedState, key)) return;
            backtestTableExpandedState[key] = !backtestTableExpandedState[key];
            if (key === 'leaderboard') {
                renderBatchDetail();
                return;
            }
            if (key === 'trades') {
                renderTrades();
                return;
            }
            if (key === 'replay') {
                renderReplay(selectedReplayRows);
            }
        }

        function toggleBacktestTextExpansion(key, section) {
            if (!Object.prototype.hasOwnProperty.call(backtestTextExpandedState, key)) return;
            backtestTextExpandedState[key] = !backtestTextExpandedState[key];
            if (section === 'batchDetail') {
                renderBatchDetail();
                return;
            }
            if (section === 'runDetail') {
                renderRunDetail();
            }
        }

        async function replayTrade(symbol, entryBarMs) {
            setBacktestTab('trades');
            document.getElementById('replayCenterBar').value = String(entryBarMs || '');
            buildReplaySymbolOptions();
            document.getElementById('replaySymbol').value = symbol || document.getElementById('replaySymbol').value;
            await loadReplayForSelection();
        }

        function applyDefaultDates() {
            const today = new Date();
            const from = new Date(today);
            from.setDate(today.getDate() - 14);
            const toIso = (date) => date.toISOString().slice(0, 10);
            document.getElementById('dateFrom').value = toIso(from);
            document.getElementById('dateTo').value = toIso(today);
        }

        window.handleStartBacktest = handleStartBacktest;
        window.cancelActiveRun = cancelActiveRun;
        window.cleanupSelectedRun = cleanupSelectedRun;
        window.cleanupSelectedBatch = cleanupSelectedBatch;
        window.selectBatch = selectBatch;
        window.openRunFromBatch = openRunFromBatch;
        window.selectRun = selectRun;
        window.toggleBacktestTableExpansion = toggleBacktestTableExpansion;
        window.toggleBacktestTextExpansion = toggleBacktestTextExpansion;
        window.refreshDashboard = refreshDashboard;
        window.refreshSelectedRun = refreshSelectedRun;
        window.refreshSelectedBatch = refreshSelectedBatch;
        window.setBacktestTab = setBacktestTab;
        window.loadReplayForSelection = loadReplayForSelection;
        window.replayTrade = replayTrade;
        window.replayTarget = replayTarget;
        window.syncSymbolSourceUI = syncSymbolSourceUI;
        window.syncTvCompareUI = syncTvCompareUI;
        window.onEnvironmentChange = function(environment) {
            currentEnvironment = environment;
            window.location.href = buildPageUrl('/ibkr_backtests.html', {}, { environment: currentEnvironment });
        };

        document.addEventListener('DOMContentLoaded', async () => {
            if (!initAuth()) return;
            document.getElementById('nav').innerHTML = renderNav('/ibkr_backtests.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('🧪 IBKR 回测工坊', {
                subtitle: '结果 / 参数 / replay',
            });
            document.getElementById('pageBridge').innerHTML = renderBacktestsBridge('/ibkr_backtests.html');
            document.getElementById('overviewChartToggle')?.addEventListener('toggle', resizeBacktestCharts);
            applyDefaultDates();
            syncSymbolSourceUI();
            syncTvCompareUI();
            setBacktestTab(activeBacktestTab);
            renderReplay([]);
            await withPageLoading(
                () => refreshDashboard(false),
                {
                    title: '回测页加载中',
                    copy: '正在同步回测数据。',
                }
            );
            refreshTimer = setInterval(() => refreshDashboard(false), 10000);
        });
