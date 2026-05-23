        function buildDataStatusTip(dataHealth, latestBar, status, runtimeStatus) {
            const statusKey = String(dataHealth?.status || '').trim().toLowerCase();
            if (!['offline', 'delayed', 'no_data', 'loading'].includes(statusKey)) return '';

            const onlineMax = typeof IBKR_DATA_ONLINE_MAX_AGE_MIN !== 'undefined' ? IBKR_DATA_ONLINE_MAX_AGE_MIN : 10;
            const delayedMax = typeof IBKR_DATA_DELAYED_MAX_AGE_MIN !== 'undefined' ? IBKR_DATA_DELAYED_MAX_AGE_MIN : 30;
            const symbol = dataHealth?.last_symbol || latestBar?.symbol || '--';
            const interval = formatIbkrIntervalLabel(latestBar?.interval || '5m');
            const age = Number(dataHealth?.last_bar_age_min);
            const lines = [];

            if (statusKey === 'loading') {
                lines.push('判定：最近 bars 正在加载，首屏暂不按断链处理。');
            } else if (!dataHealth?.last_bar_time_ms) {
                lines.push('判定：当前环境还没有读取到最近 bars。');
            } else {
                const ageText = Number.isFinite(age) ? `${age} 分钟` : '--';
                lines.push(`判定：最新 ${symbol} ${interval} bar 停在 ${dataHealth.last_bar_label || '--'}，距现在 ${ageText}。`);
                lines.push(`规则：<=${onlineMax}m 在线，${onlineMax}-${delayedMax}m 延迟，>${delayedMax}m 标记离线。`);
            }

            const reasons = [];
            if (String(currentDataEnvironment || '').trim().toLowerCase() === 'live') {
                reasons.push('美股已收盘、休市或盘后，live 5m bars 预期不会继续推进。');
            }
            if (!runtimeStatus?.gatewayActive) {
                reasons.push('Gateway 不可达，行情源断开。');
            } else if (!runtimeStatus?.authenticated) {
                reasons.push('Gateway 会话未认证，无法拉取实时行情。');
            }
            if (!runtimeStatus?.started) {
                reasons.push('Runtime 业务线程未运行，bars 聚合和写入不会推进。');
            }
            reasons.push('行情订阅、IBKR market data farm 或 bar writer 暂停，最新 bars 没写入 PocketBase。');

            lines.push('可能原因：');
            reasons.slice(0, 4).forEach((reason) => lines.push(`- ${reason}`));
            lines.push('下一步：先看“最新 Bars / 数据质量”；若在盘中，再查 Gateway、Runtime 和 bar writer。');
            return lines.join('\n');
        }

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

        function renderMetricCards(summary, health, status, twoFactorState, latestBar) {
            const today = summary?.today || {};
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const dataIsLoading = dataHealth.status === 'loading';
            const realtimeMetrics = deriveRealtimeMetrics(status, latestBar);
            const warmup = normalizeWarmup(status);
            const repairQueue = status?.bar_repair_queue || {};
            const historyFetch = getHistoryFetchModel(status);
            const watchlistTopup = getWatchlistTopupModel(status);
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
                    label: 'History Fetch',
                    value: historyFetch.value,
                    copy: historyFetch.copy
                },
                {
                    label: 'Watchlist Topup',
                    value: watchlistTopup.value,
                    copy: watchlistTopup.copy
                },
                {
                    label: 'Bar Repair',
                    value: `${Number(repairQueue.pending || 0) + Number(repairQueue.inflight || 0)}`,
                    copy: `pending ${repairQueue.pending || 0} · inflight ${repairQueue.inflight || 0} · failed ${repairQueue.failed || 0}`
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
                environment: currentDataEnvironment,
            });

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
                    ? '控制台只保留操作前摘要；服务拓扑、主机健康和 PB 磁盘统一在运维大盘排查。'
                    : '等待 service_topology 返回 runtime / compute / backtest / gateway / pocketbase。',
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
            ['ibkr-runtime', 'ibkr-compute', 'ibkr-backtest', 'ibkr-gateway', 'pocketbase'].forEach((name) => {
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
            const lockReason = actionPending
                ? ''
                : (!switchRequired
                    ? `当前已经是 ${formatBrokerModeSwitchLabel(targetMode, { compact: true })}。`
                    : (!allowed ? (firstBlocker || '切换预检未通过。') : ''));
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
                                ${escapeHtml(targetHint)} 切换会写入运行 .env 并按顺序重启 Gateway / Runtime / Compute / Scheduler / API。
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
                summarizeService('ibkr-compute', 'Compute', 'indicators / signals / data quality'),
                summarizeService('ibkr-backtest', 'Backtest', 'replay / backtest worker · independent'),
                summarizeService('ibkr-gateway', 'Gateway', 'IBC + IB Gateway session path'),
                summarizeService('pocketbase', 'PocketBase', 'state / config / event store'),
            ];

            el.innerHTML = `
                <div class="service-topology-shell">
                    <div class="runtime-link-summary">
                        <div class="runtime-link-copy">
                            <div class="runtime-link-title">控制台只看操作前关键依赖</div>
                            <div class="runtime-link-sub">当前运行位置 ${escapeHtml(runtimeModeLabel)} / ${escapeHtml(serviceProfile)} · restart independent ${escapeHtml(restartIndependent)}。完整拓扑、主机健康、请求与 PB 磁盘统一进运维大盘。</div>
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
                        <span class="mini-tag"><span class="mini-label">位置</span><span>${escapeHtml(runtimeModeLabel)}</span></span>
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
            const runtimeMode = status?.service_topology?.runtime_mode;
            const dataTip = buildDataStatusTip(dataHealth, latestBar, status, runtimeStatus);
            const chips = [
                { label: `Compute ${String(computeStatus).toUpperCase()}`, tone: chipTone(computeStatus) },
                { label: `Data ${String(dataStatus).toUpperCase()}`, tone: chipTone(dataStatus), tip: dataTip },
                { label: gatewayChipLabel, tone: gatewayRunning ? 'chip-ok' : 'chip-error' },
                { label: sessionChipLabel, tone: sessionAuthenticated ? 'chip-ok' : 'chip-warn' },
                { label: `Warmup ${warmup.gate_open ? 'READY' : String(warmup.phase || 'idle').toUpperCase()}`, tone: warmup.gate_open ? 'chip-ok' : chipTone(warmup.phase) },
                { label: `2FA ${twoFactorStatus.toUpperCase()}`, tone: sessionAuthenticated ? 'chip-ok' : chipTone(twoFactorStatus) },
                ...(runtimeStatus.snapshotIncomplete ? [{ label: 'Runtime SNAPSHOT REFRESHING', tone: 'chip-muted' }] : []),
                ...(startup.active ? [{ label: `Flow ${getManualAuthReasonLabel(startup.reason)}`, tone: chipTone(startup.status || 'active') }] : []),
                { label: `Trading ${summary?.ibkr_trading_enabled ? 'ON' : 'OFF'}`, tone: summary?.ibkr_trading_enabled ? 'chip-ok' : 'chip-error' },
                { label: `Compute ${summary?.compute_enabled ? 'ON' : 'OFF'}`, tone: summary?.compute_enabled ? 'chip-ok' : 'chip-error' },
                { label: `运行位置 ${formatRuntimeModeLabel(runtimeMode, { compact: true })}`, tone: runtimeModeChipTone(runtimeMode) },
                { label: `Broker ${formatEnvironmentLabel(currentBrokerMode)}`, tone: environmentChipTone(currentBrokerMode) },
                { label: 'Shared Data LIVE', tone: 'chip-ok' }
            ];
            document.getElementById('heroBadges').innerHTML = chips.map(renderHeroStatusChip).join('');

            const computeBase = runtimeConfig.find((item) => item.key === 'ibkr_compute_internal_url')?.value
                || summary?.config?.ibkr_compute_internal_url
                || status?.service_topology?.services?.['ibkr-compute']?.internal_url
                || 'http://127.0.0.1:5100';
            const backtestBase = runtimeConfig.find((item) => item.key === 'ibkr_backtest_internal_url')?.value
                || summary?.config?.ibkr_backtest_internal_url
                || status?.service_topology?.services?.['ibkr-backtest']?.internal_url
                || 'http://127.0.0.1:5105';
            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            document.getElementById('computeBaseInfo').textContent = `compute base: ${computeBase} · backtest base: ${backtestBase} · 运行位置: ${formatRuntimeModeLabel(runtimeService.runtime_mode, { compact: true })} · runtime: ${String(runtimeService.internal_url || '--')}`;
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
                { label: 'Broker', value: getEnvironmentLabel(currentBrokerMode), tone: currentBrokerMode },
                { label: 'Data', value: getEnvironmentLabel(currentDataEnvironment), tone: currentDataEnvironment },
                { label: 'Market Date', value: String(status?.market_universe?.market_date || '--') },
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
            const computeHealth = normalizeIbkrComputeHealth(health);
            const dataHealth = deriveDataHealth(latestBar);
            const realtimeMetrics = deriveRealtimeMetrics(status, latestBar);
            const realtimeState = deriveRealtimeComputeState(status);
            const warmup = normalizeWarmup(status);
            const warmupElapsedS = getWarmupElapsedSeconds(warmup);
            const canonical = status?.canonical_5m || {};
            const historyFetch = getHistoryFetchModel(status);
            const watchlistTopup = getWatchlistTopupModel(status);
            const twoFactor = deriveTwoFactorUiState(twoFactorState);
            const startup = normalizeStartupUiState(startupState);
            const startupStrategy = getStartupStrategy(status);
            const computeStartupPreload = getComputeStartupPreload(status, health, summary);
            const autoRestoreGuard = status?.auto_restore_guard || {};
            const sessionAuthenticated = Boolean(status?.session?.authenticated);
            const runtimeService = status?.service_topology?.services?.['ibkr-runtime'] || {};
            const computeService = status?.service_topology?.services?.['ibkr-compute'] || {};
            const backtestService = status?.service_topology?.services?.['ibkr-backtest'] || {};
            const latestBarWrite = latestBar ? String(getComputedTimeLabel(latestBar)).slice(0, 19) : '--';
            const latestIndicatorCalc = latestIndicator ? String(getComputedTimeLabel(latestIndicator)).slice(0, 19) : '--';
            const latestSignalTime = latestSignal ? String((latestSignal.us_time || latestSignal.created || '--')).slice(0, 19) : '--';
            const rows = [
                ['Runtime Service', String(runtimeService.status || '--').toUpperCase()],
                ['Runtime Owner', String(runtimeService.owner || '--')],
                ['Runtime Mode', formatRuntimeModeLabel(runtimeService.runtime_mode)],
                ['Runtime Internal URL', String(runtimeService.internal_url || '--')],
                ['Compute Upstream', String(computeService.upstream || '--')],
                ['Backtest Service', String(backtestService.status || '--').toUpperCase()],
                ['Backtest Worker', String(backtestService.worker_status || backtestService.readiness_phase || '--').toUpperCase()],
                ['Backtest Internal URL', String(backtestService.internal_url || '--')],
                ['Backtest Detail', String(backtestService.detail || backtestService.responsibility || 'replay / backtest worker')],
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
                ['History Active Requests', `${String(status?.data_backfill?.active_requests || 0)} / symbols ${String(status?.data_backfill?.active_symbols_total || 0)}`],
                ['History Trace', historyFetch.trace],
                ['History Workers', `${String(historyFetch.workers)} · spacing ${historyFetch.spacing}`],
                ['History Slowest', historyFetch.slowest],
                ['Canonical Due Bucket', String(canonical?.last_due_bucket_us || '--')],
                ['Canonical Completed Bucket', String(canonical?.last_completed_bucket_us || '--')],
                ['Canonical Lag', formatSecondsLabel(canonical?.lag_s)],
                ['Canonical Trace', String(canonical?.last_trace_id || '--')],
                ['Canonical Fetch Workers', String(canonical?.fetch_workers || 0)],
                ['Canonical Fetch Duration', formatSecondsLabel(canonical?.last_duration_s)],
                ['Canonical Slowest', formatRuntimeSlowStage(canonical?.slowest_stage || {})],
                ['Canonical Written Bars', String(canonical?.last_written_bars || 0)],
                ['Canonical Pending', String(canonical?.pending_symbols_total || 0)],
                ['Canonical Pending Symbols', (Array.isArray(canonical?.pending_symbols) && canonical.pending_symbols.length) ? canonical.pending_symbols.slice(0, 8).join(', ') : '--'],
                ['Watchlist Topup', `${watchlistTopup.label} · ${watchlistTopup.value}`],
                ['Watchlist Topup Detail', watchlistTopup.copy],
                ['Watchlist Topup Budget', watchlistTopup.detail],
                ['Watchlist Attempted', String(status?.watchlist_idle_topup?.last_attempted_symbols_total || 0)],
                ['Watchlist Processed', String(status?.watchlist_idle_topup?.last_processed_symbols_total || 0)],
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
                ['Bar Repair Pending', String(status?.bar_repair_queue?.pending || 0)],
                ['Bar Repair Inflight', String(status?.bar_repair_queue?.inflight || 0)],
                ['Bar Repair Failed', String(status?.bar_repair_queue?.failed || 0)],
                ['Bar Repair Recent', Array.isArray(status?.bar_repair_queue?.recent_jobs) ? status.bar_repair_queue.recent_jobs.slice(0, 3).map((job) => `${job.symbol || '--'} ${job.interval || '--'} ${job.status || '--'}`).join(' | ') || '--' : '--'],
                ['Market Date', String(status?.market_universe?.market_date || '--')],
                ['Last Daily Reset', formatTimeLabel(status?.market_universe?.last_daily_reset)],
                ['Trade Universe Status', formatTradeUniverseStatus(status?.market_universe?.trade_universe_status || status?.runtime?.market_universe?.trade_universe_status)],
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
                    copy: latestSignal ? `${getRecordBarLabel(latestSignal)} · ${formatRuntimeSignalStatus(latestSignal.status)}` : '无新信号',
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
