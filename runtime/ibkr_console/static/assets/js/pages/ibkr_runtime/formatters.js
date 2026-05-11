        function toArray(payload) {
            return Array.isArray(payload?.items) ? payload.items : [];
        }

        function getTotalItems(payload, fallback = 0) {
            const total = Number(payload?.totalItems);
            if (Number.isFinite(total)) return total;
            if (Array.isArray(payload?.items)) return payload.items.length;
            return Number(fallback || 0) || 0;
        }

        function normalizeManualAuthReason(value) {
            const text = String(value || '').trim().toLowerCase();
            if (!text) return 'manual_reauth';
            if (text === 'startup') return 'manual_start';
            return text;
        }

        function getManualAuthReasonLabel(reason) {
            return MANUAL_AUTH_REASON_LABELS[normalizeManualAuthReason(reason)] || '手动验证';
        }

        function getRuntimeActionLabel(action, fallback = '') {
            const key = String(action || '').trim().toLowerCase();
            return RUNTIME_ACTION_LABELS[key] || String(fallback || key || '操作').trim();
        }

        function formatRuntimePendingLabel(action, fallback = '') {
            return `执行中：${getRuntimeActionLabel(action, fallback)}`;
        }

        function statusClass(value) {
            const text = String(value || '').trim().toLowerCase();
            if (!text) return 'pill-neutral';
            if (['ready', 'open', 'success', 'recovered', 'authenticated'].includes(text)) return text === 'success' ? 'pill-success' : 'pill-ok';
            if (['warming', 'blocked', 'degraded', 'closed', 'panic_resetting', 'manual_takeover', 'received', 'no_active_targets'].includes(text)) return 'pill-warning';
            if (['stopped'].includes(text)) return 'pill-error';
            if (['running', 'ok', 'online', 'filled', 'long', 'buy', 'active', 'executed', 'protected_active'].includes(text)) return `pill-${text}`;
            if (['warning', 'delayed', 'pending', 'requested', 'triggered', 'awaiting_confirm', 'waiting_confirm', 'waiting_response', 'submitted', 'protection_incomplete', 'timeout'].includes(text)) return `pill-${text}`;
            if (['error', 'offline', 'cancelled', 'rejected', 'gateway_rejected', 'submit_failed', 'short', 'sell', 'failed'].includes(text)) return `pill-${text}`;
            if (['expired', 'init', 'candidate'].includes(text)) return 'pill-neutral';
            return 'pill-neutral';
        }

        function chipTone(value) {
            const text = String(value || '').trim().toLowerCase();
            if (['ready', 'open', 'recovered', 'authenticated'].includes(text)) return 'chip-ok';
            if (['protected_active'].includes(text)) return 'chip-ok';
            if (['warming', 'blocked', 'degraded', 'closed', 'panic_resetting', 'manual_takeover', 'received', 'submitted', 'protection_incomplete', 'no_active_targets'].includes(text)) return 'chip-warn';
            if (['stopped'].includes(text)) return 'chip-error';
            if (['running', 'ok', 'online', 'success'].includes(text)) return 'chip-ok';
            if (['warning', 'delayed', 'pending', 'requested', 'triggered', 'awaiting_confirm', 'waiting_confirm', 'waiting_response', 'timeout'].includes(text)) return 'chip-warn';
            if (['error', 'offline', 'failed', 'rejected', 'gateway_rejected', 'submit_failed'].includes(text)) return 'chip-error';
            return 'chip-muted';
        }

        function formatRuntimeSignalStatus(value) {
            const key = String(value || '').trim().toLowerCase();
            const labels = {
                awaiting_confirm: '待确认',
                pending: '等待执行',
                submitted: '已提交',
                protected_active: '保护中',
                protection_incomplete: '保护不完整',
                executed: '已执行',
                closed: '已平仓',
                expired: '已过期',
                rejected: '已拒绝',
            };
            return labels[key] || (key ? key.replace(/_/g, ' ') : '--');
        }

        function formatTradeUniverseStatus(value) {
            const key = String(value || '').trim().toLowerCase();
            const labels = {
                active: 'ACTIVE',
                ready: 'READY',
                empty_watchlist: 'EMPTY WATCHLIST',
                no_active_targets: 'NO ACTIVE TARGETS',
                unknown: 'UNKNOWN',
            };
            return labels[key] || (key ? key.replace(/_/g, ' ').toUpperCase() : '--');
        }

        function normalizeCode(value) {
            return String(value || '').replace(/[^0-9A-Za-z]/g, '').toUpperCase();
        }

        function formatMoney(value) {
            const number = Number(value || 0);
            if (!Number.isFinite(number)) return '--';
            return `$${number.toFixed(2)}`;
        }

        function formatCompactNumber(value) {
            const number = Number(value || 0);
            if (!Number.isFinite(number)) return '0';
            return Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(number);
        }

        function formatAgo(isoValue) {
            if (!isoValue) return '--';
            const date = new Date(isoValue);
            if (Number.isNaN(date.getTime())) return '--';
            const diffMin = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
            if (diffMin < 1) return 'just now';
            if (diffMin < 60) return `${diffMin}m ago`;
            const diffHour = Math.round(diffMin / 60);
            if (diffHour < 24) return `${diffHour}h ago`;
            return `${Math.round(diffHour / 24)}d ago`;
        }

        function shouldIgnoreRuntimeLoadError(error, loadId) {
            if (loadId !== latestRuntimeLoadId) return true;
            const message = String(error?.message || error || '');
            if (!message) return false;
            const navigationLike = message.includes('Failed to fetch') || message.includes('ERR_ABORTED');
            if (!navigationLike) return false;
            return runtimePageClosing || document.visibilityState === 'hidden';
        }

        function clearRefreshTimer() {
            if (refreshTimer) {
                window.clearTimeout(refreshTimer);
                refreshTimer = null;
            }
        }

        function getRefreshDelayMs() {
            if (refreshMode !== 'boost') return 60000;
            const elapsed = Date.now() - Number(refreshBoostStartedAt || 0);
            if (elapsed < 2 * 60 * 1000) return 3000;
            if (elapsed < 5 * 60 * 1000) return 10000;
            refreshMode = 'steady';
            refreshBoostStartedAt = 0;
            return 60000;
        }

        function scheduleRuntimeRefresh(delayMs = null) {
            clearRefreshTimer();
            if (runtimePageClosing) return;
            const effectiveDelay = delayMs == null ? getRefreshDelayMs() : Number(delayMs);
            refreshTimer = window.setTimeout(() => {
                refreshTimer = null;
                void loadRuntimeData(false);
            }, Math.max(1000, Number.isFinite(effectiveDelay) ? effectiveDelay : 60000));
        }

        function boostRuntimeRefresh() {
            refreshMode = 'boost';
            refreshBoostStartedAt = Date.now();
            scheduleRuntimeRefresh(3000);
        }

        function shouldKeepBoostRefresh(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState, startupState = latestStartupState) {
            const runtimeStatus = getIbkrRuntimeStatusCardModel(status);
            const startup = normalizeStartupUiState(startupState);
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const twoFactorPhase = getIbkrTwoFactorCyclePhase(twoFactor);
            const recoveryPhase = String(twoFactor?.recovery_phase || '').trim().toLowerCase();
            if (startup.active) return true;
            if (!runtimeStatus.authenticated && (runtimeStatus.gatewayActive || runtimeStatus.started)) return true;
            if (['requested', 'triggered', 'waiting_confirm', 'waiting_response_ready', 'waiting_response_waiting', 'waiting_response_received', 'waiting_response_submitted'].includes(twoFactorPhase)) return true;
            if (['panic_resetting', 'silent_probe', 'manual_takeover'].includes(recoveryPhase)) return true;
            return false;
        }

        function parseIsoMs(value) {
            if (!value) return 0;
            const date = new Date(value);
            return Number.isNaN(date.getTime()) ? 0 : date.getTime();
        }

        function intervalToMs(interval) {
            const value = normalizeIbkrInterval(interval);
            const mapping = {
                '1m': 60 * 1000,
                '5m': 5 * 60 * 1000,
                '15m': 15 * 60 * 1000,
                '30m': 30 * 60 * 1000,
                '1h': 60 * 60 * 1000,
                '4h': 4 * 60 * 60 * 1000,
                '1d': 24 * 60 * 60 * 1000
            };
            return mapping[value] || 0;
        }

        function formatSecondsLabel(value) {
            return formatIbkrSecondsLabel(value);
        }

        function formatRuntimeSlowStage(stage) {
            const payload = stage && typeof stage === 'object' ? stage : {};
            const name = String(payload.stage || '').trim() || '--';
            const symbol = String(payload.symbol || '').trim().toUpperCase();
            return `${name}${symbol ? `/${symbol}` : ''} ${formatSecondsLabel(payload.duration_s)}`;
        }

        function formatRuntimePercent(value) {
            const number = Number(value);
            return Number.isFinite(number) ? `${number.toFixed(1)}%` : '--';
        }

        function getHistoryFetchModel(status) {
            const backfill = status?.data_backfill || {};
            const lastTrace = backfill?.last_trace || {};
            const slowest = backfill?.slowest_recent_stage || lastTrace?.slowest_stage || {};
            const activeRequests = Number(backfill?.active_requests || 0) || 0;
            const activeSymbols = Number(backfill?.active_symbols_total || 0) || 0;
            const requestCount = Number(backfill?.request_count || 0) || 0;
            const traceDuration = Number(lastTrace?.duration_s);
            const traceId = String(lastTrace?.trace_id || '').trim();
            return {
                label: activeRequests > 0 ? 'ACTIVE' : (String(lastTrace?.error || '').trim() ? 'ERROR' : 'IDLE'),
                value: activeRequests > 0
                    ? `${formatCompactNumber(activeRequests)} active`
                    : (Number.isFinite(traceDuration) && traceDuration > 0 ? formatSecondsLabel(traceDuration) : formatCompactNumber(requestCount)),
                copy: `active ${activeRequests}/${activeSymbols} · requests ${formatCompactNumber(requestCount)} · throttle ${formatCompactNumber(backfill?.throttle_count || 0)}`,
                trace: traceId || '--',
                slowest: formatRuntimeSlowStage(slowest),
                workers: Number(backfill?.max_concurrency || 0) || 0,
                spacing: formatSecondsLabel(backfill?.request_spacing_s),
            };
        }

        function getWatchlistTopupModel(status) {
            const topup = status?.watchlist_idle_topup || {};
            const completion = topup?.completion || {};
            const statusText = String(topup?.status || 'idle').trim().toUpperCase();
            const running = Boolean(topup?.running) || statusText === 'RUNNING';
            const processed = Number(topup?.last_processed_symbols_total || 0) || 0;
            const attempted = Number(topup?.last_attempted_symbols_total || 0) || 0;
            const loadedBars = Number(topup?.last_loaded_bars ?? topup?.last_written_bars ?? 0) || 0;
            const stopReason = String(topup?.last_stop_reason || topup?.skip_reason || '--');
            return {
                label: running ? 'RUNNING' : statusText,
                value: running ? `${processed}/${attempted}` : formatCompactNumber(loadedBars),
                copy: `${String(topup?.mode || '--').replace(/_/g, ' ')} · batch ${topup?.batch_size || 0} · stop ${stopReason}`,
                detail: `completion ${formatRuntimePercent(completion?.progress_pct)} · due ${formatSecondsLabel(topup?.seconds_until_next_active_5m_due)} · next ${formatSecondsLabel(topup?.estimated_next_batch_s)}`,
                processed,
                attempted,
                loadedBars,
            };
        }

        function getExtraObject(record) {
            return getIbkrExtraObject(record);
        }

        function getComputedTimeLabel(record) {
            return getIbkrComputedTimeLabel(record);
        }

        function getRecordBarLabel(record) {
            return getIbkrRecordBarLabel(record);
        }

        function renderEmpty(message) {
            return `<div class="table-empty">${escapeHtml(message)}</div>`;
        }
