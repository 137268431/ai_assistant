        function runtimeModePayload(payload = {}) {
            return buildModePayload(payload, {
                brokerMode: currentBrokerMode,
                dataEnvironment: currentDataEnvironment || getSharedDataEnvironment(),
            });
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
                    body: runtimeModePayload({
                        response_code: responseCode,
                        challenge_code: challengeCode,
                        source: 'runtime_page'
                    }),
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
                    body: runtimeModePayload({
                        trigger_login: false,
                        reason: 'manual_start',
                        source: 'runtime_page'
                    })
                },
                gateway_restart: {
                    path: '/api/custom/ibkr/gateway/restart',
                    body: runtimeModePayload({
                        reason: 'manual_gateway_restart',
                        source: 'runtime_page'
                    })
                },
                stop: { path: '/api/custom/ibkr/stop', body: runtimeModePayload({}) },
                reauth: {
                    path: '/api/custom/ibkr/2fa/request',
                    body: runtimeModePayload({
                        reason: 'manual_reauth',
                        source: 'runtime_page',
                        force_reset: true,
                        message: '已请求 2FA 卡片；请在飞书验证。'
                    })
                },
                reauth_force_new: {
                    path: '/api/custom/ibkr/2fa/request',
                    body: runtimeModePayload({
                        reason: 'manual_reauth',
                        source: 'runtime_page',
                        force_reset: true,
                        force_restart: true,
                        trigger_now: true,
                        force_new: true,
                        message: '已开始新一轮 2FA，请查看手机。'
                    })
                },
                probe: {
                    path: '/api/custom/ibkr/2fa/probe',
                    body: runtimeModePayload({
                        reason: 'manual_probe',
                        source: 'runtime_page'
                    })
                },
                app_login_handoff: {
                    path: '/api/custom/ibkr/gateway/stop',
                    body: runtimeModePayload({
                        reason: 'app_login_handoff',
                        source: 'runtime_page_session_handoff'
                    })
                },
                panic_reset_2fa: {
                    path: '/api/custom/ibkr/2fa/panic-reset',
                    body: runtimeModePayload({
                        restart_gateway: true,
                        restart_runtime: true,
                        trigger_login: true,
                        confirm_text: '重开2FA',
                        reason: 'panic_reset_2fa',
                        source: 'runtime_page'
                    })
                },
                emergency_all: { path: '/api/custom/ibkr/emergency-stop', body: runtimeModePayload({ action: 'all' }) },
                recover_all: { path: '/api/custom/ibkr/recover', body: runtimeModePayload({ action: 'all' }) }
            };
        }

        function ensureAppLoginHandoffDialog() {
            let dialog = document.getElementById('appLoginHandoffDialog');
            if (dialog) return dialog;
            dialog = document.createElement('div');
            dialog.id = 'appLoginHandoffDialog';
            dialog.className = 'runtime-confirm-overlay tone-warn';
            dialog.innerHTML = `
                <div class="runtime-confirm-modal" role="dialog" aria-modal="true" aria-labelledby="appLoginHandoffTitle">
                    <div class="runtime-confirm-kicker">Session Handoff</div>
                    <div class="runtime-confirm-title" id="appLoginHandoffTitle">准备登录 IBKR App</div>
                    <div class="runtime-confirm-message" id="appLoginHandoffMessage"></div>
                    <div class="runtime-confirm-actions">
                        <button class="runtime-confirm-btn secondary" type="button" id="appLoginHandoffCancel">取消</button>
                        <button class="runtime-confirm-btn quick" type="button" id="appLoginHandoffPaper">我要登录 PAPER App</button>
                        <button class="runtime-confirm-btn primary" type="button" id="appLoginHandoffLive">我要登录 LIVE App</button>
                    </div>
                </div>
            `;
            document.body.appendChild(dialog);
            return dialog;
        }

        function showAppLoginHandoffDialog() {
            return new Promise((resolve) => {
                const dialog = ensureAppLoginHandoffDialog();
                const messageEl = dialog.querySelector('#appLoginHandoffMessage');
                const cancelButton = dialog.querySelector('#appLoginHandoffCancel');
                const paperButton = dialog.querySelector('#appLoginHandoffPaper');
                const liveButton = dialog.querySelector('#appLoginHandoffLive');
                const session = getGatewaySessionModeModel(latestRuntimeStatus);
                const gatewayText = session.running
                    ? `${session.label}${session.managedAccounts ? ` · account ${session.managedAccounts}` : ''}`
                    : '未占用 App 登录 session';
                const dataText = formatEnvironmentLabel(currentDataEnvironment);
                const brokerText = formatEnvironmentLabel(currentBrokerMode);
                let settled = false;

                const cleanup = (targetMode) => {
                    if (settled) return;
                    settled = true;
                    dialog.classList.remove('show');
                    document.removeEventListener('keydown', onKeydown);
                    cancelButton.removeEventListener('click', onCancel);
                    paperButton.removeEventListener('click', onPaper);
                    liveButton.removeEventListener('click', onLive);
                    dialog.removeEventListener('click', onBackdrop);
                    resolve(targetMode || '');
                };
                const onCancel = () => cleanup('');
                const onPaper = () => cleanup('paper');
                const onLive = () => cleanup('live');
                const onBackdrop = (event) => {
                    if (event.target === dialog) cleanup('');
                };
                const onKeydown = (event) => {
                    if (event.key === 'Escape') {
                        event.preventDefault();
                        cleanup('');
                    }
                };

                dialog.classList.remove('tone-danger', 'tone-warn', 'tone-info');
                dialog.classList.add(session.running && session.mode === 'live' ? 'tone-danger' : 'tone-warn');
                if (messageEl) {
                    messageEl.textContent = [
                        `当前 Gateway 占用：${gatewayText}`,
                        `当前下单：${brokerText}`,
                        `当前行情：${dataText}`,
                        '请选择你准备在 IBKR App 登录哪个 session。只有和 Gateway 占用一致时，页面才会停止 Runtime + Gateway。'
                    ].join('\n');
                }
                cancelButton.addEventListener('click', onCancel);
                paperButton.addEventListener('click', onPaper);
                liveButton.addEventListener('click', onLive);
                dialog.addEventListener('click', onBackdrop);
                document.addEventListener('keydown', onKeydown);
                dialog.classList.add('show');
                paperButton.focus();
            });
        }

        function buildAppLoginHandoffDecision(targetMode) {
            const target = normalizeBrokerSessionMode(targetMode);
            const session = getGatewaySessionModeModel(latestRuntimeStatus);
            const targetLabel = formatAppLoginModeLabel(target);
            const gatewayLabel = session.running ? session.label : '未占用';
            if (!target) {
                return {
                    shouldStop: false,
                    tone: 'warn',
                    message: '未选择要登录的 IBKR App session。',
                };
            }
            if (!session.running) {
                return {
                    shouldStop: false,
                    tone: 'ok',
                    message: `当前 Gateway 未占用登录 session，可以直接登录 IBKR App ${targetLabel}。`,
                };
            }
            if (session.mode !== 'unknown' && session.mode !== target) {
                return {
                    shouldStop: false,
                    tone: 'ok',
                    message: `当前 Gateway 占用 ${gatewayLabel}，你要登录 ${targetLabel}，通常不冲突；本次不停止 Gateway。`,
                };
            }
            if (session.mode === 'unknown') {
                return {
                    shouldStop: true,
                    tone: 'warn',
                    confirmText: '停止GATEWAY',
                    title: `无法判断 Gateway session · 准备登录 ${targetLabel}`,
                    message: [
                        `当前 Gateway 正在运行，但无法判断占用 PAPER 还是 LIVE。`,
                        `你准备登录：${targetLabel}`,
                        '继续会停止 Runtime 业务线程和 ibkr-gateway，让出所有 Gateway 登录占用；不会切换交易模式，也不会触发新的 2FA。'
                    ].join('\n'),
                    body: {
                        app_login_target: target,
                        gateway_session_mode: 'unknown',
                    },
                };
            }
            return {
                shouldStop: true,
                tone: target === 'live' ? 'danger' : 'warn',
                confirmText: target === 'live' ? '登录LIVE' : '登录PAPER',
                title: `确认让出 ${targetLabel} Gateway 会话`,
                message: [
                    `当前 Gateway 占用：${gatewayLabel}`,
                    `你准备登录：${targetLabel}`,
                    '这会停止 Runtime 业务线程和 ibkr-gateway，避免 App 与 Gateway 抢同一个 session。',
                    '不会切换交易模式，不会启用实盘下单，也不会触发新的 2FA。完成 App 操作后，请手动点击“启动 Runtime 线程”恢复。'
                ].join('\n'),
                body: {
                    app_login_target: target,
                    gateway_session_mode: session.mode,
                },
            };
        }

        async function handleAppLoginHandoffAction() {
            const targetMode = await showAppLoginHandoffDialog();
            const decision = buildAppLoginHandoffDecision(targetMode);
            if (!targetMode) {
                return;
            }
            if (!decision.shouldStop) {
                document.getElementById('lastAction').textContent = `最近动作：${decision.message}`;
                setAuthActionFeedback(`最近动作：${decision.message}`, decision.tone || 'info');
                showToast(decision.message);
                return;
            }
            const confirmed = await showRuntimeConfirm({
                title: decision.title,
                message: decision.message,
                confirmText: decision.confirmText,
                inputLabel: `输入 ${decision.confirmText}`,
                confirmLabel: '让出会话',
                tone: decision.tone || 'warn',
            });
            if (!confirmed) return;
            if (!ensureIbkrPageAuth()) return;
            await executeRuntimeAction('app_login_handoff', {
                path: '/api/custom/ibkr/gateway/stop',
                body: runtimeModePayload({
                    ...(decision.body || {}),
                    reason: 'app_login_handoff',
                    source: 'runtime_page_session_handoff'
                })
            });
        }

        async function executeRuntimeAction(action, overrideTarget = null) {
            const target = overrideTarget || getRuntimeActionMap()[action];
            if (!target) return;

            const longOperation = isRuntimeOperationWatchAction(action);
            const operationReason = target?.body?.reason || '';
            actionPendingLabel = formatRuntimePendingLabel(action, target?.body?.reason || '');
            setActionState(true);
            const pendingMessage = actionPendingLabel;
            document.getElementById('lastAction').textContent = pendingMessage;
            setAuthActionFeedback(pendingMessage, 'info');
            if (longOperation) {
                startRuntimeOperationWatch(action, {
                    reason: operationReason,
                    source: target?.body?.source || 'runtime_page',
                    phase: 'submitted',
                    message: `${getRuntimeActionLabel(action)} 请求已发送，正在追踪 Gateway / 2FA / Session 状态，请不要重复点击。`,
                });
            }
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
                const message = summarizeAction(action, payload, target);
                if (longOperation && payload?.ok === false) {
                    finishActiveRuntimeOperation('failed', message, { blocker: payload });
                    document.getElementById('lastAction').textContent = `最近动作：${message}`;
                    setAuthActionFeedback(`最近动作：${message}`, 'error');
                    showToast(message);
                    return;
                }
                const trackingMessage = longOperation
                    ? `${message} · 已进入状态追踪；请等待 Gateway / 2FA / Session 自动更新，不要重复点击。`
                    : message;
                document.getElementById('lastAction').textContent = `最近动作：${trackingMessage}`;
                setAuthActionFeedback(`最近动作：${trackingMessage}`, payload?.ok === false ? 'error' : 'ok');
                showToast(trackingMessage);
                if (longOperation) {
                    if (action === 'gateway_restart' && payload?.gateway_restarted === true && payload?.startup_cycle_planned === false) {
                        finishActiveRuntimeOperation('success', trackingMessage, { phase: 'gateway' });
                    } else {
                        updateActiveRuntimeOperation({
                            phase: payload?.accepted === true || Number(payload?.status_code || 0) === 202 ? 'submitted' : 'gateway',
                            status: 'watching',
                            message: trackingMessage,
                        });
                    }
                }
                if (payload?.accepted === true || Number(payload?.status_code || 0) === 202 || ['start', 'gateway_restart', 'reauth', 'reauth_force_new', 'probe', 'panic_reset_2fa', 'app_login_handoff'].includes(action)) {
                    boostRuntimeRefresh();
                }
                // The action is complete once the POST returns; don't keep buttons locked while a follow-up refresh waits on slow status APIs.
                setActionState(false);
                await loadRuntimeData(false);
            } catch (error) {
                const blockerPayload = error?.payload || {};
                if (
                    action === 'gateway_restart'
                    && blockerPayload?.restart_blocked === true
                    && blockerPayload?.blocker_code === 'market_data_session_conflict'
                ) {
                    const message = blockerPayload.message || '检测到 IBKR 行情会话被另一个 IP 占用；请退出其他登录后再重启 Gateway。';
                    if (longOperation) {
                        finishActiveRuntimeOperation('failed', message, { blocker: blockerPayload });
                    }
                    document.getElementById('lastAction').textContent = message;
                    setAuthActionFeedback(message, 'error');
                    showToast(message);
                    return;
                }
                const rawMessage = String(error?.message || error || '');
                const timedOut = rawMessage.includes('timed out');
                const likelySubmitted = ['start', 'gateway_restart', 'reauth', 'reauth_force_new', 'probe', 'panic_reset_2fa', 'app_login_handoff'].includes(action);
                const message = timedOut && likelySubmitted && longOperation
                    ? `动作响应超时：${action} · 请求可能已提交，后台仍在执行；正在追踪 Gateway / 2FA / Session 状态，请不要重复点击。`
                    : timedOut && likelySubmitted
                    ? `动作响应超时：${action} · 已解除按钮锁并继续刷新；请先观察最新状态，避免重复触发。`
                    : `动作失败：${action} · ${rawMessage}`;
                if (longOperation) {
                    if (timedOut && likelySubmitted) {
                        updateActiveRuntimeOperation({
                            phase: 'gateway',
                            status: 'watching',
                            message,
                        });
                    } else {
                        finishActiveRuntimeOperation('failed', message, { error: rawMessage });
                    }
                }
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

        function summarizeBrokerModeSwitch(payload, targetBrokerMode) {
            if (!payload || typeof payload !== 'object') {
                return `Broker mode 切换到 ${targetBrokerMode.toUpperCase()} 已提交。`;
            }
            if (payload.ok === false) {
                const blocker = Array.isArray(payload.blockers) ? payload.blockers[0] : null;
                return blocker?.message || payload.message || payload.error || 'Broker mode 切换被拒绝。';
            }
            return payload.message || `已写入 ${targetBrokerMode.toUpperCase()} 并开始重启服务；等待恢复后重新完成 2FA。`;
        }

        async function handleBrokerModeSwitch(targetBrokerMode) {
            if (actionPending) return;
            const targetMode = normalizeBrokerMode(targetBrokerMode, getOppositeBrokerMode(currentBrokerMode));
            const preview = latestBrokerModeSwitchPreview && typeof latestBrokerModeSwitchPreview === 'object'
                ? latestBrokerModeSwitchPreview
                : {};
            const blockers = Array.isArray(preview.blockers) ? preview.blockers : [];
            if (preview.allowed === false && blockers.length) {
                const message = blockers[0]?.message || '切换预检未通过。';
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                setAuthActionFeedback(message, 'warn');
                showToast(message);
                return;
            }
            const confirmText = String(preview.confirm_text || `SWITCH ${targetMode.toUpperCase()}`).trim();
            const targetLabel = typeof formatBrokerModeSwitchLabel === 'function'
                ? formatBrokerModeSwitchLabel(targetMode, { compact: true })
                : getEnvironmentLabel(targetMode);
            const currentMode = normalizeBrokerMode(preview.current_broker_mode || latestRuntimeStatus?.broker_mode || currentBrokerMode, currentBrokerMode);
            const currentLabel = typeof formatBrokerModeSwitchLabel === 'function'
                ? formatBrokerModeSwitchLabel(currentMode, { compact: true })
                : getEnvironmentLabel(currentMode);
            const confirmed = await showRuntimeConfirm({
                title: `确认切换到 ${targetLabel}`,
                message: [
                    `即将把真实运行模式从 ${currentLabel} 切换到 ${targetLabel}。`,
                    '系统会写入运行 .env，并依次停止 runtime、重启 Gateway、Runtime、Scheduler 和 API。',
                    '只有当前账户无持仓、无 IBKR 挂单、无 PB active/stale 订单组时才会执行；切换后需要重新完成 2FA。'
                ].join('\n'),
                confirmText,
                inputLabel: `输入 ${confirmText}`,
                confirmLabel: `切换到 ${targetMode.toUpperCase()}`,
                tone: targetMode === 'live' ? 'danger' : 'warn',
            });
            if (!confirmed) return;
            if (!ensureIbkrPageAuth()) return;

            actionPendingLabel = `执行中：切换到 ${targetLabel}`;
            setActionState(true);
            document.getElementById('lastAction').textContent = actionPendingLabel;
            setAuthActionFeedback(actionPendingLabel, 'info');
            try {
                const payload = await withTimeout(
                    requestIbkrEnvironmentJson('/api/custom/ibkr/broker-mode/switch', currentEnvironment, {
                        method: 'POST',
                        body: runtimeModePayload({
                            target_broker_mode: targetMode,
                            confirm_text: confirmText,
                            source: 'runtime_page',
                        }),
                        retryAttempts: 1,
                    }),
                    120000,
                    `切换 Broker mode ${targetMode}`
                );
                latestBrokerModeSwitchPreview = payload || {};
                const message = summarizeBrokerModeSwitch(payload, targetMode);
                document.getElementById('lastAction').textContent = `最近动作：${message}`;
                setAuthActionFeedback(`最近动作：${message}`, payload?.ok === false ? 'error' : 'ok');
                showToast(message, 4000);
                renderBrokerModeSwitchPanel(latestBrokerModeSwitchPreview, latestRuntimeStatus, latestTwoFactorState);
                if (payload?.accepted === true || Number(payload?.status_code || 0) === 202 || payload?.status === 'restart_requested') {
                    if (typeof setStoredBrokerMode === 'function') setStoredBrokerMode(targetMode);
                    if (typeof syncBrokerModeFromPayload === 'function') {
                        syncBrokerModeFromPayload({
                            broker_mode: targetMode,
                            environment: targetMode,
                            data_environment: getSharedDataEnvironment(),
                            market_data_environment: getSharedDataEnvironment(),
                        });
                    }
                    boostRuntimeRefresh();
                    window.setTimeout(() => {
                        window.location.href = buildPageUrl('/ibkr_runtime.html', {}, {
                            environment: targetMode,
                            brokerMode: targetMode,
                            dataEnvironment: getSharedDataEnvironment(),
                        });
                    }, 6500);
                } else {
                    await loadRuntimeData(false);
                }
            } catch (error) {
                const blockerPayload = error?.payload || {};
                if (blockerPayload && typeof blockerPayload === 'object' && (blockerPayload.blockers || blockerPayload.current_broker_mode)) {
                    latestBrokerModeSwitchPreview = blockerPayload;
                    renderBrokerModeSwitchPanel(latestBrokerModeSwitchPreview, latestRuntimeStatus, latestTwoFactorState);
                }
                const rawMessage = String(error?.message || error || '');
                const timedOut = rawMessage.includes('timed out') || rawMessage.includes('Failed to fetch') || rawMessage.includes('NetworkError');
                const firstBlocker = Array.isArray(blockerPayload.blockers) ? blockerPayload.blockers[0] : null;
                const message = timedOut
                    ? `切换请求响应超时：可能已提交，页面会继续刷新；请避免重复点击。`
                    : (firstBlocker?.message || blockerPayload.message || `切换失败：${rawMessage}`);
                document.getElementById('lastAction').textContent = message;
                setAuthActionFeedback(message, timedOut ? 'warn' : 'error');
                showToast(message, 4000);
                if (timedOut) {
                    boostRuntimeRefresh();
                    scheduleRuntimeRefresh(5000);
                }
            } finally {
                setActionState(false);
            }
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
            const operationLockReason = getRuntimeServiceActionLockReason(moduleDef.service, normalizedAction);
            if (operationLockReason) {
                document.getElementById('lastAction').textContent = `最近动作：${operationLockReason}`;
                setAuthActionFeedback(`最近动作：${operationLockReason}`, 'warn');
                showToast(operationLockReason);
                syncActionLocks();
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

            const label = getServiceActionLabel(normalizedAction);
            actionPendingLabel = `执行中：${moduleDef.service} ${label}`;
            setActionState(true);
            const pendingMessage = actionPendingLabel;
            document.getElementById('lastAction').textContent = pendingMessage;
            setAuthActionFeedback(pendingMessage, 'info');
            const serviceWatchAction = moduleDef.service === 'ibkr-gateway' && normalizedAction === 'restart'
                ? 'gateway_restart'
                : '';
            if (serviceWatchAction) {
                startRuntimeOperationWatch(serviceWatchAction, {
                    reason: `service_control_${moduleDef.service}_${normalizedAction}`,
                    source: 'runtime_page_service_control',
                    phase: 'submitted',
                    message: 'IB Gateway 服务重启请求已发送，正在追踪 Gateway / 2FA / Session 状态，请不要重复点击。',
                });
            }
            try {
                const payload = await requestIbkrEnvironmentJson('/api/custom/ibkr/services/action', currentEnvironment, {
                    method: 'POST',
                    body: runtimeModePayload({
                        service: moduleDef.service,
                        action: normalizedAction,
                        source: 'runtime_page'
                    }),
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
                if (serviceWatchAction) {
                    const delegatedPayload = payload?.payload && typeof payload.payload === 'object' ? payload.payload : {};
                    if (payload?.ok === false) {
                        finishActiveRuntimeOperation('failed', message, { payload });
                    } else if (delegatedPayload.gateway_restarted === true && delegatedPayload.startup_cycle_planned === false) {
                        finishActiveRuntimeOperation('success', `${message} · Gateway 已重启。`, { phase: 'gateway' });
                    } else {
                        updateActiveRuntimeOperation({
                            phase: 'gateway',
                            status: 'watching',
                            message: `${message} · 已进入状态追踪；请等待 Gateway / 2FA / Session 自动更新，不要重复点击。`,
                        });
                        boostRuntimeRefresh();
                    }
                }
                renderServiceControlPanel(latestRuntimeStatus, latestServiceMonitorPayload);
                await loadRuntimeData(false);
            } catch (error) {
                const blockerPayload = error?.payload || {};
                const message = (
                    blockerPayload?.restart_blocked === true
                    && blockerPayload?.blocker_code === 'market_data_session_conflict'
                )
                    ? (blockerPayload.message || '检测到 IBKR 行情会话被另一个 IP 占用；请退出其他登录后再重启 Gateway。')
                    : `服务动作失败：${moduleDef.service} ${normalizedAction} · ${error.message || error}`;
                latestServiceActionStates = {
                    ...latestServiceActionStates,
                    [moduleDef.service]: {
                        ok: false,
                        service: moduleDef.service,
                        action: normalizedAction,
                        message
                    }
                };
                if (serviceWatchAction) {
                    finishActiveRuntimeOperation('failed', message, { blocker: blockerPayload, error: error?.message || String(error || '') });
                }
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
            const operationLockReason = getActiveRuntimeOperationLockReason(action);
            if (operationLockReason) {
                document.getElementById('lastAction').textContent = `最近动作：${operationLockReason}`;
                setAuthActionFeedback(`最近动作：${operationLockReason}`, 'warn');
                showToast(operationLockReason);
                syncActionLocks();
                return;
            }
            const guardedActions = new Set(['start', 'stop', 'gateway_restart', 'reauth', 'reauth_force_new', 'probe', 'app_login_handoff', 'panic_reset_2fa', 'emergency_all', 'recover_all']);
            const runtimeMismatch = getRuntimeEnvironmentMismatch();
            if (runtimeMismatch && guardedActions.has(action)) {
                document.getElementById('lastAction').textContent = runtimeMismatch.message;
                setAuthActionFeedback(runtimeMismatch.message, 'error');
                showToast(runtimeMismatch.message);
                return;
            }
            if (action === 'app_login_handoff') {
                await handleAppLoginHandoffAction();
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
                message: '这会关闭交易执行、行情写入、IBKR Scheduler，并停止当前 runtime。',
                confirmLabel: '全部急停',
                tone: 'danger',
            })) {
                return;
            }
            if (action === 'recover_all' && !await showRuntimeConfirm({
                title: '确认恢复运行开关',
                message: '这会恢复交易执行、行情写入、IBKR Scheduler 开关，但不会自动重启服务。',
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

        function openRuntimeSystemStatus() {
            const target = typeof buildPageUrl === 'function'
                ? buildPageUrl('/ibkr_system.html', {}, { environment: currentEnvironment, allowGlobal: true })
                : `/ibkr_system.html?environment=${encodeURIComponent(currentEnvironment || currentBrokerMode || 'paper')}`;
            window.location.href = target;
        }

        window.handleRuntimeAction = handleRuntimeAction;
        window.handleBrokerModeSwitch = handleBrokerModeSwitch;
        window.handleServiceAction = handleServiceAction;
        window.runPrimaryAuthAction = runPrimaryAuthAction;
        window.runSecondaryAuthAction = runSecondaryAuthAction;
        window.openRuntimeSystemStatus = openRuntimeSystemStatus;
        window.submitTwoFactorResponse = submitTwoFactorResponse;
