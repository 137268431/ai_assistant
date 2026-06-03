        function renderHeroStatusChip(chip) {
            const tip = String(chip.tip || '').trim();
            const tipAttrs = tip
                ? ` tabindex="0" aria-label="${escapeHtml(`${chip.label}：${tip}`)}" data-tip="${escapeHtml(tip)}"`
                : '';
            const tipIcon = tip ? '<span class="chip-tip-icon" aria-hidden="true">!</span>' : '';
            return `
                <span class="status-chip ${escapeHtml(chip.tone || '')}${tip ? ' has-tip' : ''}"${tipAttrs}>
                    <span class="dot" style="background:currentColor"></span>${escapeHtml(chip.label)}${tipIcon}
                </span>
            `;
        }

        function getRuntimeMarketSessionLabel(session = {}) {
            const kind = String(session?.kind || '').trim().toLowerCase();
            const labels = {
                premarket: '盘前',
                regular: '盘中',
                close_transition: '盘后过渡',
                afterhours: '盘后',
                overnight: '夜盘',
                night: '夜盘',
                closed: '闭市'
            };
            return String(session?.label_zh || session?.display_label || labels[kind] || kind || '--');
        }

        function getRuntimeMarketSessionTone(session = {}) {
            const kind = String(session?.kind || '').trim().toLowerCase();
            if (kind === 'closed') return 'warn';
            if (kind === 'afterhours' || kind === 'close_transition' || kind === 'premarket' || kind === 'overnight' || kind === 'night') return 'shared';
            if (kind === 'regular') return 'ok';
            return '';
        }

        function buildRuntimeMarketSessionTitle(session = {}) {
            const lines = [];
            const source = String(session?.source || session?.calendar?.source || '').trim();
            const sourceError = String(session?.source_error || session?.calendar?.source_error || '').trim();
            const sourceLabel = source === 'ibkr_schedule'
                ? 'IBKR 合约交易时间'
                : (source === 'local_nyse_fallback' ? '本地 NYSE 兜底日历' : (source || '来源待确认'));
            lines.push(`来源：${sourceError ? `${sourceLabel}（IBKR 拉取失败: ${sourceError}）` : sourceLabel}`);
            if (session?.us_time || session?.cn_time) {
                lines.push(`当前：${session?.us_time || '--'} ET / ${session?.cn_time || '--'} 北京`);
            }
            if (session?.regular_open_us || session?.regular_close_us) {
                lines.push(`常规美东：${session?.regular_open_us || '--'} - ${session?.regular_close_us || '--'}`);
            }
            if (session?.regular_open_beijing || session?.regular_close_beijing) {
                lines.push(`常规北京：${session?.regular_open_beijing || '--'} - ${session?.regular_close_beijing || '--'}`);
            }
            if (session?.extended_open_us || session?.extended_close_us) {
                lines.push(`扩展美东：${session?.extended_open_us || '--'} - ${session?.extended_close_us || '--'}`);
            }
            if (session?.extended_open_beijing || session?.extended_close_beijing) {
                lines.push(`扩展北京：${session?.extended_open_beijing || '--'} - ${session?.extended_close_beijing || '--'}`);
            }
            if (String(session?.kind || '').toLowerCase() === 'closed' && (session?.next_open_us || session?.next_open_beijing)) {
                lines.push(`下次开盘：${session?.next_open_us || '--'} ET / ${session?.next_open_beijing || '--'} 北京`);
            }
            return lines.join('\n');
        }

        function renderMetricCards(summary, health, status, twoFactorState, latestBar) {
            const today = summary?.today || {};
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactorState);
            const twoFactorStatus = String(twoFactorState?.status || '').trim().toUpperCase() || '--';
            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            const schedulerService = status?.service_topology?.services?.['ibkr-scheduler'] || {};
            const gatewayCopy = runtimeStatus.snapshotIncomplete
                ? `2FA ${twoFactorStatus} · 快照待刷新`
                : `2FA ${twoFactorStatus} · ${runtimeStatus.gatewayActive ? 'gateway active' : 'gateway offline'}`;
            const cards = [
                {
                    label: 'Session Auth',
                    value: runtimeStatus.authenticated ? 'AUTHED' : (runtimeStatus.started ? 'WAITING' : 'STOPPED'),
                    copy: gatewayCopy
                },
                {
                    label: 'Gateway',
                    value: runtimeStatus.gatewayActive ? 'ACTIVE' : 'OFFLINE',
                    copy: status?.gateway?.managed_by ? `manager ${String(status.gateway.managed_by).toUpperCase()}` : 'IBKR session path'
                },
                {
                    label: 'Runtime',
                    value: String(runtimeService.status || summary?.ibkr_runtime?.status || 'unknown').toUpperCase(),
                    copy: `mode ${formatRuntimeModeLabel(runtimeService.runtime_mode || status?.service_topology?.runtime_mode, { compact: true })}`
                },
                {
                    label: '2FA',
                    value: twoFactorStatus,
                    copy: twoFactorState?.last_request_at ? `last ${formatTimeLabel(twoFactorState.last_request_at)}` : 'push / challenge / response'
                },
                {
                    label: 'Today Signals',
                    value: formatCompactNumber(today.ibkr_signals || 0),
                    copy: `orders ${today.orders || 0} · events ${today.events || 0}`
                },
                {
                    label: 'Trading',
                    value: summary?.ibkr_trading_enabled ? 'ON' : 'OFF',
                    copy: summary?.ibkr_live_trading_enabled ? 'live switch ON' : 'live switch guarded'
                },
                {
                    label: 'Supporting Ops',
                    value: String(schedulerService.status || 'peer').toUpperCase(),
                    copy: schedulerService.detail || schedulerService.responsibility || 'scheduler / notifications / storage'
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
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const runtimeStatus = getEffectiveRuntimeStatusCardModel(status, twoFactor);
            const gatewayActive = runtimeStatus.gatewayActive;
            const runtimeStarted = runtimeStatus.started;
            const sessionAuthenticated = runtimeStatus.authenticated;
            const twoFactorStatus = String(twoFactor?.status || '').trim().toLowerCase();
            const challengeCode = String(twoFactor?.challenge_code || '').trim();
            const recoveryPhase = String(twoFactor?.recovery_phase || '').trim().toLowerCase();
            const responsePhase = getIbkrTwoFactorResponsePhase(twoFactor);
            const runtimeMismatch = getRuntimeEnvironmentMismatch(status, twoFactor);
            let primaryTone = 'ok';
            let primaryTitle = 'IBKR execution ready';
            let primaryCopy = `Session AUTHED · runtime ${runtimeStarted ? 'RUNNING' : 'STOPPED'}`;
            if (runtimeMismatch) {
                primaryTone = 'error';
                primaryTitle = 'Broker mode mismatch';
                primaryCopy = runtimeMismatch.message;
            } else if (startup.active) {
                primaryTone = 'warn';
                primaryTitle = getStartupCurrentStepLabel(startup) || 'Startup flow active';
                primaryCopy = String(startup.operator_action || startup.current_blocker || startup.status || '--');
            } else if (!gatewayActive) {
                primaryTone = 'error';
                primaryTitle = 'Gateway offline';
                primaryCopy = '先恢复 Gateway，再处理 2FA 与执行动作。';
            } else if (!runtimeStarted) {
                primaryTone = 'warn';
                primaryTitle = 'Runtime stopped';
                primaryCopy = '可在主运行流程启动 runtime 线程。';
            } else if (!sessionAuthenticated) {
                primaryTone = challengeCode || responsePhase !== 'idle' || recoveryPhase
                    ? 'warn'
                    : 'error';
                primaryTitle = challengeCode ? `2FA challenge ${challengeCode}` : 'Session waiting for 2FA';
                primaryCopy = `2FA ${twoFactorStatus || 'idle'} · response ${responsePhase || 'idle'}`;
            }
            const primaryCard = {
                tone: primaryTone,
                kicker: 'EXECUTION READINESS',
                title: primaryTitle,
                copy: primaryCopy,
            };

            const signalStatus = String(latestSignal?.status || '').trim();
            const signalDirection = String(latestSignal?.direction || '').trim().toUpperCase();
            const signalCard = {
                tone: latestSignal ? 'ok' : 'info',
                kicker: 'TV WEBHOOK',
                title: latestSignal
                    ? `${latestSignal.symbol || '--'} ${signalDirection || '--'}`
                    : '等待 TV 预警',
                copy: latestSignal
                    ? `${formatRuntimeSignalStatus(signalStatus)} · ${(latestSignal.us_time || latestSignal.created || '').slice(0, 19) || '--'}`
                    : 'TradingView 负责预警、开仓和平仓；这里仅承接 webhook 与执行闭环。',
                links: [
                    { label: '查看信号', path: '/ibkr_signals.html' },
                ],
            };

            const marketUniverse = status?.market_universe || status?.runtime?.market_universe || {};
            const targetCount = Number(marketUniverse.active_target_count || summary?.today?.ibkr_targets || 0) || 0;
            const poolCount = Number(marketUniverse.watchlist_pool_count || 0) || 0;
            const targetCard = {
                tone: targetCount > 0 ? 'ok' : 'warn',
                kicker: 'TODAY TARGETS',
                title: targetCount > 0 ? `${targetCount} active targets` : 'target pool pending',
                copy: `pool ${poolCount} · ${formatTradeUniverseStatus(marketUniverse.trade_universe_status)}`,
                links: [
                    { label: '查看标的', path: '/ibkr_screener.html', params: { tab: 'screener', view: 'current' } },
                ],
            };

            const topologyServices = getOrderedTopologyServices(status?.service_topology || {});
            const topologyReadyCount = topologyServices.filter((service) => {
                const serviceStatus = String(service?.status || '').trim().toLowerCase();
                return ['running', 'peer', 'external', 'online', 'idle'].includes(serviceStatus);
            }).length;
            const topologyCard = {
                tone: topologyServices.length && topologyReadyCount >= topologyServices.length ? 'ok' : 'info',
                kicker: 'OPS ROUTING',
                title: topologyServices.length
                    ? `split ${topologyReadyCount}/${topologyServices.length} visible`
                    : 'runtime topology pending',
                copy: topologyServices.length
                    ? '控制台只保留 Gateway、runtime、Scheduler 与 PB 操作前摘要。'
                    : '等待 service_topology 返回 runtime / gateway / scheduler / pocketbase。',
            };

            const cards = [primaryCard, signalCard, targetCard, topologyCard];
            document.getElementById('opsGrid').innerHTML = cards.map((card) => `
                <div class="ops-card ${escapeHtml(card.tone || 'info')}">
                    <div class="ops-kicker">${escapeHtml(card.kicker || '--')}</div>
                    <div class="ops-title">${escapeHtml(card.title || '--')}</div>
                    <div class="ops-copy">${escapeHtml(card.copy || '--')}</div>
                    ${Array.isArray(card.links) && card.links.length ? `
                        <div class="ops-links">
                            ${card.links.map((link) => `
                                <a class="ops-link" href="${buildPageUrl(link.path, link.params || {}, link.options || { environment: currentEnvironment })}">${escapeHtml(link.label)}</a>
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
            ['ibkr-runtime', 'ibkr-gateway', 'ibkr-scheduler', 'pocketbase'].forEach((name) => {
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
            renderAppLoginHandoffButton(status);
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

        function renderAppLoginHandoffButton(status = latestRuntimeStatus) {
            const button = document.getElementById('appLoginHandoffButton');
            if (!button) return;
            const session = getGatewaySessionModeModel(status);
            const label = button.querySelector('.action-label');
            const copy = button.querySelector('.action-copy');
            const modeText = session.running ? session.compactLabel : '未占用';
            const dataText = formatEnvironmentLabel(currentDataEnvironment, { compact: true });
            const brokerText = formatEnvironmentLabel(currentBrokerMode, { compact: true });
            button.classList.toggle('action-danger', session.running && session.mode === 'live');
            button.classList.toggle('action-accent', !(session.running && session.mode === 'live'));
            button.title = session.running
                ? `当前 Gateway 占用 ${modeText}；选择 App 登录目标后自动判断是否需要停止 Gateway。`
                : '当前 Gateway 未占用 App 登录 session，可以直接登录 IBKR App。';
            if (label) {
                label.textContent = session.running
                    ? `准备登录 IBKR App · Gateway ${modeText}`
                    : '准备登录 IBKR App · Gateway 未占用';
            }
            if (copy) {
                copy.textContent = session.running
                    ? `当前下单 ${brokerText} · 行情 ${dataText}；选择 PAPER / LIVE 后判断是否冲突。`
                    : `当前下单 ${brokerText} · 行情 ${dataText}；Gateway 未运行时不会执行停止动作。`;
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

        function getOppositeBrokerMode(mode) {
            return normalizeBrokerMode(mode, 'paper') === 'paper' ? 'live' : 'paper';
        }

        function formatBrokerModeSwitchLabel(mode, options = {}) {
            const normalized = normalizeBrokerMode(mode, 'paper');
            const label = typeof formatEnvironmentLabel === 'function'
                ? formatEnvironmentLabel(normalized, { compact: Boolean(options.compact) })
                : getEnvironmentLabel(normalized);
            return label || normalized.toUpperCase();
        }

        function getBrokerModeSwitchCounts(preview = {}) {
            const counts = preview?.counts && typeof preview.counts === 'object' ? preview.counts : {};
            const toCount = (value) => {
                const number = Number(value || 0);
                return Number.isFinite(number) ? Math.max(0, Math.round(number)) : 0;
            };
            return {
                openPositions: toCount(counts.open_positions),
                openOrders: toCount(counts.open_orders),
                pbBlockingGroups: toCount(counts.pb_blocking_groups),
                pbActiveGroups: toCount(counts.pb_active_order_groups),
                staleGroups: toCount(counts.stale_pb_order_groups),
                pbOnlyGroups: toCount(counts.pb_only_active_order_groups),
                shadowGroups: toCount(counts.pb_shadow_groups),
            };
        }

        function renderBrokerModeSwitchPanel(preview = latestBrokerModeSwitchPreview, status = latestRuntimeStatus, twoFactorState = latestTwoFactorState) {
            const el = document.getElementById('brokerModeSwitchArea');
            if (!el) return;
            const payload = preview && typeof preview === 'object' ? preview : {};
            const currentMode = normalizeBrokerMode(
                payload.current_broker_mode || status?.broker_mode || status?.environment || currentBrokerMode,
                currentBrokerMode || 'paper'
            );
            const targetMode = normalizeBrokerMode(
                payload.target_broker_mode || getOppositeBrokerMode(currentMode),
                getOppositeBrokerMode(currentMode)
            );
            const switchRequired = payload.switch_required === false ? false : targetMode !== currentMode;
            const blockers = Array.isArray(payload.blockers) ? payload.blockers : [];
            const allowed = payload.allowed === true && switchRequired && blockers.length === 0;
            const counts = getBrokerModeSwitchCounts(payload);
            const account = payload.account && typeof payload.account === 'object' ? payload.account : {};
            const twoFactor = payload.two_factor && typeof payload.two_factor === 'object' ? payload.two_factor : deriveTwoFactorUiState(twoFactorState || {});
            const envFiles = Array.isArray(payload.env_files) ? payload.env_files : [];
            const envReady = envFiles.filter((item) => item?.exists && item?.readable).length;
            const targetIsLive = targetMode === 'live';
            const tone = payload.ok === false
                ? 'error'
                : (!switchRequired ? 'info' : (allowed ? 'ok' : (blockers.length ? 'warn' : 'info')));
            const firstBlocker = blockers[0]?.message || payload.error || '';
            const operationLockReason = actionPending ? '' : getActiveRuntimeOperationLockReason('gateway_restart');
            const lockReason = operationLockReason || (actionPending
                ? ''
                : (!switchRequired
                    ? `当前已经是 ${formatBrokerModeSwitchLabel(targetMode, { compact: true })}。`
                    : (!allowed ? (firstBlocker || '切换预检未通过。') : '')));
            const buttonLabel = switchRequired
                ? `一键切换到 ${targetMode.toUpperCase()}`
                : `已在 ${targetMode.toUpperCase()}`;
            const confirmText = String(payload.confirm_text || `SWITCH ${targetMode.toUpperCase()}`).trim();
            const riskItems = [
                { label: '持仓', value: counts.openPositions, tone: counts.openPositions > 0 ? 'bad' : 'ok' },
                { label: 'IBKR 挂单', value: counts.openOrders, tone: counts.openOrders > 0 ? 'bad' : 'ok' },
                { label: 'PB active/stale', value: counts.pbBlockingGroups, tone: counts.pbBlockingGroups > 0 ? 'bad' : 'ok' },
                { label: '2FA', value: twoFactor?.active ? 'ACTIVE' : 'IDLE', tone: twoFactor?.active ? 'bad' : 'ok' },
            ];
            const restartPlan = Array.isArray(payload.restart_plan) ? payload.restart_plan : [];
            const blockerHtml = blockers.length
                ? `
                    <div class="broker-mode-switch-blockers">
                        ${blockers.slice(0, 4).map((blocker) => `
                            <div class="broker-mode-switch-blocker">
                                <span class="broker-mode-switch-blocker-code">${escapeHtml(blocker.code || 'blocker')}</span>
                                <span>${escapeHtml(blocker.message || blocker.detail || '预检未通过')}</span>
                            </div>
                        `).join('')}
                    </div>
                `
                : `
                    <div class="broker-mode-switch-ready">
                        ${switchRequired
                            ? '预检通过：当前持仓、IBKR 挂单和 PB active/stale 订单组均为 0。'
                            : '当前 broker mode 与目标一致，无需切换。'}
                    </div>
                `;
            const restartPlanHtml = restartPlan.length
                ? `
                    <div class="broker-mode-switch-plan">
                        ${restartPlan.map((step) => `
                            <span class="broker-mode-switch-step">${escapeHtml(step.service || '--')} <b>${escapeHtml(String(step.action || '').toUpperCase())}</b></span>
                        `).join('')}
                    </div>
                `
                : '';
            const targetHint = targetIsLive
                ? 'LIVE 是真实账户：必须先确认无持仓、无挂单、无 PB active/stale 订单组。'
                : 'PAPER 切换会停止当前 runtime 并重启 Gateway，切换后仍需重新完成 2FA。';
            el.innerHTML = `
                <div class="broker-mode-switch-card ${escapeHtml(tone)} ${targetIsLive ? 'target-live' : 'target-paper'}">
                    <div class="broker-mode-switch-route" aria-label="broker mode switch route">
                        <div class="broker-mode-node current">
                            <span class="broker-mode-node-kicker">CURRENT</span>
                            <strong>${escapeHtml(formatBrokerModeSwitchLabel(currentMode, { compact: true }))}</strong>
                            <small>${escapeHtml(account.current_account_id_masked || '--')}</small>
                        </div>
                        <div class="broker-mode-switch-arrow">→</div>
                        <div class="broker-mode-node target">
                            <span class="broker-mode-node-kicker">TARGET</span>
                            <strong>${escapeHtml(formatBrokerModeSwitchLabel(targetMode, { compact: true }))}</strong>
                            <small>${escapeHtml(account.target_account_id_masked || (account.target_account_present === false ? '未配置' : '--'))}</small>
                        </div>
                    </div>
                    <div class="broker-mode-switch-body">
                        <div class="broker-mode-switch-main">
                            <div class="broker-mode-switch-status-row">
                                <span class="pill ${statusClass(allowed ? 'ready' : (blockers.length ? 'blocked' : 'neutral'))}">
                                    ${escapeHtml(allowed ? 'READY' : (blockers.length ? 'BLOCKED' : 'CHECK'))}
                                </span>
                                <span class="broker-mode-switch-confirm">确认词：${escapeHtml(confirmText)}</span>
                                <span class="broker-mode-switch-confirm">ENV files ${escapeHtml(String(envReady || 0))}/${escapeHtml(String(envFiles.length || 0))}</span>
                            </div>
                            <div class="broker-mode-switch-copy">
                                ${escapeHtml(targetHint)} 切换会写入运行 .env 并按顺序重启 Gateway / Runtime / Scheduler / API。
                            </div>
                            <div class="broker-mode-switch-risks">
                                ${riskItems.map((item) => `
                                    <div class="broker-mode-switch-risk ${escapeHtml(item.tone)}">
                                        <span>${escapeHtml(item.label)}</span>
                                        <strong>${escapeHtml(String(item.value))}</strong>
                                    </div>
                                `).join('')}
                            </div>
                            ${blockerHtml}
                            ${payload.runtime_status_error ? `<div class="broker-mode-switch-error">${escapeHtml(payload.runtime_status_error)}</div>` : ''}
                            ${restartPlanHtml}
                        </div>
                        <div class="broker-mode-switch-side">
                            <button
                                class="broker-mode-switch-btn ${targetIsLive ? 'danger' : 'paper'}"
                                type="button"
                                data-lock-reason="${escapeHtml(lockReason)}"
                                onclick="handleBrokerModeSwitch('${escapeHtml(targetMode)}')"
                                ${actionPending || Boolean(lockReason) ? 'disabled' : ''}
                            >${escapeHtml(buttonLabel)}</button>
                            <div class="broker-mode-switch-hint">按钮会先要求输入 <b>${escapeHtml(confirmText)}</b>，再提交真实 runtime 切换。</div>
                        </div>
                    </div>
                </div>
            `;
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
                const primaryLockReason = getRuntimeServiceActionLockReason(moduleDef.service, primaryAction);
                const restartLockReason = getRuntimeServiceActionLockReason(moduleDef.service, 'restart');
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
                            <button class="service-action-btn ${primaryAction === 'stop' ? 'stop' : ''}" type="button" data-service-action="${escapeHtml(moduleDef.service)}:${escapeHtml(primaryAction)}" title="${escapeHtml(primaryLockReason)}" onclick="handleServiceAction('${escapeHtml(moduleDef.service)}', '${escapeHtml(primaryAction)}')" ${actionPending || Boolean(primaryLockReason) ? 'disabled' : ''}>${escapeHtml(primaryLabel)}</button>
                            <button class="service-action-btn restart" type="button" data-service-action="${escapeHtml(moduleDef.service)}:restart" title="${escapeHtml(restartLockReason)}" onclick="handleServiceAction('${escapeHtml(moduleDef.service)}', 'restart')" ${actionPending || Boolean(restartLockReason) ? 'disabled' : ''}>${escapeHtml(restartLabel)}</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function ensureRuntimeFaultRecoveryStrip() {
            let strip = document.getElementById('runtimeFaultRecoveryStrip');
            if (strip) return strip;
            strip = document.createElement('div');
            strip.id = 'runtimeFaultRecoveryStrip';
            strip.setAttribute('role', 'region');
            strip.setAttribute('aria-label', 'Runtime fault recovery');
            strip.style.cssText = [
                'position:fixed',
                'left:16px',
                'right:16px',
                'bottom:16px',
                'z-index:1200',
                'display:none',
                'gap:12px',
                'align-items:center',
                'justify-content:space-between',
                'padding:14px',
                'border:1px solid rgba(251,191,36,.45)',
                'border-radius:18px',
                'background:linear-gradient(135deg,rgba(15,23,42,.96),rgba(69,26,3,.94))',
                'box-shadow:0 22px 60px rgba(15,23,42,.35)',
                'color:#f8fafc',
                'backdrop-filter:blur(14px)',
                'flex-wrap:wrap'
            ].join(';');
            document.body.appendChild(strip);
            return strip;
        }

        function renderRuntimeFaultRecoveryStrip(options = {}) {
            const strip = ensureRuntimeFaultRecoveryStrip();
            const visible = options.visible !== false;
            if (!visible) {
                strip.style.display = 'none';
                return;
            }
            const rawMessage = String(options.message || '').trim();
            const message = rawMessage || 'bootstrap / status payload 暂不可用，但恢复动作仍可直接提交。';
            strip.style.display = 'flex';
            strip.innerHTML = `
                <div style="min-width:220px;flex:1 1 320px">
                    <div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#fbbf24;font-weight:800">Fault Recovery</div>
                    <div style="font-size:15px;font-weight:800;margin-top:2px">Runtime 状态加载失败时仍可恢复</div>
                    <div style="font-size:12px;color:#fde68a;margin-top:4px;line-height:1.5">${escapeHtml(message)}</div>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                    <button class="runtime-secondary-action" type="button" data-action="panic_reset_2fa" onclick="handleRuntimeAction('panic_reset_2fa')" style="border-color:rgba(248,113,113,.65);color:#fecaca;background:rgba(127,29,29,.38)">重开 2FA</button>
                    <button class="runtime-secondary-action" type="button" data-action="gateway_restart" onclick="handleRuntimeAction('gateway_restart')" style="border-color:rgba(251,191,36,.55);color:#fef3c7;background:rgba(120,53,15,.35)">重启 Gateway</button>
                    <button class="runtime-secondary-action" type="button" onclick="loadRuntimeData(true)" style="border-color:rgba(125,211,252,.45);color:#e0f2fe;background:rgba(12,74,110,.35)">手动刷新状态</button>
                    <button class="runtime-secondary-action" type="button" onclick="openRuntimeSystemStatus()" style="border-color:rgba(148,163,184,.45);color:#f8fafc;background:rgba(51,65,85,.55)">查看系统状态</button>
                </div>
            `;
            syncActionLocks();
        }

        function renderServiceTopology(status = {}) {
            const el = document.getElementById('serviceTopologyArea');
            if (!el) return;
            const topologyPayload = status?.service_topology || {};
            const services = getOrderedTopologyServices(topologyPayload);
            if (!services.length) {
                el.innerHTML = `
                    <div class="runtime-link-summary">
                        <div class="runtime-link-copy">
                            <div class="runtime-link-title">链路摘要暂不可用</div>
                            <div class="runtime-link-sub">控制台只保留操作前关键依赖；深入排障请查看主机服务日志或系统配置。</div>
                        </div>
                    </div>
                    <div class="table-empty">暂无链路摘要</div>
                `;
                return;
            }

            const runtimeModeRaw = String(topologyPayload?.runtime_mode || '').trim();
            const runtimeMode = runtimeModeRaw ? runtimeModeRaw.toUpperCase() : '--';
            const runtimeModeLabel = formatRuntimeModeLabel(runtimeModeRaw || topologyPayload?.runtime_mode, { compact: true });
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
                summarizeService('ibkr-runtime', 'Runtime', `运行位置 ${runtimeModeLabel}`),
                summarizeService('ibkr-gateway', 'Gateway', 'IBC + IB Gateway session path'),
                summarizeService('ibkr-scheduler', 'Supporting Ops', 'scheduler / notifications / storage jobs'),
                summarizeService('pocketbase', 'PocketBase', 'state / config / event store'),
            ];

            el.innerHTML = `
                <div class="service-topology-shell">
                    <div class="runtime-link-summary">
                        <div class="runtime-link-copy">
                            <div class="runtime-link-title">控制台只看操作前关键依赖</div>
                            <div class="runtime-link-sub">当前运行位置 ${escapeHtml(runtimeModeLabel)} / ${escapeHtml(serviceProfile)} · restart independent ${escapeHtml(restartIndependent)}。深入排障使用主机服务日志与系统配置。</div>
                        </div>
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
                        <span class="mini-tag"><span class="mini-label">位置</span><span>${escapeHtml(runtimeModeLabel)}</span></span>
                        <span class="mini-tag"><span class="mini-label">Profile</span><span>${escapeHtml(serviceProfile)}</span></span>
                        <span class="mini-tag"><span class="mini-label">Restart</span><span>${escapeHtml(restartIndependent)}</span></span>
                        <span class="mini-tag"><span class="mini-label">Services</span><span>${escapeHtml(String(services.length))}</span></span>
                    </div>
                </div>
            `;
        }

        function renderHero(summary, health, status, runtimeConfig, twoFactorState, startupState, latestBar) {
            const startup = normalizeStartupUiState(startupState);
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
            const runtimeMode = status?.service_topology?.runtime_mode;
            const chips = [
                { label: gatewayChipLabel, tone: gatewayRunning ? 'chip-ok' : 'chip-error' },
                { label: sessionChipLabel, tone: sessionAuthenticated ? 'chip-ok' : 'chip-warn' },
                { label: `2FA ${twoFactorStatus.toUpperCase()}`, tone: sessionAuthenticated ? 'chip-ok' : chipTone(twoFactorStatus) },
                ...(runtimeStatus.snapshotIncomplete ? [{ label: 'Runtime SNAPSHOT REFRESHING', tone: 'chip-muted' }] : []),
                ...(startup.active ? [{ label: `Flow ${getManualAuthReasonLabel(startup.reason)}`, tone: chipTone(startup.status || 'active') }] : []),
                { label: `Trading ${summary?.ibkr_trading_enabled ? 'ON' : 'OFF'}`, tone: summary?.ibkr_trading_enabled ? 'chip-ok' : 'chip-error' },
                { label: `运行位置 ${formatRuntimeModeLabel(runtimeMode, { compact: true })}`, tone: runtimeModeChipTone(runtimeMode) },
                { label: `Broker ${formatEnvironmentLabel(currentBrokerMode)}`, tone: environmentChipTone(currentBrokerMode) },
                { label: 'Shared Data LIVE', tone: 'chip-ok' }
            ];
            document.getElementById('heroBadges').innerHTML = chips.map(renderHeroStatusChip).join('');

            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            const gatewayService = status?.service_topology?.services?.['ibkr-gateway'] || {};
            document.getElementById('runtimeBaseInfo').textContent = `runtime: ${String(runtimeService.internal_url || '--')} · gateway: ${String(gatewayService.status || (gatewayRunning ? 'active' : 'offline')).toUpperCase()} · 运行位置: ${formatRuntimeModeLabel(runtimeService.runtime_mode, { compact: true })}`;
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
                warmupSummaryText: '',
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
                { label: 'Broker', value: getEnvironmentLabel(currentBrokerMode), tone: currentBrokerMode },
                { label: 'Data', value: getEnvironmentLabel(currentDataEnvironment), tone: currentDataEnvironment },
                { label: 'Market Date', value: String(status?.market_universe?.market_date || '--') },
                {
                    label: '时段',
                    value: getRuntimeMarketSessionLabel(status?.market_session || {}),
                    tone: getRuntimeMarketSessionTone(status?.market_session || {}),
                    title: buildRuntimeMarketSessionTitle(status?.market_session || {}),
                    includeInContext: true,
                },
                { label: '运行位置', value: formatRuntimeModeLabel(status?.service_topology?.runtime_mode, { compact: true }) },
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
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const startupStrategy = getStartupStrategy(status);
            const autoRestoreGuard = status?.auto_restore_guard || {};
            const sessionAuthenticated = Boolean(status?.session?.authenticated);
            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            const schedulerService = status?.service_topology?.services?.['ibkr-scheduler'] || {};
            const gatewayService = status?.service_topology?.services?.['ibkr-gateway'] || {};
            const marketUniverse = status?.market_universe || status?.runtime?.market_universe || {};
            const latestSignalTime = latestSignal ? String((latestSignal.us_time || latestSignal.created || '--')).slice(0, 19) : '--';
            const rows = [
                ['Runtime Service', String(runtimeService.status || '--').toUpperCase()],
                ['Runtime Owner', String(runtimeService.owner || '--')],
                ['Runtime Mode', formatRuntimeModeLabel(runtimeService.runtime_mode)],
                ['Runtime Internal URL', String(runtimeService.internal_url || '--')],
                ['Scheduler', String(schedulerService.status || '--').toUpperCase()],
                ['Gateway', status?.gateway?.running || status?.gateway?.reachable || gatewayService.status ? 'ACTIVE' : 'OFFLINE'],
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
                ['Weekly Deadline', twoFactor?.business_deadline_at ? `${String(twoFactor.business_deadline_at)} ET` : '--'],
                ['Cycle Deadline', twoFactor?.confirm_deadline_at ? `${String(twoFactor.confirm_deadline_at)} ET` : '--'],
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
                ['Market Date', String(marketUniverse.market_date || '--')],
                ['Trade Universe Status', formatTradeUniverseStatus(marketUniverse.trade_universe_status)],
                ['Watchlist Pool', String(marketUniverse.watchlist_pool_count || 0)],
                ['Today Targets', String(marketUniverse.active_target_count || summary?.today?.ibkr_targets || 0)],
                ['Trade Targets', Array.isArray(marketUniverse.active_trade_symbols) ? marketUniverse.active_trade_symbols.join(', ') || '--' : '--'],
                ['Target Date', String(marketUniverse.active_target_date || '--')],
                ['Last Target Refresh', formatTimeLabel(marketUniverse.last_target_refresh)],
                ['Latest Signal Time', latestSignalTime],
                ['Default Envs', Array.isArray(status?.default_environments) ? status.default_environments.join(', ') : '--'],
                ['Supported Envs', Array.isArray(status?.supported_environments) ? status.supported_environments.join(', ') : '--'],
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
                ? definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentDataEnvironment))
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
            const scheduler = getIbkrSchedulerSummary(schedulerPayload?.scheduler || {}, currentDataEnvironment);
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
                    </div>
                `);
            }
            document.getElementById('configDetail').innerHTML = blocks.length
                ? blocks.join('<div class="config-divider"></div>')
                : renderEmpty('暂无关键配置');
        }
