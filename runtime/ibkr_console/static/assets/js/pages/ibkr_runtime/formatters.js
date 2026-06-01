        function toArray(payload) {
            return Array.isArray(payload?.items) ? payload.items : [];
        }

        function getTotalItems(payload, fallback = 0) {
            if (typeof payload === 'number' && Number.isFinite(payload)) return payload;
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

        function normalizeRuntimeOperationAction(action) {
            return String(action || '').trim().toLowerCase();
        }

        function getRuntimeOperationStorageKey() {
            const environment = normalizeBrokerMode(currentEnvironment || currentBrokerMode || 'paper', 'paper');
            return `${RUNTIME_OPERATION_STORAGE_PREFIX}:${environment}`;
        }

        function isRuntimeOperationWatchAction(action) {
            return RUNTIME_OPERATION_WATCH_ACTIONS.has(normalizeRuntimeOperationAction(action));
        }

        function isRuntimeOperationLockAction(action) {
            return RUNTIME_OPERATION_LOCK_ACTIONS.has(normalizeRuntimeOperationAction(action));
        }

        function persistActiveRuntimeOperation() {
            try {
                if (!activeRuntimeOperation) {
                    window.sessionStorage.removeItem(getRuntimeOperationStorageKey());
                    return;
                }
                window.sessionStorage.setItem(getRuntimeOperationStorageKey(), JSON.stringify(activeRuntimeOperation));
            } catch (error) {
                console.warn('Unable to persist runtime operation watch state:', error);
            }
        }

        function restoreActiveRuntimeOperation() {
            try {
                const raw = window.sessionStorage.getItem(getRuntimeOperationStorageKey());
                if (!raw) return null;
                const parsed = JSON.parse(raw);
                if (!parsed || typeof parsed !== 'object' || !isRuntimeOperationWatchAction(parsed.action)) {
                    window.sessionStorage.removeItem(getRuntimeOperationStorageKey());
                    return null;
                }
                const nowMs = Date.now();
                const deadlineMs = Number(parsed.deadlineMs || 0) || 0;
                const updatedAt = Number(parsed.updatedAt || parsed.startedAt || 0) || 0;
                const terminal = parsed.terminal === true;
                const terminalTtlMs = String(parsed.status || '').trim().toLowerCase() === 'success'
                    ? RUNTIME_OPERATION_SUCCESS_VISIBLE_MS
                    : RUNTIME_OPERATION_TTL_MS;
                const staleTerminal = terminal && updatedAt > 0 && nowMs - updatedAt > terminalTtlMs;
                const staleActive = !terminal && deadlineMs > 0 && nowMs - deadlineMs > RUNTIME_OPERATION_SUCCESS_VISIBLE_MS;
                if (staleTerminal || staleActive) {
                    window.sessionStorage.removeItem(getRuntimeOperationStorageKey());
                    return null;
                }
                activeRuntimeOperation = {
                    ...parsed,
                    action: normalizeRuntimeOperationAction(parsed.action),
                    label: parsed.label || getRuntimeActionLabel(parsed.action),
                    terminal,
                };
                return activeRuntimeOperation;
            } catch (error) {
                console.warn('Unable to restore runtime operation watch state:', error);
                try { window.sessionStorage.removeItem(getRuntimeOperationStorageKey()); } catch (_) {}
                return null;
            }
        }

        function startRuntimeOperationWatch(action, options = {}) {
            const normalizedAction = normalizeRuntimeOperationAction(action);
            if (!isRuntimeOperationWatchAction(normalizedAction)) return null;
            const nowMs = Date.now();
            const label = getRuntimeActionLabel(normalizedAction);
            activeRuntimeOperation = {
                id: `${normalizedAction}:${nowMs}`,
                action: normalizedAction,
                label,
                reason: String(options.reason || '').trim(),
                source: String(options.source || 'runtime_page').trim() || 'runtime_page',
                startedAt: nowMs,
                updatedAt: nowMs,
                deadlineMs: nowMs + RUNTIME_OPERATION_TTL_MS,
                status: String(options.status || 'watching').trim() || 'watching',
                phase: String(options.phase || 'submitted').trim() || 'submitted',
                terminal: false,
                message: String(options.message || `${label} 请求已发送，正在追踪 Gateway / 2FA / Session 状态。`).trim(),
            };
            persistActiveRuntimeOperation();
            boostRuntimeRefresh();
            if (typeof renderAuthActionBanner === 'function') renderAuthActionBanner(latestNextActionModel);
            if (typeof syncActionLocks === 'function') syncActionLocks();
            return activeRuntimeOperation;
        }

        function updateActiveRuntimeOperation(patch = {}) {
            if (!activeRuntimeOperation) return null;
            activeRuntimeOperation = {
                ...activeRuntimeOperation,
                ...(patch && typeof patch === 'object' ? patch : {}),
                updatedAt: Date.now(),
            };
            persistActiveRuntimeOperation();
            if (typeof renderAuthActionBanner === 'function') renderAuthActionBanner(latestNextActionModel);
            if (typeof syncActionLocks === 'function') syncActionLocks();
            return activeRuntimeOperation;
        }

        function finishActiveRuntimeOperation(status, message, patch = {}) {
            if (!activeRuntimeOperation) return null;
            const normalizedStatus = String(status || '').trim().toLowerCase() || 'success';
            return updateActiveRuntimeOperation({
                ...(patch && typeof patch === 'object' ? patch : {}),
                status: normalizedStatus,
                phase: normalizedStatus === 'success' ? 'ready' : 'failed',
                terminal: true,
                message: String(message || '').trim() || (normalizedStatus === 'success'
                    ? 'Gateway / Session 已恢复。'
                    : '长动作未确认完成，可以刷新状态或重试。'),
            });
        }

        function clearActiveRuntimeOperation() {
            activeRuntimeOperation = null;
            persistActiveRuntimeOperation();
            if (typeof renderAuthActionBanner === 'function') renderAuthActionBanner(latestNextActionModel);
            if (typeof syncActionLocks === 'function') syncActionLocks();
        }

        function isRuntimeOperationLocking(operation = activeRuntimeOperation) {
            if (!operation || operation.terminal === true) return false;
            const deadlineMs = Number(operation.deadlineMs || 0) || 0;
            if (!deadlineMs) return true;
            return Date.now() <= deadlineMs;
        }

        function getActiveRuntimeOperationLockReason(action) {
            if (!isRuntimeOperationLocking() || !isRuntimeOperationLockAction(action)) return '';
            const label = activeRuntimeOperation?.label || 'Runtime 长动作';
            return `当前已有 ${label} 轮次在追踪中，请等待 Gateway / 2FA / Session 状态确认，不要重复触发。`;
        }

        function getRuntimeServiceActionLockReason(serviceName, action) {
            if (!isRuntimeOperationLocking()) return '';
            const service = String(serviceName || '').trim().toLowerCase();
            const normalizedAction = String(action || '').trim().toLowerCase();
            if (service === 'ibkr-gateway' && ['restart', 'start'].includes(normalizedAction)) {
                return getActiveRuntimeOperationLockReason('gateway_restart');
            }
            if (service === 'ibkr-runtime' && ['restart', 'start'].includes(normalizedAction)) {
                return getActiveRuntimeOperationLockReason('start');
            }
            return '';
        }

        function getRuntimeOperationStatusSnapshot(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState, startupState = latestStartupState) {
            const runtimeStatus = typeof getEffectiveRuntimeStatusCardModel === 'function'
                ? getEffectiveRuntimeStatusCardModel(status, twoFactorState)
                : {};
            const gateway = status?.gateway || {};
            const websocket = status?.websocket || {};
            const gatewayStatusCode = Number(gateway.status_code || 0) || 0;
            const twoFactor = typeof deriveTwoFactorUiState === 'function'
                ? deriveTwoFactorUiState(twoFactorState || {})
                : { ...(twoFactorState || {}) };
            const startup = typeof normalizeStartupUiState === 'function'
                ? normalizeStartupUiState(startupState || {})
                : { ...(startupState || {}) };
            const twoFactorStatus = typeof normalizeIbkrTwoFactorStatus === 'function'
                ? normalizeIbkrTwoFactorStatus(twoFactor?.status || '')
                : String(twoFactor?.status || '').trim().toLowerCase();
            const twoFactorPhase = typeof getIbkrTwoFactorCyclePhase === 'function'
                ? getIbkrTwoFactorCyclePhase(twoFactor)
                : twoFactorStatus;
            const twoFactorSuccess = typeof isTwoFactorVerifiedSuccess === 'function'
                ? isTwoFactorVerifiedSuccess(twoFactor)
                : twoFactorStatus === 'success';
            const gatewayReachable = Boolean(
                gateway.reachable
                || runtimeStatus.authenticated
                || twoFactorSuccess
                || (gateway.running && gatewayStatusCode > 0 && ![502, 503].includes(gatewayStatusCode))
            );
            const gatewayRunning = Boolean(gateway.running || runtimeStatus.gatewayActive || status?.gateway_running || gatewayReachable);
            const sessionAuthenticated = Boolean(runtimeStatus.authenticated || twoFactorSuccess);
            const websocketReady = Boolean(websocket.ready || websocket.connected || status?.websocket_ready || (twoFactorSuccess && sessionAuthenticated));
            const waitingManual = Boolean(
                startup.active
                || twoFactor?.active
                || ['requested', 'triggered', 'waiting_confirm', 'waiting_response_ready', 'waiting_response_waiting', 'waiting_response_received', 'waiting_response_submitted'].includes(twoFactorPhase)
                || ['requested', 'triggered', 'waiting_confirm', 'waiting_response'].includes(twoFactorStatus)
            );
            return {
                gatewayReachable,
                gatewayRunning,
                sessionAuthenticated,
                websocketReady,
                waitingManual,
                twoFactor,
                startup,
                twoFactorPhase,
                twoFactorStatus,
                twoFactorSuccess,
            };
        }

        function deriveRuntimeOperationProgress(operation = activeRuntimeOperation, status = latestRuntimeStatus, twoFactorState = latestTwoFactorState, startupState = latestStartupState) {
            if (!operation) return null;
            const snapshot = getRuntimeOperationStatusSnapshot(status, twoFactorState, startupState);
            const nowMs = Date.now();
            const deadlineMs = Number(operation.deadlineMs || 0) || 0;
            const expired = deadlineMs > 0 && nowMs > deadlineMs;
            const success = Boolean(operation.status === 'success' || (snapshot.sessionAuthenticated && (snapshot.websocketReady || snapshot.twoFactorSuccess)));
            const failed = !success && (operation.status === 'failed' || (expired && (!snapshot.waitingManual || !snapshot.gatewayReachable)));
            let phase = 'submitted';
            let tone = 'info';
            let title = `${operation.label || 'Runtime 动作'} 已提交`;
            let copy = '后台可能仍在重启 Gateway 或恢复会话；页面会持续刷新，请不要重复点击。';
            let message = operation.message || copy;

            if (success || operation.status === 'success') {
                phase = 'ready';
                tone = 'ok';
                title = 'Gateway / Session 已恢复';
                copy = 'Gateway、IBKR Session 或 2FA 已确认恢复，无需再次点击重启。';
                message = operation.message || '状态已恢复，按钮已解锁。';
            } else if (failed || operation.status === 'failed') {
                phase = 'failed';
                tone = 'error';
                title = `${operation.label || 'Runtime 动作'} 未确认完成`;
                copy = snapshot.gatewayReachable
                    ? 'Gateway 已有响应但会话尚未恢复；可以刷新状态，必要时再重试。'
                    : '超过 3 分钟仍未确认 Gateway 可达；可以刷新状态或重试 Gateway 重启。';
                message = operation.message || copy;
            } else if (snapshot.waitingManual && snapshot.gatewayReachable && !snapshot.sessionAuthenticated) {
                phase = 'auth';
                tone = 'warn';
                title = '等待当前 2FA / 会话轮次';
                copy = 'Gateway 已响应，接下来只处理当前飞书或手机验证；不要重复重启 Gateway。';
                message = operation.message || '等待你完成当前验证或系统确认会话恢复。';
            } else if (snapshot.gatewayReachable) {
                phase = snapshot.sessionAuthenticated ? 'ready' : 'auth';
                tone = snapshot.sessionAuthenticated ? 'info' : 'warn';
                title = snapshot.sessionAuthenticated ? 'Session 已恢复，等待 Runtime Ready' : 'Gateway 已可达，等待认证';
                copy = snapshot.sessionAuthenticated
                    ? '会话已恢复，页面正在确认 WebSocket / Runtime ready 状态。'
                    : 'Gateway 已经响应，正在等待 2FA / Session 恢复。';
                message = operation.message || copy;
            } else if (snapshot.gatewayRunning) {
                phase = 'gateway';
                tone = 'info';
                title = 'Gateway 进程已启动，等待 API 可达';
                copy = 'IB Gateway GUI/API 启动可能需要几十秒；页面正在加速刷新，请勿重复点击。';
                message = operation.message || copy;
            }

            const steps = [
                { key: 'submitted', label: '请求已发送', done: true },
                { key: 'gateway', label: 'Gateway 重启/可达', done: snapshot.gatewayReachable, active: phase === 'gateway' },
                { key: 'auth', label: '2FA / 会话恢复', done: snapshot.sessionAuthenticated, active: phase === 'auth' },
                { key: 'ready', label: 'Runtime / WebSocket Ready', done: success, active: phase === 'ready' && !success },
            ].map((step) => ({
                ...step,
                state: failed && !step.done && step.key !== 'submitted'
                    ? 'error'
                    : (step.done ? 'done' : (step.active ? 'active' : 'pending')),
            }));

            return {
                phase,
                tone,
                title,
                copy,
                message,
                steps,
                success,
                failed,
                expired,
                snapshot,
            };
        }

        function reconcileActiveRuntimeOperation(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState, startupState = latestStartupState) {
            if (!activeRuntimeOperation) return null;
            const nowMs = Date.now();
            if (activeRuntimeOperation.terminal === true) {
                const updatedAt = Number(activeRuntimeOperation.updatedAt || 0) || 0;
                const terminalTtlMs = String(activeRuntimeOperation.status || '').trim().toLowerCase() === 'success'
                    ? RUNTIME_OPERATION_SUCCESS_VISIBLE_MS
                    : RUNTIME_OPERATION_TTL_MS;
                if (updatedAt > 0 && nowMs - updatedAt > terminalTtlMs) {
                    clearActiveRuntimeOperation();
                    return null;
                }
                return activeRuntimeOperation;
            }
            const progress = deriveRuntimeOperationProgress(activeRuntimeOperation, status, twoFactorState, startupState);
            if (!progress) return activeRuntimeOperation;
            if (progress.success) {
                return finishActiveRuntimeOperation('success', 'Gateway / Session 已恢复，按钮已解锁。', { phase: 'ready' });
            }
            if (progress.failed) {
                return finishActiveRuntimeOperation('failed', progress.copy, { phase: 'failed' });
            }
            return updateActiveRuntimeOperation({
                phase: progress.phase,
                status: 'watching',
                message: progress.message,
            });
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

        function formatRuntimeModeLabel(value, options = {}) {
            const compact = Boolean(options.compact);
            const key = String(value || '').trim().toLowerCase();
            const labels = {
                remote: compact ? '远端服务' : '远端服务 REMOTE',
                local: compact ? '本机服务' : '本机服务 LOCAL',
                embedded: compact ? '内置运行' : '内置运行 EMBEDDED',
                standalone: compact ? '独立服务' : '独立服务 STANDALONE',
            };
            if (!key) return '--';
            return labels[key] || (compact ? key.toUpperCase() : `运行模式 ${key.toUpperCase()}`);
        }

        function runtimeModeChipTone(value) {
            const key = String(value || '').trim().toLowerCase();
            if (key === 'remote' || key === 'standalone') return 'chip-context';
            return 'chip-muted';
        }

        function formatEnvironmentLabel(value, options = {}) {
            const compact = Boolean(options.compact);
            const key = String(value || '').trim().toLowerCase();
            const labels = {
                live: compact ? '实盘' : '实盘环境 LIVE',
                paper: compact ? '模拟盘' : '模拟盘环境 PAPER',
                backtest: compact ? '回测' : '回测环境 BACKTEST',
                dev: compact ? '开发' : '开发环境 DEV',
                test: compact ? '测试' : '测试环境 TEST',
            };
            if (!key) return '--';
            return labels[key] || (compact ? key.toUpperCase() : `环境 ${key.toUpperCase()}`);
        }

        function environmentChipTone(value) {
            const key = String(value || '').trim().toLowerCase();
            if (key === 'live') return 'chip-live';
            if (key === 'paper') return 'chip-context';
            return 'chip-muted';
        }

        function formatRuntimeSignalStatus(value) {
            const key = String(value || '').trim().toLowerCase();
            const labels = {
                awaiting_confirm: '待确认',
                pending: '等待执行',
                submitted: '已提交',
                protected_active: '持仓保护中',
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
            const jitterMs = delayMs == null && refreshMode !== 'boost'
                ? Math.floor(Math.random() * 5000)
                : 0;
            refreshTimer = window.setTimeout(() => {
                refreshTimer = null;
                if (document.visibilityState === 'hidden') {
                    scheduleRuntimeRefresh(60000);
                    return;
                }
                void loadRuntimeData(false);
            }, Math.max(1000, (Number.isFinite(effectiveDelay) ? effectiveDelay : 60000) + jitterMs));
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
