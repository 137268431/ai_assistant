        function summarizeAction(action, payload, target = null) {
            if (!payload || typeof payload !== 'object') {
                return `${action} 已执行。`;
            }
            if (payload.accepted === true || payload.status_code === 202) {
                return payload.message || `${action} 已受理，正在后台处理；页面会自动刷新状态。`;
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
            if (action === 'app_login_handoff') {
                const targetMode = normalizeBrokerSessionMode(target?.body?.app_login_target || payload?.app_login_target || '');
                const gatewayMode = normalizeBrokerSessionMode(target?.body?.gateway_session_mode || payload?.gateway_session_mode || '');
                const gatewayLabel = gatewayMode ? `${formatAppLoginModeLabel(gatewayMode)} Gateway` : '当前 Gateway';
                if (payload.ok === false) {
                    return payload.message || `让出 ${gatewayLabel} 会话失败。`;
                }
                return `已让出 ${gatewayLabel} 会话，现在可以登录 IBKR App ${formatAppLoginModeLabel(targetMode || gatewayMode)}。完成后点“启动 Runtime 线程”恢复。`;
            }
            if (action === 'probe' || action === 'panic_reset_2fa') {
                if (payload.message) return payload.message;
                if (action === 'probe') return '已触发认证检查；如果会话已恢复，状态会自动转为 success。';
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
            if (!actionPending) actionPendingLabel = '';
            renderRuntimeFlowPrimaryAction(latestRuntimeStatus, latestTwoFactorState);
            syncActionLocks();
            if (typeof renderBrokerModeSwitchPanel === 'function') {
                renderBrokerModeSwitchPanel(latestBrokerModeSwitchPreview, latestRuntimeStatus, latestTwoFactorState);
            }
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

        function ensureRuntimeConfirmDialog() {
            let dialog = document.getElementById('runtimeConfirmDialog');
            if (dialog) return dialog;
            dialog = document.createElement('div');
            dialog.id = 'runtimeConfirmDialog';
            dialog.className = 'runtime-confirm-overlay';
            dialog.innerHTML = `
                <div class="runtime-confirm-modal" role="dialog" aria-modal="true" aria-labelledby="runtimeConfirmTitle">
                    <div class="runtime-confirm-kicker" id="runtimeConfirmKicker">Confirm Action</div>
                    <div class="runtime-confirm-title" id="runtimeConfirmTitle">确认操作</div>
                    <div class="runtime-confirm-message" id="runtimeConfirmMessage"></div>
                    <label class="runtime-confirm-input-wrap is-hidden" id="runtimeConfirmInputWrap">
                        <span id="runtimeConfirmInputLabel">输入确认文本</span>
                        <input id="runtimeConfirmInput" class="runtime-confirm-input" autocomplete="off" spellcheck="false">
                    </label>
                    <div class="runtime-confirm-actions">
                        <button class="runtime-confirm-btn secondary" type="button" id="runtimeConfirmCancel">取消</button>
                        <button class="runtime-confirm-btn quick is-hidden" type="button" id="runtimeConfirmQuick">快速确认</button>
                        <button class="runtime-confirm-btn primary" type="button" id="runtimeConfirmOk">确定</button>
                    </div>
                </div>
            `;
            document.body.appendChild(dialog);
            return dialog;
        }

        function showRuntimeConfirm({
            title = '确认操作',
            message = '',
            confirmText = '',
            confirmLabel = '确定',
            cancelLabel = '取消',
            tone = 'warn',
            inputLabel = '',
            inputValue = '',
            quickConfirmLabel = '快速确认',
        } = {}) {
            return new Promise((resolve) => {
                const dialog = ensureRuntimeConfirmDialog();
                const modal = dialog.querySelector('.runtime-confirm-modal');
                const kickerEl = dialog.querySelector('#runtimeConfirmKicker');
                const titleEl = dialog.querySelector('#runtimeConfirmTitle');
                const messageEl = dialog.querySelector('#runtimeConfirmMessage');
                const inputWrap = dialog.querySelector('#runtimeConfirmInputWrap');
                const inputLabelEl = dialog.querySelector('#runtimeConfirmInputLabel');
                const inputEl = dialog.querySelector('#runtimeConfirmInput');
                const cancelButton = dialog.querySelector('#runtimeConfirmCancel');
                const quickButton = dialog.querySelector('#runtimeConfirmQuick');
                const okButton = dialog.querySelector('#runtimeConfirmOk');
                const requiredText = String(confirmText || '').trim();
                let settled = false;

                const cleanup = (result) => {
                    if (settled) return;
                    settled = true;
                    dialog.classList.remove('show');
                    document.removeEventListener('keydown', onKeydown);
                    inputEl.removeEventListener('input', syncConfirmButton);
                    cancelButton.removeEventListener('click', onCancel);
                    quickButton.removeEventListener('click', onQuickConfirm);
                    okButton.removeEventListener('click', onConfirm);
                    dialog.removeEventListener('click', onBackdrop);
                    resolve(Boolean(result));
                };
                const syncConfirmButton = () => {
                    okButton.disabled = Boolean(requiredText) && String(inputEl.value || '').trim() !== requiredText;
                };
                const onCancel = () => cleanup(false);
                const onQuickConfirm = () => {
                    if (!requiredText) return;
                    inputEl.value = requiredText;
                    syncConfirmButton();
                    quickButton.textContent = '已确认';
                    quickButton.disabled = true;
                    okButton.focus();
                };
                const onConfirm = () => {
                    if (okButton.disabled) return;
                    cleanup(true);
                };
                const onBackdrop = (event) => {
                    if (event.target === dialog) cleanup(false);
                };
                const onKeydown = (event) => {
                    if (event.key === 'Escape') {
                        event.preventDefault();
                        cleanup(false);
                    }
                    if (event.key === 'Enter' && document.activeElement === inputEl && !okButton.disabled) {
                        event.preventDefault();
                        cleanup(true);
                    }
                };

                dialog.classList.remove('tone-danger', 'tone-warn', 'tone-info');
                dialog.classList.add(`tone-${String(tone || 'warn').trim() || 'warn'}`);
                kickerEl.textContent = requiredText ? 'Type To Confirm' : 'Confirm Action';
                titleEl.textContent = title;
                messageEl.textContent = message;
                cancelButton.textContent = cancelLabel;
                quickButton.textContent = quickConfirmLabel;
                quickButton.disabled = false;
                okButton.textContent = confirmLabel;
                inputEl.value = '';
                inputEl.placeholder = inputValue || requiredText;
                if (requiredText) {
                    inputWrap.classList.remove('is-hidden');
                    quickButton.classList.remove('is-hidden');
                    inputLabelEl.textContent = inputLabel || `请输入 ${requiredText}`;
                } else {
                    inputWrap.classList.add('is-hidden');
                    quickButton.classList.add('is-hidden');
                    inputLabelEl.textContent = '';
                }
                syncConfirmButton();

                inputEl.addEventListener('input', syncConfirmButton);
                cancelButton.addEventListener('click', onCancel);
                quickButton.addEventListener('click', onQuickConfirm);
                okButton.addEventListener('click', onConfirm);
                dialog.addEventListener('click', onBackdrop);
                document.addEventListener('keydown', onKeydown);
                dialog.classList.add('show');

                window.requestAnimationFrame(() => {
                    if (requiredText) {
                        inputEl.focus();
                    } else {
                        okButton.focus();
                    }
                    if (modal) modal.scrollTop = 0;
                });
            });
        }
