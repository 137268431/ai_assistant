        function toIsoDate(date) {
            return date.toISOString().slice(0, 10);
        }

        function getLatestCompleteBacktestDate() {
            const now = new Date();
            const etFormatter = new Intl.DateTimeFormat('en-CA', {
                timeZone: 'America/New_York',
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
            });
            const parts = Object.fromEntries(
                etFormatter.formatToParts(now)
                    .filter((part) => part.type !== 'literal')
                    .map((part) => [part.type, part.value])
            );
            const todayEt = new Date(Date.UTC(
                Number(parts.year),
                Number(parts.month) - 1,
                Number(parts.day),
                12,
                0,
                0
            ));
            todayEt.setUTCDate(todayEt.getUTCDate() - 1);
            return toIsoDate(todayEt);
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
