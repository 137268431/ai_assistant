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
            return buildBrokerEnvironmentFilter();
        }

        function buildBrokerEnvironmentFilter() {
            return `environment = "${escapeQueryValue(currentBrokerMode || currentEnvironment)}"`;
        }

        function buildDataEnvironmentFilter() {
            return `environment = "${escapeQueryValue(currentDataEnvironment || getSharedDataEnvironment())}"`;
        }

        function resolveRuntimeMarketDate(status) {
            const marketDate = String(
                status?.runtime?.market_universe?.market_date
                || status?.market_universe?.market_date
                || ''
            ).trim();
            return marketDate || getCurrentEtDateString();
        }

        async function loadRuntimeTodayCounts(status) {
            const dataEnvFilter = buildDataEnvironmentFilter();
            const brokerEnvFilter = buildBrokerEnvironmentFilter();
            const marketDate = resolveRuntimeMarketDate(status);
            const dataTodayFilterBase = `created >= "${escapeQueryValue(`${marketDate} 00:00:00`)}" && ${dataEnvFilter}`;
            const brokerTodayFilterBase = `created >= "${escapeQueryValue(`${marketDate} 00:00:00`)}" && ${brokerEnvFilter}`;
            const targetDateFilter = `date = "${escapeQueryValue(marketDate)}" && ${dataEnvFilter}`;
            const readCountFetch = (collection, filter) => (
                typeof cachedCountFetch === 'function'
                    ? cachedCountFetch(collection, filter, { ttlMs: 30000, ttl: 30000 })
                    : apiFetch(collection, { filter, perPage: 1, page: 1 })
            );

            const [
                barsCountResp,
                indicatorsCountResp,
                signalsCountResp,
                ordersCountResp,
                eventsCountResp,
                targetsCountResp,
            ] = await Promise.all([
                readCountFetch('ibkr_bars', dataTodayFilterBase).catch(() => null),
                readCountFetch('ibkr_indicators', dataTodayFilterBase).catch(() => null),
                readCountFetch('ibkr_signals', dataTodayFilterBase).catch(() => null),
                readCountFetch('orders', brokerTodayFilterBase).catch(() => null),
                readCountFetch('system_events', brokerTodayFilterBase).catch(() => null),
                readCountFetch('ibkr_targets', targetDateFilter).catch(() => null),
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
            const requested = String(currentBrokerMode || currentEnvironment || '').trim().toLowerCase() || 'paper';
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

        function normalizeBrokerSessionMode(value) {
            const text = String(value || '').trim().toLowerCase();
            if (!text) return '';
            if (['prod', 'production', 'live'].includes(text)) return 'live';
            if (['paper', 'sim', 'simulated', 'simulation'].includes(text)) return 'paper';
            return '';
        }

        function formatAppLoginModeLabel(mode, options = {}) {
            const key = normalizeBrokerSessionMode(mode);
            const compact = Boolean(options.compact);
            if (key === 'paper') return compact ? 'PAPER' : 'PAPER 模拟账户';
            if (key === 'live') return compact ? 'LIVE' : 'LIVE 真实账户';
            if (String(mode || '').trim().toLowerCase() === 'offline') return compact ? 'OFFLINE' : '未占用';
            return compact ? 'UNKNOWN' : '未知 session';
        }

        function deriveBrokerSessionModeFromAccounts(accounts) {
            const tokens = String(accounts || '')
                .split(/[,\s]+/)
                .map((item) => item.trim().toUpperCase())
                .filter(Boolean);
            const hasPaper = tokens.some((item) => item.startsWith('DU'));
            const hasLive = tokens.some((item) => item.startsWith('U') && !item.startsWith('DU'));
            if (hasPaper && !hasLive) return 'paper';
            if (hasLive && !hasPaper) return 'live';
            return '';
        }

        function getGatewaySessionModeModel(status = latestRuntimeStatus) {
            const gateway = status?.gateway && typeof status.gateway === 'object' ? status.gateway : {};
            const broker = gateway?.broker && typeof gateway.broker === 'object' ? gateway.broker : {};
            const running = Boolean(
                gateway.running
                || gateway.reachable
                || Number(gateway.pid || 0) > 0
            );
            if (!running) {
                return {
                    running: false,
                    mode: 'offline',
                    label: formatAppLoginModeLabel('offline'),
                    compactLabel: formatAppLoginModeLabel('offline', { compact: true }),
                    managedAccounts: String(broker.managed_accounts || '').trim(),
                    statusCode: Number(gateway.status_code || broker.status_code || 0) || 0,
                };
            }

            const explicitMode = [
                status?.gateway_mode,
                gateway?.gateway_mode,
                broker?.gateway_mode,
                status?.broker_mode,
                gateway?.broker_mode,
                broker?.broker_mode,
            ].map(normalizeBrokerSessionMode).find(Boolean);
            const accountMode = deriveBrokerSessionModeFromAccounts(broker.managed_accounts);
            const fallbackMode = [
                status?.environment,
                status?.actual_runtime_environment,
            ].map(normalizeBrokerSessionMode).find(Boolean);
            const mode = explicitMode || accountMode || fallbackMode || 'unknown';
            return {
                running: true,
                mode,
                label: formatAppLoginModeLabel(mode),
                compactLabel: formatAppLoginModeLabel(mode, { compact: true }),
                managedAccounts: String(broker.managed_accounts || '').trim(),
                statusCode: Number(gateway.status_code || broker.status_code || 0) || 0,
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
            const primaryLabel = actionPending && model.buttonLabel
                ? (actionPendingLabel || `执行中：${model.buttonLabel}`)
                : model.buttonLabel;
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
            if (action === 'probe') {
                const cyclePhase = getIbkrTwoFactorCyclePhase(state);
                if (['waiting_confirm', 'waiting_response_submitted', 'waiting_response_received'].includes(cyclePhase)) {
                    return '';
                }
            }
            return getIbkrTwoFactorCycleActionLockReason(action, getIbkrTwoFactorCyclePhase(state));
        }

        function syncActionLocks() {
            document.querySelectorAll('.action-btn, .runtime-secondary-action').forEach((button) => {
                const action = String(button?.dataset?.action || '').trim();
                const lockReason = actionPending ? '' : getTwoFactorActionLockReason(action);
                let probeReason = '';
                if (!actionPending && !lockReason && action === 'probe') {
                    const state = deriveTwoFactorUiState(latestTwoFactorState);
                    const recoveryPhase = String(state?.recovery_phase || '').trim().toLowerCase();
                    const cyclePhase = getIbkrTwoFactorCyclePhase(state);
                    const canProbe = state.manual_takeover_active === true
                        || ['waiting_confirm', 'waiting_response_submitted', 'waiting_response_received'].includes(cyclePhase)
                        || ['silent_probe', 'manual_takeover'].includes(recoveryPhase);
                    if (!canProbe) {
                        probeReason = '只有在已完成手机确认/Response、人工接管中，或系统正在恢复探测时，才需要手动检查当前认证。';
                    }
                }
                button.disabled = actionPending || Boolean(lockReason) || Boolean(probeReason);
                button.title = actionPending
                    ? '动作执行中，请稍候。'
                    : (lockReason || probeReason || '');
            });
            document.querySelectorAll('.service-action-btn').forEach((button) => {
                button.disabled = actionPending;
                button.title = actionPending ? '动作执行中，请稍候。' : '';
            });
            document.querySelectorAll('.broker-mode-switch-btn').forEach((button) => {
                const lockReason = String(button?.dataset?.lockReason || '').trim();
                button.disabled = actionPending || Boolean(lockReason);
                button.title = actionPending ? '动作执行中，请稍候。' : lockReason;
            });
        }
