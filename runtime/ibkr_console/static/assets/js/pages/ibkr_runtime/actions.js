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
                boostRuntimeRefresh();
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

            actionPendingLabel = formatRuntimePendingLabel(action, target?.body?.reason || '');
            setActionState(true);
            const pendingMessage = actionPendingLabel;
            document.getElementById('lastAction').textContent = pendingMessage;
            setAuthActionFeedback(pendingMessage, 'info');
            try {
                const payload = await withTimeout(
                    requestIbkrEnvironmentJson(target.path, currentEnvironment, {
                        method: 'POST',
                        body: target.body,
                        retryAttempts: 1
                    }),
                    25000,
                    `动作 ${action}`
                );
                const message = summarizeAction(action, payload);
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                setAuthActionFeedback(`最近动作：${message}`, payload?.ok === false ? 'error' : 'ok');
                showToast(message);
                if (payload?.accepted === true || Number(payload?.status_code || 0) === 202 || ['start', 'gateway_restart', 'reauth', 'reauth_force_new', 'probe', 'panic_reset_2fa'].includes(action)) {
                    boostRuntimeRefresh();
                }
                // The action is complete once the POST returns; don't keep buttons locked while a follow-up refresh waits on slow status APIs.
                setActionState(false);
                await loadRuntimeData(false);
            } catch (error) {
                const rawMessage = String(error?.message || error || '');
                const timedOut = rawMessage.includes('timed out');
                const likelySubmitted = ['start', 'gateway_restart', 'reauth', 'reauth_force_new', 'probe', 'panic_reset_2fa'].includes(action);
                const message = timedOut && likelySubmitted
                    ? `动作响应超时：${action} · 已解除按钮锁并继续刷新；请先观察最新状态，避免重复触发。`
                    : `动作失败：${action} · ${rawMessage}`;
                document.getElementById('lastAction').textContent = message;
                setAuthActionFeedback(message, timedOut && likelySubmitted ? 'warn' : 'error');
                showToast(message);
                if (timedOut && likelySubmitted) {
                    boostRuntimeRefresh();
                    void loadRuntimeData(false);
                }
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

        async function confirmServiceAction(serviceName, action) {
            const moduleDef = getServiceModuleDef(serviceName);
            if (!moduleDef) return false;
            const service = moduleDef.service;
            const normalizedAction = String(action || '').trim().toLowerCase();
            const label = getServiceActionLabel(normalizedAction);
            const highRiskAction = moduleDef.highRiskRestart && ['restart', 'stop'].includes(normalizedAction);
            if (highRiskAction) {
                return showRuntimeConfirm({
                    title: `确认${label} ${service}`,
                    message: `${service} 会影响当前 runtime / Gateway 会话。继续前请输入服务名确认。`,
                    confirmText: service,
                    inputLabel: '输入完整服务名',
                    confirmLabel: label,
                    tone: normalizedAction === 'stop' ? 'danger' : 'warn',
                });
            }
            return showRuntimeConfirm({
                title: `确认${label} ${service}`,
                message: `即将对 systemd 服务 ${service} 执行${label}。`,
                confirmLabel: label,
                tone: 'info',
            });
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
            if (!await confirmServiceAction(moduleDef.service, normalizedAction)) return;
            if (!ensureIbkrPageAuth()) return;

            actionPendingLabel = `执行中：${moduleDef.service} ${label}`;
            setActionState(true);
            const pendingMessage = actionPendingLabel;
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
            if (action === 'emergency_all' && !await showRuntimeConfirm({
                title: '确认执行全部急停',
                message: '这会关闭 compute / trading / bars publish / IBKR Scheduler，并停止当前 runtime。',
                confirmLabel: '全部急停',
                tone: 'danger',
            })) {
                return;
            }
            if (action === 'recover_all' && !await showRuntimeConfirm({
                title: '确认恢复运行开关',
                message: '这会恢复 compute / trading / bars publish / IBKR Scheduler 开关，但不会自动重启服务。',
                confirmLabel: '恢复',
                tone: 'info',
            })) {
                return;
            }
            if (action === 'gateway_restart') {
                const startup = normalizeStartupUiState(latestStartupState);
                const runtimeActive = Boolean(latestRuntimeStatus?.starting || latestRuntimeStatus?.startup_complete || latestRuntimeStatus?.runtime_phase === 'running');
                const requiresFreshCycle = runtimeActive || startup.active;
                const message = requiresFreshCycle
                    ? '确认重启 systemd ibkr-gateway？这会重启 IBC + IB Gateway GUI/API，并在新的启动卡片上等待你手动触发 2FA。'
                    : '确认重启 systemd ibkr-gateway？当前不会自动恢复 ibkr-runtime，也不会自动触发新的 2FA。';
                if (!await showRuntimeConfirm({
                    title: '确认重启 IB Gateway',
                    message,
                    confirmText: 'ibkr-gateway',
                    inputLabel: '输入完整服务名',
                    confirmLabel: '重启',
                    tone: 'warn',
                })) {
                    return;
                }
            }
            if (action === 'reauth_force_new' && !await showRuntimeConfirm({
                title: '确认开始新一轮 2FA',
                message: '这会触发 IBKR 手机验证；如果已有手机通知或 Challenge，请取消并继续当前轮次。',
                confirmLabel: '开始 2FA',
                tone: 'warn',
            })) {
                return;
            }
            if (action === 'panic_reset_2fa' && !await showRuntimeConfirm({
                title: '确认重开 2FA',
                message: '将清空旧 Challenge / Response / 接管状态，并重启验证。旧手机通知和旧飞书卡片都不要再处理。',
                confirmText: '重开2FA',
                inputLabel: '输入 重开2FA',
                confirmLabel: '重开',
                tone: 'danger',
            })) {
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
