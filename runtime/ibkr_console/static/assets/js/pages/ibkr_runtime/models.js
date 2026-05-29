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
            const signalTodayFilterBase = `created >= "${escapeQueryValue(`${marketDate} 00:00:00`)}" && ${dataEnvFilter}`;
            const brokerTodayFilterBase = `created >= "${escapeQueryValue(`${marketDate} 00:00:00`)}" && ${brokerEnvFilter}`;
            const targetDateFilter = `date = "${escapeQueryValue(marketDate)}" && ${dataEnvFilter}`;
            const readCountFetch = (collection, filter) => (
                typeof cachedCountFetch === 'function'
                    ? cachedCountFetch(collection, filter, { ttlMs: 30000, ttl: 30000 })
                    : apiFetch(collection, { filter, perPage: 1, page: 1 })
            );

            const [
                signalsCountResp,
                ordersCountResp,
                eventsCountResp,
                targetsCountResp,
            ] = await Promise.all([
                readCountFetch('ibkr_signals', signalTodayFilterBase).catch(() => null),
                readCountFetch('orders', brokerTodayFilterBase).catch(() => null),
                readCountFetch('system_events', brokerTodayFilterBase).catch(() => null),
                readCountFetch('ibkr_targets', targetDateFilter).catch(() => null),
            ]);

            return {
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
