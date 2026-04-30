let currentEnvironment = getCurrentRuntimeEnvironment();
        let refreshTimer = null;
        let actionPending = false;
        let latestRuntimeStatus = {};
        let latestTwoFactorState = {};
        let latestStartupState = {};
        let latestServiceMonitorPayload = {};
        let latestServiceActionStates = {};
        let latestNextActionModel = null;
        let latestRuntimeLoadId = 0;
        let hasLoadedRuntimeData = false;
        let latestRuntimeBarsSnapshot = [];
        let latestRuntimeIndicatorSnapshot = [];
        let runtimeRecentDataLoading = false;
        let runtimePageClosing = false;
        let authActionFeedback = null;
        const CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000;
        const MANUAL_AUTH_REASON_LABELS = {
            weekly_reauth: '每周重登提醒',
            manual_start: '启动验证',
            startup: '启动验证',
            manual_reauth: '手动重登验证',
            manual_gateway_restart: '网关重启验证',
            panic_reset_2fa: '重开验证'
        };
        const STARTUP_STEP_LABELS = {
            service_boot: '服务拉起',
            card_ready: '准备 2FA 卡片',
            manual_trigger: '在飞书手动触发 2FA',
            manual_confirm: '完成当前 2FA 验证',
            runtime_resume: '恢复 Runtime',
            health_check: '启动后健康检查'
        };
        const SERVICE_CONTROL_MODULES = [
            {
                service: 'ibkr-runtime',
                title: 'Runtime Service',
                kicker: 'DATA PLANE',
                copy: 'broker session / live bars / runtime state',
                highRiskRestart: true,
            },
            {
                service: 'ibkr-gateway',
                title: 'IB Gateway',
                kicker: 'BROKER',
                copy: 'IBC + IB Gateway GUI/API',
                highRiskRestart: true,
            },
            {
                service: 'ibkr-compute',
                title: 'Compute Service',
                kicker: 'COMPUTE',
                copy: 'indicators / signals / backtests',
                highRiskRestart: false,
            },
            {
                service: 'ibkr-scheduler',
                title: 'Scheduler Service',
                kicker: 'SCHEDULER',
                copy: 'cron registry / cursor dispatch',
                highRiskRestart: false,
            },
        ];

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
                ? '手动启动=重启 IB Gateway 服务 + 新卡片 + 人工 2FA'
                : '手动启动=直接恢复 Runtime';
            const weeklyReauth = strategy.weekly_reauth_mode === 'fresh_cycle'
                ? '每周提醒=fresh cycle'
                : '每周提醒=resume only';
            const serverBoot = strategy.server_boot_mode === 'fresh_cycle'
                ? 'server_boot=fresh cycle'
                : 'server_boot=resume only';
            const startupCard = strategy.server_boot_publish_startup_card
                ? 'server_boot 会发启动卡片'
                : 'server_boot 不新发启动卡片';
            return `${manualStart}；${weeklyReauth}；${serverBoot}；${startupCard}`;
        }

        function normalizeComputeStartupPreload(payload = {}) {
            const source = payload && typeof payload === 'object' ? payload : {};
            const status = String(source.status || '').trim().toLowerCase()
                || (source.running ? 'running' : (source.finished_at ? 'completed' : (source.scheduled ? 'scheduled' : 'idle')));
            return {
                enabled: source.enabled !== false,
                shouldSchedule: source.should_schedule !== false,
                scheduled: source.scheduled === true,
                running: source.running === true,
                status,
                environments: Array.isArray(source.environments)
                    ? source.environments.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean)
                    : [],
                envTotal: Number(source.env_total || 0) || 0,
                envCompleted: Number(source.env_completed || 0) || 0,
                symbolTotal: Number(source.symbol_total || 0) || 0,
                symbolCompleted: Number(source.symbol_completed || 0) || 0,
                readyCount: Number(source.ready_count || 0) || 0,
                startedAt: source.started_at || null,
                finishedAt: source.finished_at || null,
                elapsedS: Number(source.elapsed_s || 0) || 0,
                reason: String(source.reason || '').trim(),
                error: String(source.error || '').trim(),
            };
        }

        function getComputeStartupPreload(status, health, summary) {
            return normalizeComputeStartupPreload(
                status?.compute_startup_preload
                || status?.compute?.compute_startup_preload
                || health?.compute?.compute_startup_preload
                || summary?.ibkr_compute?.compute_startup_preload
                || {}
            );
        }

        function formatComputeStartupPreloadSummary(preload) {
            if (
                preload.status === 'idle'
                && !preload.scheduled
                && !preload.startedAt
                && !preload.finishedAt
                && preload.envTotal <= 0
                && preload.symbolTotal <= 0
            ) {
                return '--';
            }
            if (!preload.enabled) return 'DISABLED';
            const parts = [];
            if (preload.envTotal > 0) parts.push(`env ${preload.envCompleted}/${preload.envTotal}`);
            if (preload.symbolTotal > 0) parts.push(`symbols ${preload.symbolCompleted}/${preload.symbolTotal}`);
            if (preload.status === 'completed') parts.push(`startup ready ${preload.readyCount}/${preload.symbolTotal || 0}`);
            else if (preload.readyCount > 0) parts.push(`ready ${preload.readyCount}/${preload.symbolTotal || 0}`);
            if (preload.elapsedS > 0) parts.push(`elapsed ${formatSecondsLabel(preload.elapsedS)}`);
            if (preload.status === 'failed') {
                return `FAILED${parts.length ? ` · ${parts.join(' · ')}` : ''}${preload.error ? ` · ${preload.error}` : ''}`;
            }
            if (preload.status === 'completed') return `DONE${parts.length ? ` · ${parts.join(' · ')}` : ''}`;
            if (preload.status === 'running') return `RUNNING${parts.length ? ` · ${parts.join(' · ')}` : ''}`;
            if (preload.status === 'scheduled') return `SCHEDULED${preload.environments.length ? ` · ${preload.environments.join(', ')}` : ''}`;
            if (preload.status === 'skipped') return `SKIPPED${preload.reason ? ` · ${preload.reason}` : ''}`;
            return String(preload.status || '--').toUpperCase();
        }

        function formatComputeStartupPreloadTimestamp(value) {
            const raw = String(value || '').trim();
            if (!raw) return '--';
            const formatted = formatTimeLabel(raw);
            if (formatted && formatted !== '--' && formatted !== '-') return formatted;
            return raw.replace('T', ' ').slice(0, 19);
        }

        function deriveDataHealth(latestBar) {
            return buildIbkrDataHealth(latestBar?.bar_time_ms, {
                symbol: latestBar?.symbol || '',
                noDataStatus: runtimeRecentDataLoading && !latestBar ? 'loading' : 'no_data'
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
                message: `${getEnvironmentLabel(requested)} 无独立 runtime；当前运行 ${getEnvironmentLabel(actual)}。请切换环境。`
            };
        }

        function getRuntimeStarted(status = latestRuntimeStatus) {
            return getIbkrRuntimeStarted(status);
        }

        function isTwoFactorVerifiedSuccess(twoFactorState = latestTwoFactorState) {
            const status = normalizeIbkrTwoFactorStatus(twoFactorState?.status || '');
            const recoveryPhase = String(twoFactorState?.recovery_phase || '').trim().toLowerCase();
            const probeResult = String(twoFactorState?.probe_result || '').trim().toLowerCase();
            return status === 'success'
                || (recoveryPhase === 'recovered' && probeResult === 'authenticated' && Boolean(twoFactorState?.last_runtime_authenticated_at));
        }

        function isRuntimeSnapshotIncomplete(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState) {
            if (twoFactorState?.runtime_status_incomplete === true) return true;
            if (!isTwoFactorVerifiedSuccess(twoFactorState)) return false;
            const gateway = status?.gateway || {};
            const session = status?.session || {};
            return !status?.runtime_phase
                && !status?.starting
                && !status?.startup_complete
                && !session?.running
                && !session?.authenticated
                && !gateway?.running
                && !gateway?.reachable
                && !Number(gateway?.status_code || 0)
                && !Number(gateway?.pid || 0);
        }

        function getEffectiveRuntimeStatusCardModel(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState) {
            const runtimeStatus = getIbkrRuntimeStatusCardModel(status);
            if (!isTwoFactorVerifiedSuccess(twoFactorState)) return runtimeStatus;
            if (!isRuntimeSnapshotIncomplete(status, twoFactorState) && runtimeStatus.authenticated) return runtimeStatus;
            return {
                ...runtimeStatus,
                dotClass: 'dot-green',
                mainText: '认证',
                subText: '2FA 已验证 · runtime 快照待刷新',
                started: true,
                gatewayActive: true,
                authenticated: true,
                snapshotIncomplete: isRuntimeSnapshotIncomplete(status, twoFactorState)
            };
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
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactorState);
            const runtimeStarted = runtimeStatus.started;
            const sessionAuthenticated = runtimeStatus.authenticated;
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const twoFactorCyclePhase = getIbkrTwoFactorCyclePhase(twoFactor);
            const startup = normalizeStartupUiState(startupState);
            const reason = getEffectiveManualAuthReason(status, twoFactor, startup);
            const reasonLabel = getManualAuthReasonLabel(reason);
            const startupStepLabel = getStartupCurrentStepLabel(startup) || '等待下一步';
            const model = getIbkrRuntimeAuthGuidanceModel({
                runtimeMismatch,
                sessionAuthenticated,
                runtimeStarted,
                startup,
                reason,
                reasonLabel,
                startupStepLabel,
                twoFactorCyclePhase,
                twoFactorState: twoFactor,
                environment: currentEnvironment,
                source: 'runtime_page_banner'
            });
            return model;
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
            const feedbackHtml = authActionFeedback?.text
                ? `<div class="auth-banner-feedback ${escapeHtml(authActionFeedback.tone || 'info')}">${escapeHtml(authActionFeedback.text)}</div>`
                : '';
            const primaryLabel = actionPending && model.buttonLabel ? '执行中...' : model.buttonLabel;
            const buttonHtml = model.buttonLabel
                ? `<button class="auth-banner-btn" onclick="runPrimaryAuthAction()" ${actionPending ? 'disabled' : ''}>${escapeHtml(primaryLabel)}</button>`
                : '';
            const secondaryButtonHtml = model.secondaryButtonLabel
                ? `<button class="auth-banner-btn auth-banner-btn-secondary" onclick="runSecondaryAuthAction()" ${actionPending ? 'disabled' : ''}>${escapeHtml(model.secondaryButtonLabel)}</button>`
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
                    ${feedbackHtml}
                </div>
                <div class="auth-banner-side">
                    ${buttonHtml}
                    ${secondaryButtonHtml}
                    <div class="auth-banner-hint">每次重启后直接看这块。只有你手动触发或确认后，流程才会继续往下走。</div>
                </div>
            `;
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
            const status = normalizeIbkrTwoFactorStatus(state.status || '');
            const responseStatus = String(state.response_status || '').trim().toLowerCase();
            const challengeCode = String(state.challenge_code || '').trim();
            const feedback = String(state.challenge_feedback || '').trim();
            const recoveryPhase = String(state.recovery_phase || '').trim().toLowerCase();
            const runtimeGateway = latestRuntimeStatus?.gateway && typeof latestRuntimeStatus.gateway === 'object'
                ? latestRuntimeStatus.gateway
                : {};
            const gatewayStatusCode = Number(runtimeGateway.status_code ?? state.gateway_status_code ?? 0) || 0;
            const gatewayRunning = Boolean(runtimeGateway.running || state.gateway_running);
            const gatewayReachable = [0, 502, 503].includes(gatewayStatusCode)
                ? false
                : Boolean(runtimeGateway.reachable || state.gateway_reachable || gatewayRunning);
            const hasGatewayEvidence = Boolean(
                Object.keys(runtimeGateway).length
                || state.gateway_status_code != null
                || state.gateway_reachable != null
                || state.gateway_running != null
            );
            if (['triggered', 'waiting_confirm'].includes(status) && !challengeCode && hasGatewayEvidence && !gatewayReachable) {
                state.gateway_2fa_not_reached = true;
                state.push_confirmed = false;
                state.last_result = state.last_result || 'gateway_not_ready_push_not_confirmed';
                state.message = state.message || 'Gateway 尚未真正进入 2FA，手机 Push 未确认发出。';
            }
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

            state.status = status;
            state.response_status = responseStatus;
            state.challenge_feedback = feedback;
            state.operator_action = operatorAction;
            state.reset_recommended = resetRecommended;
            state.reset_reason = resetReason;
            state.response_submitted_age_sec = submittedAgeMs > 0 ? Math.round(submittedAgeMs / 1000) : 0;
            state.response_rejected_age_sec = rejectedAgeMs > 0 ? Math.round(rejectedAgeMs / 1000) : 0;
            return state;
        }

        function getTwoFactorActionLockReason(action, twoFactorState = latestTwoFactorState) {
            const guarded = new Set(['start', 'reauth', 'reauth_force_new', 'probe']);
            if (!guarded.has(action)) return '';

            const state = deriveTwoFactorUiState(twoFactorState);
            if (!isIbkrTwoFactorCycleActive(state)) return '';
            return getIbkrTwoFactorCycleActionLockReason(action, getIbkrTwoFactorCyclePhase(state));
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
            document.querySelectorAll('.service-action-btn').forEach((button) => {
                button.disabled = actionPending;
                button.title = actionPending ? '动作执行中，请稍候。' : '';
            });
        }

        function summarizeAction(action, payload) {
            if (!payload || typeof payload !== 'object') {
                return `${action} 已执行。`;
            }
            if (action === 'start') {
                if (payload.message && payload.trigger_login === false) {
                    return 'ibkr-runtime 已开始拉起；不会自动触发手机 Push，下一步请看顶部“下一步 / 手动验证”入口。';
                }
                return payload.message || 'ibkr-runtime 启动中。';
            }
            if (action === 'reauth' || action === 'reauth_force_new') {
                if (payload.message) return payload.message;
                const actionState = deriveTwoFactorUiState(payload?.state || latestTwoFactorState);
                const cyclePhase = getIbkrTwoFactorCyclePhase(actionState);
                if (payload?.state?.runtime_authenticated && payload?.state?.gateway_reachable && Number(payload?.state?.gateway_status_code || 0) !== 401) {
                    return '当前 Gateway 会话已认证，无需再次确认。';
                }
                return getIbkrTwoFactorCycleActionSummary(action, cyclePhase);
            }
            if (action === 'gateway_restart') {
                if (payload.message) return payload.message;
                return 'systemd ibkr-gateway 重启动作已执行。';
            }
            if (action === 'probe' || action === 'panic_reset_2fa') {
                if (payload.message) return payload.message;
                if (action === 'probe') return '已触发静默探测。';
                return '已全量清空旧状态并重新拉起新的验证周期。';
            }
            if (action === 'compute') {
                const pieces = [];
                if (payload.environments) pieces.push(`env=${payload.environments.join(',')}`);
                if (payload.processed != null) pieces.push(`processed=${payload.processed}`);
                if (payload.signals != null) pieces.push(`signals=${payload.signals}`);
                if (payload.errors != null) pieces.push(`errors=${payload.errors}`);
                if (payload.candidates != null) pieces.push(`candidates=${payload.candidates}`);
                return `${action} 返回：${pieces.join(' · ') || 'ok'}`;
            }
            if (action === 'recover_all') {
                const updatedKeys = Array.isArray(payload.updated)
                    ? payload.updated.map(item => item.key).filter(Boolean).join(',')
                    : '';
                return `恢复运行开关完成：${updatedKeys || 'no_config_change'}`;
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
            renderRuntimeFlowPrimaryAction(latestRuntimeStatus, latestTwoFactorState);
            syncActionLocks();
            renderServiceControlPanel(latestRuntimeStatus, latestServiceMonitorPayload);
            renderAuthActionBanner(latestNextActionModel);
        }

        function setAuthActionFeedback(message, tone = 'info') {
            const text = String(message || '').trim();
            authActionFeedback = text
                ? { text, tone: String(tone || 'info').trim() || 'info' }
                : null;
            renderAuthActionBanner(latestNextActionModel);
        }

        function renderMetricCards(summary, health, status, twoFactorState, latestBar) {
            const today = summary?.today || {};
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const dataIsLoading = dataHealth.status === 'loading';
            const realtimeMetrics = deriveRealtimeMetrics(status, latestBar);
            const warmup = normalizeWarmup(status);
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactorState);
            const twoFactorStatus = String(twoFactorState?.status || '').trim().toUpperCase() || '--';
            const sessionCopy = runtimeStatus.snapshotIncomplete
                ? `2FA ${twoFactorStatus} · 快照待刷新`
                : `2FA ${twoFactorStatus} · gateway ${runtimeStatus.gatewayActive ? 'active' : 'offline'}`;
            const cards = [
                {
                    label: 'Session Auth',
                    value: runtimeStatus.authenticated ? 'AUTHED' : (runtimeStatus.started ? 'WAITING' : 'STOPPED'),
                    copy: sessionCopy
                },
                {
                    label: 'Ready Engines',
                    value: `${status?.ready_engines || 0}/${status?.total_engines || 0}`,
                    copy: `compute ${status?.compute_count || 0}`
                },
                {
                    label: 'Warmup Gate',
                    value: warmup.gate_open ? 'OPEN' : String(warmup.phase || 'idle').toUpperCase(),
                    copy: `${warmup.ready_trade_symbols}/${warmup.trade_symbols_total} trade`
                        + ` · block ${warmup.blocking_pending_symbols_total}`
                },
                {
                    label: 'Monitor Coverage',
                    value: `${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}`,
                    copy: `pending ${warmup.monitor_pending_symbols_total} · monitor`
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
                    copy: `compute ${formatAgo(computeHealth.last_compute)}`
                },
                {
                    label: 'Data Freshness',
                    value: dataIsLoading ? 'LOADING' : (dataHealth.last_bar_age_min != null ? `${dataHealth.last_bar_age_min}m` : '--'),
                    copy: dataHealth.last_bar_time_ms ? `${dataHealth.last_symbol || 'n/a'} · ${dataHealth.last_bar_label}` : (dataIsLoading ? 'loading latest bar' : 'no latest')
                },
                {
                    label: 'Close Delay',
                    value: formatSecondsLabel(realtimeMetrics.close_delay_s),
                    copy: realtimeMetrics.last_bar_close ? `close ${formatTimeLabel(realtimeMetrics.last_bar_close)}` : 'close --'
                },
                {
                    label: 'Compute After Close',
                    value: formatSecondsLabel(realtimeMetrics.compute_after_close_s),
                    copy: realtimeMetrics.last_compute_run ? `run ${formatTimeLabel(realtimeMetrics.last_compute_run)}` : 'run --'
                },
                {
                    label: 'Active Tick Lag',
                    value: formatSecondsLabel(realtimeMetrics.active_tick_lag_s),
                    copy: `${realtimeMetrics.active_symbol_count || 0} active`
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
            const warmupSummaryText = getIbkrWarmupSummaryText(warmup, {
                elapsedText: warmupElapsedS != null ? formatDurationCompact(warmupElapsedS) : ''
            });
            const canonical = status?.canonical_5m || {};
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactor);
            const gatewayActive = runtimeStatus.gatewayActive;
            const runtimeStarted = runtimeStatus.started;
            const sessionAuthenticated = runtimeStatus.authenticated;
            const readyEngines = Number(status?.ready_engines || 0) || 0;
            const totalEngines = Number(status?.total_engines || 0) || 0;
            const twoFactorStatus = String(twoFactor?.status || '').trim().toLowerCase();
            const challengeCode = String(twoFactor?.challenge_code || '').trim();
            const recoveryPhase = String(twoFactor?.recovery_phase || '').trim().toLowerCase();
            const responsePhase = getIbkrTwoFactorResponsePhase(twoFactor);
            const runtimeMismatch = getRuntimeEnvironmentMismatch(status, twoFactor);
            const blocker = getIbkrRuntimePrimaryBlockerCardModel({
                runtimeMismatch,
                startup,
                sessionAuthenticated,
                gatewayActive,
                runtimeStarted,
                responsePhase,
                twoFactorStatus,
                challengeCode,
                twoFactorState: twoFactor,
                recoveryPhase,
                warmup,
                dataHealth,
                canonical,
                realtimeState,
                readyEngines,
                totalEngines,
            });

            const dataChain = getIbkrRuntimeDataChainCardModel({
                latestBar,
                latestIndicator,
                latestSignal,
                dataHealth,
                realtimeMetrics,
                realtimeState,
                canonical,
                warmupSummaryText,
                inflightAgeS: status?.realtime_compute?.inflight_age_s,
            });

            const shortcuts = getIbkrRuntimeQuickViewCardModel({
                barsCountLabel: formatCompactNumber(summary?.today?.ibkr_bars || 0),
                indicatorsCountLabel: formatCompactNumber(summary?.today?.ibkr_indicators || 0),
                signalsCountLabel: formatCompactNumber(summary?.today?.ibkr_signals || 0),
                latestIndicator,
                latestSignal,
                environment: currentEnvironment,
            });

            const topologyServices = getOrderedTopologyServices(status?.service_topology || {});
            const topologyReadyCount = topologyServices.filter((service) => {
                const serviceStatus = String(service?.status || '').trim().toLowerCase();
                return ['running', 'peer', 'external', 'online'].includes(serviceStatus);
            }).length;
            const topologyCard = {
                tone: topologyServices.length && topologyReadyCount >= topologyServices.length ? 'ok' : 'info',
                kicker: 'OPS ROUTING',
                title: topologyServices.length
                    ? `split ${topologyReadyCount}/${topologyServices.length} visible`
                    : 'runtime topology pending',
                copy: topologyServices.length
                    ? '控制台只保留操作前摘要；服务拓扑、主机健康和 PB 磁盘统一在运维大盘排查。'
                    : '等待 service_topology 返回 runtime / compute / gateway / pocketbase。',
                links: [
                    { label: '打开运维大盘', path: '/ibkr_monitor.html' },
                ],
            };

            const cards = [blocker, dataChain, shortcuts, topologyCard];
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

        function getOrderedTopologyServices(topologyPayload = {}) {
            const topology = topologyPayload?.services && typeof topologyPayload.services === 'object'
                ? topologyPayload.services
                : {};
            const ordered = [];
            const seen = new Set();
            ['ibkr-runtime', 'ibkr-compute', 'ibkr-gateway', 'pocketbase'].forEach((name) => {
                if (!topology[name]) return;
                ordered.push({
                    ...topology[name],
                    service_name: String(topology[name]?.service_name || name).trim() || name,
                });
                seen.add(name);
            });
            Object.keys(topology).sort().forEach((name) => {
                if (seen.has(name)) return;
                ordered.push({
                    ...topology[name],
                    service_name: String(topology[name]?.service_name || name).trim() || name,
                });
            });
            return ordered;
        }

        function getServiceTopologyTone(status) {
            const text = String(status || '').trim().toLowerCase();
            if (['running', 'peer', 'external', 'online', 'ok', 'ready', 'healthy'].includes(text)) return 'ok';
            if (['starting', 'warming', 'pending', 'degraded', 'warning', 'warn'].includes(text)) return 'warn';
            if (['error', 'failed', 'offline', 'stopped'].includes(text)) return 'error';
            return 'info';
        }

        function getMonitorServicePayload(serviceName, status = latestRuntimeStatus, monitorPayload = latestServiceMonitorPayload) {
            const name = String(serviceName || '').trim();
            const monitorServices = monitorPayload?.service_monitor?.services && typeof monitorPayload.service_monitor.services === 'object'
                ? monitorPayload.service_monitor.services
                : {};
            const topologyServices = status?.service_topology?.services && typeof status.service_topology.services === 'object'
                ? status.service_topology.services
                : {};
            return {
                ...(topologyServices[name] && typeof topologyServices[name] === 'object' ? topologyServices[name] : {}),
                ...(monitorServices[name] && typeof monitorServices[name] === 'object' ? monitorServices[name] : {}),
                service_name: name,
            };
        }

        function getServiceActionState(serviceName) {
            const key = String(serviceName || '').trim();
            const actionState = latestServiceActionStates?.[key];
            return actionState && typeof actionState === 'object' ? actionState : {};
        }

        function normalizeServiceCardStatus(serviceName, servicePayload, actionState) {
            const systemdState = actionState?.service_state && typeof actionState.service_state === 'object'
                ? actionState.service_state
                : {};
            const activeState = String(systemdState.active_state || '').trim().toLowerCase();
            if (activeState) {
                if (activeState === 'active') return 'running';
                if (activeState === 'inactive') return 'offline';
                if (activeState === 'failed') return 'failed';
                return activeState;
            }
            const rawStatus = String(servicePayload?.status || '').trim().toLowerCase();
            if (rawStatus === 'peer' || rawStatus === 'expected_remote' || rawStatus === 'embedded') return rawStatus;
            if (serviceName === 'ibkr-runtime') {
                const runtimeStatus = getEffectiveRuntimeStatusCardModel(latestRuntimeStatus, latestTwoFactorState);
                if (runtimeStatus.started || latestRuntimeStatus?.starting || latestRuntimeStatus?.startup_complete) return 'running';
                if (rawStatus) return rawStatus;
            }
            if (serviceName === 'ibkr-gateway') {
                const runtimeStatus = getEffectiveRuntimeStatusCardModel(latestRuntimeStatus, latestTwoFactorState);
                if (runtimeStatus.gatewayActive) return 'running';
                if (rawStatus) return rawStatus;
            }
            return rawStatus || 'unknown';
        }

        function getServiceCardTone(statusText) {
            const text = String(statusText || '').trim().toLowerCase();
            if (['running', 'active', 'online', 'ok', 'ready'].includes(text)) return 'ok';
            if (['peer', 'expected_remote', 'embedded', 'starting', 'activating', 'reloading'].includes(text)) return 'info';
            if (['degraded', 'warning', 'warn', 'pending'].includes(text)) return 'warn';
            if (['failed', 'offline', 'inactive', 'stopped', 'error'].includes(text)) return 'error';
            return 'info';
        }

        function getRuntimeFlowPrimaryAction(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState) {
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactorState);
            const running = Boolean(runtimeStatus.started || status?.starting || status?.startup_complete);
            return running ? 'stop' : 'start';
        }

        function renderRuntimeFlowPrimaryAction(status = latestRuntimeStatus, twoFactorState = latestTwoFactorState) {
            const button = document.getElementById('runtimeFlowToggleButton');
            if (!button) return;
            const action = getRuntimeFlowPrimaryAction(status, twoFactorState);
            const isStop = action === 'stop';
            const label = button.querySelector('.action-label');
            const copy = button.querySelector('.action-copy');
            button.dataset.action = action;
            button.classList.toggle('action-primary', !isStop);
            button.classList.toggle('action-danger', isStop);
            if (label) label.textContent = isStop ? '停止 Runtime 线程' : '启动 Runtime 线程';
            if (copy) {
                copy.textContent = isStop
                    ? '停止 runtime 内部交易/行情线程，不直接停止 Gateway systemd 服务。'
                    : '启动 runtime 内部交易/行情线程；是否重启 Gateway 由启动策略决定。';
            }
        }

        function shouldShowServiceStopAction(statusText) {
            const text = String(statusText || '').trim().toLowerCase();
            return ['running', 'active', 'online', 'ok', 'ready', 'starting', 'activating', 'reloading'].includes(text);
        }

        function formatServicePid(servicePayload, actionState) {
            const systemdState = actionState?.service_state && typeof actionState.service_state === 'object'
                ? actionState.service_state
                : {};
            const pid = Number(systemdState.main_pid || servicePayload?.pid || 0) || 0;
            return pid > 0 ? String(pid) : '--';
        }

        function formatServiceDetail(moduleDef, servicePayload, actionState) {
            const systemdState = actionState?.service_state && typeof actionState.service_state === 'object'
                ? actionState.service_state
                : {};
            const actionLabel = actionState?.action
                ? `${String(actionState.action).toUpperCase()} ${actionState.ok === false ? 'FAILED' : 'REQUESTED'}`
                : '';
            const systemdLabel = systemdState.active_state
                ? `systemd ${systemdState.active_state}/${systemdState.sub_state || '--'}`
                : '';
            const detail = String(servicePayload?.detail || servicePayload?.responsibility || moduleDef.copy || '').trim();
            return [actionLabel, systemdLabel, detail].filter(Boolean).join(' · ') || '--';
        }

        function renderServiceControlPanel(status = latestRuntimeStatus, monitorPayload = latestServiceMonitorPayload) {
            const el = document.getElementById('serviceControlGrid');
            if (!el) return;
            el.innerHTML = SERVICE_CONTROL_MODULES.map((moduleDef) => {
                const servicePayload = getMonitorServicePayload(moduleDef.service, status, monitorPayload);
                const actionState = getServiceActionState(moduleDef.service);
                const statusText = normalizeServiceCardStatus(moduleDef.service, servicePayload, actionState);
                const tone = getServiceCardTone(statusText);
                const pid = formatServicePid(servicePayload, actionState);
                const owner = String(servicePayload?.owner || servicePayload?.managed_by || '--').trim() || '--';
                const detail = formatServiceDetail(moduleDef, servicePayload, actionState);
                const restartLabel = moduleDef.highRiskRestart ? '输入服务名后重启' : '重启服务';
                const primaryAction = shouldShowServiceStopAction(statusText) ? 'stop' : 'start';
                const primaryLabel = primaryAction === 'stop' ? '停止服务' : '启动服务';
                return `
                    <div class="service-control-card ${escapeHtml(tone)}">
                        <div class="service-control-card-head">
                            <div>
                                <div class="service-control-kicker">${escapeHtml(moduleDef.kicker)}</div>
                                <div class="service-control-title">${escapeHtml(moduleDef.title)}</div>
                            </div>
                            <span class="pill ${statusClass(statusText)}">${escapeHtml(String(statusText || 'unknown').toUpperCase())}</span>
                        </div>
                        <div class="service-control-detail">${escapeHtml(detail)}</div>
                        <div class="service-control-meta">
                            <span class="mini-tag"><span class="mini-label">UNIT</span>${escapeHtml(moduleDef.service)}</span>
                            <span class="mini-tag"><span class="mini-label">PID</span>${escapeHtml(pid)}</span>
                            <span class="mini-tag"><span class="mini-label">OWNER</span>${escapeHtml(owner)}</span>
                        </div>
                        <div class="service-control-actions">
                            <button class="service-action-btn ${primaryAction === 'stop' ? 'stop' : ''}" type="button" data-service-action="${escapeHtml(moduleDef.service)}:${escapeHtml(primaryAction)}" onclick="handleServiceAction('${escapeHtml(moduleDef.service)}', '${escapeHtml(primaryAction)}')" ${actionPending ? 'disabled' : ''}>${escapeHtml(primaryLabel)}</button>
                            <button class="service-action-btn restart" type="button" data-service-action="${escapeHtml(moduleDef.service)}:restart" onclick="handleServiceAction('${escapeHtml(moduleDef.service)}', 'restart')" ${actionPending ? 'disabled' : ''}>${escapeHtml(restartLabel)}</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function renderServiceTopology(status = {}) {
            const el = document.getElementById('serviceTopologyArea');
            if (!el) return;
            const topologyPayload = status?.service_topology || {};
            const services = getOrderedTopologyServices(topologyPayload);
            const monitorHref = buildPageUrl('/ibkr_monitor.html', {}, { environment: currentEnvironment });
            if (!services.length) {
                el.innerHTML = `
                    <div class="runtime-link-summary">
                        <div class="runtime-link-copy">
                            <div class="runtime-link-title">链路摘要暂不可用</div>
                            <div class="runtime-link-sub">控制台只保留操作前关键依赖；完整 split-stack 服务拓扑、主机健康和 PB 细节请进运维大盘。</div>
                        </div>
                        <a class="runtime-link-action" href="${monitorHref}">打开运维大盘 →</a>
                    </div>
                    <div class="table-empty">暂无链路摘要</div>
                `;
                return;
            }

            const runtimeMode = String(topologyPayload?.runtime_mode || '--').trim().toUpperCase() || '--';
            const serviceProfile = String(topologyPayload?.service_profile || '--').trim().toUpperCase() || '--';
            const restartIndependent = topologyPayload?.restart_independent ? 'YES' : 'NO';
            const serviceByName = services.reduce((acc, service) => {
                const name = String(service?.service_name || '').trim();
                if (name) acc[name] = service;
                return acc;
            }, {});
            const summarizeService = (name, label, copyFallback) => {
                const service = serviceByName[name] || {};
                const statusText = String(service?.status || 'unknown').trim() || 'unknown';
                const tone = getServiceTopologyTone(statusText);
                const copy = String(service?.detail || service?.responsibility || service?.kind || copyFallback || '').trim() || '--';
                return { label, statusText, tone, copy };
            };
            const cards = [
                summarizeService('ibkr-runtime', 'Runtime', `mode ${runtimeMode}`),
                summarizeService('ibkr-gateway', 'Gateway', 'IBC + IB Gateway session path'),
                summarizeService('ibkr-compute', 'Compute', 'compute engines / manual actions'),
                summarizeService('pocketbase', 'PocketBase', 'state / config / event store'),
            ];

            el.innerHTML = `
                <div class="service-topology-shell">
                    <div class="runtime-link-summary">
                        <div class="runtime-link-copy">
                            <div class="runtime-link-title">控制台只看操作前关键依赖</div>
                            <div class="runtime-link-sub">当前 ${escapeHtml(runtimeMode)} / ${escapeHtml(serviceProfile)} · restart independent ${escapeHtml(restartIndependent)}。完整拓扑、主机健康、请求与 PB 磁盘统一进运维大盘。</div>
                        </div>
                        <a class="runtime-link-action" href="${monitorHref}">打开运维大盘 →</a>
                    </div>
                    <div class="runtime-link-grid">
                        ${cards.map((card) => {
                            const pillState = card.statusText.toLowerCase() === 'peer'
                                ? 'ready'
                                : (card.statusText.toLowerCase() === 'external' ? 'online' : card.statusText);
                            return `
                                <div class="runtime-link-card ${escapeHtml(card.tone)}">
                                    <div class="runtime-link-card-head">
                                        <div class="runtime-link-card-label">${escapeHtml(card.label)}</div>
                                        <span class="pill ${statusClass(pillState)}">${escapeHtml(card.statusText.toUpperCase())}</span>
                                    </div>
                                    <div class="runtime-link-card-copy">${escapeHtml(card.copy)}</div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                    <div class="tag-row">
                        <span class="mini-tag"><span class="mini-label">Mode</span><span>${escapeHtml(runtimeMode)}</span></span>
                        <span class="mini-tag"><span class="mini-label">Profile</span><span>${escapeHtml(serviceProfile)}</span></span>
                        <span class="mini-tag"><span class="mini-label">Restart</span><span>${escapeHtml(restartIndependent)}</span></span>
                        <span class="mini-tag"><span class="mini-label">Services</span><span>${escapeHtml(String(services.length))}</span></span>
                    </div>
                </div>
            `;
        }

        function renderHero(summary, health, status, runtimeConfig, twoFactorState, startupState, latestBar) {
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const warmup = normalizeWarmup(status);
            const startup = normalizeStartupUiState(startupState);
            const warmupSummaryText = getIbkrWarmupSummaryText(warmup);
            const dataStatus = dataHealth.status || 'no_data';
            const computeStatus = computeHealth.status || 'unknown';
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactorState);
            const gatewayRunning = runtimeStatus.gatewayActive;
            const sessionAuthenticated = runtimeStatus.authenticated;
            const twoFactorStatus = String(twoFactorState?.status || '').trim().toLowerCase() || 'idle';
            latestNextActionModel = deriveNextAuthActionModel(status, twoFactorState, startup);
            renderAuthActionBanner(latestNextActionModel);
            const gatewayChipLabel = runtimeStatus.snapshotIncomplete
                ? 'Gateway VERIFIED'
                : `Gateway ${gatewayRunning ? 'ACTIVE' : 'OFFLINE'}`;
            const sessionChipLabel = runtimeStatus.snapshotIncomplete
                ? 'Session AUTHED'
                : `Session ${sessionAuthenticated ? 'AUTHED' : 'PENDING'}`;
            const activeEnvironmentTone = String(currentEnvironment || '').trim().toLowerCase() === 'live'
                ? 'chip-warn'
                : 'chip-muted';
            const chips = [
                { label: `Compute ${String(computeStatus).toUpperCase()}`, tone: chipTone(computeStatus) },
                { label: `Data ${String(dataStatus).toUpperCase()}`, tone: chipTone(dataStatus) },
                { label: gatewayChipLabel, tone: gatewayRunning ? 'chip-ok' : 'chip-error' },
                { label: sessionChipLabel, tone: sessionAuthenticated ? 'chip-ok' : 'chip-warn' },
                { label: `Warmup ${warmup.gate_open ? 'READY' : String(warmup.phase || 'idle').toUpperCase()}`, tone: warmup.gate_open ? 'chip-ok' : chipTone(warmup.phase) },
                { label: `2FA ${twoFactorStatus.toUpperCase()}`, tone: sessionAuthenticated ? 'chip-ok' : chipTone(twoFactorStatus) },
                ...(runtimeStatus.snapshotIncomplete ? [{ label: 'Runtime SNAPSHOT REFRESHING', tone: 'chip-muted' }] : []),
                ...(startup.active ? [{ label: `Flow ${getManualAuthReasonLabel(startup.reason)}`, tone: chipTone(startup.status || 'active') }] : []),
                { label: `Trading ${summary?.ibkr_trading_enabled ? 'ON' : 'OFF'}`, tone: summary?.ibkr_trading_enabled ? 'chip-ok' : 'chip-error' },
                { label: `Compute ${summary?.compute_enabled ? 'ON' : 'OFF'}`, tone: summary?.compute_enabled ? 'chip-ok' : 'chip-error' },
                { label: `Runtime ${String(status?.service_topology?.runtime_mode || '--').toUpperCase()}`, tone: 'chip-muted' },
                { label: `Active Env ${String(currentEnvironment).toUpperCase()}`, tone: activeEnvironmentTone }
            ];
            document.getElementById('heroBadges').innerHTML = chips.map((chip) => `
                <span class="status-chip ${chip.tone}"><span class="dot" style="background:currentColor"></span>${escapeHtml(chip.label)}</span>
            `).join('');

            const computeBase = runtimeConfig.find((item) => item.key === 'ibkr_compute_internal_url')?.value
                || summary?.config?.ibkr_compute_internal_url
                || status?.service_topology?.services?.['ibkr-compute']?.internal_url
                || 'http://127.0.0.1:5100';
            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            document.getElementById('computeBaseInfo').textContent = `compute base: ${computeBase} · runtime ${String(runtimeService.runtime_mode || '--')} · ${String(runtimeService.internal_url || '--')}`;
            const effectiveReasonLabel = getManualAuthReasonLabel(getEffectiveManualAuthReason(status, twoFactorState, startup));
            const twoFactorMode = String(twoFactorState?.mode || '').trim().toLowerCase();
            const recoveryPhase = String(twoFactorState?.recovery_phase || '').trim().toLowerCase();
            const runtimeMismatch = getRuntimeEnvironmentMismatch(status, twoFactorState);
            const keepCurrentCycle = isIbkrTwoFactorCycleActive(twoFactorState);
            const lastRequestAtLabel = twoFactorState?.last_request_at ? formatTimeLabel(twoFactorState.last_request_at) : '';
            const authSummary = runtimeStatus.snapshotIncomplete
                ? '2FA success · runtime status snapshot incomplete · no action needed'
                : getIbkrRuntimeHeroAuthSummaryText({
                runtimeMismatch,
                runtimeStatus,
                startupActive: startup.active,
                startupStepLabel: getStartupCurrentStepLabel(startup) || 'startup active',
                warmupSummaryText,
                effectiveReasonLabel,
                twoFactorStatus,
                twoFactorMode,
                recoveryPhase,
                lastRequestAtLabel: lastRequestAtLabel && lastRequestAtLabel !== '--' ? lastRequestAtLabel : '',
                keepCurrentCycle
            });
            document.getElementById('authInfo').textContent = startup.startup_label
                ? `${authSummary} · ${startup.startup_label}`
                : authSummary;
            setPageContextMeta([
                { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
                { label: 'Market Date', value: String(status?.market_universe?.market_date || '--') },
                { label: 'Runtime', value: String(status?.service_topology?.runtime_mode || '--').toUpperCase() },
                { label: 'Session', value: sessionAuthenticated ? 'AUTHED' : 'PENDING', tone: sessionAuthenticated ? 'ok' : 'warn' },
            ]);
            setPageRefreshTime();
            document.getElementById('refreshInfo').textContent = '状态已加载';
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
            const computeStartupPreload = getComputeStartupPreload(status, health, summary);
            const autoRestoreGuard = status?.auto_restore_guard || {};
            const sessionAuthenticated = Boolean(status?.session?.authenticated);
            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            const computeService = status?.service_topology?.services?.['ibkr-compute'] || {};
            const latestBarWrite = latestBar ? String(getComputedTimeLabel(latestBar)).slice(0, 19) : '--';
            const latestIndicatorCalc = latestIndicator ? String(getComputedTimeLabel(latestIndicator)).slice(0, 19) : '--';
            const latestSignalTime = latestSignal ? String((latestSignal.us_time || latestSignal.created || '--')).slice(0, 19) : '--';
            const rows = [
                ['Runtime Service', String(runtimeService.status || '--').toUpperCase()],
                ['Runtime Owner', String(runtimeService.owner || '--')],
                ['Runtime Mode', String(runtimeService.runtime_mode || '--').toUpperCase()],
                ['Runtime Internal URL', String(runtimeService.internal_url || '--')],
                ['Compute Upstream', String(computeService.upstream || '--')],
                ['Restart Independent', runtimeService.restart_independent ? 'YES' : 'NO'],
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
                ['Weekly Deadline', twoFactor?.business_deadline_cn ? `${String(twoFactor.business_deadline_cn)} CN / ${String(twoFactor.business_deadline_at || '--')} US` : '--'],
                ['Cycle Deadline', twoFactor?.confirm_deadline_cn ? `${String(twoFactor.confirm_deadline_cn)} CN / ${String(twoFactor.confirm_deadline_at || '--')} US` : '--'],
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
                ['Compute Preload', formatComputeStartupPreloadSummary(computeStartupPreload)],
                ['Preload Envs', computeStartupPreload.environments.length ? computeStartupPreload.environments.join(', ') : '--'],
                ['Preload Start', formatComputeStartupPreloadTimestamp(computeStartupPreload.startedAt)],
                ['Preload Finish', formatComputeStartupPreloadTimestamp(computeStartupPreload.finishedAt)],
                ['Uptime', `${Math.round(Number(computeHealth.uptime_s || 0) / 60)} min`],
                ['Backfill Written', String(status?.data_backfill?.total_backfilled || 0)],
                ['Canonical Due Bucket', String(canonical?.last_due_bucket_us || '--')],
                ['Canonical Completed Bucket', String(canonical?.last_completed_bucket_us || '--')],
                ['Canonical Lag', formatSecondsLabel(canonical?.lag_s)],
                ['Canonical Written Bars', String(canonical?.last_written_bars || 0)],
                ['Canonical Pending', String(canonical?.pending_symbols_total || 0)],
                ['Canonical Pending Symbols', (Array.isArray(canonical?.pending_symbols) && canonical.pending_symbols.length) ? canonical.pending_symbols.slice(0, 8).join(', ') : '--'],
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
            const runtimeMismatch = getRuntimeEnvironmentMismatch(latestRuntimeStatus, state);
            const panel = getIbkrTwoFactorPanelViewModel({
                twoFactorState: state,
                runtimeMismatch,
                manualAuthReasonLabel: state?.reason ? getManualAuthReasonLabel(state.reason) : '--',
            });

            document.getElementById('twoFactorPanel').innerHTML = `
                <div class="challenge-shell">
                    <div class="challenge-head">
                        <span class="mini-tag"><span class="mini-label">STATUS</span><span class="pill ${statusClass(panel.statusKey)}">${escapeHtml(panel.statusText)}</span></span>
                        <span class="mini-tag"><span class="mini-label">MODE</span>${escapeHtml(panel.modeText)}</span>
                        <span class="mini-tag"><span class="mini-label">RECOVERY</span>${escapeHtml(panel.recoveryText)}</span>
                        <span class="mini-tag"><span class="mini-label">SOURCE</span>${escapeHtml(panel.sourceText)}</span>
                        <span class="mini-tag"><span class="mini-label">RESPONSE</span>${escapeHtml(panel.responseText)}</span>
                    </div>
                    <div class="challenge-code ${panel.challengeCode ? '' : 'is-empty'}" id="challengeCodeDisplay">${escapeHtml(panel.challengeDisplayText)}</div>
                    <div class="challenge-copy">${escapeHtml(panel.helperText)}</div>
                    ${panel.metaItems.length ? `<div class="challenge-copy">${escapeHtml(panel.metaItems.join(' · '))}</div>` : ''}
                    ${panel.canSubmit ? `
                        <label class="response-label" for="challengeResponseInput">Response Code</label>
                        <div class="response-row">
                            <input
                                id="challengeResponseInput"
                                class="response-input"
                                type="text"
                                inputmode="numeric"
                                autocomplete="off"
                                spellcheck="false"
                                placeholder="${panel.inputPlaceholder}"
                            />
                            <button id="challengeResponseSubmit" class="response-btn" onclick="submitTwoFactorResponse()">提交响应码</button>
                        </div>
                    ` : panel.showResetCta ? `
                        <label class="response-label">Reset Recommended</label>
                        <div class="response-row">
                            <button class="response-btn response-danger" onclick="handleRuntimeAction('panic_reset_2fa')">重开 2FA</button>
                        </div>
                    ` : ''}
                </div>
            `;
        }

        function renderCronSummary(definitions, configMap) {
            const cronCards = Array.isArray(definitions)
                ? definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentEnvironment))
                : [];
            if (!cronCards.length) {
                return renderEmpty('暂无 IBKR Scheduler job 定义');
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
                                        <span class="label">执行器</span>
                                        <span>${escapeHtml(card.modeLabel)}</span>
                                    </div>
                                </div>
                                <div class="cron-chip-row">
                                    <span class="mini-tag"><span class="mini-label">STATUS</span>${escapeHtml(card.statusLabel)}</span>
                                    <span class="mini-tag"><span class="mini-label">SCHED</span>${card.schedulerEnabled ? 'ON' : 'OFF'}</span>
                                    <span class="mini-tag"><span class="mini-label">JOB</span>${card.cronEnabled ? 'ON' : 'OFF'}</span>
                                    <span class="mini-tag"><span class="mini-label">ENV</span>${escapeHtml(card.environmentLabel)}</span>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
        }

        function renderConfigDetail(summary, runtimeConfig, schedulerPayload) {
            const configMap = buildIbkrConfigMap(summary, runtimeConfig);
            const cronDefinitions = Array.isArray(schedulerPayload?.items) ? schedulerPayload.items : [];
            const scheduler = getIbkrSchedulerSummary(schedulerPayload?.scheduler || {}, currentEnvironment);
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
                        <div class="config-block-label">IBKR Scheduler 摘要</div>
                        <div class="tag-row">
                            <span class="mini-tag"><span class="mini-label">STATUS</span>${escapeHtml(String(scheduler.status || '--').toUpperCase())}</span>
                            <span class="mini-tag"><span class="mini-label">LOOP</span>${escapeHtml(scheduler.loopIntervalLabel)}</span>
                            <span class="mini-tag"><span class="mini-label">LAG</span>${escapeHtml(scheduler.dispatchLagLabel)}</span>
                            <span class="mini-tag"><span class="mini-label">JOBS</span>${escapeHtml(`${scheduler.enabledJobCount || 0}/${scheduler.jobCount || 0}`)}</span>
                            <span class="mini-tag"><span class="mini-label">DISPATCH</span>${escapeHtml(scheduler.lastDispatchLabel)}</span>
                        </div>
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
            const dataIsLoading = dataHealth.status === 'loading';
            const realtimeState = deriveRealtimeComputeState(status);
            const warmup = normalizeWarmup(status);
            const warmupElapsedS = getWarmupElapsedSeconds(warmup);
            const canonical = status?.canonical_5m || {};
            const latestBarExtra = getExtraObject(latestBar);
            const indicatorExtra = getExtraObject(latestIndicator);
            const signalExtra = getExtraObject(latestSignal);
            const engineCount = Number(status?.total_engines || 0) || 0;
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactorState);
            const isAuthenticated = runtimeStatus.authenticated;
            const gatewayActive = runtimeStatus.gatewayActive;

            let chainValue = dataIsLoading ? 'LOADING' : 'WAITING';
            let chainCopy = dataIsLoading ? '正在加载最近 bars' : '等待 bars 写入';
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
                    value: latestBar ? `${latestBar.symbol || '--'} ${formatIbkrIntervalLabel(latestBar.interval)}` : (dataIsLoading ? 'LOADING' : '--'),
                    copy: latestBar ? `${getRecordBarLabel(latestBar)} · ${latestBar.session_type || 'session?'}` : (dataIsLoading ? '加载最近 bars...' : '无 bar'),
                },
                {
                    label: 'Latest Indicator',
                    value: latestIndicator ? `${latestIndicator.symbol || '--'} ${formatIbkrIntervalLabel(latestIndicator.interval)}` : '--',
                    copy: latestIndicator ? `${getRecordBarLabel(latestIndicator)} · calc ${getComputedTimeLabel(latestIndicator)}` : '无指标',
                },
                {
                    label: 'Latest Signal',
                    value: latestSignal ? `${latestSignal.symbol || '--'} ${String(latestSignal.direction || '--').toUpperCase()}` : '--',
                    copy: latestSignal ? `${getRecordBarLabel(latestSignal)} · ${latestSignal.status || '--'}` : '无新信号',
                },
                {
                    label: 'Warmup Gate',
                    value: warmup.gate_open ? 'OPEN' : String(warmup.phase || 'idle').toUpperCase(),
                    copy: `${warmup.ready_trade_symbols}/${warmup.trade_symbols_total} trade`
                        + ` · block ${warmup.blocking_pending_symbols_total}`,
                },
                {
                    label: 'Monitor Coverage',
                    value: `${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}`,
                    copy: `pending ${warmup.monitor_pending_symbols_total} · monitor`,
                }
            ];

            const notes = [];
            if (dataIsLoading) {
                notes.push({ tone: 'info', text: '正在加载最近 bars，暂不把首屏空值判定为 NO_DATA。' });
            } else if (!latestBarMs) {
                notes.push({ tone: 'error', text: '当前环境没有 bars，先检查 Gateway 会话、行情订阅和写入链路。' });
            }
            if (latestBarMs && dataHealth.last_bar_age_min != null && dataHealth.last_bar_age_min > 30) {
                notes.push({ tone: 'error', text: `最新 live bar 停在 ${formatBarTimeMsToET(latestBarMs)}，已经落后 ${dataHealth.last_bar_age_min} 分钟。` });
            }
            if (latestBarMs && !latestIndicatorMs) {
                notes.push({ tone: 'error', text: 'bars 已存在但 indicators 为空，说明 compute 尚未真正落库。' });
            }
            if (latestBarMs && latestIndicatorMs && latestIndicatorMs < latestBarMs - 5 * 60 * 1000) {
                notes.push({ tone: 'warn', text: `indicator 落后 ${Math.round((latestBarMs - latestIndicatorMs) / 60000)} 分钟，请检查 compute。` });
            }
            if (latestBarMs && normalizeIbkrInterval(latestBar?.interval) === '5m') {
                notes.push({ tone: 'ok', text: '页面展示的是最新已收盘 5m bar，时间标签是 bar 起始时间，不显示正在形成的那根，所以视觉上会慢一根。' });
            }
            if (latestBarMs && !latestBarExtra.computed_at_us && !latestBarExtra.computed_at_cn) {
                notes.push({ tone: 'warn', text: '最新 bar 的 extra 仍为空，当前看到的是旧写入记录，暂时无法核对 bar 实际写入时间。' });
            }
            if (!engineCount) {
                notes.push({ tone: 'warn', text: '当前 compute engines = 0，说明内存引擎还没有被 bars 预热。' });
            }
            if (runtimeStatus.snapshotIncomplete) {
                notes.push({ tone: 'ok', text: '2FA 已认证成功；当前 Gateway / Session 的离线字样来自运行态快照未刷新，不需要重新触发 2FA。' });
            } else if (!gatewayActive) {
                notes.push({ tone: 'error', text: 'Gateway 当前不可达，先恢复网关进程，再谈 2FA 和 bars 刷新。' });
            } else if (!runtimeStatus.started) {
                notes.push({ tone: 'warn', text: '当前 runtime service 没有真正拉起；这种状态下会看到 Session 待认证，但根因通常是服务停止或刚重启后未恢复。' });
            } else if (!isAuthenticated) {
                notes.push({ tone: 'warn', text: 'Gateway 已在线，但 IBKR Session 仍未认证，新的 bars/高周期 bars 不会持续刷新。' });
            }
            if (warmup.phase === 'pending' || warmup.phase === 'running') {
                notes.push({ tone: 'warn', text: `预热中：trade ${warmup.ready_trade_symbols}/${warmup.trade_symbols_total} · block ${warmup.blocking_pending_symbols_total} · monitor ${warmup.ready_monitor_symbols}/${warmup.monitor_symbols_total}。` });
            } else if (warmup.phase === 'failed') {
                notes.push({ tone: 'error', text: `warmup 失败：${warmup.last_error || '需要检查回填与 compute 日志。'}` });
            } else if (warmup.trade_symbols_total > 0 && !warmup.gate_open) {
                notes.push({ tone: 'warn', text: `交易闸门关闭：${(warmup.blocking_pending_symbols || []).join(', ') || '部分目标'} 未预热。` });
            } else if (warmupElapsedS != null && warmupElapsedS >= 120) {
                notes.push({ tone: 'warn', text: `warmup 已耗时 ${formatDurationCompact(warmupElapsedS)}，请等待修复完成。` });
            }
            if (realtimeState.phase === 'stalled') {
                notes.push({ tone: 'error', text: `bar 到 ${String(canonical.last_completed_bucket_us || '--')}，compute 卡住：${realtimeState.summary}。` });
            } else if (realtimeState.phase === 'running') {
                notes.push({ tone: 'warn', text: `bar 到 ${String(canonical.last_completed_bucket_us || '--')}，指标追平中：${realtimeState.summary}。` });
            } else if (realtimeState.phase === 'queued') {
                notes.push({ tone: 'warn', text: `5m 到 ${String(canonical.last_completed_bucket_us || '--')}，indicators 排队中：${realtimeState.summary}。` });
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
            const environmentReadySummary = buildIbkrEngineEnvironmentReadySummary(status?.engines, {
                preferredOrder: [currentEnvironment],
            });
            const hasEngineSummaryOnly = Boolean(status?.engines_available) && !Boolean(status?.engines_included);
            document.getElementById('engineHint').textContent = hasEngineSummaryOnly
                ? `${readySummary}${environmentReadySummary ? ` · ${environmentReadySummary}` : ''} · loading detail`
                : `${readySummary}${environmentReadySummary ? ` · ${environmentReadySummary}` : ''}`;
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
                const fullStatus = await requestIbkrEnvironmentJson('/api/custom/ibkr/statusz?full=1', currentEnvironment, { retryAttempts: 3 });
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

        function renderBarsTable(items, options = {}) {
            if (!items.length) {
                document.getElementById('barsTable').innerHTML = renderEmpty(options.loading ? '正在加载最近 bars...' : '暂无 bars 数据');
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
                                <td>${escapeHtml(formatIbkrIntervalLabel(item.interval))}</td>
                                <td>${formatMoney(item.close)}</td>
                                <td>${escapeHtml(getExtraObject(item).source || '--')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderIndicatorsTable(items, options = {}) {
            if (!items.length) {
                document.getElementById('indicatorsTable').innerHTML = renderEmpty(options.loading ? '正在加载最近指标...' : '暂无最近指标');
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
                                    <td>${escapeHtml(formatIbkrIntervalLabel(item.interval || extra.chart_tf))}</td>
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
            if (!ensureIbkrPageAuth()) return;
            const loadId = ++latestRuntimeLoadId;
            const isInitialLoad = !hasLoadedRuntimeData;
            if (isInitialLoad) {
                setIbkrPageLoading(
                    true,
                    '控制台加载中',
                    `正在拉取 ${getEnvironmentLabel(currentEnvironment)} 环境的 runtime、2FA、配置与最近链路数据。`
                );
            }
            try {
                const envFilter = buildEnvironmentFilter();
                const [health, status, summary, monitorResp, cronResp, twoFactorResp, startupResp, runtimeConfigResp, signalsResp, ordersResp, eventsResp] = await Promise.all([
                    requestIbkrEnvironmentJson('/api/custom/ibkr/healthz', currentEnvironment, { retryAttempts: 3 }),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/statusz?lite=1', currentEnvironment, { retryAttempts: 3 }),
                    requestIbkrEnvironmentJson('/api/custom/system/summaryz?lite=1', currentEnvironment, { retryAttempts: 3 }),
                    withTimeout(
                        requestIbkrEnvironmentJson('/api/custom/system/monitorz', currentEnvironment, { retryAttempts: 2 }),
                        9000,
                        'system/monitorz'
                    ).catch(() => ({ service_monitor: { services: {} } })),
                    requestIbkrEnvironmentJson('/api/custom/system/cronz', currentEnvironment, { retryAttempts: 3 }).catch(() => ({ items: [] })),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/2fa/status', currentEnvironment, { retryAttempts: 3 }),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/startup/status', currentEnvironment, { retryAttempts: 3 }).catch(() => ({ state: {} })),
                    requestIbkrEnvironmentJson('/api/custom/ibkr/runtime/config', currentEnvironment, { retryAttempts: 3 }),
                    apiFetch('ibkr_signals', { filter: envFilter, sort: '-created', perPage: 8 }),
                    apiFetch('orders', { filter: envFilter, sort: '-created', perPage: 8 }),
                    apiFetch('system_events', { filter: envFilter, sort: '-created', perPage: 8 })
                ]);
                if (loadId !== latestRuntimeLoadId) return;

                const runtimeConfig = Array.isArray(runtimeConfigResp?.items) ? runtimeConfigResp.items : [];
                const baseSummary = {
                    ...(summary || {}),
                    today: {
                        ...((summary && summary.today) || {}),
                    }
                };
                latestRuntimeStatus = status || {};
                latestServiceMonitorPayload = monitorResp || {};
                const twoFactorState = twoFactorResp?.state || {};
                latestTwoFactorState = deriveTwoFactorUiState(twoFactorState);
                const startupState = startupResp?.state || {};
                latestStartupState = normalizeStartupUiState(startupState);
                const signalItems = toArray(signalsResp);
                let latestBar = latestRuntimeBarsSnapshot[0] || null;
                const latestSignal = signalItems[0] || null;

                const renderRuntimeSnapshot = (resolvedSummary, indicatorItems = latestRuntimeIndicatorSnapshot, options = {}) => {
                    const latestIndicator = indicatorItems[0] || null;
                    const previousLoading = runtimeRecentDataLoading;
                    runtimeRecentDataLoading = Boolean(options.dataLoading);
                    try {
                        renderHero(resolvedSummary, health, status, runtimeConfig, twoFactorState, latestStartupState, latestBar);
                        renderOpsGrid(resolvedSummary, status, twoFactorState, latestStartupState, latestBar, latestIndicator, latestSignal);
                        renderMetricCards(resolvedSummary, health, status, twoFactorState, latestBar);
                        renderRuntimeDetail(resolvedSummary, health, status, twoFactorState, latestStartupState, latestBar, latestIndicator, latestSignal);
                        renderConfigDetail(resolvedSummary, runtimeConfig, cronResp || {});
                        renderPipelinePanel(resolvedSummary, status, twoFactorState, latestBar, latestIndicator, latestSignal);
                        renderRuntimeFlowPrimaryAction(status, latestTwoFactorState);
                        renderServiceControlPanel(status, latestServiceMonitorPayload);
                        renderIndicatorsTable(indicatorItems, { loading: Boolean(options.dataLoading) && !indicatorItems.length });
                    } finally {
                        runtimeRecentDataLoading = previousLoading;
                    }
                };

                const recentDataLoading = !latestBar;
                renderRuntimeSnapshot(baseSummary, latestRuntimeIndicatorSnapshot, { dataLoading: recentDataLoading });
                renderTwoFactorPanel(twoFactorState);
                syncActionLocks();
                renderEngineTable(status);
                renderServiceTopology(status);
                void loadEngineDetail(loadId, status);
                renderBarsTable(latestRuntimeBarsSnapshot, { loading: recentDataLoading && !latestRuntimeBarsSnapshot.length });
                renderSignalsTable(signalItems);
                renderOrdersTable(toArray(ordersResp));
                renderEventsTable(toArray(eventsResp));

                if (showToastOnSuccess) showToast('Runtime 数据已刷新');

                const recentMarketDate = resolveRuntimeMarketDate(status);
                const recentRecordFilter = `created >= "${escapeQueryValue(`${recentMarketDate} 00:00:00`)}" && ${envFilter}`;
                const barsPromise = apiFetch('ibkr_bars', {
                    filter: recentRecordFilter,
                    sort: '-bar_time_ms',
                    perPage: 8,
                }).catch((error) => {
                    console.warn('加载最近 bars 失败:', error);
                    return { items: [] };
                });
                const indicatorsPromise = apiFetch('ibkr_indicators', {
                    filter: recentRecordFilter,
                    sort: '-bar_time_ms',
                    perPage: 8,
                }).catch((error) => {
                    console.warn('加载最近 indicators 失败:', error);
                    return { items: [] };
                });

                void Promise.all([
                    barsPromise,
                    indicatorsPromise,
                    loadRuntimeTodayCounts(status).catch(() => null),
                ]).then(([barsResp, indicatorsResp, todayCounts]) => {
                    if (loadId !== latestRuntimeLoadId) return;
                    const barsItems = toArray(barsResp);
                    const indicatorItems = toArray(indicatorsResp);
                    latestRuntimeBarsSnapshot = barsItems;
                    latestRuntimeIndicatorSnapshot = indicatorItems;
                    latestBar = barsItems[0] || null;
                    const resolvedSummary = {
                        ...baseSummary,
                        today: {
                            ...(baseSummary.today || {}),
                            ...(todayCounts || {}),
                        }
                    };
                    renderRuntimeSnapshot(resolvedSummary, indicatorItems);
                    renderBarsTable(barsItems);
                    syncActionLocks();
                });
            } catch (error) {
                if (shouldIgnoreRuntimeLoadError(error, loadId)) return;
                console.error('Runtime 加载失败:', error);
                document.getElementById('refreshInfo').textContent = '加载失败';
                document.getElementById('lastAction').textContent = `加载失败：${error.message || error}`;
                showToast(`加载失败: ${error.message || error}`);
            } finally {
                if (isInitialLoad) {
                    hasLoadedRuntimeData = true;
                    setIbkrPageLoading(false);
                }
            }
        }

        async function submitTwoFactorResponse() {
            if (!ensureIbkrPageAuth()) return;
            const runtimeMismatch = getRuntimeEnvironmentMismatch();
            if (runtimeMismatch) {
                document.getElementById('lastAction').textContent = runtimeMismatch.message;
                showToast(runtimeMismatch.message);
                return;
            }
            const twoFactorState = deriveTwoFactorUiState(latestTwoFactorState);
            const responsePhase = getIbkrTwoFactorResponsePhase(twoFactorState);
            const input = document.getElementById('challengeResponseInput');
            const button = document.getElementById('challengeResponseSubmit');
            const responseCode = normalizeCode(input?.value || '');
            const challengeCode = String(twoFactorState?.challenge_code || '').trim();

            if (!challengeCode) {
                showToast('当前没有可提交的 Challenge');
                return;
            }
            if (responsePhase.showResetCta) {
                const message = '2FA / Session 可能失配，请重开 2FA。';
                document.getElementById('lastAction').textContent = message;
                showToast(message);
                return;
            }
            if (!responsePhase.canSubmit) {
                const message = getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
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
                const payload = await requestIbkrEnvironmentJson('/api/custom/ibkr/2fa/respond', currentEnvironment, {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        response_code: responseCode,
                        challenge_code: challengeCode,
                        source: 'runtime_page'
                    },
                    retryAttempts: 3
                });
                if (input) input.value = '';
                document.getElementById('lastAction').textContent = `最近动作：Challenge 已提交 (${payload.status || 'ok'})`;
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
                        message: '已请求 2FA 卡片；请在飞书验证。'
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
                        message: '已开始新一轮 2FA，请查看手机。'
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
                emergency_all: { path: '/api/custom/ibkr/emergency-stop', body: { action: 'all', environment: currentEnvironment } },
                recover_all: { path: '/api/custom/ibkr/recover', body: { action: 'all', environment: currentEnvironment } }
            };
        }

        async function executeRuntimeAction(action, overrideTarget = null) {
            const target = overrideTarget || getRuntimeActionMap()[action];
            if (!target) return;

            setActionState(true);
            const pendingMessage = `执行中：${action} ...`;
            document.getElementById('lastAction').textContent = pendingMessage;
            setAuthActionFeedback(pendingMessage, 'info');
            try {
                const payload = await requestIbkrEnvironmentJson(target.path, currentEnvironment, {
                    method: 'POST',
                    body: target.body,
                    retryAttempts: 3
                });
                const message = summarizeAction(action, payload);
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                setAuthActionFeedback(`最近动作：${message}`, payload?.ok === false ? 'error' : 'ok');
                showToast(message);
                await loadRuntimeData(false);
            } catch (error) {
                const message = `动作失败：${action} · ${error.message || error}`;
                document.getElementById('lastAction').textContent = message;
                setAuthActionFeedback(message, 'error');
                showToast(message);
            } finally {
                setActionState(false);
            }
        }

        function getServiceModuleDef(serviceName) {
            const service = String(serviceName || '').trim();
            return SERVICE_CONTROL_MODULES.find((item) => item.service === service) || null;
        }

        function getServiceActionLabel(action) {
            const normalized = String(action || '').trim().toLowerCase();
            if (normalized === 'restart') return '重启';
            if (normalized === 'stop') return '停止';
            return '启动';
        }

        function confirmServiceAction(serviceName, action) {
            const moduleDef = getServiceModuleDef(serviceName);
            if (!moduleDef) return false;
            const service = moduleDef.service;
            const normalizedAction = String(action || '').trim().toLowerCase();
            const label = getServiceActionLabel(normalizedAction);
            const highRiskAction = moduleDef.highRiskRestart && ['restart', 'stop'].includes(normalizedAction);
            if (highRiskAction) {
                const answer = window.prompt(`确认${label} ${service}？请输入服务名：${service}`);
                return String(answer || '').trim() === service;
            }
            return window.confirm(`确认${label} ${service}？`);
        }

        function summarizeServiceAction(payload) {
            const service = String(payload?.service || '').trim() || 'service';
            const action = String(payload?.action || '').trim() || 'action';
            const actionLabel = getServiceActionLabel(action);
            const state = payload?.service_state || {};
            const activeState = String(state?.active_state || '').trim();
            const subState = String(state?.sub_state || '').trim();
            const pid = Number(state?.main_pid || 0) || 0;
            const stateText = activeState ? `${activeState}${subState ? `/${subState}` : ''}${pid ? ` · pid ${pid}` : ''}` : '';
            if (payload?.ok === false) {
                const error = payload?.command?.stderr || payload?.error || payload?.message || 'failed';
                return `${service} ${actionLabel}失败：${error}`;
            }
            return `${service} ${actionLabel}已请求${stateText ? ` · ${stateText}` : ''}`;
        }

        async function handleServiceAction(serviceName, action) {
            if (actionPending) return;
            const moduleDef = getServiceModuleDef(serviceName);
            const normalizedAction = String(action || '').trim().toLowerCase();
            if (!moduleDef || !['start', 'stop', 'restart'].includes(normalizedAction)) {
                const message = '不支持的服务动作。';
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                showToast(message);
                return;
            }
            const runtimeMismatch = getRuntimeEnvironmentMismatch();
            if (runtimeMismatch && ['ibkr-runtime', 'ibkr-gateway'].includes(moduleDef.service)) {
                document.getElementById('lastAction').textContent = runtimeMismatch.message;
                setAuthActionFeedback(runtimeMismatch.message, 'error');
                showToast(runtimeMismatch.message);
                return;
            }
            if (!confirmServiceAction(moduleDef.service, normalizedAction)) return;
            if (!ensureIbkrPageAuth()) return;

            setActionState(true);
            const pendingMessage = `执行中：${moduleDef.service} ${normalizedAction} ...`;
            document.getElementById('lastAction').textContent = pendingMessage;
            setAuthActionFeedback(pendingMessage, 'info');
            try {
                const payload = await requestIbkrEnvironmentJson('/api/custom/ibkr/services/action', currentEnvironment, {
                    method: 'POST',
                    body: {
                        environment: currentEnvironment,
                        service: moduleDef.service,
                        action: normalizedAction,
                        source: 'runtime_page'
                    },
                    retryAttempts: 1
                });
                latestServiceActionStates = {
                    ...latestServiceActionStates,
                    [moduleDef.service]: payload
                };
                const message = summarizeServiceAction(payload);
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                setAuthActionFeedback(`最近动作：${message}`, payload?.ok === false ? 'error' : 'ok');
                showToast(message);
                renderServiceControlPanel(latestRuntimeStatus, latestServiceMonitorPayload);
                await loadRuntimeData(false);
            } catch (error) {
                const message = `服务动作失败：${moduleDef.service} ${normalizedAction} · ${error.message || error}`;
                latestServiceActionStates = {
                    ...latestServiceActionStates,
                    [moduleDef.service]: {
                        ok: false,
                        service: moduleDef.service,
                        action: normalizedAction,
                        message
                    }
                };
                document.getElementById('lastAction').textContent = message;
                setAuthActionFeedback(message, 'error');
                renderServiceControlPanel(latestRuntimeStatus, latestServiceMonitorPayload);
                showToast(message);
            } finally {
                setActionState(false);
            }
        }

        async function runAuthActionFromModel(model, { secondary = false } = {}) {
            if (!model || model.visible !== true || actionPending) return;

            const behavior = secondary ? model.secondaryBehavior : model.behavior;
            const actionName = secondary ? model.secondaryActionName : model.actionName;
            const requestTarget = secondary ? model.secondaryRequestTarget : model.requestTarget;

            if (behavior === 'refresh') {
                await loadRuntimeData(true);
                return;
            }
            if (behavior === 'focus_response') {
                focusTwoFactorResponseInput();
                return;
            }
            if (behavior === 'action' && actionName) {
                await handleRuntimeAction(actionName, requestTarget || null);
            }
        }

        async function runPrimaryAuthAction() {
            const model = latestNextActionModel || deriveNextAuthActionModel();
            await runAuthActionFromModel(model);
        }

        async function runSecondaryAuthAction() {
            const model = latestNextActionModel || deriveNextAuthActionModel();
            await runAuthActionFromModel(model, { secondary: true });
        }

        async function handleRuntimeAction(action, overrideTarget = null) {
            if (actionPending) return;
            const guardedActions = new Set(['start', 'stop', 'gateway_restart', 'reauth', 'reauth_force_new', 'probe', 'panic_reset_2fa', 'emergency_all', 'recover_all']);
            const runtimeMismatch = getRuntimeEnvironmentMismatch();
            if (runtimeMismatch && guardedActions.has(action)) {
                document.getElementById('lastAction').textContent = runtimeMismatch.message;
                setAuthActionFeedback(runtimeMismatch.message, 'error');
                showToast(runtimeMismatch.message);
                return;
            }
            const actionLockReason = getTwoFactorActionLockReason(action);
            if (actionLockReason) {
                document.getElementById('lastAction').textContent = `最近动作：${actionLockReason}`;
                setAuthActionFeedback(`最近动作：${actionLockReason}`, 'warn');
                showToast(actionLockReason);
                syncActionLocks();
                return;
            }
            if (action === 'emergency_all' && !window.confirm('确认执行全部急停？这会关闭 compute / trading / bars publish / IBKR Scheduler，并停止当前 runtime。')) {
                return;
            }
            if (action === 'recover_all' && !window.confirm('确认恢复 compute / trading / bars publish / IBKR Scheduler 开关？这不会自动重启服务。')) {
                return;
            }
            if (action === 'gateway_restart') {
                const startup = normalizeStartupUiState(latestStartupState);
                const runtimeActive = Boolean(latestRuntimeStatus?.starting || latestRuntimeStatus?.startup_complete || latestRuntimeStatus?.runtime_phase === 'running');
                const requiresFreshCycle = runtimeActive || startup.active;
                const message = requiresFreshCycle
                    ? '确认重启 systemd ibkr-gateway？这会重启 IBC + IB Gateway GUI/API，并在新的启动卡片上等待你手动触发 2FA。'
                    : '确认重启 systemd ibkr-gateway？当前不会自动恢复 ibkr-runtime，也不会自动触发新的 2FA。';
                if (!window.confirm(message)) {
                    return;
                }
            }
            if (action === 'reauth_force_new' && !window.confirm('确认开始 2FA？这会触发 IBKR 手机验证；如果已有手机通知或 Challenge，请取消并继续当前轮次。')) {
                return;
            }
            if (action === 'panic_reset_2fa' && !window.confirm('确认重开 2FA？将清空旧 Challenge / Response / 接管状态，并重启验证。')) {
                return;
            }
            if (!ensureIbkrPageAuth()) return;
            await executeRuntimeAction(action, overrideTarget);
        }

        window.handleRuntimeAction = handleRuntimeAction;
        window.handleServiceAction = handleServiceAction;
        window.runPrimaryAuthAction = runPrimaryAuthAction;
        window.runSecondaryAuthAction = runSecondaryAuthAction;
        window.submitTwoFactorResponse = submitTwoFactorResponse;
        window.onEnvironmentChange = function(environment) {
            currentEnvironment = environment;
            window.location.href = buildPageUrl('/ibkr_runtime.html', {}, { environment: currentEnvironment });
        };

        document.addEventListener('DOMContentLoaded', async () => {
            if (!ensureIbkrPageAuth()) return;
            window.addEventListener('pagehide', () => {
                runtimePageClosing = true;
                latestRuntimeLoadId += 1;
            });
            window.addEventListener('beforeunload', () => {
                runtimePageClosing = true;
                latestRuntimeLoadId += 1;
            });
            document.getElementById('nav').innerHTML = renderNav('/ibkr_runtime.html');
            document.getElementById('contextBar').innerHTML = renderPageContextBar('🎛️ IBKR 运行时', { subtitle: '控制 / 调度 / 链路' });
            document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_runtime.html');
            document.getElementById('configLink').href = buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true, environment: currentEnvironment });
            await loadRuntimeData(false);
            refreshTimer = setInterval(() => loadRuntimeData(false), 60000);
        });
