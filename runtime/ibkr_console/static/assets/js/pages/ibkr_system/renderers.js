function buildStatusCardMarkup(dotClass, mainText, subText = '') {
    const safeMain = String(mainText || '--').trim() || '--';
    const safeSub = String(subText || '').trim();
    return `
        <div class="status-main">
            <span class="status-dot ${dotClass}"></span>
            <span class="status-main-text">${escapeHtml(safeMain)}</span>
        </div>
        <div class="status-sub${safeSub ? '' : ' is-empty'}">${escapeHtml(safeSub || '--')}</div>
    `;
}

function getSchedulerStatusCardModel(summary = {}) {
    const scheduler = getIbkrSchedulerSummary(summary, currentEnvironment);
    const lagLabel = scheduler.dispatchLagLabel && scheduler.dispatchLagLabel !== '--'
        ? `lag ${scheduler.dispatchLagLabel}`
        : '等待 bar cursor';
    if (!scheduler.ok && scheduler.status === 'offline') {
        return {
            dotClass: 'dot-red',
            mainText: 'OFFLINE',
            subText: scheduler.statusCountSummary || 'scheduler status 不可用',
        };
    }
    if (scheduler.status === 'degraded' || scheduler.dispatchLagMin >= 10) {
        return {
            dotClass: 'dot-yellow',
            mainText: 'DEGRADED',
            subText: `${lagLabel} · ${scheduler.statusCountSummary}`,
        };
    }
    if (scheduler.status === 'disabled') {
        return {
            dotClass: 'dot-yellow',
            mainText: 'DISABLED',
            subText: scheduler.statusCountSummary || '调度总开关关闭',
        };
    }
    return {
        dotClass: 'dot-green',
        mainText: String(scheduler.status || 'running').toUpperCase(),
        subText: `${lagLabel} · ${scheduler.statusCountSummary}`,
    };
}

function renderStatus(health) {
    const dataEl = document.getElementById('dataStatus');
    const dataStatusModel = getIbkrDataStatusCardModel(health.ibkr_data || {});
    dataEl.innerHTML = buildStatusCardMarkup(dataStatusModel.dotClass, dataStatusModel.mainText, dataStatusModel.subText);

    const compEl = document.getElementById('computeStatus');
    const computeStatusModel = getIbkrComputeStatusCardModel(health.ibkr_compute || {});
    compEl.innerHTML = buildStatusCardMarkup(computeStatusModel.dotClass, computeStatusModel.mainText, computeStatusModel.subText);

    const runtimeEl = document.getElementById('runtimeStatus');
    const runtimeStatusModel = getIbkrRuntimeStatusCardModel(health.runtime || {});
    runtimeEl.innerHTML = buildStatusCardMarkup(runtimeStatusModel.dotClass, runtimeStatusModel.mainText, runtimeStatusModel.subText);

    const schedulerEl = document.getElementById('schedulerStatus');
    if (schedulerEl) {
        const schedulerStatusModel = getSchedulerStatusCardModel(health.scheduler || {});
        schedulerEl.innerHTML = buildStatusCardMarkup(
            schedulerStatusModel.dotClass,
            schedulerStatusModel.mainText,
            schedulerStatusModel.subText
        );
    }
}

function renderServiceTopology(topologyPayload = {}) {
    const el = document.getElementById('serviceTopologyArea');
    const topology = topologyPayload?.services && typeof topologyPayload.services === 'object'
        ? topologyPayload.services
        : {};
    const services = Object.values(topology);
    const monitorHref = buildPageUrl('/ibkr_monitor.html', {}, { environment: currentEnvironment });
    const screenerHref = buildPageUrl('/ibkr_screener.html', {
        tab: 'screener',
        view: 'current',
    }, { environment: currentEnvironment });
    if (!services.length) {
        el.innerHTML = `
            <div class="ops-summary-shell tone-neutral">
                <div class="ops-summary-lead">
                    <div class="ops-summary-headline">
                        <span class="ops-summary-dot"></span>
                        <div>
                            <div class="ops-summary-kicker">Ops Routing</div>
                            <div class="ops-summary-title">服务拓扑摘要待同步</div>
                            <div class="ops-summary-copy">总览页只保留入口和健康摘要；请求、订阅、主机和 PB 明细请在运维页排查。</div>
                        </div>
                    </div>
                    <div class="ops-summary-actions">
                        <a class="ops-summary-link is-primary" href="${screenerHref}"><span>重选今日标的</span><span aria-hidden="true">→</span></a>
                        <a class="ops-summary-link" href="${monitorHref}"><span>打开运维大盘</span><span aria-hidden="true">→</span></a>
                    </div>
                </div>
                <div class="ops-summary-empty">
                    <div class="ops-summary-label">Snapshot</div>
                    <div class="ops-summary-card-copy">暂无服务拓扑摘要，等待下一次 statusz 同步。</div>
                </div>
            </div>
        `;
        return;
    }

    const normalizedServices = services.map((service) => {
        const rawStatus = String(service?.status || 'unknown').trim().toLowerCase() || 'unknown';
        const status = ['ok', 'ready', 'healthy', 'online', 'peer', 'external'].includes(rawStatus) ? 'running' : rawStatus;
        return {
            status,
            title: String(service?.service_name || service?.kind || '--').trim() || '--',
        };
    });
    const counts = normalizedServices.reduce((acc, service) => {
        acc[service.status] = (acc[service.status] || 0) + 1;
        return acc;
    }, {});
    const issueServices = normalizedServices.filter((service) => !['running', 'unknown'].includes(service.status));
    const unknownCount = counts.unknown || 0;
    const runningCount = counts.running || 0;
    const degradedCount = (counts.degraded || 0) + (counts.warning || 0) + (counts.warn || 0);
    const offlineCount = (counts.offline || 0) + (counts.failed || 0) + (counts.error || 0);
    const attentionCount = issueServices.length + unknownCount;
    const overallTone = offlineCount > 0 ? 'danger' : (degradedCount > 0 || attentionCount > 0 ? 'warn' : 'ok');
    const overallLabel = offlineCount > 0 ? '需要排查' : (degradedCount > 0 || attentionCount > 0 ? '关注' : '正常');
    const countParts = Object.entries(counts)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([status, count]) => `${String(status).toUpperCase()} ${count}`);
    const issueText = issueServices.length
        ? issueServices.slice(0, 3).map((service) => `${service.title} ${service.status.toUpperCase()}`).join(' · ')
        : (unknownCount ? `${unknownCount} 个服务状态未知` : '未发现异常服务状态');

    const cards = [
        {
            label: 'Services',
            value: String(normalizedServices.length),
            copy: countParts.join(' · ') || '--',
            tone: overallTone,
        },
        {
            label: 'Running',
            value: String(runningCount),
            copy: '完整服务拓扑请进运维大盘',
            tone: runningCount === normalizedServices.length ? 'ok' : 'neutral',
        },
        {
            label: 'Needs Attention',
            value: String(attentionCount),
            copy: issueText,
            tone: attentionCount ? 'warn' : 'ok',
        },
    ];

    el.innerHTML = `
        <div class="ops-summary-shell tone-${escapeHtml(overallTone)}">
            <div class="ops-summary-lead">
                <div class="ops-summary-headline">
                    <span class="ops-summary-dot"></span>
                    <div>
                        <div class="ops-summary-kicker">Ops Routing</div>
                        <div class="ops-summary-title">服务状态 ${escapeHtml(overallLabel)}</div>
                        <div class="ops-summary-copy">总览页只看健康摘要；请求、订阅、主机、PB 磁盘与完整 split-stack 详情统一在运维大盘。</div>
                    </div>
                </div>
                <div class="ops-summary-actions">
                    <a class="ops-summary-link is-primary" href="${screenerHref}"><span>重选今日标的</span><span aria-hidden="true">→</span></a>
                    <a class="ops-summary-link" href="${monitorHref}"><span>打开运维大盘</span><span aria-hidden="true">→</span></a>
                </div>
            </div>
            <div class="ops-summary-grid">
                ${cards.map((card) => `
                    <div class="ops-summary-card tone-${escapeHtml(card.tone)}">
                        <div class="ops-summary-label">${escapeHtml(card.label)}</div>
                        <div class="ops-summary-value">${escapeHtml(card.value)}</div>
                        <div class="ops-summary-card-copy">${escapeHtml(card.copy || '--')}</div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function renderTodayStats(today) {
    const hasData = today && typeof today === 'object' && Object.keys(today).length > 0;
    const safeToday = hasData ? today : {};
    const stats = [
        { label: 'ORDERS', value: safeToday.orders },
        { label: 'IBKR BARS', value: safeToday.ibkr_bars },
        { label: 'IBKR IND', value: safeToday.ibkr_indicators },
        { label: 'IBKR SIG', value: safeToday.ibkr_signals },
        { label: 'TARGETS', value: safeToday.ibkr_targets },
        { label: 'EVENTS', value: safeToday.events }
    ];
    document.getElementById('todayStats').innerHTML = stats.map((item) => `
        <div class="stat-mini">
            <div class="stat-mini-label">${escapeHtml(item.label)}</div>
            <div class="stat-mini-value">${escapeHtml(hasData ? (item.value == null ? '-' : String(item.value)) : '-')}</div>
        </div>
    `).join('');
}

function renderFreshness(data) {
    const el = document.getElementById('freshnessArea');
    const byInterval = Array.isArray(data)
        ? data.reduce((acc, item) => {
            if (item && item.interval) acc[item.interval] = item;
            return acc;
        }, {})
        : (data || {});
    if (!byInterval || Object.keys(byInterval).length === 0) {
        el.innerHTML = '<div class="loading-text">暂无数据</div>';
        return;
    }
    const tfs = ['5m', '15m', '30m', '1h', '4h', '1d'];
    const cards = tfs.map((tf) => {
        const item = byInterval[tf];
        if (!item) {
            const freshnessVisual = getIbkrFreshnessVisualState(null);
            return `<div class="freshness-card ${freshnessVisual.chipClass}">
                <div class="freshness-card-head">
                    <span class="freshness-label">${tf}</span>
                    <span class="freshness-chip ${freshnessVisual.chipClass}">${escapeHtml(freshnessVisual.ageLabel)}</span>
                </div>
                <div class="freshness-meta">
                    <div class="freshness-meta-top">
                        <span class="freshness-link" style="color:var(--muted)">--</span>
                        <span class="freshness-state">${freshnessVisual.stateText}</span>
                    </div>
                    <div class="freshness-time">--</div>
                </div>
                <div class="freshness-bar-bg"><div class="freshness-bar-fill" style="width:${freshnessVisual.pct}%;background:${freshnessVisual.color}"></div></div>
            </div>`;
        }
        const age = Math.max(0, Number(item.age_min || 0) || 0);
        const freshnessVisual = getIbkrFreshnessVisualState(age);
        const chartHref = item.symbol
            ? buildPageUrl('/ibkr_chart.html', { symbol: item.symbol, interval: tf }, { environment: currentEnvironment })
            : '';
        const timeLabel = item.last_bar_time_ms
            ? formatTimeLabel(item.last_bar_time_ms)
            : (item.last_bar_time ? formatTimeLabel(item.last_bar_time) : '--');

        return `<div class="freshness-card ${freshnessVisual.chipClass}">
            <div class="freshness-card-head">
                <span class="freshness-label">${tf}</span>
                <span class="freshness-chip ${freshnessVisual.chipClass}">${escapeHtml(freshnessVisual.ageLabel)}</span>
            </div>
            <div class="freshness-meta">
                <div class="freshness-meta-top">
                    ${chartHref ? `<a class="freshness-link" href="${chartHref}">${escapeHtml(item.symbol || '--')}</a>` : '<span class="freshness-link" style="color:var(--muted)">--</span>'}
                    <span class="freshness-state" style="color:${freshnessVisual.color}">${freshnessVisual.stateText}</span>
                </div>
                <div class="freshness-time">${escapeHtml(timeLabel)}</div>
            </div>
            <div class="freshness-bar-bg"><div class="freshness-bar-fill" style="width:${freshnessVisual.pct}%;background:${freshnessVisual.color}"></div></div>
        </div>`;
    });
    el.innerHTML = `<div class="freshness-grid">${cards.join('')}</div>`;
}

function renderEngines(computeData) {
    const el = document.getElementById('engineArea');
    const countEl = document.getElementById('engineCount');
    const preloadStatusLabel = buildStartupPreloadStatusLabel(computeData?.startup_preload);
    const preloadSummary = buildStartupPreloadSummary(computeData?.startup_preload);
    const preloadDetail = buildStartupPreloadDetail(computeData?.startup_preload);
    const environmentReadySummary = buildIbkrEngineEnvironmentReadySummary(computeData?.engines, {
        preferredOrder: [currentEnvironment],
    });

    if (!computeData || !computeData.engines || typeof computeData.engines !== 'object') {
        const messages = ['无引擎数据'];
        if (preloadSummary) messages.push(preloadSummary);
        el.innerHTML = messages.map((item) => `<div class="loading-text">${escapeHtml(item)}</div>`).join('');
        countEl.textContent = preloadStatusLabel || '0 engines';
        return;
    }

    const engines = getSortedEngineEntries(computeData.engines);
    const computeMeta = [];
    if (Number(computeData.last_realtime_elapsed_s || 0) > 0) computeMeta.push(`last ${Number(computeData.last_realtime_elapsed_s || 0).toFixed(2)}s`);
    if (Number(computeData.last_realtime_signals || 0) > 0) computeMeta.push(`sig ${Number(computeData.last_realtime_signals || 0)}`);
    if (Number(computeData.queue_size || 0) > 0) computeMeta.push(`queue ${Number(computeData.queue_size || 0)}`);
    const countParts = [`${computeData.ready_engines || 0}/${computeData.total_engines || engines.length} ready`];
    if (environmentReadySummary) countParts.push(environmentReadySummary);
    if (preloadStatusLabel) countParts.push(preloadStatusLabel);
    if (computeMeta.length) countParts.push(computeMeta.join(' · '));
    else if (!preloadStatusLabel) countParts.push('top 12');
    countEl.textContent = countParts.join(' · ');

    if (!engines.length) {
        const messages = ['无引擎数据'];
        if (preloadSummary) messages.push(preloadSummary);
        if (preloadDetail) messages.push(preloadDetail);
        el.innerHTML = messages.map((item) => `<div class="loading-text">${escapeHtml(item)}</div>`).join('');
        return;
    }

    let html = '<div class="engine-summary">总览页只保留最关键的 12 条引擎概况；完整排查与动作控制请切到控制台。</div>';
    if (environmentReadySummary) {
        html += `<div class="engine-summary">环境 ready：${escapeHtml(environmentReadySummary)}</div>`;
    }
    if (preloadSummary) {
        html += `<div class="engine-summary">${escapeHtml(preloadSummary)}</div>`;
    }
    if (preloadDetail) {
        html += `<div class="engine-summary">${escapeHtml(preloadDetail)}</div>`;
    }
    html += '<div class="engine-grid">';
    engines.slice(0, 12).forEach(([key, engine]) => {
        const model = getIbkrEngineViewModel(key, engine, currentEnvironment);
        html += `<div class="engine-card">
            <div class="engine-card-head">
                <div class="engine-card-title">
                    <div class="engine-card-name">${escapeHtml(model.displayName)}</div>
                    <div class="engine-card-sub">${escapeHtml(model.subtitle)}</div>
                </div>
                <span class="engine-state ${model.ready ? 'ready' : 'warming'}">${escapeHtml(model.readyLabel)}</span>
            </div>
            <div class="engine-meta-grid">
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Bars</span>
                    <span class="engine-meta-value">${escapeHtml(String(model.barCount))}</span>
                </div>
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Last Close</span>
                    <span class="engine-meta-value">${escapeHtml(model.lastCloseLabel)}</span>
                </div>
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Last Bar</span>
                    <span class="engine-meta-value">${escapeHtml(model.lastBarLabel)}</span>
                </div>
                <div class="engine-meta-item">
                    <span class="engine-meta-label">Chart</span>
                    <span class="engine-meta-value">${model.chartHref ? `<a class="engine-link" href="${model.chartHref}">打开图表</a>` : '--'}</span>
                </div>
            </div>
        </div>`;
    });
    html += '</div>';
    el.innerHTML = html;
}

function renderSchedulerOverview(cronPayload = {}, summary = {}) {
    const el = document.getElementById('schedulerArea');
    if (!el) return;
    const definitions = Array.isArray(cronPayload?.items) ? cronPayload.items : [];
    const scheduler = getIbkrSchedulerSummary(cronPayload?.scheduler || {}, currentEnvironment);
    const configMap = buildIbkrConfigMap(summary);
    const cards = definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentEnvironment));

    const summaryCards = [
        {
            label: 'Scheduler State',
            value: String(scheduler.status || '--').toUpperCase(),
            copy: `loop ${scheduler.loopIntervalLabel} · ${scheduler.environmentLabel}`,
            tone: scheduler.tone,
        },
        {
            label: 'Dispatch Lag',
            value: scheduler.dispatchLagLabel,
            copy: `persisted ${scheduler.latestIngestedBarLabel} · dispatched ${scheduler.latestDispatchedBarLabel}`,
            tone: scheduler.dispatchLagMin >= 10 ? 'error' : (scheduler.latestIngestedBarTimeMs ? 'ok' : 'warn'),
        },
        {
            label: 'Job Coverage',
            value: `${scheduler.enabledJobCount || 0}/${scheduler.jobCount || 0}`,
            copy: `native ${scheduler.nativeJobCount || 0} · compat ${scheduler.compatibilityJobCount || 0}`,
            tone: scheduler.compatibilityJobCount > 0 ? 'warn' : 'ok',
        },
        {
            label: 'Last Dispatch',
            value: scheduler.lastDispatchLabel,
            copy: scheduler.statusCountSummary,
            tone: scheduler.ok ? 'ok' : 'error',
        },
    ];

    const toneClass = (tone) => {
        if (tone === 'error') return 'is-danger';
        if (tone === 'warn') return 'is-warn';
        if (tone === 'ok') return 'is-ok';
        return '';
    };

    const cronMarkup = cards.length
        ? `
            <div class="cron-grid">
                ${cards.map((card) => `
                    <div class="cron-card">
                        <div class="cron-card-head">
                            <div>
                                <div class="cron-card-title">${escapeHtml(card.title)}</div>
                                <div class="cron-card-key">${escapeHtml(card.configKey)}</div>
                            </div>
                            <span class="cron-state ${card.effectiveEnabled ? 'on' : 'off'}">${escapeHtml(card.statusLabel)}</span>
                        </div>
                        <div class="cron-copy">${escapeHtml(card.functionSummary)}</div>
                        <div class="cron-meta">
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">执行器</span>
                                <span class="cron-meta-value">${escapeHtml(card.modeLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">最近完成</span>
                                <span class="cron-meta-value">${escapeHtml(card.lastRunFinishedLabel)}</span>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">最近成功</span>
                                <span class="cron-meta-value">${escapeHtml(card.lastSuccessLabel)}</span>
                            </div>
                            <div class="cron-meta-row cron-meta-row-wide">
                                <span class="cron-meta-label">窗口</span>
                                <span class="cron-meta-value">${escapeHtml(card.windowLabel)}</span>
                            </div>
                            <div class="cron-meta-row cron-meta-row-wide">
                                <span class="cron-meta-label">周期</span>
                                <span class="cron-meta-value">${escapeHtml(card.primaryCycleLabel)}</span>
                            </div>
                            <div class="cron-meta-row cron-meta-row-wide">
                                <span class="cron-meta-label">结果</span>
                                <span class="cron-meta-value">${escapeHtml(card.lastError || card.resultReason || '--')}</span>
                            </div>
                        </div>
                        <div class="cron-tags">
                            <span class="cron-tag ${toneClass(card.tone)}">${escapeHtml(card.modeLabel)}</span>
                            <span class="cron-tag">GLOBAL ${card.schedulerEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">JOB ${card.cronEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">${escapeHtml(card.environmentLabel)}</span>
                        </div>
                    </div>
                `).join('')}
            </div>
        `
        : '<div class="loading-text" style="padding:10px 0 0">暂无 IBKR Scheduler job 定义</div>';

    el.innerHTML = `
        <div class="scheduler-shell">
            <div class="scheduler-summary-grid">
                ${summaryCards.map((card) => `
                    <div class="scheduler-summary-card">
                        <div class="scheduler-summary-label">${escapeHtml(card.label)}</div>
                        <div class="scheduler-summary-value">${escapeHtml(card.value || '--')}</div>
                        <div class="scheduler-summary-copy ${card.tone === 'error' ? 'is-danger' : ''}">${escapeHtml(card.copy || '--')}</div>
                    </div>
                `).join('')}
            </div>
            <div class="scheduler-inline-meta">
                <span class="scheduler-meta-tag"><strong>Global</strong>${configMap?.pb_scheduler_enabled ?? '--'}</span>
                <span class="scheduler-meta-tag"><strong>Cursor</strong>${escapeHtml(scheduler.latestIngestedBarLabel)} -> ${escapeHtml(scheduler.latestDispatchedBarLabel)}</span>
                <span class="scheduler-meta-tag"><strong>Dispatch</strong>${escapeHtml(scheduler.lastDispatchLabel)}</span>
            </div>
            ${cronMarkup}
        </div>
    `;
}

function renderCronSummary(definitions, configMap) {
    const cronCards = Array.isArray(definitions)
        ? definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentEnvironment))
        : [];
    if (!cronCards.length) {
        return '<div class="loading-text" style="padding:10px 0 0">暂无 IBKR Scheduler job 定义</div>';
    }
    return `
        <div class="cron-grid">
            ${cronCards.map((card) => {
                return `
                    <div class="cron-card">
                        <div class="cron-card-head">
                            <div>
                                <div class="cron-card-title">${escapeHtml(card.title)}</div>
                                <div class="cron-card-key">${escapeHtml(card.configKey)}</div>
                            </div>
                            <span class="cron-state ${card.effectiveEnabled ? 'on' : 'off'}">${card.effectiveEnabled ? 'ENABLED' : 'DISABLED'}</span>
                        </div>
                        <div class="cron-copy">${escapeHtml(card.functionSummary)}</div>
                        <div class="cron-meta">
                            <div class="cron-meta-row cron-meta-row-wide">
                                <span class="cron-meta-label">时区</span>
                                <div class="cron-meta-value">
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
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">Cron</span>
                                <div class="cron-meta-value">
                                    <details class="cron-exp-details">
                                        <summary class="cron-exp-summary">查看表达式</summary>
                                        <div class="cron-exp-text">${escapeHtml(card.cronExpr)}</div>
                                    </details>
                                </div>
                            </div>
                            <div class="cron-meta-row">
                                <span class="cron-meta-label">执行器</span>
                                <span class="cron-meta-value">${escapeHtml(card.modeLabel)}</span>
                            </div>
                        </div>
                        <div class="cron-tags">
                            <span class="cron-tag ${card.tone === 'error' ? 'is-danger' : (card.tone === 'warn' ? 'is-warn' : 'is-ok')}">${escapeHtml(card.statusLabel)}</span>
                            <span class="cron-tag">SCHED ${card.schedulerEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">JOB ${card.cronEnabled ? 'ON' : 'OFF'}</span>
                            <span class="cron-tag">${escapeHtml(card.environmentLabel)}</span>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderConfig(summary, cronDefinitions) {
    const el = document.getElementById('configArea');
    const configMap = buildIbkrConfigMap(summary);
    const primaryItems = getIbkrSystemPrimaryConfigItems(summary, configMap);
    const secondaryItems = getIbkrSystemSecondaryConfigItems(configMap);

    const renderConfigGrid = (items, className = '') => `<div class="config-grid ${className}">${items.map((item) => {
        const displayState = getIbkrConfigDisplayState(item.value);
        let valClass = 'val-neutral';
        if (displayState.tone === 'on') valClass = 'val-on';
        else if (displayState.tone === 'off') valClass = 'val-off';
        return `<div class="config-item">
                <span class="config-key">${escapeHtml(item.label)}</span>
                <span class="config-val ${valClass}">${escapeHtml(displayState.display)}</span>
            </div>`;
    }).join('')}</div>`;
    const primaryGrid = renderConfigGrid(primaryItems, 'config-grid-primary');
    const secondaryGrid = secondaryItems.length
        ? `<details class="config-more-details">
            <summary class="config-more-summary">
                <span>更多配置 ${secondaryItems.length} 项</span>
                <span class="config-more-copy">展开查看长尾参数</span>
            </summary>
            <div class="config-more-body">
                ${renderConfigGrid(secondaryItems, 'config-grid-secondary')}
            </div>
        </details>`
        : '';

    el.innerHTML = `
        <div class="config-stack">
            <div class="config-block-label">关键配置</div>
            ${primaryGrid}
            ${secondaryGrid}
            <div class="config-divider"></div>
            <div class="config-block-label">IBKR Scheduler 摘要</div>
            ${renderCronSummary(cronDefinitions, configMap)}
        </div>
    `;
}

function renderEvents(events) {
    const el = document.getElementById('eventArea');
    const countEl = document.getElementById('eventCount');

    if (!events || events.length === 0) {
        el.innerHTML = '<div class="loading-text">暂无事件</div>';
        countEl.textContent = '0';
        return;
    }

    countEl.textContent = String(events.length);
    const levelColors = { info: '#64748b', warning: '#eab308', error: '#ef4444' };
    const sourceLabels = { ibkr_compute: 'COMPUTE', pb: 'PB', manual: 'MANUAL', tradingview: 'TV', ibkr: 'IBKR' };

    el.innerHTML = `<div class="event-list">${events.map((event) => {
        const dotColor = levelColors[event.level] || '#64748b';
        const source = sourceLabels[event.source] || event.source || '--';
        const time = event.us_time ? String(event.us_time).slice(11, 16) : '--';
        return `<div class="event-item">
            <div class="event-dot" style="background:${dotColor}"></div>
            <div class="event-content">
                <div class="event-title">${escapeHtml(event.title || '--')}</div>
                <div class="event-meta">${escapeHtml(time)} · ${escapeHtml(source)} · ${escapeHtml(event.event_type || '')}</div>
            </div>
        </div>`;
    }).join('')}</div>`;
}
