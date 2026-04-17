let currentEnvironment = getCurrentRuntimeEnvironment();
        let refreshTimer = null;
        let actionPending = false;
        let latestRuntimeStatus = {};
        let latestTwoFactorState = {};
        let latestStartupState = {};
        let latestNextActionModel = null;
        let latestRuntimeLoadId = 0;
        let hasLoadedRuntimeData = false;
        const CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000;
        const MANUAL_AUTH_REASON_LABELS = {
            weekly_reauth: '每周重登验证',
            manual_start: '启动验证',
            startup: '启动验证',
            manual_reauth: '手动重登验证',
            manual_gateway_restart: '网关重启验证',
            panic_reset_2fa: '重开验证'
        };
        const STARTUP_STEP_LABELS = {
            service_boot: '服务已拉起',
            card_ready: '已准备 2FA 卡片',
            manual_trigger: '已在飞书手动触发 2FA',
            manual_confirm: '已完成当前 2FA 验证',
            runtime_resume: '鉴权成功并恢复 Runtime',
            health_check: '健康检查通过'
        };

        function setRuntimeLoading(active, title, copy) {
            const overlay = document.getElementById('pageLoading');
            if (!overlay) return;
            if (title) {
                const titleEl = document.getElementById('pageLoadingTitle');
                if (titleEl) titleEl.textContent = title;
            }
            if (copy) {
                const copyEl = document.getElementById('pageLoadingCopy');
                if (copyEl) copyEl.textContent = copy;
            }
            overlay.classList.toggle('is-hidden', !active);
        }

        function initRuntimeAuth() {
            const token = getToken();
            if (!token) {
                redirectToLogin(`${location.pathname}${location.search}`);
                return false;
            }
            return true;
        }

        async function requestRuntimeJson(path, { method = 'GET', body = null } = {}) {
            return requestIbkrPageJson(path, {
                environment: currentEnvironment,
                method,
                body,
                retryAttempts: 3
            });
        }

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

        function statusClass(value) {
            const text = String(value || '').trim().toLowerCase();
            if (!text) return 'pill-neutral';
            if (['ready', 'open', 'success', 'recovered', 'authenticated'].includes(text)) return text === 'success' ? 'pill-success' : 'pill-ok';
            if (['warming', 'blocked', 'degraded', 'closed', 'panic_resetting', 'manual_takeover', 'received'].includes(text)) return 'pill-warning';
            if (['stopped'].includes(text)) return 'pill-error';
            if (['running', 'ok', 'online', 'filled', 'long', 'buy', 'active', 'executed'].includes(text)) return `pill-${text}`;
            if (['warning', 'delayed', 'pending', 'requested', 'triggered', 'awaiting_confirm', 'waiting_confirm', 'waiting_response', 'submitted', 'timeout'].includes(text)) return `pill-${text}`;
            if (['error', 'offline', 'cancelled', 'rejected', 'gateway_rejected', 'submit_failed', 'short', 'sell', 'failed'].includes(text)) return `pill-${text}`;
            if (['expired', 'init', 'candidate'].includes(text)) return 'pill-neutral';
            return 'pill-neutral';
        }

        function chipTone(value) {
            const text = String(value || '').trim().toLowerCase();
            if (['ready', 'open', 'recovered', 'authenticated'].includes(text)) return 'chip-ok';
            if (['warming', 'blocked', 'degraded', 'closed', 'panic_resetting', 'manual_takeover', 'received', 'submitted'].includes(text)) return 'chip-warn';
            if (['stopped'].includes(text)) return 'chip-error';
            if (['running', 'ok', 'online', 'success'].includes(text)) return 'chip-ok';
            if (['warning', 'delayed', 'pending', 'requested', 'triggered', 'awaiting_confirm', 'waiting_confirm', 'waiting_response', 'timeout'].includes(text)) return 'chip-warn';
            if (['error', 'offline', 'failed', 'rejected', 'gateway_rejected', 'submit_failed'].includes(text)) return 'chip-error';
            return 'chip-muted';
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

        function formatTimeLabelWithFallback(value) {
            const formatted = formatTimeLabel(value);
            if (formatted && formatted !== '--') return formatted;
            const raw = String(value || '').trim();
            if (!raw) return '--';
            return raw.replace('T', ' ').slice(0, 19);
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

        function parseIsoMs(value) {
            if (!value) return 0;
            const date = new Date(value);
            return Number.isNaN(date.getTime()) ? 0 : date.getTime();
        }

        function intervalToMs(interval) {
            const value = String(interval || '').trim().toLowerCase();
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
            const number = Number(value);
            if (!Number.isFinite(number)) return '--';
            if (number < 1) return `${number.toFixed(3)}s`;
            if (number < 10) return `${number.toFixed(2)}s`;
            if (number < 60) return `${number.toFixed(1)}s`;
            return `${Math.round(number)}s`;
        }

        function getExtraObject(record) {
            return record && typeof record.extra === 'object' && record.extra ? record.extra : {};
        }

        function getComputedTimeLabel(record) {
            const extra = getExtraObject(record);
            return extra.computed_at_us || extra.computed_at_cn || record?.updated || record?.created || '--';
        }

        function getRecordBarLabel(record) {
            if (!record) return '--';
            return record.bar_time_ms ? formatBarTimeMsToET(record.bar_time_ms) : (record.us_time || '--');
        }

        function renderEmpty(message) {
            return `<div class="table-empty">${escapeHtml(message)}</div>`;
        }

        function normalizeSymbolList(values) {
            const source = Array.isArray(values) ? values : [values];
            const seen = new Set();
            return source.reduce((items, value) => {
                const entries = Array.isArray(value) ? value : [value];
                entries.forEach((entry) => {
                    const symbol = String(entry || '').trim().toUpperCase();
                    if (!symbol || seen.has(symbol)) return;
                    seen.add(symbol);
                    items.push(symbol);
                });
                return items;
            }, []);
        }

        function normalizeWarmup(status) {
            const warmup = status?.warmup || {};
            const monitorSymbols = normalizeSymbolList(warmup.monitor_symbols);
            const monitorSet = new Set(monitorSymbols);
            const pendingSymbols = normalizeSymbolList(warmup.pending_symbols);
            const fallbackBlockingPendingSymbols = pendingSymbols.filter((symbol) => !monitorSet.has(symbol));
            const fallbackMonitorPendingSymbols = pendingSymbols.filter((symbol) => monitorSet.has(symbol));
            return {
                phase: String(warmup.phase || 'idle').trim().toLowerCase() || 'idle',
                gate_open: Boolean(warmup.trading_gate_open),
                gate_reason: String(warmup.trading_gate_reason || '').trim() || 'warmup_idle',
                required_interval: String(warmup.required_interval || '5m'),
                symbols_total: Number(warmup.symbols_total || 0) || 0,
                trade_symbols_total: Number(warmup.trade_symbols_total || 0) || 0,
                monitor_symbols_total: Number(warmup.monitor_symbols_total || 0) || 0,
                ready_symbols: Number(warmup.ready_symbols || 0) || 0,
                ready_trade_symbols: Number(warmup.ready_trade_symbols || 0) || 0,
                ready_monitor_symbols: Number(warmup.ready_monitor_symbols || 0) || 0,
                trade_symbols: normalizeSymbolList(warmup.trade_symbols),
                monitor_symbols: monitorSymbols,
                pending_symbols: pendingSymbols,
                blocking_pending_symbols: Array.isArray(warmup.blocking_pending_symbols)
                    ? normalizeSymbolList(warmup.blocking_pending_symbols)
                    : fallbackBlockingPendingSymbols,
                blocking_pending_symbols_total: Number(warmup.blocking_pending_symbols_total || fallbackBlockingPendingSymbols.length) || 0,
                monitor_pending_symbols: Array.isArray(warmup.monitor_pending_symbols)
                    ? normalizeSymbolList(warmup.monitor_pending_symbols)
                    : fallbackMonitorPendingSymbols,
                monitor_pending_symbols_total: Number(warmup.monitor_pending_symbols_total || fallbackMonitorPendingSymbols.length) || 0,
                started_at: warmup.started_at || null,
                finished_at: warmup.finished_at || null,
                last_error: String(warmup.last_error || '').trim(),
            };
        }

        function getStartupStrategy(status) {
            const strategy = status?.startup_strategy || {};
            return {
                manual_start_mode: String(strategy.manual_start_mode || 'fresh_cycle').trim().toLowerCase() || 'fresh_cycle',
                weekly_reauth_mode: String(strategy.weekly_reauth_mode || 'fresh_cycle').trim().toLowerCase() || 'fresh_cycle',
                manual_gateway_restart_mode: String(strategy.manual_gateway_restart_mode || 'fresh_cycle').trim().toLowerCase() || 'fresh_cycle',
                server_boot_mode: String(strategy.server_boot_mode || 'resume_only').trim().toLowerCase() || 'resume_only',
                server_boot_publish_startup_card: strategy.server_boot_publish_startup_card === true,
                fresh_cycle_requires_manual_2fa: strategy.fresh_cycle_requires_manual_2fa !== false,
                startup_card_scope: String(strategy.startup_card_scope || '').trim().toLowerCase() || 'fresh_cycles_only',
                summary: String(strategy.summary || '').trim()
            };
        }

        function formatStartupModeLabel(mode) {
            return String(mode || '').trim().toLowerCase() === 'fresh_cycle' ? 'FRESH CYCLE' : 'RESUME ONLY';
        }

        function formatStartupStrategySummary(status) {
            const strategy = getStartupStrategy(status);
            if (strategy.summary) return strategy.summary;
            const manualStart = strategy.manual_start_mode === 'fresh_cycle'
                ? '手动启动=重启 Gateway + 新卡片 + 人工 2FA'
                : '手动启动=直接恢复 Runtime';
            const weeklyReauth = strategy.weekly_reauth_mode === 'fresh_cycle'
                ? '每周重验=fresh cycle'
                : '每周重验=resume only';
            const serverBoot = strategy.server_boot_mode === 'fresh_cycle'
                ? 'server_boot=fresh cycle'
                : 'server_boot=resume only';
            const startupCard = strategy.server_boot_publish_startup_card
                ? 'server_boot 会发启动卡片'
                : 'server_boot 不新发启动卡片';
            return `${manualStart}；${weeklyReauth}；${serverBoot}；${startupCard}`;
        }

        function deriveDataHealth(latestBar) {
            return buildIbkrDataHealth(latestBar?.bar_time_ms, {
                symbol: latestBar?.symbol || '',
                noDataStatus: 'no_data'
            });
        }

        function deriveRealtimeMetrics(status, latestBar) {
            const realtime = status?.realtime_compute || {};
            const barAggregator = status?.bar_aggregator || {};
            const lastRunMs = parseIsoMs(realtime.last_run);
            const lastBarCloseMs = parseIsoMs(realtime.last_bar_close);
            const barTimeMs = Number(latestBar?.bar_time_ms || 0) || 0;
            const expectedCloseMs = barTimeMs > 0 ? barTimeMs + (intervalToMs(latestBar?.interval || '5m') || 0) : 0;
            const closeDelayS = lastBarCloseMs > 0 && expectedCloseMs > 0
                ? Math.max(0, (lastBarCloseMs - expectedCloseMs) / 1000)
                : null;
            const computeAfterCloseS = lastRunMs > 0 && lastBarCloseMs > 0
                ? Math.max(0, (lastRunMs - lastBarCloseMs) / 1000)
                : null;
            const activeBars = Object.values(barAggregator.active_bars || {});
            const activeTickAges = activeBars
                .map((item) => Number(item?.last_update_age_s || 0))
                .filter((item) => Number.isFinite(item));
            return {
                last_bar_close: realtime.last_bar_close || null,
                last_compute_run: realtime.last_run || null,
                close_delay_s: closeDelayS,
                compute_after_close_s: computeAfterCloseS,
                active_tick_lag_s: activeTickAges.length ? Math.max(...activeTickAges) : null,
                active_symbol_count: activeBars.length,
            };
        }

        function formatDurationCompact(totalSeconds) {
            const seconds = Number(totalSeconds);
            if (!Number.isFinite(seconds) || seconds < 0) return '--';
            if (seconds < 60) return `${Math.round(seconds)}s`;
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const remainder = Math.round(seconds % 60);
            if (hours > 0) return `${hours}h ${minutes}m`;
            if (minutes > 0 && remainder > 0) return `${minutes}m ${remainder}s`;
            return `${minutes}m`;
        }

        function getElapsedSeconds(startValue, endValue = null) {
            const startMs = parseIsoMs(startValue);
            if (startMs <= 0) return null;
            const endMs = endValue ? parseIsoMs(endValue) : Date.now();
            if (endMs <= 0 || endMs < startMs) return null;
            return Math.max(0, Math.round((endMs - startMs) / 1000));
        }

        function getWarmupElapsedSeconds(warmup) {
            return getElapsedSeconds(warmup?.started_at, warmup?.finished_at);
        }

        function deriveRealtimeComputeState(status) {
            const realtime = status?.realtime_compute || {};
            const inflight = realtime.inflight === true;
            const stalled = realtime.stalled === true;
            const queueSize = Number(realtime.queue_size || 0) || 0;
            const inflightAgeS = Number(realtime.inflight_age_s);
            const lastElapsedS = Number(realtime.last_elapsed_s);

            if (stalled) {
                return {
                    phase: 'stalled',
                    tone: 'error',
                    title: 'Indicators 计算已卡住',
                    summary: `started ${formatTimeLabel(realtime.last_started)} · age ${formatSecondsLabel(realtime.inflight_age_s)} · reason ${String(realtime.stall_reason || 'unknown')}`,
                };
            }

            if (inflight) {
                const longRunning = Number.isFinite(inflightAgeS) && inflightAgeS >= Math.max(60, Number.isFinite(lastElapsedS) ? lastElapsedS * 0.5 : 60);
                return {
                    phase: 'running',
                    tone: longRunning ? 'warn' : 'info',
                    title: longRunning
                        ? `Indicators 计算耗时较长 · ${formatSecondsLabel(realtime.inflight_age_s)}`
                        : `Indicators 正在计算 · ${formatSecondsLabel(realtime.inflight_age_s)}`,
                    summary: `started ${formatTimeLabel(realtime.last_started)} · prev ${formatSecondsLabel(realtime.last_elapsed_s)} · queue ${queueSize}`,
                };
            }

            if (queueSize > 0) {
                return {
                    phase: 'queued',
                    tone: 'warn',
                    title: `Indicators 等待计算 · queue ${queueSize}`,
                    summary: `last run ${formatTimeLabel(realtime.last_run)} · lag ${formatSecondsLabel(realtime.lag_since_last_run_s)}`,
                };
            }

            return {
                phase: 'idle',
                tone: 'ok',
                title: 'Indicators 已追平',
                summary: `last run ${formatTimeLabel(realtime.last_run)}`,
            };
        }

        function parseSymbolList(rawValue) {
            const seen = new Set();
            return String(rawValue || '')
                .split(',')
                .map((item) => String(item || '').trim().toUpperCase())
                .filter((item) => {
                    if (!item || seen.has(item)) return false;
                    seen.add(item);
                    return true;
                });
        }

        function buildEnvironmentFilter() {
            return `environment = "${escapeQueryValue(currentEnvironment)}"`;
        }

        function resolveRuntimeMarketDate(status) {
            const marketDate = String(
                status?.runtime?.market_universe?.market_date
                || status?.market_universe?.market_date
                || ''
            ).trim();
            return marketDate || new Date().toISOString().slice(0, 10);
        }

        async function loadRuntimeTodayCounts(status) {
            const envFilter = buildEnvironmentFilter();
            const marketDate = resolveRuntimeMarketDate(status);
            const todayFilterBase = `created >= "${escapeQueryValue(`${marketDate} 00:00:00`)}" && ${envFilter}`;
            const targetDateFilter = `date = "${escapeQueryValue(marketDate)}" && ${envFilter}`;

            const [
                barsCountResp,
                indicatorsCountResp,
                signalsCountResp,
                ordersCountResp,
                eventsCountResp,
                targetsCountResp,
            ] = await Promise.all([
                apiFetch('ibkr_bars', { filter: todayFilterBase, perPage: 1, page: 1 }).catch(() => null),
                apiFetch('ibkr_indicators', { filter: todayFilterBase, perPage: 1, page: 1 }).catch(() => null),
                apiFetch('ibkr_signals', { filter: todayFilterBase, perPage: 1, page: 1 }).catch(() => null),
                apiFetch('orders', { filter: todayFilterBase, perPage: 1, page: 1 }).catch(() => null),
                apiFetch('system_events', { filter: todayFilterBase, perPage: 1, page: 1 }).catch(() => null),
                apiFetch('ibkr_targets', { filter: targetDateFilter, perPage: 1, page: 1 }).catch(() => null),
            ]);

            return {
                ibkr_bars: getTotalItems(barsCountResp),
                ibkr_indicators: getTotalItems(indicatorsCountResp),
                ibkr_signals: getTotalItems(signalsCountResp),
                orders: getTotalItems(ordersCountResp),
                events: getTotalItems(eventsCountResp),
                ibkr_targets: getTotalItems(targetsCountResp),
            };
        }

        function getRuntimeEnvironmentMismatch(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState) {
            const requested = String(currentEnvironment || '').trim().toLowerCase() || 'live';
            const actual = String(
                status?.actual_runtime_environment
                || status?.environment
                || twoFactorState?.actual_runtime_environment
                || ''
            ).trim().toLowerCase();
            if (!actual || actual === requested) return null;
            return {
                requested,
                actual,
                message: `当前 ${getEnvironmentLabel(requested)} 页面没有独立 runtime；实际运行中的是 ${getEnvironmentLabel(actual)}。2FA / start / stop 等动作已阻止，请切到对应环境页面。`
            };
        }

        function getRuntimeStarted(status = latestRuntimeStatus) {
            return getIbkrRuntimeStarted(status);
        }

        function normalizeStartupUiState(startupState = latestStartupState) {
            const state = { ...(startupState || {}) };
            state.active = state.active === true;
            state.status = String(state.status || '').trim().toLowerCase() || 'idle';
            state.reason = normalizeManualAuthReason(state.reason || '');
            state.startup_label = String(state.startup_label || '').trim();
            state.startup_chat_id = String(state.startup_chat_id || '').trim();
            state.cycle_id = String(state.cycle_id || '').trim();
            state.current_step = mapStartupStepKey(state.current_step || '');
            state.current_blocker = String(state.current_blocker || '').trim();
            state.operator_action = String(state.operator_action || '').trim();
            state.summary = String(state.summary || '').trim();
            state.steps = state?.steps && typeof state.steps === 'object' ? state.steps : {};
            return state;
        }

        function mapStartupStepKey(key) {
            const rawKey = String(key || '').trim();
            const mapping = {
                gateway: 'service_boot',
                auth: 'manual_trigger',
                subscriptions: 'runtime_resume',
                core_threads: 'runtime_resume',
                warmup: 'runtime_resume',
                trading_gate: 'health_check'
            };
            if (STARTUP_STEP_LABELS[rawKey]) return rawKey;
            return mapping[rawKey] || rawKey;
        }

        function getStartupCurrentStepLabel(startupState = latestStartupState) {
            const state = normalizeStartupUiState(startupState);
            const currentStep = String(state.current_step || '').trim();
            if (!currentStep) return '';
            if (STARTUP_STEP_LABELS[currentStep]) return STARTUP_STEP_LABELS[currentStep];
            if (state.steps?.[currentStep]?.label) return String(state.steps[currentStep].label || '').trim();
            return currentStep;
        }

        function getEffectiveManualAuthReason(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState, startupState = latestStartupState) {
            const sessionAuthenticated = Boolean(status?.session?.authenticated);
            const startup = normalizeStartupUiState(startupState);
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            if (startup.active && startup.reason) return startup.reason;
            if (twoFactor.reason) return normalizeManualAuthReason(twoFactor.reason);
            if (!sessionAuthenticated) return 'manual_start';
            return 'manual_reauth';
        }

        function focusTwoFactorResponseInput() {
            const panel = document.getElementById('twoFactorPanel');
            panel?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            window.setTimeout(() => {
                const input = document.getElementById('challengeResponseInput');
                input?.focus();
                input?.select?.();
            }, 180);
        }

        function deriveNextAuthActionModel(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState, startupState = latestStartupState) {
            const runtimeMismatch = getRuntimeEnvironmentMismatch(status, twoFactorState);
            const runtimeStarted = getRuntimeStarted(status);
            const sessionAuthenticated = Boolean(status?.session?.authenticated);
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const reason = getEffectiveManualAuthReason(status, twoFactor, startup);
            const reasonLabel = getManualAuthReasonLabel(reason);
            const startupStepLabel = getStartupCurrentStepLabel(startup) || '等待下一步';

            if (runtimeMismatch) {
                return {
                    visible: true,
                    tone: 'error',
                    badge: '环境错配',
                    stepLabel: '切换环境',
                    title: `当前实际运行环境是 ${String(runtimeMismatch.actual || '--').toUpperCase()}`,
                    copy: runtimeMismatch.message,
                    meta: ['动作已阻止', '请切到对应环境页面'],
                    buttonLabel: '刷新状态',
                    behavior: 'refresh'
                };
            }

            if (twoFactor.status === 'waiting_response') {
                if (twoFactor.reset_recommended) {
                    return {
                        visible: true,
                        tone: 'error',
                        badge: reasonLabel,
                        stepLabel: '重开验证',
                        title: '旧 2FA 轮次已失配',
                        copy: buildWaitingResponseHelper(twoFactor),
                        meta: [startupStepLabel, '不要重复发起新一轮'],
                        buttonLabel: '干净重开 2FA',
                        behavior: 'action',
                        actionName: 'panic_reset_2fa'
                    };
                }
                if (canSubmitTwoFactorResponse(twoFactor)) {
                    return {
                        visible: true,
                        tone: 'warn',
                        badge: reasonLabel,
                        stepLabel: '提交响应码',
                        title: '继续当前 2FA 轮次',
                        copy: '当前已经进入 Challenge/Response。请直接在下方 2FA 控制台提交 Response Code，不要重新发起。',
                        meta: [startupStepLabel, '继续当前轮次'],
                        buttonLabel: '定位到响应码输入',
                        behavior: 'focus_response'
                    };
                }
                return {
                    visible: true,
                    tone: 'warn',
                    badge: reasonLabel,
                    stepLabel: '等待响应码流程',
                    title: '当前轮次仍在收口',
                    copy: buildWaitingResponseHelper(twoFactor),
                    meta: [startupStepLabel, '不要重新触发'],
                    buttonLabel: '刷新状态',
                    behavior: 'refresh'
                };
            }

            if (twoFactor.status === 'waiting_confirm') {
                return {
                    visible: true,
                    tone: 'warn',
                    badge: reasonLabel,
                    stepLabel: '手机确认',
                    title: '等待手机确认',
                    copy: '这一步只看手机通知，不要再发起新一轮。确认完成后回到这里刷新状态。',
                    meta: [startupStepLabel, '继续当前轮次'],
                    buttonLabel: '刷新状态',
                    behavior: 'refresh'
                };
            }

            if (twoFactor.status === 'triggered') {
                return {
                    visible: true,
                    tone: 'info',
                    badge: reasonLabel,
                    stepLabel: '等待验证模式',
                    title: '当前轮次已手动触发',
                    copy: '飞书按钮已经点下。现在等待手机确认，或稍后切到响应码模式。',
                    meta: [startupStepLabel, '不要重复触发'],
                    buttonLabel: '刷新状态',
                    behavior: 'refresh'
                };
            }

            if (twoFactor.status === 'requested') {
                return {
                    visible: true,
                    tone: reason === 'weekly_reauth' ? 'warn' : 'info',
                    badge: reasonLabel,
                    stepLabel: '去飞书开始',
                    title: reason === 'weekly_reauth' ? '开始本周重登验证' : '去飞书开始 2FA 验证',
                    copy: startup.operator_action || '这一步还没有真正开始验证。请去飞书点击“开始 2FA 验证”，点完后回到这里刷新状态。',
                    meta: [startupStepLabel, startup.current_blocker || '当前停在待手动触发'],
                    buttonLabel: '刷新 / 复用 2FA 卡片',
                    behavior: 'action',
                    actionName: 'reauth',
                    requestTarget: {
                        path: '/api/custom/ibkr/2fa/request',
                        body: {
                            environment: currentEnvironment,
                            reason: reason === 'weekly_reauth' ? 'weekly_reauth' : 'manual_start',
                            source: 'runtime_page_banner',
                            force_reset: true,
                            message: reason === 'weekly_reauth'
                                ? '本周重登仍等待你在飞书手动点开始验证。'
                                : '启动验证仍等待你在飞书手动点开始验证。'
                        }
                    }
                };
            }

            if (!sessionAuthenticated && !runtimeStarted && !startup.active) {
                return {
                    visible: true,
                    tone: 'info',
                    badge: '顶部主入口',
                    stepLabel: '先拉起服务',
                    title: '先启动 IBKR 服务',
                    copy: '这一步只拉起服务，不会自动触发手机 Push。启动后顶部入口会切到飞书手动验证。',
                    meta: ['启动后再进入手动验证', '不会自动往下推进'],
                    buttonLabel: '启动 IBKR 服务',
                    behavior: 'action',
                    actionName: 'start',
                    requestTarget: {
                        path: '/api/custom/ibkr/start',
                        body: {
                            environment: currentEnvironment,
                            trigger_login: false,
                            reason: 'manual_start',
                            source: 'runtime_page_banner'
                        }
                    }
                };
            }

            if (!sessionAuthenticated && (startup.active || reason === 'weekly_reauth' || reason === 'manual_start' || reason === 'panic_reset_2fa')) {
                return {
                    visible: true,
                    tone: reason === 'weekly_reauth' ? 'warn' : 'info',
                    badge: reasonLabel,
                    stepLabel: startupStepLabel,
                    title: reason === 'weekly_reauth' ? '开始本周重登验证' : '继续手动验证',
                    copy: startup.operator_action || startup.current_blocker || '请先把同一张 2FA 卡片刷到飞书，然后去飞书点击开始验证。',
                    meta: [startup.summary || '按顶部入口一步一步推进', '只有人工确认后才继续'],
                    buttonLabel: '请求 / 刷新 2FA 卡片',
                    behavior: 'action',
                    actionName: 'reauth',
                    requestTarget: {
                        path: '/api/custom/ibkr/2fa/request',
                        body: {
                            environment: currentEnvironment,
                            reason: reason === 'weekly_reauth' ? 'weekly_reauth' : 'manual_start',
                            source: 'runtime_page_banner',
                            force_reset: true,
                            message: reason === 'weekly_reauth'
                                ? '本周重登等待你在飞书手动点开始验证。'
                                : '启动验证等待你在飞书手动点开始验证。'
                        }
                    }
                };
            }

            if (sessionAuthenticated && startup.active && startup.status === 'active') {
                return {
                    visible: true,
                    tone: 'ok',
                    badge: '启动推进中',
                    stepLabel: startupStepLabel,
                    title: 'Runtime 正在继续恢复',
                    copy: startup.current_blocker || startup.summary || '当前不需要额外手动操作，等待 Runtime 完成剩余恢复与检查。',
                    meta: [startup.operator_action || '等待系统继续推进'],
                    buttonLabel: '刷新状态',
                    behavior: 'refresh'
                };
            }

            return { visible: false };
        }

        function renderAuthActionBanner(model = latestNextActionModel) {
            const banner = document.getElementById('authActionBanner');
            if (!banner) return;
            if (!model || model.visible !== true) {
                banner.className = 'auth-banner is-hidden';
                banner.innerHTML = '';
                return;
            }

            const metaHtml = Array.isArray(model.meta) && model.meta.length
                ? `<div class="auth-banner-meta">${model.meta.filter(Boolean).map((item) => `<span class="auth-banner-pill">${escapeHtml(item)}</span>`).join('')}</div>`
                : '';
            const buttonHtml = model.buttonLabel
                ? `<button class="auth-banner-btn" onclick="runPrimaryAuthAction()" ${actionPending ? 'disabled' : ''}>${escapeHtml(model.buttonLabel)}</button>`
                : '';

            banner.className = `auth-banner ${escapeHtml(model.tone || 'info')}`;
            banner.innerHTML = `
                <div class="auth-banner-main">
                    <div class="auth-banner-head">
                        <span class="auth-banner-kicker">下一步 / 手动验证</span>
                        ${model.badge ? `<span class="auth-banner-pill">${escapeHtml(model.badge)}</span>` : ''}
                        ${model.stepLabel ? `<span class="auth-banner-pill">${escapeHtml(model.stepLabel)}</span>` : ''}
                    </div>
                    <div class="auth-banner-title">${escapeHtml(model.title || '等待下一步')}</div>
                    <div class="auth-banner-copy">${escapeHtml(model.copy || '')}</div>
                    ${metaHtml}
                </div>
                <div class="auth-banner-side">
                    ${buttonHtml}
                    <div class="auth-banner-hint">每次重启后直接看这块。只有你手动触发或确认后，流程才会继续往下走。</div>
                </div>
            `;
        }

        function getTwoFactorStatusKey(twoFactorState = latestTwoFactorState) {
            return String(twoFactorState?.status || '').trim().toLowerCase();
        }

        function isTwoFactorCycleActive(twoFactorState = latestTwoFactorState) {
            return ['triggered', 'waiting_confirm', 'waiting_response'].includes(getTwoFactorStatusKey(twoFactorState));
        }

        function parseShiftedTimeMs(value, offsetMinutes) {
            const text = String(value || '').trim();
            if (!text) return 0;
            const parsed = Date.parse(text.replace(' ', 'T') + 'Z');
            if (!Number.isFinite(parsed)) return 0;
            return parsed - (Number(offsetMinutes || 0) * 60000);
        }

        function parseUsTimeMs(value) {
            return parseShiftedTimeMs(value, -4 * 60);
        }

        function deriveTwoFactorUiState(twoFactorState = latestTwoFactorState, options = {}) {
            const nowMs = Number(options.now_ms || Date.now()) || Date.now();
            const state = { ...(twoFactorState || {}) };
            const status = String(state.status || '').trim().toLowerCase();
            const responseStatus = String(state.response_status || '').trim().toLowerCase();
            const challengeCode = String(state.challenge_code || '').trim();
            const feedback = String(state.challenge_feedback || '').trim();
            const recoveryPhase = String(state.recovery_phase || '').trim().toLowerCase();
            const submittedMs = parseUsTimeMs(state.response_submitted_at);
            const rejectedMs = parseUsTimeMs(state.response_rejected_at);
            const submittedAgeMs = submittedMs > 0 ? Math.max(0, nowMs - submittedMs) : 0;
            const rejectedAgeMs = rejectedMs > 0 ? Math.max(0, nowMs - rejectedMs) : 0;
            let operatorAction = 'request_approval';
            let resetRecommended = state.reset_recommended === true;
            let resetReason = String(state.reset_reason || '').trim();

            if (recoveryPhase === 'panic_resetting') {
                operatorAction = 'panic_resetting';
            } else if (state.manual_takeover_active) {
                operatorAction = 'manual_takeover';
            } else if (status === 'waiting_response') {
                if (responseStatus === 'received') {
                    operatorAction = 'wait_browser_submit';
                } else if (responseStatus === 'submitted') {
                    operatorAction = 'wait_auth_restore';
                    if (!resetRecommended && submittedAgeMs >= CHALLENGE_RESET_RECOMMEND_MS) {
                        operatorAction = 'panic_reset';
                        resetRecommended = true;
                        resetReason = resetReason || 'submitted_no_recovery';
                    }
                } else if (responseStatus === 'gateway_rejected') {
                    operatorAction = 'retry_response_same_challenge';
                    if (!resetRecommended && rejectedAgeMs >= CHALLENGE_RESET_RECOMMEND_MS) {
                        operatorAction = 'panic_reset';
                        resetRecommended = true;
                        resetReason = resetReason || 'gateway_rejected_no_recovery';
                    }
                } else if (responseStatus === 'submit_failed') {
                    operatorAction = 'retry_response_same_challenge';
                } else {
                    operatorAction = challengeCode ? 'submit_response' : 'wait_challenge';
                }
            } else if (status === 'waiting_confirm') {
                operatorAction = 'confirm_push';
            } else if (status === 'triggered') {
                operatorAction = 'wait_for_mode';
            } else if (status === 'success') {
                operatorAction = 'none';
            } else if (status === 'timeout' || status === 'failed') {
                operatorAction = 'request_new_cycle';
            }

            state.response_status = responseStatus;
            state.challenge_feedback = feedback;
            state.operator_action = operatorAction;
            state.reset_recommended = resetRecommended;
            state.reset_reason = resetReason;
            state.response_submitted_age_sec = submittedAgeMs > 0 ? Math.round(submittedAgeMs / 1000) : 0;
            state.response_rejected_age_sec = rejectedAgeMs > 0 ? Math.round(rejectedAgeMs / 1000) : 0;
            return state;
        }

        function buildWaitingResponseHelper(twoFactorState = latestTwoFactorState) {
            const state = deriveTwoFactorUiState(twoFactorState);
            const feedback = state.challenge_feedback || 'Authentication failed';
            if (state.reset_recommended) {
                if (state.response_status === 'gateway_rejected') {
                    return 'Gateway 已拒绝当前 Response Code，且旧轮次长时间未恢复。当前旧 2FA / Session 状态很可能已失配，请直接执行“放弃当前轮次并干净重开”。';
                }
                return 'Response Code 已提交较久但 Gateway 仍未恢复认证。当前旧 2FA / Session 状态很可能已失配，请直接执行“放弃当前轮次并干净重开”。';
            }
            if (state.response_status === 'received') {
                return 'Runtime 已收到 Response Code，正在等待 compute 浏览器提交流程。此时不要重复输入，也不要触发新一轮。';
            }
            if (state.response_status === 'submitted') {
                return '浏览器已提交 Response Code，正在等待 Gateway 恢复认证。此时不要重复提交旧 Response，也不要触发新一轮。';
            }
            if (state.response_status === 'gateway_rejected') {
                return `Gateway 已拒绝当前 Response Code（${feedback}）。请核对当前 Challenge，用 IBKR App 重新生成后在这里重提。`;
            }
            if (state.response_status === 'submit_failed') {
                return '浏览器提交动作失败。请在这里重新提交当前 Challenge 的 Response Code，不要重新触发。';
            }
            return '当前已切到 Challenge/Response。仅在手机上点确认不会完成验证；请在 IBKR App 的 Two-Factor Authentication 中输入当前 Challenge，拿到 Response Code 后回到这里提交。不要重复触发新一轮。';
        }

        function canSubmitTwoFactorResponse(twoFactorState = latestTwoFactorState) {
            const state = deriveTwoFactorUiState(twoFactorState);
            return (
                state.status === 'waiting_response'
                && !!String(state.challenge_code || '').trim()
                && !state.reset_recommended
                && ['', 'gateway_rejected', 'submit_failed'].includes(state.response_status)
            );
        }

        function shouldShowTwoFactorResetCta(twoFactorState = latestTwoFactorState) {
            const state = deriveTwoFactorUiState(twoFactorState);
            return state.status === 'waiting_response' && state.reset_recommended;
        }

        function getTwoFactorActionLockReason(action, twoFactorState = latestTwoFactorState) {
            const guarded = new Set(['start', 'reauth', 'reauth_force_new', 'probe']);
            if (!guarded.has(action)) return '';

            const state = deriveTwoFactorUiState(twoFactorState);
            const status = getTwoFactorStatusKey(state);
            if (!isTwoFactorCycleActive(state)) return '';

            if (action === 'probe') {
                if (state.reset_recommended) {
                    return '当前旧 2FA / Session 状态很可能已失配，先执行“放弃当前轮次并干净重开”；此时再做静默探测只会增加判断噪音。';
                }
                return '当前已有一轮 2FA 正在进行，先等这一轮收口；此时再做静默探测只会增加判断噪音。';
            }
            if (action === 'reauth_force_new') {
                if (state.reset_recommended) {
                    return '当前旧 2FA / Session 状态很可能已失配。如要放弃当前轮次，请只使用“放弃当前轮次并干净重开”。';
                }
                return '当前已有一轮 2FA 正在进行。如要放弃当前轮次，请只使用“放弃当前轮次并干净重开”。';
            }
            if (status === 'waiting_response') {
                if (state.reset_recommended) {
                    return '当前旧 2FA / Session 状态很可能已失配，请直接执行“放弃当前轮次并干净重开”，不要重新触发。';
                }
                if (state.response_status === 'submitted') {
                    return '当前 Response Code 已提交，正在等待 Gateway 恢复认证；不要重新触发。';
                }
                if (state.response_status === 'gateway_rejected') {
                    return 'Gateway 已拒绝当前 Response Code，请先在本页按当前 Challenge 重试，不要重新触发。';
                }
                if (state.response_status === 'submit_failed') {
                    return '浏览器提交 Response Code 失败，请先在本页重试，不要重新触发。';
                }
                if (state.response_status === 'received') {
                    return 'Runtime 已收到 Response Code，正在等待浏览器提交流程；不要重新触发。';
                }
                return '当前已进入 Challenge/Response，请继续当前轮次并提交 Response Code，不要重复触发。';
            }
            if (status === 'waiting_confirm') {
                return '当前正在等待手机确认，请继续当前轮次，不要重复触发。';
            }
            return '当前已有一轮 2FA 正在进行，请继续当前轮次，不要重复触发。';
        }

        function syncActionLocks() {
            document.querySelectorAll('.action-btn').forEach((button) => {
                const action = String(button?.dataset?.action || '').trim();
                const lockReason = actionPending ? '' : getTwoFactorActionLockReason(action);
                button.disabled = actionPending || Boolean(lockReason);
                button.title = actionPending
                    ? '动作执行中，请稍候。'
                    : (lockReason || '');
            });
        }

        function summarizeAction(action, payload) {
            if (!payload || typeof payload !== 'object') {
                return `${action} 已执行。`;
            }
            if (action === 'start') {
                if (payload.message && payload.trigger_login === false) {
                    return 'IBKR 服务已开始拉起；不会自动触发手机 Push，下一步请看顶部“下一步 / 手动验证”入口。';
                }
                return payload.message || 'IBKR 服务启动中。';
            }
            if (action === 'reauth' || action === 'reauth_force_new') {
                if (payload.message) return payload.message;
                const actionState = deriveTwoFactorUiState(payload?.state || latestTwoFactorState);
                const actionStatus = getTwoFactorStatusKey(actionState);
                if (payload?.state?.runtime_authenticated && payload?.state?.gateway_reachable && Number(payload?.state?.gateway_status_code || 0) !== 401) {
                    return '当前 Gateway 会话已认证，无需再次确认。';
                }
                if (actionStatus === 'waiting_response') {
                    if (actionState.reset_recommended) {
                        return '当前旧 2FA / Session 状态很可能已失配，请打开 Runtime 页面执行“放弃当前轮次并干净重开”，不要重复触发。';
                    }
                    if (actionState.response_status === 'submitted') {
                        return '当前 Response Code 已提交，正在等待 Gateway 恢复认证；请继续当前轮次，不要重复触发。';
                    }
                    if (actionState.response_status === 'gateway_rejected') {
                        return 'Gateway 已拒绝当前 Response Code，请打开 Runtime 页面核对当前 Challenge 后重新提交，不要重复触发。';
                    }
                    if (actionState.response_status === 'submit_failed') {
                        return '浏览器提交 Response Code 失败，请打开 Runtime 页面重试当前 Challenge，不要重复触发。';
                    }
                    if (actionState.response_status === 'received') {
                        return 'Runtime 已收到 Response Code，正在等待浏览器提交流程；请继续当前轮次，不要重复触发。';
                    }
                    return '当前已进入 Challenge/Response，请继续当前轮次并在 Runtime 页面提交 Response Code，不要重复触发。';
                }
                if (isTwoFactorCycleActive(actionState)) {
                    return '当前已有一轮 2FA 正在进行，请继续当前轮次，不要重复触发。';
                }
                return action === 'reauth_force_new'
                    ? '已开始新一轮 2FA，请立即查看手机通知或飞书卡片。'
                    : '已请求 2FA 卡片，请在飞书点击按钮触发验证。';
            }
            if (action === 'gateway_start' || action === 'gateway_stop' || action === 'gateway_restart') {
                if (payload.message) return payload.message;
                if (action === 'gateway_start') return 'Gateway 动作已执行。';
                if (action === 'gateway_stop') return 'Gateway 停止动作已执行。';
                return 'Gateway 重启动作已执行。';
            }
            if (action === 'takeover_on' || action === 'takeover_off' || action === 'probe' || action === 'panic_reset_2fa') {
                if (payload.message) return payload.message;
                if (action === 'takeover_on') return '已开启人工接管，系统会继续静默探测。';
                if (action === 'takeover_off') return '已结束人工接管，并恢复静默探测。';
                if (action === 'probe') return '已触发静默探测。';
                return '已全量清空旧状态并重新拉起新的验证周期。';
            }
            if (action === 'compute' || action === 'scan' || action === 'recompute') {
                const pieces = [];
                if (payload.environments) pieces.push(`env=${payload.environments.join(',')}`);
                if (payload.processed != null) pieces.push(`processed=${payload.processed}`);
                if (payload.signals != null) pieces.push(`signals=${payload.signals}`);
                if (payload.errors != null) pieces.push(`errors=${payload.errors}`);
                if (payload.candidates != null) pieces.push(`candidates=${payload.candidates}`);
                return `${action} 返回：${pieces.join(' · ') || 'ok'}`;
            }
            if (action.startsWith('emergency_')) {
                const updatedKeys = Array.isArray(payload.updated)
                    ? payload.updated.map(item => item.key).filter(Boolean).join(',')
                    : '';
                const stopState = payload.runtime_stop?.ok === false ? 'runtime_stop_failed' : 'runtime_stop_requested';
                return `紧急动作完成：${payload.action || action} · ${updatedKeys || 'no_config_change'} · ${stopState}`;
            }
            return `${action} 返回：${payload.message || payload.status || (payload.ok === false ? 'failed' : 'ok')}`;
        }

        function setActionState(isPending) {
            actionPending = isPending;
            syncActionLocks();
            renderAuthActionBanner(latestNextActionModel);
        }

        function renderMetricCards(summary, health, status, twoFactorState, latestBar) {
            const today = summary?.today || {};
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const realtimeMetrics = deriveRealtimeMetrics(status, latestBar);
            const warmup = normalizeWarmup(status);
            const runtimeStatus = getIbkrRuntimeStatusCardModel(status);
            const twoFactorStatus = String(twoFactorState?.status || '').trim().toUpperCase() || '--';
            const cards = [
                {
                    label: 'Session Auth',
                    value: runtimeStatus.authenticated ? 'AUTHED' : (runtimeStatus.started ? 'WAITING' : 'STOPPED'),
                    copy: `2FA ${twoFactorStatus} · gateway ${runtimeStatus.gatewayActive ? 'active' : 'offline'}`
                },
                {
                    label: 'Ready Engines',
                    value: `${status?.ready_engines || 0}/${status?.total_engines || 0}`,
                    copy: `compute count ${status?.compute_count || 0}`
                },
                {
                    label: 'Warmup Gate',
                    value: warmup.gate_open ? 'OPEN' : String(warmup.phase || 'idle').toUpperCase(),
                    copy: `${warmup.ready_trade_symbols}/${warmup.trade_symbols_total} trade · blocking ${warmup.blocking_pending_symbols_total}`
                },
                {
                    label: 'Monitor Coverage',
                    value: `${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}`,
                    copy: `pending ${warmup.monitor_pending_symbols_total} · not gating`
                },
                {
                    label: 'Today Signals',
                    value: formatCompactNumber(today.ibkr_signals || 0),
                    copy: `orders ${today.orders || 0} · events ${today.events || 0}`
                },
                {
                    label: 'Today Bars',
                    value: formatCompactNumber(today.ibkr_bars || 0),
                    copy: `targets ${today.ibkr_targets || 0}`
                },
                {
                    label: 'Compute Status',
                    value: String(computeHealth.status || 'unknown').toUpperCase(),
                    copy: `last compute ${formatAgo(computeHealth.last_compute)}`
                },
                {
                    label: 'Data Freshness',
                    value: dataHealth.last_bar_age_min != null ? `${dataHealth.last_bar_age_min}m` : '--',
                    copy: dataHealth.last_bar_time_ms ? `${dataHealth.last_symbol || 'n/a'} · ${dataHealth.last_bar_label}` : 'latest n/a'
                },
                {
                    label: 'Close Delay',
                    value: formatSecondsLabel(realtimeMetrics.close_delay_s),
                    copy: realtimeMetrics.last_bar_close ? `last close ${formatTimeLabel(realtimeMetrics.last_bar_close)}` : 'last close --'
                },
                {
                    label: 'Compute After Close',
                    value: formatSecondsLabel(realtimeMetrics.compute_after_close_s),
                    copy: realtimeMetrics.last_compute_run ? `last run ${formatTimeLabel(realtimeMetrics.last_compute_run)}` : 'last run --'
                },
                {
                    label: 'Active Tick Lag',
                    value: formatSecondsLabel(realtimeMetrics.active_tick_lag_s),
                    copy: `${realtimeMetrics.active_symbol_count || 0} active symbols`
                }
            ];
            document.getElementById('metricGrid').innerHTML = cards.map((card) => `
                <div class="metric-card">
                    <div class="metric-label">${escapeHtml(card.label)}</div>
                    <div class="metric-value">${escapeHtml(card.value)}</div>
                    <div class="metric-copy">${escapeHtml(card.copy)}</div>
                </div>
            `).join('');
        }

        function renderOpsGrid(summary, status, twoFactorState, startupState, latestBar, latestIndicator, latestSignal) {
            const dataHealth = deriveDataHealth(latestBar);
            const realtimeMetrics = deriveRealtimeMetrics(status, latestBar);
            const realtimeState = deriveRealtimeComputeState(status);
            const warmup = normalizeWarmup(status);
            const warmupElapsedS = getWarmupElapsedSeconds(warmup);
            const canonical = status?.canonical_5m || {};
            const latestBarExtra = getExtraObject(latestBar);
            const latestIndicatorExtra = getExtraObject(latestIndicator);
            const latestSignalExtra = getExtraObject(latestSignal);
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const runtimeStatus = getIbkrRuntimeStatusCardModel(status);
            const gatewayActive = runtimeStatus.gatewayActive;
            const runtimeStarted = runtimeStatus.started;
            const sessionAuthenticated = runtimeStatus.authenticated;
            const readyEngines = Number(status?.ready_engines || 0) || 0;
            const totalEngines = Number(status?.total_engines || 0) || 0;
            const twoFactorStatus = String(twoFactor?.status || '').trim().toLowerCase();
            const challengeCode = String(twoFactor?.challenge_code || '').trim();
            const recoveryPhase = String(twoFactor?.recovery_phase || '').trim().toLowerCase();
            const runtimeMismatch = getRuntimeEnvironmentMismatch(status, twoFactor);

            const blocker = { tone: 'info', kicker: 'Primary Blocker', title: '运行链路可控', copy: '当前没有硬阻塞，继续观察 bars → indicators → signals 是否连续刷新。' };
            if (runtimeMismatch) {
                blocker.tone = 'error';
                blocker.title = `Runtime 环境错配 · ${getEnvironmentLabel(runtimeMismatch.actual)}`;
                blocker.copy = runtimeMismatch.message;
            } else if (!sessionAuthenticated && startup.active && startup.current_step === 'manual_trigger') {
                blocker.tone = startup.reason === 'weekly_reauth' ? 'warn' : 'info';
                blocker.title = startup.reason === 'weekly_reauth' ? '本周重登等待手动触发' : '启动流程等待手动触发';
                blocker.copy = startup.operator_action || '现在只需要去飞书点击“开始 2FA 验证”，系统不会自动往下继续。';
            } else if (!sessionAuthenticated && startup.active && startup.current_step === 'manual_confirm') {
                blocker.tone = 'warn';
                blocker.title = '等待当前 2FA 轮次完成';
                blocker.copy = startup.current_blocker || '请继续当前轮次的手机确认或响应码提交流程，不要重复触发。';
            } else if (recoveryPhase === 'panic_resetting') {
                blocker.tone = 'warn';
                blocker.title = '正在全量清空旧 2FA 状态';
                blocker.copy = '系统正在停止旧 runtime、清理 cookie 并准备拉起一轮新的干净验证；这期间旧 challenge / response 不再可信。';
            } else if (recoveryPhase === 'manual_takeover') {
                blocker.tone = 'warn';
                blocker.title = '人工接管中';
                blocker.copy = '当前视为你正在真实账户里确认挂单；系统不会把这轮直接判死，但后台仍会持续探测认证是否已恢复。';
            } else if (!gatewayActive) {
                blocker.tone = 'error';
                blocker.title = 'Gateway 当前离线';
                blocker.copy = '先恢复网关或重启 IBKR 服务，否则 2FA、bars、订单和信号链路都会停住。';
            } else if (!runtimeStarted) {
                blocker.tone = 'warn';
                blocker.title = 'Runtime 当前未启动';
                blocker.copy = '这次更像是 runtime service 没有拉起，不是单纯 session pending；重启 compute 后如果没有自动恢复，需要重新触发一次 IBKR start / 2FA。';
            } else if (twoFactorStatus === 'waiting_response') {
                if (twoFactor.reset_recommended) {
                    blocker.tone = 'error';
                    blocker.title = '旧 2FA / Session 状态已失配';
                    blocker.copy = '当前旧 Challenge / Response 状态已经不可信。不要继续围绕旧轮次重试，请直接执行“放弃当前轮次并干净重开”。';
                } else if (twoFactor.response_status === 'gateway_rejected') {
                    blocker.tone = 'error';
                    blocker.title = `Gateway 已拒绝当前 Response${challengeCode ? ` · ${challengeCode}` : ''}`;
                    blocker.copy = buildWaitingResponseHelper(twoFactor);
                } else if (twoFactor.response_status === 'submit_failed') {
                    blocker.tone = 'error';
                    blocker.title = `Response Code 浏览器提交失败${challengeCode ? ` · ${challengeCode}` : ''}`;
                    blocker.copy = buildWaitingResponseHelper(twoFactor);
                } else if (twoFactor.response_status === 'submitted') {
                    blocker.tone = 'warn';
                    blocker.title = `Response Code 已提交${challengeCode ? ` · ${challengeCode}` : ''}`;
                    blocker.copy = buildWaitingResponseHelper(twoFactor);
                } else if (twoFactor.response_status === 'received') {
                    blocker.tone = 'warn';
                    blocker.title = `Response Code 已收到${challengeCode ? ` · ${challengeCode}` : ''}`;
                    blocker.copy = buildWaitingResponseHelper(twoFactor);
                } else {
                    blocker.tone = 'warn';
                    blocker.title = `等待 Response Code${challengeCode ? ` · ${challengeCode}` : ''}`;
                    blocker.copy = buildWaitingResponseHelper(twoFactor);
                }
            } else if (!sessionAuthenticated) {
                blocker.tone = twoFactorStatus === 'waiting_confirm' ? 'warn' : 'error';
                blocker.title = twoFactorStatus === 'waiting_confirm' ? '等待手机确认 2FA' : 'Session 仍未认证';
                blocker.copy = twoFactorStatus === 'waiting_confirm'
                    ? '浏览器已经停在手机确认阶段，确认完成后会自动继续；请继续当前轮次，不要重复点重新验证 / 开始新一轮。'
                    : '当前还没有拿到可用 Session，新的 bars / backfill / 订单链路都不会继续刷新。';
            } else if (warmup.phase === 'failed') {
                blocker.tone = 'error';
                blocker.title = 'Warmup 执行失败';
                blocker.copy = warmup.last_error || '启动后的预热链路失败，交易闸门保持关闭，需要检查回填与 compute 日志。';
            } else if (warmup.phase === 'pending' || warmup.phase === 'running') {
                blocker.tone = 'warn';
                blocker.title = `Startup Warmup ${warmup.ready_trade_symbols}/${warmup.trade_symbols_total}`;
                blocker.copy = `正在为 ${warmup.required_interval} 建立交易预热，blocking ${warmup.blocking_pending_symbols_total} · monitor ${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}。`;
            } else if (warmup.trade_symbols_total > 0 && !warmup.gate_open) {
                blocker.tone = 'warn';
                blocker.title = `Trading Gate Closed · ${warmup.ready_trade_symbols}/${warmup.trade_symbols_total}`;
                blocker.copy = `目标池还没有全部预热完成，pending: ${(warmup.blocking_pending_symbols || []).slice(0, 4).join(', ') || 'n/a'}。`;
            } else if (dataHealth.last_bar_age_min != null && dataHealth.last_bar_age_min > 30) {
                blocker.tone = 'error';
                blocker.title = `bars 停在 ${dataHealth.last_bar_label}`;
                blocker.copy = `最新 live bar 已经落后 ${dataHealth.last_bar_age_min} 分钟，需要优先检查订阅、写入器和行情桥。`;
            } else if ((Number(canonical.pending_symbols_total || 0) || 0) > 0 || (Number(canonical.lag_s || 0) || 0) > 0) {
                blocker.tone = 'warn';
                blocker.title = `Canonical 5m 仍在补齐 · ${String(canonical.last_completed_bucket_us || '--')}`;
                blocker.copy = `due ${String(canonical.last_due_bucket_us || '--')} · completed ${String(canonical.last_completed_bucket_us || '--')} · pending ${Number(canonical.pending_symbols_total || 0) || 0}。`;
            } else if (realtimeState.phase === 'stalled') {
                blocker.tone = 'error';
                blocker.title = realtimeState.title;
                blocker.copy = `bar 已写到 ${String(canonical.last_completed_bucket_us || '--')}，但 realtime compute 没有完成：${realtimeState.summary}。`;
            } else if (realtimeState.phase === 'running') {
                blocker.tone = realtimeState.tone;
                blocker.title = realtimeState.title;
                blocker.copy = `bar 已写到 ${String(canonical.last_completed_bucket_us || '--')}，指标仍在追赶：${realtimeState.summary}。`;
            } else if (realtimeState.phase === 'queued') {
                blocker.tone = 'warn';
                blocker.title = realtimeState.title;
                blocker.copy = `canonical bar 已写入，但 compute 还在排队：${realtimeState.summary}。`;
            } else if (totalEngines && readyEngines < totalEngines) {
                blocker.tone = 'warn';
                blocker.title = `Warmup 未完成 ${readyEngines}/${totalEngines}`;
                blocker.copy = '指标链路还在预热，ready engines 没起来前，signals 偏少通常是正常现象。';
            }

            const dataChain = {
                tone: dataHealth.last_bar_age_min != null && dataHealth.last_bar_age_min > 30 ? 'warn' : 'info',
                kicker: 'Data Chain',
                title: latestBar
                    ? `${latestBar.symbol || '--'} ${latestBar.interval || '--'} · ${dataHealth.last_bar_age_min || 0}m`
                    : '当前没有 live bars',
                copy: [
                    latestBar ? `bar ${getRecordBarLabel(latestBar)}` : 'bars missing',
                    latestBar ? `write ${String(getComputedTimeLabel(latestBar)).slice(0, 19)}` : 'write --',
                    realtimeMetrics.last_bar_close ? `close ${formatTimeLabel(realtimeMetrics.last_bar_close)}` : 'close --',
                    realtimeMetrics.close_delay_s != null ? `close delay ${formatSecondsLabel(realtimeMetrics.close_delay_s)}` : '',
                    realtimeMetrics.compute_after_close_s != null ? `compute ${formatSecondsLabel(realtimeMetrics.compute_after_close_s)}` : '',
                    canonical.last_completed_bucket_us ? `canonical ${String(canonical.last_completed_bucket_us)}` : '',
                    realtimeState.phase !== 'idle' ? `compute ${realtimeState.phase} ${realtimeState.phase === 'running' ? formatSecondsLabel(status?.realtime_compute?.inflight_age_s) : ''}`.trim() : 'compute ready',
                    latestBar && !latestBarExtra.computed_at_us && !latestBarExtra.computed_at_cn ? 'bar extra 缺失' : '',
                    `warmup ${warmup.gate_open ? 'gate open' : `${warmup.phase || 'idle'} ${warmup.ready_trade_symbols}/${warmup.trade_symbols_total}`}${warmupElapsedS != null ? ` · ${formatDurationCompact(warmupElapsedS)}` : ''}`,
                    latestIndicator ? `indicator ${latestIndicator.symbol || '--'} ${latestIndicator.interval || '--'} · ${String(getComputedTimeLabel(latestIndicator)).slice(0, 19)}` : 'indicator missing',
                    latestSignal ? `signal ${latestSignal.symbol || '--'} ${String(latestSignal.direction || '--').toUpperCase()} · ${latestSignal.status || '--'}` : 'signal 暂无',
                ].filter(Boolean).join(' · ')
            };

            const shortcuts = {
                tone: 'ok',
                kicker: 'Quick View',
                title: '把排查入口放在一层',
                copy: [
                    `bars ${formatCompactNumber(summary?.today?.ibkr_bars || 0)}`,
                    `indicators ${formatCompactNumber(summary?.today?.ibkr_indicators || 0)}`,
                    `signals ${formatCompactNumber(summary?.today?.ibkr_signals || 0)}`,
                    latestIndicatorExtra.computed_at_us ? `latest calc ${String(getComputedTimeLabel(latestIndicator)).slice(0, 19)}` : '',
                    latestSignalExtra.computed_at_us ? `signal calc ${String(getComputedTimeLabel(latestSignal)).slice(0, 19)}` : '',
                ].filter(Boolean).join(' · '),
                links: [
                    { path: '/ibkr_signals.html', label: '看信号' },
                    { path: '/ibkr_indicators.html', label: '看指标' },
                    { path: '/orders.html', label: '看订单' },
                    { path: '/ibkr_config.html', label: '改配置', options: { allowGlobal: true, environment: currentEnvironment } },
                ]
            };

            const cards = [blocker, dataChain, shortcuts];
            document.getElementById('opsGrid').innerHTML = cards.map((card) => `
                <div class="ops-card ${escapeHtml(card.tone || 'info')}">
                    <div class="ops-kicker">${escapeHtml(card.kicker || '--')}</div>
                    <div class="ops-title">${escapeHtml(card.title || '--')}</div>
                    <div class="ops-copy">${escapeHtml(card.copy || '--')}</div>
                    ${Array.isArray(card.links) && card.links.length ? `
                        <div class="ops-links">
                            ${card.links.map((link) => `
                                <a class="ops-link" href="${buildPageUrl(link.path, {}, link.options || { environment: currentEnvironment })}">${escapeHtml(link.label)}</a>
                            `).join('')}
                        </div>
                    ` : ''}
                </div>
            `).join('');
        }

        function renderHero(summary, health, status, runtimeConfig, twoFactorState, startupState, latestBar) {
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const warmup = normalizeWarmup(status);
            const startup = normalizeStartupUiState(startupState);
            const dataStatus = dataHealth.status || 'no_data';
            const computeStatus = computeHealth.status || 'unknown';
            const runtimeStatus = getIbkrRuntimeStatusCardModel(status);
            const gatewayRunning = runtimeStatus.gatewayActive;
            const sessionAuthenticated = runtimeStatus.authenticated;
            const twoFactorStatus = String(twoFactorState?.status || '').trim().toLowerCase() || 'idle';
            latestNextActionModel = deriveNextAuthActionModel(status, twoFactorState, startup);
            renderAuthActionBanner(latestNextActionModel);
            const chips = [
                { label: `Compute ${String(computeStatus).toUpperCase()}`, tone: chipTone(computeStatus) },
                { label: `Data ${String(dataStatus).toUpperCase()}`, tone: chipTone(dataStatus) },
                { label: `Gateway ${gatewayRunning ? 'ACTIVE' : 'OFFLINE'}`, tone: gatewayRunning ? 'chip-ok' : 'chip-error' },
                { label: `Session ${sessionAuthenticated ? 'AUTHED' : 'PENDING'}`, tone: sessionAuthenticated ? 'chip-ok' : 'chip-warn' },
                { label: `Warmup ${warmup.gate_open ? 'READY' : String(warmup.phase || 'idle').toUpperCase()}`, tone: warmup.gate_open ? 'chip-ok' : chipTone(warmup.phase) },
                { label: `2FA ${twoFactorStatus.toUpperCase()}`, tone: sessionAuthenticated ? 'chip-ok' : chipTone(twoFactorStatus) },
                ...(startup.active ? [{ label: `Flow ${getManualAuthReasonLabel(startup.reason)}`, tone: chipTone(startup.status || 'active') }] : []),
                { label: `Trading ${summary?.ibkr_trading_enabled ? 'ON' : 'OFF'}`, tone: summary?.ibkr_trading_enabled ? 'chip-ok' : 'chip-error' },
                { label: `Compute ${summary?.compute_enabled ? 'ON' : 'OFF'}`, tone: summary?.compute_enabled ? 'chip-ok' : 'chip-error' },
                { label: `Active Env ${String(currentEnvironment).toUpperCase()}`, tone: 'chip-muted' }
            ];
            document.getElementById('heroBadges').innerHTML = chips.map((chip) => `
                <span class="status-chip ${chip.tone}"><span class="dot" style="background:currentColor"></span>${escapeHtml(chip.label)}</span>
            `).join('');

            const computeBase = runtimeConfig.find((item) => item.key === 'ibkr_compute_public_url')?.value
                || summary?.config?.ibkr_compute_public_url
                || 'https://qc.lzw-glory.top';
            document.getElementById('computeBaseInfo').textContent = `compute base: ${computeBase}`;
            const twoFactorMode = String(twoFactorState?.mode || '').trim().toLowerCase();
            const recoveryPhase = String(twoFactorState?.recovery_phase || '').trim().toLowerCase();
            const runtimeStarted = runtimeStatus.started;
            const runtimeMismatch = getRuntimeEnvironmentMismatch(status, twoFactorState);
            const keepCurrentCycleHint = isTwoFactorCycleActive(twoFactorState) ? ' · keep current cycle' : '';
            const authSummary = sessionAuthenticated
                ? (runtimeMismatch
                    ? `runtime mismatch · actual ${String(runtimeMismatch.actual || '--').toUpperCase()}`
                    : `session authenticated · ${startup.active ? `${getStartupCurrentStepLabel(startup) || 'startup active'}` : `warmup ${warmup.gate_open ? 'gate open' : `${warmup.phase || 'idle'} ${warmup.ready_trade_symbols}/${warmup.trade_symbols_total}`}`}`)
                : !runtimeStarted
                    ? 'runtime stopped · waiting manual start'
                : (runtimeMismatch
                    ? `runtime mismatch · actual ${String(runtimeMismatch.actual || '--').toUpperCase()}`
                    : `${getManualAuthReasonLabel(getEffectiveManualAuthReason(status, twoFactorState, startup))} · 2FA ${twoFactorStatus}${twoFactorMode ? ` · ${twoFactorMode}` : ''}${recoveryPhase ? ` · ${recoveryPhase}` : ''}${twoFactorState?.last_request_at ? ` · requested ${formatTimeLabel(twoFactorState.last_request_at)}` : ''}${keepCurrentCycleHint}`);
            document.getElementById('authInfo').textContent = startup.startup_label
                ? `${authSummary} · ${startup.startup_label}`
                : authSummary;
            document.getElementById('refreshInfo').textContent = `更新于 ${new Date().toLocaleTimeString()}`;
            const startupStrategyEl = document.getElementById('startupStrategyInfo');
            if (startupStrategyEl) {
                startupStrategyEl.textContent = `启动策略：${formatStartupStrategySummary(status)}`;
            }
        }

        function renderRuntimeDetail(summary, health, status, twoFactorState, startupState, latestBar, latestIndicator, latestSignal) {
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const realtimeMetrics = deriveRealtimeMetrics(status, latestBar);
            const realtimeState = deriveRealtimeComputeState(status);
            const warmup = normalizeWarmup(status);
            const warmupElapsedS = getWarmupElapsedSeconds(warmup);
            const canonical = status?.canonical_5m || {};
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const startupStrategy = getStartupStrategy(status);
            const autoRestoreGuard = status?.auto_restore_guard || {};
            const sessionAuthenticated = Boolean(status?.session?.authenticated);
            const latestBarWrite = latestBar ? String(getComputedTimeLabel(latestBar)).slice(0, 19) : '--';
            const latestIndicatorCalc = latestIndicator ? String(getComputedTimeLabel(latestIndicator)).slice(0, 19) : '--';
            const latestSignalTime = latestSignal ? String((latestSignal.us_time || latestSignal.created || '--')).slice(0, 19) : '--';
            const rows = [
                ['Gateway', status?.gateway?.running || status?.gateway?.reachable ? 'ACTIVE' : 'OFFLINE'],
                ['Gateway Reachable', status?.gateway?.reachable ? 'YES' : 'NO'],
                ['Gateway Manager', String(status?.gateway?.managed_by || '--').toUpperCase()],
                ['Gateway PID', String(status?.gateway?.pid || '--')],
                ['Gateway Uptime', formatSecondsLabel(status?.gateway?.uptime_s)],
                ['Session', sessionAuthenticated ? 'AUTHENTICATED' : 'WAITING_FOR_2FA'],
                ['2FA Status', String(twoFactor?.status || '--').toUpperCase()],
                ['Recovery Phase', String(twoFactor?.recovery_phase || '--').toUpperCase()],
                ['Interruption Kind', String(twoFactor?.interruption_kind || '--')],
                ['Manual Takeover', twoFactor?.manual_takeover_active ? 'YES' : 'NO'],
                ['Manual Takeover Until', formatTimeLabel(twoFactor?.manual_takeover_until)],
                ['Probe Result', String(twoFactor?.probe_result || '--')],
                ['Probe Attempts', String(twoFactor?.probe_attempts || 0)],
                ['Probe Last Checked', formatTimeLabel(twoFactor?.probe_last_checked_at)],
                ['Last Runtime Auth', formatTimeLabel(twoFactor?.last_runtime_authenticated_at)],
                ['2FA Mode', String(twoFactor?.mode || '--').toUpperCase()],
                ['Challenge', String(twoFactor?.challenge_code || '--')],
                ['Response Status', String(twoFactor?.response_status || '--').toUpperCase()],
                ['Gateway Feedback', String(twoFactor?.challenge_feedback || '--')],
                ['Response Received', formatTimeLabel(twoFactor?.response_received_at)],
                ['Response Submitted', formatTimeLabel(twoFactor?.response_submitted_at)],
                ['Response Rejected', formatTimeLabel(twoFactor?.response_rejected_at)],
                ['Reset Recommended', twoFactor?.reset_recommended ? 'YES' : 'NO'],
                ['Reset Reason', String(twoFactor?.reset_reason || '--')],
                ['Operator Action', String(twoFactor?.operator_action || '--')],
                ['Manual Auth Reason', getManualAuthReasonLabel(getEffectiveManualAuthReason(status, twoFactor, startup))],
                ['Startup Flow', startup.active ? 'ACTIVE' : String(startup.status || '--').toUpperCase()],
                ['Startup Label', String(startup?.startup_label || '--')],
                ['Startup Cycle', String(startup?.cycle_id || '--')],
                ['Startup Step', getStartupCurrentStepLabel(startup) || '--'],
                ['Startup Blocker', String(startup?.current_blocker || '--')],
                ['Startup Next Action', String(startup?.operator_action || '--')],
                ['Startup Chat', String(startup?.startup_chat_id || '--')],
                ['Startup Card Delivery', String(startup?.last_delivery_mode || '--').toUpperCase()],
                ['Startup Strategy', formatStartupStrategySummary(status)],
                ['Manual Start Policy', formatStartupModeLabel(startupStrategy.manual_start_mode)],
                ['Weekly Reauth Policy', formatStartupModeLabel(startupStrategy.weekly_reauth_mode)],
                ['Gateway Restart Policy', formatStartupModeLabel(startupStrategy.manual_gateway_restart_mode)],
                ['Server Boot Policy', formatStartupModeLabel(startupStrategy.server_boot_mode)],
                ['Server Boot Card', startupStrategy.server_boot_publish_startup_card ? 'YES' : 'NO'],
                ['Fresh Cycle 2FA', startupStrategy.fresh_cycle_requires_manual_2fa ? 'MANUAL' : 'AUTO'],
                ['Auto-Restore Guard', autoRestoreGuard?.blocked ? 'BLOCKED' : 'ALLOWED'],
                ['Guard Reason', Array.isArray(autoRestoreGuard?.reasons) && autoRestoreGuard.reasons.length ? autoRestoreGuard.reasons.join(' | ') : '--'],
                ['2FA Request', formatTimeLabel(twoFactor?.last_request_at)],
                ['2FA Result', formatTimeLabel(twoFactor?.result_at)],
                ['Last Compute', formatTimeLabel(computeHealth.last_compute)],
                ['Last Scan', formatTimeLabel(computeHealth.last_scan)],
                ['Compute Count', String(status?.compute_count || 0)],
                ['Error Count', String(computeHealth.error_count || 0)],
                ['Uptime', `${Math.round(Number(computeHealth.uptime_s || 0) / 60)} min`],
                ['Backfill Written', String(status?.data_backfill?.total_backfilled || 0)],
                ['Canonical Due Bucket', String(canonical?.last_due_bucket_us || '--')],
                ['Canonical Completed Bucket', String(canonical?.last_completed_bucket_us || '--')],
                ['Canonical Lag', formatSecondsLabel(canonical?.lag_s)],
                ['Canonical Written Bars', String(canonical?.last_written_bars || 0)],
                ['Canonical Pending', String(canonical?.pending_symbols_total || 0)],
                ['Close Compute Runs', String(status?.realtime_compute?.runs || 0)],
                ['Close Compute State', String(realtimeState?.phase || '--').toUpperCase()],
                ['Close Compute Queue', String(status?.realtime_compute?.queue_size || 0)],
                ['Close Compute Inflight', status?.realtime_compute?.inflight ? 'YES' : 'NO'],
                ['Close Compute Started', formatTimeLabel(status?.realtime_compute?.last_started)],
                ['Close Compute Age', formatSecondsLabel(status?.realtime_compute?.inflight_age_s)],
                ['Close Compute Last Elapsed', formatSecondsLabel(status?.realtime_compute?.last_elapsed_s)],
                ['Close Compute Threshold', formatSecondsLabel(status?.realtime_compute?.inflight_timeout_threshold_s)],
                ['Close Compute Stall', status?.realtime_compute?.stalled ? 'YES' : 'NO'],
                ['Close Compute Stall Reason', String(status?.realtime_compute?.stall_reason || '--')],
                ['Close Compute Processed', String(status?.realtime_compute?.last_processed || status?.realtime_compute?.last_result?.processed || 0)],
                ['Close Compute Errors', String(status?.realtime_compute?.last_errors || status?.realtime_compute?.last_result?.errors || 0)],
                ['Close Compute Signals', String(status?.realtime_compute?.last_signals || status?.realtime_compute?.last_result?.signals || 0)],
                ['Last Bar Close', formatTimeLabel(status?.realtime_compute?.last_bar_close)],
                ['Close Compute Last', formatTimeLabel(status?.realtime_compute?.last_run)],
                ['Close Delay', formatSecondsLabel(realtimeMetrics.close_delay_s)],
                ['Compute After Close', formatSecondsLabel(realtimeMetrics.compute_after_close_s)],
                ['Active Tick Lag', formatSecondsLabel(realtimeMetrics.active_tick_lag_s)],
                ['Warmup Phase', String(warmup.phase || '--').toUpperCase()],
                ['Trading Gate', warmup.gate_open ? 'OPEN' : 'CLOSED'],
                ['Warmup Reason', String(warmup.gate_reason || '--')],
                ['Warmup Trade', `${warmup.ready_trade_symbols}/${warmup.trade_symbols_total}`],
                ['Warmup Blocking', String(warmup.blocking_pending_symbols_total || 0)],
                ['Warmup Monitor', `${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}`],
                ['Monitor Pending', String(warmup.monitor_pending_symbols_total || 0)],
                ['Warmup Start', formatTimeLabel(warmup.started_at)],
                ['Warmup Finish', formatTimeLabel(warmup.finished_at)],
                ['Warmup Elapsed', formatDurationCompact(warmupElapsedS)],
                ['Market Date', String(status?.market_universe?.market_date || '--')],
                ['Last Daily Reset', formatTimeLabel(status?.market_universe?.last_daily_reset)],
                ['Watchlist Pool', String(status?.market_universe?.watchlist_pool_count || 0)],
                ['Today Targets', String(status?.market_universe?.active_target_count || 0)],
                ['Trade Targets', Array.isArray(status?.market_universe?.active_trade_symbols) ? status.market_universe.active_trade_symbols.join(', ') || '--' : '--'],
                ['Monitor Symbols', Array.isArray(warmup.monitor_symbols) ? warmup.monitor_symbols.join(', ') || '--' : '--'],
                ['Target Date', String(status?.market_universe?.active_target_date || '--')],
                ['Last Target Refresh', formatTimeLabel(status?.market_universe?.last_target_refresh)],
                ['Active Repair Every', `${Number(status?.market_universe?.active_repair_interval_min || 0) || 0}m`],
                ['Last Active Repair', formatTimeLabel(status?.market_universe?.last_active_repair)],
                ['Active Repair Symbols', Array.isArray(status?.market_universe?.last_active_repair_symbols) ? status.market_universe.last_active_repair_symbols.join(', ') || '--' : '--'],
                ['Active Repair Reasons', status?.market_universe?.last_active_repair_reasons ? Object.entries(status.market_universe.last_active_repair_reasons).map(([symbol, reason]) => `${symbol}: ${reason}`).join(' | ') || '--' : '--'],
                ['Pool Backfill Every', `${Number(status?.market_universe?.watchlist_backfill_interval_min || 0) || 0}m`],
                ['Last Pool Backfill', formatTimeLabel(status?.market_universe?.last_watchlist_backfill)],
                ['Last Bar Time', dataHealth.last_bar_label || '--'],
                ['Bar Write Time', latestBarWrite],
                ['Latest Indicator Calc', latestIndicatorCalc],
                ['Latest Signal Time', latestSignalTime],
                ['Default Envs', Array.isArray(status?.default_environments) ? status.default_environments.join(', ') : '--'],
                ['Supported Envs', Array.isArray(status?.supported_environments) ? status.supported_environments.join(', ') : '--'],
                ['Bars Latest', dataHealth.last_symbol ? `${dataHealth.last_symbol} · ${dataHealth.last_bar_age_min || 0}m` : '--']
            ];
            document.getElementById('runtimeDetail').innerHTML = `<div class="detail-list">${rows.map(([key, value]) => `
                <div class="detail-row">
                    <div class="detail-key">${escapeHtml(key)}</div>
                    <div class="detail-value">${escapeHtml(value)}</div>
                </div>
            `).join('')}</div>`;
        }

        function renderTwoFactorPanel(twoFactorState) {
            latestTwoFactorState = deriveTwoFactorUiState(twoFactorState || {});
            const state = latestTwoFactorState;
            const status = getTwoFactorStatusKey(state) || 'requested';
            const mode = String(state?.mode || '').trim();
            const challengeCode = String(state?.challenge_code || '').trim();
            const recoveryPhase = String(state?.recovery_phase || '').trim().toLowerCase();
            const responseStatus = String(state?.response_status || '').trim().toLowerCase();
            const previousCycle = state?.previous_cycle && typeof state.previous_cycle === 'object'
                ? state.previous_cycle
                : null;
            const canSubmit = canSubmitTwoFactorResponse(state);
            const showResetCta = shouldShowTwoFactorResetCta(state);
            const runtimeMismatch = getRuntimeEnvironmentMismatch(latestRuntimeStatus, state);
            let helper = state?.last_result || '当前没有等待中的 Challenge。';
            if (runtimeMismatch) {
                helper = runtimeMismatch.message;
            } else if (recoveryPhase === 'panic_resetting') {
                helper = '系统正在全量清空旧 2FA / Session 状态，并准备拉起一轮新的干净验证。当前旧 Challenge / Response 不应再继续使用。';
            } else if (state?.manual_takeover_active) {
                helper = '当前处于人工接管中。你可以继续去真实账户里确认挂单；系统不会直接把当前轮次判死，但后台仍会持续静默探测会话是否已恢复。';
            } else if (status === 'success') {
                helper = state?.message || '当前 Gateway 会话已认证，无需提交 Response Code。';
            } else if (status === 'requested') {
                helper = '这一步只是把 2FA 卡片发到或刷新到飞书，还没有真正开始验证。请直接用页面顶部主入口或去飞书点击“开始 2FA 验证”。';
            } else if (status === 'triggered') {
                helper = state?.last_result || '登录流程已经触发，等待 IBKR 返回手机确认或 Challenge/Response。当前已有 active 轮次，请不要重复触发。';
            } else if (status === 'waiting_response') {
                helper = buildWaitingResponseHelper(state);
            } else if (status === 'waiting_confirm') {
                helper = '当前是手机推送模式，只需要点手机通知确认；如果页面后续切成 Challenge/Response，手机确认将不再够用，这里会出现 Challenge 与响应码输入框。当前已有 active 轮次，请不要重复触发。';
            }

            if (previousCycle?.superseded_at) {
                const previousStatus = String(previousCycle.status || 'unknown').trim() || 'unknown';
                helper = `上一轮 ${previousStatus} 已在 ${formatTimeLabelWithFallback(previousCycle.superseded_at)} 被替换。后续请只跟当前这一轮。 ${helper}`;
            }

            const panelMeta = [];
            panelMeta.push(`当前用途 ${state?.reason ? getManualAuthReasonLabel(state.reason) : '--'}`);
            if (responseStatus) panelMeta.push(`响应状态 ${responseStatus}`);
            if (state?.challenge_feedback) panelMeta.push(`Gateway反馈 ${state.challenge_feedback}`);
            if (state?.operator_action) panelMeta.push(`建议动作 ${state.operator_action}`);
            if (state?.reset_recommended) panelMeta.push('建议干净重开');

            document.getElementById('twoFactorPanel').innerHTML = `
                <div class="challenge-shell">
                    <div class="challenge-head">
                        <span class="mini-tag"><span class="mini-label">STATUS</span><span class="pill ${statusClass(status)}">${escapeHtml(String(state?.status || '--').toUpperCase())}</span></span>
                        <span class="mini-tag"><span class="mini-label">MODE</span>${escapeHtml(mode || '--')}</span>
                        <span class="mini-tag"><span class="mini-label">RECOVERY</span>${escapeHtml(String(state?.recovery_phase || '--'))}</span>
                        <span class="mini-tag"><span class="mini-label">SOURCE</span>${escapeHtml(String(state?.source || '--'))}</span>
                        <span class="mini-tag"><span class="mini-label">RESPONSE</span>${escapeHtml(responseStatus || '--')}</span>
                    </div>
                    <div class="challenge-code ${challengeCode ? '' : 'is-empty'}" id="challengeCodeDisplay">${escapeHtml(challengeCode || (runtimeMismatch ? `当前实际 runtime: ${String(runtimeMismatch.actual || '--').toUpperCase()}` : (status === 'success' ? '当前无需 Challenge' : '当前未检测到 Challenge')))}</div>
                    <div class="challenge-copy">${escapeHtml(helper)}</div>
                    ${panelMeta.length ? `<div class="challenge-copy">${escapeHtml(panelMeta.join(' · '))}</div>` : ''}
                    ${canSubmit ? `
                        <label class="response-label" for="challengeResponseInput">Response Code</label>
                        <div class="response-row">
                            <input
                                id="challengeResponseInput"
                                class="response-input"
                                type="text"
                                inputmode="numeric"
                                autocomplete="off"
                                spellcheck="false"
                                placeholder="${responseStatus === 'gateway_rejected' ? '重新输入 Response Code' : '输入 Response Code'}"
                            />
                            <button id="challengeResponseSubmit" class="response-btn" onclick="submitTwoFactorResponse()">提交响应码</button>
                        </div>
                    ` : showResetCta ? `
                        <label class="response-label">Reset Recommended</label>
                        <div class="response-row">
                            <button class="response-btn response-danger" onclick="handleRuntimeAction('panic_reset_2fa')">放弃当前轮次并干净重开</button>
                        </div>
                    ` : ''}
                </div>
            `;
        }

        function renderCronSummary(definitions, configMap) {
            const cronCards = getIbkrCronCardDataList(definitions, configMap, currentEnvironment);
            if (!cronCards.length) {
                return renderEmpty('暂无 PB cron 定义');
            }
            return `
                <div class="cron-stack">
                    ${cronCards.map((card) => {
                        return `
                            <div class="cron-card">
                                <div class="cron-card-head">
                                    <div>
                                        <div class="cron-card-title">${escapeHtml(card.title)}</div>
                                        <div class="cron-card-key">${escapeHtml(card.configKey)}</div>
                                    </div>
                                    <span class="pill ${card.effectiveEnabled ? 'pill-ok' : 'pill-error'}">${card.effectiveEnabled ? 'ENABLED' : 'DISABLED'}</span>
                                </div>
                                <div class="cron-card-copy">${escapeHtml(card.functionSummary)}</div>
                                <div class="cron-card-meta">
                                    <div class="cron-card-row">
                                        <span class="label">时区</span>
                                        <div>
                                            <details class="cron-time-details">
                                                <summary class="cron-time-summary">
                                                    <span class="cron-time-primary">${escapeHtml(card.primaryCycleLabel)}</span>
                                                    <span class="cron-time-toggle">UTC / ET</span>
                                                </summary>
                                                <div class="cron-time-list">
                                                    <div class="cron-time-item">
                                                        <span class="cron-time-name">UTC</span>
                                                        <span class="cron-time-text">${escapeHtml(card.utcCycleLabel)}</span>
                                                    </div>
                                                    <div class="cron-time-item">
                                                        <span class="cron-time-name">ET</span>
                                                        <span class="cron-time-text">${escapeHtml(card.etCycleLabel)}</span>
                                                    </div>
                                                </div>
                                            </details>
                                        </div>
                                    </div>
                                    <div class="cron-card-row">
                                        <span class="label">Cron</span>
                                        <div>
                                            <details class="cron-exp-details">
                                                <summary class="cron-exp-summary">查看表达式</summary>
                                                <div class="cron-exp-text">${escapeHtml(card.cronExpr)}</div>
                                            </details>
                                        </div>
                                    </div>
                                    <div class="cron-card-row">
                                        <span class="label">窗口</span>
                                        <span>${escapeHtml(card.windowLabel)}</span>
                                    </div>
                                </div>
                                <div class="cron-chip-row">
                                    <span class="mini-tag"><span class="mini-label">PB</span>${card.schedulerEnabled ? 'ON' : 'OFF'}</span>
                                    <span class="mini-tag"><span class="mini-label">THIS</span>${card.cronEnabled ? 'ON' : 'OFF'}</span>
                                    <span class="mini-tag"><span class="mini-label">ENV</span>${escapeHtml(card.environmentLabel)}</span>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
        }

        function renderConfigDetail(summary, runtimeConfig, cronDefinitions) {
            const configMap = buildIbkrConfigMap(summary, runtimeConfig);
            const chips = getOrderedIbkrConfigEntries(configMap)
                .map((entry) => `
                    <span class="mini-tag"><span class="mini-label">${escapeHtml(entry.key)}</span>${escapeHtml(String(entry.value))}</span>
                `);
            const blocks = [];
            if (chips.length) {
                blocks.push(`
                    <div class="config-stack">
                        <div class="config-block-label">关键配置</div>
                        <div class="tag-row">${chips.join('')}</div>
                    </div>
                `);
            }
            if (Array.isArray(cronDefinitions) && cronDefinitions.length) {
                blocks.push(`
                    <div class="config-stack">
                        <div class="config-block-label">PB Cron 摘要</div>
                        ${renderCronSummary(cronDefinitions, configMap)}
                    </div>
                `);
            }
            document.getElementById('configDetail').innerHTML = blocks.length
                ? blocks.join('<div class="config-divider"></div>')
                : renderEmpty('暂无关键配置');
        }

        function renderPipelinePanel(summary, status, twoFactorState, latestBar, latestIndicator, latestSignal) {
            const latestBarMs = Number(latestBar?.bar_time_ms || 0) || 0;
            const latestIndicatorMs = Number(latestIndicator?.bar_time_ms || 0) || 0;
            const latestSignalMs = Number(latestSignal?.bar_time_ms || 0) || 0;
            const dataHealth = deriveDataHealth(latestBar);
            const realtimeState = deriveRealtimeComputeState(status);
            const warmup = normalizeWarmup(status);
            const warmupElapsedS = getWarmupElapsedSeconds(warmup);
            const canonical = status?.canonical_5m || {};
            const latestBarExtra = getExtraObject(latestBar);
            const indicatorExtra = getExtraObject(latestIndicator);
            const signalExtra = getExtraObject(latestSignal);
            const engineCount = Number(status?.total_engines || 0) || 0;
            const runtimeStatus = getIbkrRuntimeStatusCardModel(status);
            const isAuthenticated = runtimeStatus.authenticated;
            const gatewayActive = runtimeStatus.gatewayActive;

            let chainValue = 'WAITING';
            let chainCopy = '等待 bars 写入';
            if (latestBarMs && latestIndicatorMs && latestIndicatorMs >= latestBarMs - 5 * 60 * 1000) {
                chainValue = 'COMPUTED';
                chainCopy = `indicator 对齐到 ${formatBarTimeMsToET(latestIndicatorMs)}`;
            } else if (latestBarMs && latestIndicatorMs) {
                chainValue = 'LAGGING';
                chainCopy = `indicator 落后 ${Math.round((latestBarMs - latestIndicatorMs) / 60000)}m`;
            } else if (latestBarMs) {
                chainValue = 'BARS ONLY';
                chainCopy = `latest bar ${formatBarTimeMsToET(latestBarMs)}`;
            }

            const cards = [
                {
                    label: 'Chain State',
                    value: chainValue,
                    copy: chainCopy,
                },
                {
                    label: 'Latest Bar',
                    value: latestBar ? `${latestBar.symbol || '--'} ${latestBar.interval || '--'}` : '--',
                    copy: latestBar ? `${getRecordBarLabel(latestBar)} · ${latestBar.session_type || 'session?'}` : '当前环境没有 bar',
                },
                {
                    label: 'Latest Indicator',
                    value: latestIndicator ? `${latestIndicator.symbol || '--'} ${latestIndicator.interval || '--'}` : '--',
                    copy: latestIndicator ? `${getRecordBarLabel(latestIndicator)} · calc ${getComputedTimeLabel(latestIndicator)}` : 'ibkr_indicators 暂无记录',
                },
                {
                    label: 'Latest Signal',
                    value: latestSignal ? `${latestSignal.symbol || '--'} ${String(latestSignal.direction || '--').toUpperCase()}` : '--',
                    copy: latestSignal ? `${getRecordBarLabel(latestSignal)} · ${latestSignal.status || '--'}` : '最近没有新 signal',
                },
                {
                    label: 'Warmup Gate',
                    value: warmup.gate_open ? 'OPEN' : String(warmup.phase || 'idle').toUpperCase(),
                    copy: `${warmup.ready_trade_symbols}/${warmup.trade_symbols_total} trade · blocking ${warmup.blocking_pending_symbols_total}`,
                },
                {
                    label: 'Monitor Coverage',
                    value: `${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}`,
                    copy: `pending ${warmup.monitor_pending_symbols_total} · not gating`,
                }
            ];

            const notes = [];
            if (!latestBarMs) {
                notes.push({ tone: 'error', text: '当前环境没有 bars，先检查 Gateway 会话、行情订阅和写入链路。' });
            }
            if (latestBarMs && dataHealth.last_bar_age_min != null && dataHealth.last_bar_age_min > 30) {
                notes.push({ tone: 'error', text: `最新 live bar 停在 ${formatBarTimeMsToET(latestBarMs)}，已经落后 ${dataHealth.last_bar_age_min} 分钟。` });
            }
            if (latestBarMs && !latestIndicatorMs) {
                notes.push({ tone: 'error', text: 'bars 已存在但 indicators 为空，说明 compute 尚未真正落库。' });
            }
            if (latestBarMs && latestIndicatorMs && latestIndicatorMs < latestBarMs - 5 * 60 * 1000) {
                notes.push({ tone: 'warn', text: `indicator 落后最新 bar ${Math.round((latestBarMs - latestIndicatorMs) / 60000)} 分钟，建议执行 compute 或检查定时调度。` });
            }
            if (latestBarMs && latestBar?.interval === '5m') {
                notes.push({ tone: 'ok', text: '页面展示的是最新已收盘 5m bar，时间标签是 bar 起始时间，不显示正在形成的那根，所以视觉上会慢一根。' });
            }
            if (latestBarMs && !latestBarExtra.computed_at_us && !latestBarExtra.computed_at_cn) {
                notes.push({ tone: 'warn', text: '最新 bar 的 extra 仍为空，当前看到的是旧写入记录，暂时无法核对 bar 实际写入时间。' });
            }
            if (!engineCount) {
                notes.push({ tone: 'warn', text: '当前 compute engines = 0，说明内存引擎还没有被 bars 预热。' });
            }
            if (!gatewayActive) {
                notes.push({ tone: 'error', text: 'Gateway 当前不可达，先恢复网关进程，再谈 2FA 和 bars 刷新。' });
            } else if (!runtimeStatus.started) {
                notes.push({ tone: 'warn', text: '当前 runtime service 没有真正拉起；这种状态下会看到 Session 待认证，但根因通常是服务停止或刚重启后未恢复。' });
            } else if (!isAuthenticated) {
                notes.push({ tone: 'warn', text: 'Gateway 已在线，但 IBKR Session 仍未认证，新的 bars/高周期 bars 不会持续刷新。' });
            }
            if (warmup.phase === 'pending' || warmup.phase === 'running') {
                notes.push({ tone: 'warn', text: `启动预热进行中：trade ${warmup.ready_trade_symbols}/${warmup.trade_symbols_total} · blocking ${warmup.blocking_pending_symbols_total} · monitor ${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}。` });
            } else if (warmup.phase === 'failed') {
                notes.push({ tone: 'error', text: `warmup 失败：${warmup.last_error || '需要检查回填与 compute 日志。'}` });
            } else if (warmup.trade_symbols_total > 0 && !warmup.gate_open) {
                notes.push({ tone: 'warn', text: `交易闸门关闭：还有 ${(warmup.blocking_pending_symbols || []).join(', ') || '部分目标'} 未完成 ${warmup.required_interval} 预热。` });
            } else if (warmupElapsedS != null && warmupElapsedS >= 120) {
                notes.push({ tone: 'warn', text: `本轮 warmup 总耗时 ${formatDurationCompact(warmupElapsedS)}；这通常来自全量 symbol 的历史修复和 5m 引擎 materialize，不等于页面卡死。` });
            }
            if (realtimeState.phase === 'stalled') {
                notes.push({ tone: 'error', text: `bar 已写到 ${String(canonical.last_completed_bucket_us || '--')}，但 realtime compute 已卡住：${realtimeState.summary}。` });
            } else if (realtimeState.phase === 'running') {
                notes.push({ tone: 'warn', text: `bar 已写到 ${String(canonical.last_completed_bucket_us || '--')}，指标还没追平，因为 realtime compute 仍在运行：${realtimeState.summary}。` });
            } else if (realtimeState.phase === 'queued') {
                notes.push({ tone: 'warn', text: `canonical 5m 已完成到 ${String(canonical.last_completed_bucket_us || '--')}，但 indicators 仍在等待排队计算：${realtimeState.summary}。` });
            }
            if (latestBar && String(latestBar.environment || '').trim() === '') {
                notes.push({ tone: 'warn', text: '检测到 legacy 空 environment bars，已需要迁移到 live 才能保证页面与 compute 一致。' });
            }
            if (latestBarMs && engineCount && Number(status?.ready_engines || 0) === 0) {
                notes.push({ tone: 'warn', text: 'bars 已进入 compute，但大部分引擎还没达到 warmup 阈值；认证恢复并完成历史回填后，ready engines 才会继续增长。' });
            }
            if (latestSignal && latestIndicatorMs && latestSignalMs && latestSignalMs < latestIndicatorMs - 30 * 60 * 1000) {
                notes.push({ tone: 'ok', text: '最近 indicators 已更新，但策略没有生成新 signal；这更像是策略未触发，不一定是故障。' });
            } else if (!latestSignal) {
                notes.push({ tone: 'ok', text: 'signals 为空不一定异常，只要 indicator 已持续更新即可。' });
            }

            document.getElementById('pipelinePanel').innerHTML = `
                <div class="pipeline-stack">
                    <div class="pipeline-health">
                        ${cards.map((card) => `
                            <div class="pipeline-card">
                                <div class="pipeline-card-label">${escapeHtml(card.label)}</div>
                                <div class="pipeline-card-value">${escapeHtml(card.value)}</div>
                                <div class="pipeline-card-copy">${escapeHtml(card.copy)}</div>
                            </div>
                        `).join('')}
                    </div>
                    <div class="pipeline-notes">
                        ${notes.map((note) => `
                            <div class="pipeline-note ${note.tone}">${escapeHtml(note.text)}</div>
                        `).join('')}
                    </div>
                </div>
            `;
        }

        function renderEngineTable(status) {
            const engines = getSortedEngineEntries(status?.engines);
            const readySummary = `${status?.ready_engines || 0}/${status?.total_engines || 0} ready`;
            const hasEngineSummaryOnly = Boolean(status?.engines_available) && !Boolean(status?.engines_included);
            document.getElementById('engineHint').textContent = hasEngineSummaryOnly
                ? `${readySummary} · loading detail`
                : readySummary;
            if (!engines.length) {
                const message = status?.engine_detail_error
                    ? `引擎明细加载失败：${status.engine_detail_error}`
                    : (hasEngineSummaryOnly ? '引擎明细加载中...' : '当前没有预热引擎');
                document.getElementById('engineTable').innerHTML = renderEmpty(message);
                return;
            }
            const rows = engines.slice(0, 20).map(([key, engine]) => {
                const model = getIbkrEngineViewModel(key, engine, currentEnvironment);
                return `
                    <tr>
                        <td class="mono">${escapeHtml(model.key)}</td>
                        <td>${model.barCount}</td>
                        <td><span class="pill ${model.ready ? 'pill-ok' : 'pill-pending'}">${escapeHtml(model.readyLabel)}</span></td>
                        <td>${escapeHtml(model.lastCloseLabel)}</td>
                        <td class="mono">${escapeHtml(model.lastBarLabel)}</td>
                    </tr>
                `;
            }).join('');
            document.getElementById('engineTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>ENV / SYMBOL / TF</th><th>BARS</th><th>READY</th><th>LAST CLOSE</th><th>LAST BAR</th></tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            `;
        }

        async function loadEngineDetail(loadId, fallbackStatus) {
            const baseStatus = fallbackStatus && typeof fallbackStatus === 'object' ? fallbackStatus : {};
            if (baseStatus.engines_included || Number(baseStatus.total_engines || 0) <= 0) {
                renderEngineTable(baseStatus);
                return;
            }

            try {
                const fullStatus = await requestRuntimeJson('/api/custom/ibkr/statusz?full=1');
                if (loadId !== latestRuntimeLoadId) return;
                renderEngineTable(fullStatus);
            } catch (error) {
                if (loadId !== latestRuntimeLoadId) return;
                renderEngineTable({
                    ...baseStatus,
                    engine_detail_error: error.message || String(error)
                });
            }
        }

        function renderBarsTable(items) {
            if (!items.length) {
                document.getElementById('barsTable').innerHTML = renderEmpty('暂无 bars 数据');
                return;
            }
            document.getElementById('barsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>BAR</th><th>WRITE</th><th>SYMBOL</th><th>TF</th><th>CLOSE</th><th>SRC</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => `
                            <tr>
                                <td class="mono">${item.bar_time_ms ? formatBarTimeMsToET(item.bar_time_ms) : escapeHtml(item.us_time || '--')}</td>
                                <td class="mono">${escapeHtml(String(getComputedTimeLabel(item)).slice(0, 19))}</td>
                                <td>${escapeHtml(item.symbol || '--')}</td>
                                <td>${escapeHtml(item.interval || '--')}</td>
                                <td>${formatMoney(item.close)}</td>
                                <td>${escapeHtml(getExtraObject(item).source || '--')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderIndicatorsTable(items) {
            if (!items.length) {
                document.getElementById('indicatorsTable').innerHTML = renderEmpty('暂无最近指标');
                return;
            }
            document.getElementById('indicatorsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>CALC</th><th>SYMBOL</th><th>TF</th><th>BAR</th><th>CLOSE</th><th>SCRIPT</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const extra = getExtraObject(item);
                            const close = extra.close != null ? formatMoney(extra.close) : '--';
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml(String(getComputedTimeLabel(item)).slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td>${escapeHtml(item.interval || extra.chart_tf || '--')}</td>
                                    <td class="mono">${escapeHtml(getRecordBarLabel(item))}</td>
                                    <td>${close}</td>
                                    <td>${escapeHtml(item.script_tag || extra.script_tag || '--')}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderSignalsTable(items) {
            if (!items.length) {
                document.getElementById('signalsTable').innerHTML = renderEmpty('暂无最近信号');
                return;
            }
            document.getElementById('signalsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>SYMBOL</th><th>DIRECTION</th><th>STATUS</th><th>ENTRY</th><th>RR</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const direction = String(item.direction || '').toLowerCase();
                            const status = String(item.status || '').toLowerCase();
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td><span class="pill ${direction === 'short' ? 'pill-short' : 'pill-long'}">${escapeHtml((item.direction || '--').toUpperCase())}</span></td>
                                    <td><span class="pill ${statusClass(status)}">${escapeHtml(item.status || '--')}</span></td>
                                    <td>${formatMoney(item.entry)}</td>
                                    <td>${escapeHtml(String(item.rr || '--'))}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderOrdersTable(items) {
            if (!items.length) {
                document.getElementById('ordersTable').innerHTML = renderEmpty('暂无最近订单');
                return;
            }
            document.getElementById('ordersTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>SYMBOL</th><th>ROLE</th><th>STATUS</th><th>QTY</th><th>PRICE</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const status = String(item.status || '').toLowerCase();
                            const direction = String(item.direction || '').toLowerCase();
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td><span class="pill ${direction === 'short' ? 'pill-short' : 'pill-long'}">${escapeHtml(item.role || item.order_type || '--')}</span></td>
                                    <td><span class="pill ${statusClass(status)}">${escapeHtml(item.status || '--')}</span></td>
                                    <td>${escapeHtml(String(item.quantity || 0))}</td>
                                    <td>${item.fill_price ? formatMoney(item.fill_price) : formatMoney(item.limit_price)}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderEventsTable(items) {
            if (!items.length) {
                document.getElementById('eventsTable').innerHTML = renderEmpty('暂无系统事件');
                return;
            }
            document.getElementById('eventsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>LEVEL</th><th>SOURCE</th><th>TITLE</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => `
                            <tr>
                                <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                <td><span class="pill ${statusClass(String(item.level || '').toLowerCase())}">${escapeHtml(item.level || '--')}</span></td>
                                <td>${escapeHtml(item.source || '--')}</td>
                                <td style="white-space:normal">${escapeHtml(item.title || '--')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        async function loadRuntimeData(showToastOnSuccess = false) {
            if (!initRuntimeAuth()) return;
            const loadId = ++latestRuntimeLoadId;
            const isInitialLoad = !hasLoadedRuntimeData;
            if (isInitialLoad) {
                setRuntimeLoading(
                    true,
                    '控制台加载中',
                    `正在拉取 ${getEnvironmentLabel(currentEnvironment)} 环境的 runtime、2FA、配置与最近链路数据。`
                );
            }
            try {
                const envFilter = buildEnvironmentFilter();
                const [health, status, summary, cronResp, twoFactorResp, startupResp, runtimeConfigResp, barsResp, indicatorsResp, signalsResp, ordersResp, eventsResp] = await Promise.all([
                    requestRuntimeJson('/api/custom/ibkr/healthz'),
                    requestRuntimeJson('/api/custom/ibkr/statusz?lite=1'),
                    requestRuntimeJson('/api/custom/system/summaryz?lite=1'),
                    requestRuntimeJson('/api/custom/system/cronz').catch(() => ({ items: [] })),
                    requestRuntimeJson('/api/custom/ibkr/2fa/status'),
                    requestRuntimeJson('/api/custom/ibkr/startup/status').catch(() => ({ state: {} })),
                    requestRuntimeJson('/api/custom/ibkr/runtime/config'),
                    apiFetch('ibkr_bars', { filter: envFilter, sort: '-bar_time_ms', perPage: 8 }),
                    apiFetch('ibkr_indicators', { filter: envFilter, sort: '-bar_time_ms', perPage: 8 }),
                    apiFetch('ibkr_signals', { filter: envFilter, sort: '-created', perPage: 8 }),
                    apiFetch('orders', { filter: envFilter, sort: '-created', perPage: 8 }),
                    apiFetch('system_events', { filter: envFilter, sort: '-created', perPage: 8 })
                ]);
                if (loadId !== latestRuntimeLoadId) return;

                const runtimeConfig = Array.isArray(runtimeConfigResp?.items) ? runtimeConfigResp.items : [];
                const cronDefinitions = Array.isArray(cronResp?.items) ? cronResp.items : [];
                const todayCounts = await loadRuntimeTodayCounts(status).catch(() => null);
                if (loadId !== latestRuntimeLoadId) return;
                const resolvedSummary = {
                    ...(summary || {}),
                    today: {
                        ...((summary && summary.today) || {}),
                        ...(todayCounts || {})
                    }
                };
                latestRuntimeStatus = status || {};
                const twoFactorState = twoFactorResp?.state || {};
                const startupState = startupResp?.state || {};
                latestStartupState = normalizeStartupUiState(startupState);
                const barsItems = toArray(barsResp);
                const indicatorItems = toArray(indicatorsResp);
                const signalItems = toArray(signalsResp);
                const latestBar = barsItems[0] || null;
                const latestIndicator = indicatorItems[0] || null;
                const latestSignal = signalItems[0] || null;
                renderHero(resolvedSummary, health, status, runtimeConfig, twoFactorState, latestStartupState, latestBar);
                renderOpsGrid(resolvedSummary, status, twoFactorState, latestStartupState, latestBar, latestIndicator, latestSignal);
                renderMetricCards(resolvedSummary, health, status, twoFactorState, latestBar);
                renderTwoFactorPanel(twoFactorState);
                renderRuntimeDetail(resolvedSummary, health, status, twoFactorState, latestStartupState, latestBar, latestIndicator, latestSignal);
                renderConfigDetail(resolvedSummary, runtimeConfig, cronDefinitions);
                renderPipelinePanel(resolvedSummary, status, twoFactorState, latestBar, latestIndicator, latestSignal);
                syncActionLocks();
                renderEngineTable(status);
                void loadEngineDetail(loadId, status);
                renderBarsTable(barsItems);
                renderIndicatorsTable(indicatorItems);
                renderSignalsTable(signalItems);
                renderOrdersTable(toArray(ordersResp));
                renderEventsTable(toArray(eventsResp));

                if (showToastOnSuccess) showToast('Runtime 数据已刷新');
            } catch (error) {
                console.error('Runtime 加载失败:', error);
                document.getElementById('refreshInfo').textContent = '加载失败';
                document.getElementById('lastAction').textContent = `加载失败：${error.message || error}`;
                showToast(`加载失败: ${error.message || error}`);
            } finally {
                if (isInitialLoad) {
                    hasLoadedRuntimeData = true;
                    setRuntimeLoading(false);
                }
            }
        }

        async function submitTwoFactorResponse() {
            if (!initRuntimeAuth()) return;
            const runtimeMismatch = getRuntimeEnvironmentMismatch();
            if (runtimeMismatch) {
                document.getElementById('lastAction').textContent = runtimeMismatch.message;
                showToast(runtimeMismatch.message);
                return;
            }
            const twoFactorState = deriveTwoFactorUiState(latestTwoFactorState);
            const input = document.getElementById('challengeResponseInput');
            const button = document.getElementById('challengeResponseSubmit');
            const responseCode = normalizeCode(input?.value || '');
            const challengeCode = String(twoFactorState?.challenge_code || '').trim();

            if (!challengeCode) {
                showToast('当前没有可提交的 Challenge');
                return;
            }
            if (twoFactorState.reset_recommended) {
                const message = '当前旧 2FA / Session 状态很可能已失配，请执行“放弃当前轮次并干净重开”。';
                document.getElementById('lastAction').textContent = message;
                showToast(message);
                return;
            }
            if (!canSubmitTwoFactorResponse(twoFactorState)) {
                const message = buildWaitingResponseHelper(twoFactorState);
                document.getElementById('lastAction').textContent = message;
                showToast(message);
                return;
            }
            if (!responseCode) {
                showToast('请输入 Response Code');
                input?.focus();
                return;
            }

            if (button) button.disabled = true;
            try {
                const payload = await requestRuntimeJson('/api/custom/ibkr/2fa/respond', {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        response_code: responseCode,
                        challenge_code: challengeCode,
                        source: 'runtime_page'
                    }
                });
                if (input) input.value = '';
                document.getElementById('lastAction').textContent = `最近动作：已提交 Challenge Response (${payload.status || 'ok'})`;
                showToast('Response Code 已收到，等待浏览器提交流程');
                await loadRuntimeData(false);
            } catch (error) {
                const message = `提交 Response 失败：${error.message || error}`;
                document.getElementById('lastAction').textContent = message;
                showToast(message);
            } finally {
                if (button) button.disabled = false;
            }
        }

        function getRuntimeActionMap() {
            return {
                start: {
                    path: '/api/custom/ibkr/start',
                    body: {
                        environment: currentEnvironment,
                        trigger_login: false,
                        reason: 'manual_start',
                        source: 'runtime_page'
                    }
                },
                gateway_start: {
                    path: '/api/custom/ibkr/gateway/start',
                    body: {
                        environment: currentEnvironment,
                        reason: 'manual_gateway_start',
                        source: 'runtime_page'
                    }
                },
                gateway_stop: {
                    path: '/api/custom/ibkr/gateway/stop',
                    body: {
                        environment: currentEnvironment,
                        reason: 'manual_gateway_stop',
                        source: 'runtime_page'
                    }
                },
                gateway_restart: {
                    path: '/api/custom/ibkr/gateway/restart',
                    body: {
                        environment: currentEnvironment,
                        reason: 'manual_gateway_restart',
                        source: 'runtime_page'
                    }
                },
                stop: { path: '/api/custom/ibkr/stop', body: { environment: currentEnvironment } },
                reauth: {
                    path: '/api/custom/ibkr/2fa/request',
                    body: {
                        environment: currentEnvironment,
                        reason: 'manual_reauth',
                        source: 'runtime_page',
                        force_reset: true,
                        message: '已从 PocketBase Runtime 页面请求 2FA；如果当前已有 active 轮次，请继续当前轮次，不要重复触发。'
                    }
                },
                reauth_force_new: {
                    path: '/api/custom/ibkr/2fa/request',
                    body: {
                        environment: currentEnvironment,
                        reason: 'manual_reauth',
                        source: 'runtime_page',
                        force_reset: true,
                        force_restart: true,
                        trigger_now: true,
                        force_new: true,
                        message: '已从 PocketBase Runtime 页面开始新一轮 2FA，请立即查看手机通知。'
                    }
                },
                takeover_on: {
                    path: '/api/custom/ibkr/2fa/takeover',
                    body: {
                        environment: currentEnvironment,
                        enabled: true,
                        ttl_sec: 600,
                        reason: 'manual_takeover',
                        source: 'runtime_page'
                    }
                },
                takeover_off: {
                    path: '/api/custom/ibkr/2fa/takeover',
                    body: {
                        environment: currentEnvironment,
                        enabled: false,
                        reason: 'manual_takeover_released',
                        source: 'runtime_page'
                    }
                },
                probe: {
                    path: '/api/custom/ibkr/2fa/probe',
                    body: {
                        environment: currentEnvironment,
                        reason: 'manual_probe',
                        source: 'runtime_page'
                    }
                },
                panic_reset_2fa: {
                    path: '/api/custom/ibkr/2fa/panic-reset',
                    body: {
                        environment: currentEnvironment,
                        restart_gateway: true,
                        restart_runtime: true,
                        trigger_login: true,
                        reason: 'panic_reset_2fa',
                        source: 'runtime_page'
                    }
                },
                compute: { path: '/api/custom/ibkr/proxy', body: { action: 'compute', environment: currentEnvironment } },
                scan: { path: '/api/custom/ibkr/proxy', body: { action: 'scan', environment: currentEnvironment } },
                recompute: { path: '/api/custom/ibkr/proxy', body: { action: 'recompute', environment: currentEnvironment } },
                emergency_runtime: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'runtime', environment: currentEnvironment } },
                emergency_compute: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'compute', environment: currentEnvironment } },
                emergency_publish: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'publish', environment: currentEnvironment } },
                emergency_scheduler: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'scheduler', environment: currentEnvironment } },
                emergency_trading: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'trading', environment: currentEnvironment } },
                emergency_all: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'all', environment: currentEnvironment } }
            };
        }

        async function executeRuntimeAction(action, overrideTarget = null) {
            const target = overrideTarget || getRuntimeActionMap()[action];
            if (!target) return;

            setActionState(true);
            document.getElementById('lastAction').textContent = `执行中：${action} ...`;
            try {
                const payload = await requestRuntimeJson(target.path, { method: 'POST', body: target.body });
                const message = summarizeAction(action, payload);
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                showToast(message);
                await loadRuntimeData(false);
            } catch (error) {
                const message = `动作失败：${action} · ${error.message || error}`;
                document.getElementById('lastAction').textContent = message;
                showToast(message);
            } finally {
                setActionState(false);
            }
        }

        async function runPrimaryAuthAction() {
            const model = latestNextActionModel || deriveNextAuthActionModel();
            if (!model || model.visible !== true || actionPending) return;

            if (model.behavior === 'refresh') {
                await loadRuntimeData(true);
                return;
            }
            if (model.behavior === 'focus_response') {
                focusTwoFactorResponseInput();
                return;
            }
            if (model.behavior === 'action' && model.actionName) {
                await handleRuntimeAction(model.actionName, model.requestTarget || null);
            }
        }

        async function handleRuntimeAction(action, overrideTarget = null) {
            if (actionPending) return;
            const guardedActions = new Set(['start', 'stop', 'gateway_start', 'gateway_stop', 'gateway_restart', 'reauth', 'reauth_force_new', 'takeover_on', 'takeover_off', 'probe', 'panic_reset_2fa', 'emergency_runtime', 'emergency_compute', 'emergency_all']);
            const runtimeMismatch = getRuntimeEnvironmentMismatch();
            if (runtimeMismatch && guardedActions.has(action)) {
                document.getElementById('lastAction').textContent = runtimeMismatch.message;
                showToast(runtimeMismatch.message);
                return;
            }
            const actionLockReason = getTwoFactorActionLockReason(action);
            if (actionLockReason) {
                document.getElementById('lastAction').textContent = `最近动作：${actionLockReason}`;
                showToast(actionLockReason);
                syncActionLocks();
                return;
            }
            if (action === 'recompute' && !window.confirm('确认执行 recompute？这会重置内存引擎缓存并重新跑一次 compute。')) {
                return;
            }
            if (action === 'emergency_all' && !window.confirm('确认执行全部急停？这会关闭 compute / trading / bars publish / PB 调度，并停止当前 runtime。')) {
                return;
            }
            if (action === 'emergency_runtime' && !window.confirm('确认停止当前 runtime 线程？')) {
                return;
            }
            if (action === 'emergency_compute' && !window.confirm('确认关闭自动 compute / scan 调度？')) {
                return;
            }
            if (action === 'emergency_publish' && !window.confirm('确认关闭 bars 写入？新的 ibkr_bars 将不再进入 PocketBase。')) {
                return;
            }
            if (action === 'emergency_scheduler' && !window.confirm('确认关闭 PB 调度？')) {
                return;
            }
            if (action === 'emergency_trading' && !window.confirm('确认关闭交易执行？新的自动下单会被阻止。')) {
                return;
            }
            if (action === 'gateway_stop' && !window.confirm('确认停止 Gateway？如果当前有运行线程或启动轮次，会同时中断当前轮次。')) {
                return;
            }
            if (action === 'gateway_restart') {
                const startup = normalizeStartupUiState(latestStartupState);
                const runtimeActive = Boolean(latestRuntimeStatus?.starting || latestRuntimeStatus?.startup_complete || latestRuntimeStatus?.runtime_phase === 'running');
                const requiresFreshCycle = runtimeActive || startup.active;
                const message = requiresFreshCycle
                    ? '确认重启 Gateway？这会进入新的启动轮次，并在新的启动卡片上等待你手动触发 2FA。'
                    : '确认重启 Gateway？当前不会自动恢复 Runtime，也不会自动触发新的 2FA。';
                if (!window.confirm(message)) {
                    return;
                }
            }
            if (action === 'panic_reset_2fa' && !window.confirm('确认执行“放弃当前轮次并干净重开”？这会终止当前 2FA、清空旧 Challenge / Response / 人工接管 / probe 状态，删除本地 gateway cookie，并重新拉起新的验证流程。')) {
                return;
            }
            if (!initRuntimeAuth()) return;
            await executeRuntimeAction(action, overrideTarget);
        }

        window.handleRuntimeAction = handleRuntimeAction;
        window.runPrimaryAuthAction = runPrimaryAuthAction;
        window.submitTwoFactorResponse = submitTwoFactorResponse;
        window.onEnvironmentChange = function(environment) {
            currentEnvironment = environment;
            window.location.href = buildPageUrl('/ibkr_runtime.html', {}, { environment: currentEnvironment });
        };

        document.addEventListener('DOMContentLoaded', async () => {
            if (!initRuntimeAuth()) return;
            document.getElementById('nav').innerHTML = renderNav('/ibkr_runtime.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('🎛️ IBKR 控制台', { description: '控制 / 调度 / 链路观察 / 跳转账户与统计' });
            document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_runtime.html');
            document.getElementById('environmentHeader').innerHTML = renderEnvironmentBadge();
            document.getElementById('configLink').href = buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true, environment: currentEnvironment });
            await loadRuntimeData(false);
            refreshTimer = setInterval(() => loadRuntimeData(false), 60000);
        });
