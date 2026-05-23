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

function formatFreshnessPercent(value) {
    if (value === null || value === undefined || value === '') return '--';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '--';
    const clamped = Math.max(0, Math.min(100, numeric));
    return `${clamped % 1 === 0 ? clamped.toFixed(0) : clamped.toFixed(1)}%`;
}

function getSystemIbkrDataStatusCardModel(dataHealth = {}) {
    if (!dataHealth?.freshness_aggregate) {
        return getIbkrDataStatusCardModel(dataHealth || {});
    }

    let dotClass = 'dot-gray';
    let mainText = '数据状态';
    if (dataHealth.status === 'online') {
        dotClass = 'dot-green';
        mainText = '在线';
    } else if (dataHealth.status === 'delayed') {
        dotClass = 'dot-yellow';
        mainText = '延迟';
    } else if (dataHealth.status === 'offline') {
        dotClass = 'dot-red';
        mainText = '超时';
    } else if (dataHealth.status === 'loading') {
        mainText = '加载中';
    }

    const readyPct = formatFreshnessPercent(dataHealth.ready_pct);
    if (readyPct !== '--') mainText += ` · ${readyPct}`;

    const metaParts = [];
    const due = Number(dataHealth.due_checks ?? dataHealth.due_symbols ?? 0) || 0;
    const total = Number(dataHealth.total_checks ?? dataHealth.total_symbols ?? 0) || 0;
    if (total || due) metaParts.push(`due ${due}/${total || '--'}`);
    const counts = [];
    if (Number(dataHealth.ready || 0) > 0) counts.push(`ready ${Number(dataHealth.ready || 0)}`);
    if (Number(dataHealth.overdue || 0) > 0) counts.push(`overdue ${Number(dataHealth.overdue || 0)}`);
    if (Number(dataHealth.missing || 0) > 0) counts.push(`missing ${Number(dataHealth.missing || 0)}`);
    if (Number(dataHealth.waiting_5m || 0) > 0) counts.push(`waiting5m ${Number(dataHealth.waiting_5m || 0)}`);
    if (Number(dataHealth.not_due || 0) > 0) counts.push(`not due ${Number(dataHealth.not_due || 0)}`);
    if (Number(dataHealth.quiet_extended || 0) > 0) counts.push(`quiet ${Number(dataHealth.quiet_extended || 0)}`);
    if (counts.length) metaParts.push(counts.join(' · '));
    const coveragePct = formatFreshnessPercent(dataHealth.coverage_pct);
    if (coveragePct !== '--') metaParts.push(`coverage ${coveragePct}`);
    const expectedLabel = dataHealth.expected_close_us
        || (dataHealth.expected_close_ms ? formatTimeLabel(dataHealth.expected_close_ms) : '');
    if (expectedLabel) metaParts.push(`expected ${expectedLabel}`);
    if (Array.isArray(dataHealth.sample_lag_symbols) && dataHealth.sample_lag_symbols.length) {
        metaParts.push(`samples ${formatSymbolPreview(dataHealth.sample_lag_symbols, 4)}`);
    }

    return { dotClass, mainText, subText: metaParts.join(' · ') };
}

function getSchedulerStatusCardModel(summary = {}) {
    const scheduler = getIbkrSchedulerSummary(summary, currentEnvironment);
    const lagLabel = scheduler.dispatchLagLabel && scheduler.dispatchLagLabel !== '--'
        ? `lag ${scheduler.dispatchLagLabel}`
        : '等待 bar cursor';
    const cacheLabel = scheduler.stale ? `缓存 ${scheduler.staleAgeLabel}` : '';
    if (!scheduler.ok && scheduler.status === 'offline') {
        return {
            dotClass: 'dot-red',
            mainText: 'OFFLINE',
            subText: scheduler.statusCountSummary || 'scheduler status 不可用',
        };
    }
    if (!scheduler.ok && scheduler.status === 'unknown') {
        return {
            dotClass: 'dot-yellow',
            mainText: 'UNKNOWN',
            subText: scheduler.metaError || scheduler.statusCountSummary || 'scheduler status 同步中',
        };
    }
    if (scheduler.stale) {
        return {
            dotClass: 'dot-yellow',
            mainText: `${String(scheduler.status || 'running').toUpperCase()} CACHE`,
            subText: [cacheLabel, lagLabel, scheduler.statusCountSummary].filter(Boolean).join(' · '),
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
    const dataStatusModel = getSystemIbkrDataStatusCardModel(health.ibkr_data || {});
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
    const serviceEntries = Object.entries(topology);
    const monitorHref = buildPageUrl('/ibkr_monitor.html', {}, { environment: currentEnvironment });
    const screenerHref = buildPageUrl('/ibkr_screener.html', {
        tab: 'screener',
        view: 'current',
    }, { environment: currentEnvironment });
    if (!serviceEntries.length) {
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

    const serviceOrder = ['ibkr-console', 'ibkr-api', 'ibkr-scheduler', 'ibkr-compute', 'ibkr-backtest', 'ibkr-runtime', 'ibkr-gateway', 'pocketbase'];
    const serviceSemantics = {
        'ibkr-console': 'console UI',
        'ibkr-api': 'API / control plane',
        'ibkr-scheduler': 'cron dispatch',
        'ibkr-compute': 'indicators / signals / data quality',
        'ibkr-backtest': 'replay / backtest worker',
        'ibkr-runtime': 'broker session / live bars',
        'ibkr-gateway': 'IB Gateway session',
        pocketbase: 'state / config store',
    };
    const orderedEntries = serviceEntries.sort(([left], [right]) => {
        const leftIndex = serviceOrder.indexOf(left);
        const rightIndex = serviceOrder.indexOf(right);
        const leftWeight = leftIndex >= 0 ? leftIndex : 999;
        const rightWeight = rightIndex >= 0 ? rightIndex : 999;
        if (leftWeight !== rightWeight) return leftWeight - rightWeight;
        return left.localeCompare(right);
    });
    const normalizedServices = orderedEntries.map(([key, service]) => {
        const rawStatus = String(service?.status || 'unknown').trim().toLowerCase() || 'unknown';
        const workerStatus = String(service?.worker_status || service?.readiness_phase || '').trim().toLowerCase();
        const backtestIdle = key === 'ibkr-backtest'
            && !['degraded', 'warning', 'warn', 'error', 'offline', 'failed', 'stopped'].includes(rawStatus)
            && (rawStatus === 'idle' || workerStatus === 'idle');
        const status = ['ok', 'ready', 'healthy', 'online', 'peer', 'external'].includes(rawStatus)
            ? 'running'
            : (backtestIdle ? 'idle' : rawStatus);
        const semantic = serviceSemantics[key] || String(service?.kind || service?.fault_domain || 'service').trim();
        const detail = String(service?.detail || service?.responsibility || semantic || '').trim();
        const operational = status === 'running' || backtestIdle;
        const tone = backtestIdle ? 'neutral' : (operational ? 'ok' : (['offline', 'failed', 'error'].includes(status) ? 'danger' : (status === 'unknown' ? 'neutral' : 'warn')));
        return {
            key,
            status,
            workerStatus,
            readinessPhase: String(service?.readiness_phase || '').trim().toLowerCase(),
            clientId: Number(service?.ib_gateway_client_id || 0) || 0,
            semantic,
            detail,
            operational,
            tone,
            title: String(service?.service_name || key || service?.kind || '--').trim() || '--',
        };
    });
    const counts = normalizedServices.reduce((acc, service) => {
        acc[service.status] = (acc[service.status] || 0) + 1;
        return acc;
    }, {});
    const issueServices = normalizedServices.filter((service) => !service.operational && service.status !== 'unknown');
    const unknownCount = counts.unknown || 0;
    const operationalCount = normalizedServices.filter((service) => service.operational).length;
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
            label: 'Operational',
            value: String(operationalCount),
            copy: '完整服务拓扑请进运维大盘',
            tone: operationalCount === normalizedServices.length ? 'ok' : 'neutral',
        },
        {
            label: 'Needs Attention',
            value: String(attentionCount),
            copy: issueText,
            tone: attentionCount ? 'warn' : 'ok',
        },
    ];
    const clientServiceKeys = new Set(['ibkr-runtime', 'ibkr-compute', 'ibkr-api', 'ibkr-scheduler', 'ibkr-backtest']);
    const clientEntries = normalizedServices
        .filter((service) => service.clientId && clientServiceKeys.has(service.key))
        .map((service) => `${service.title.replace(/^ibkr-/, '')} ${service.clientId}`);
    if (clientEntries.length) {
        cards.push({
            label: 'IB Clients',
            value: String(clientEntries.length),
            copy: clientEntries.join(' · '),
            tone: 'neutral',
        });
    }
    const backtestService = normalizedServices.find((service) => service.key === 'ibkr-backtest');
    if (backtestService) {
        const workerLabel = String(backtestService.workerStatus || backtestService.readinessPhase || backtestService.status || '--').toUpperCase();
        const backtestCopy = [
            backtestService.semantic,
            backtestService.clientId ? `IB client ${backtestService.clientId}` : '',
            backtestService.detail || '仅表示服务/worker 状态',
            '不等同最近回测统计',
        ].filter(Boolean).join(' · ');
        cards.push({
            label: 'Backtest Service',
            value: workerLabel,
            copy: backtestCopy,
            tone: backtestService.tone,
        });
    }

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

function getFreshnessAggregateVisual(item = {}) {
    const status = String(item?.status || '').trim().toLowerCase();
    const hasReadyPct = item?.ready_pct !== null && item?.ready_pct !== undefined && item?.ready_pct !== '';
    const readyPct = hasReadyPct ? Number(item.ready_pct) : NaN;
    const overdue = Number(item?.overdue || 0) || 0;
    const missing = Number(item?.missing || 0) || 0;
    const waiting5m = Number(item?.waiting_5m || 0) || 0;
    const { due, total } = getFreshnessDueTotal(item);
    const notDue = Number(item?.not_due || 0) || 0;
    const quiet = Number(item?.quiet_extended || 0) || 0;
    const pct = Number.isFinite(readyPct) ? Math.max(0, Math.min(100, readyPct)) : 0;

    if (!item || (!total && !due && !status && !Number.isFinite(readyPct))) {
        return { color: '#64748b', pct: 0, chipClass: 'is-empty', stateText: '暂无', chipText: '--' };
    }
    if (status === 'not_due' || (total > 0 && notDue >= total && due === 0)) {
        return { color: '#38bdf8', pct: 0, chipClass: 'is-idle', stateText: '未到周期', chipText: 'NOT DUE' };
    }
    if (status === 'quiet_extended' || (total > 0 && quiet >= total && due === 0)) {
        return { color: '#94a3b8', pct: 0, chipClass: 'is-idle', stateText: '扩展静默', chipText: 'QUIET' };
    }
    if (status === 'waiting_5m' || (waiting5m > 0 && due === 0)) {
        return { color: '#38bdf8', pct, chipClass: 'is-idle', stateText: '等待 5m', chipText: 'WAIT 5M' };
    }
    if (overdue > 0 || missing > 0 || ['stale', 'overdue', 'missing', 'offline', 'error', 'critical', 'rollup_lag'].includes(status)) {
        return { color: '#ef4444', pct, chipClass: 'is-stale', stateText: '超时', chipText: formatFreshnessPercent(readyPct) };
    }
    if (['warn', 'warning', 'delayed', 'degraded', 'partial'].includes(status) || (Number.isFinite(readyPct) && readyPct < 95)) {
        return { color: '#f59e0b', pct, chipClass: 'is-warn', stateText: '部分延迟', chipText: formatFreshnessPercent(readyPct) };
    }
    return { color: '#22c55e', pct: Number.isFinite(readyPct) ? pct : 100, chipClass: 'is-fresh', stateText: '正常', chipText: formatFreshnessPercent(readyPct) };
}

function getFreshnessDueTotal(item = {}) {
    const totalChecks = Number(item?.total_checks || 0) || 0;
    const dueChecks = Number(item?.due_checks || 0) || 0;
    if (totalChecks > 0) {
        return { due: dueChecks, total: totalChecks };
    }
    return {
        due: Number(item?.due_symbols || 0) || 0,
        total: Number(item?.total_symbols || 0) || 0,
    };
}

function isFreshnessIdleAggregate(item = {}) {
    const status = String(item?.status || '').trim().toLowerCase();
    if (status === 'not_due' || status === 'quiet_extended') return true;

    const { due, total } = getFreshnessDueTotal(item);
    const notDue = Number(item?.not_due || 0) || 0;
    const quiet = Number(item?.quiet_extended || 0) || 0;
    return total > 0 && due === 0 && (notDue >= total || quiet >= total || notDue + quiet >= total);
}

function formatFreshnessReadyPercent(item = {}) {
    return isFreshnessIdleAggregate(item) ? '--' : formatFreshnessPercent(item?.ready_pct);
}

function getFreshnessExpectedLabel(item = {}) {
    if (item?.expected_close_us) return String(item.expected_close_us);
    if (item?.expected_close_ms) return formatTimeLabel(item.expected_close_ms);
    return '--';
}

function renderFreshnessReasonPills(item = {}) {
    const reasons = [
        ['due', `${Number(item.due_symbols || 0) || 0}/${Number(item.total_symbols || 0) || 0}`],
        ['ready', Number(item.ready || 0) || 0],
        ['overdue', Number(item.overdue || 0) || 0],
        ['missing', Number(item.missing || 0) || 0],
        ['waiting5m', Number(item.waiting_5m || 0) || 0],
        ['not due', Number(item.not_due || 0) || 0],
        ['quiet', Number(item.quiet_extended || 0) || 0],
    ];
    return reasons
        .filter(([, value], index) => index < 2 || Number(value || 0) > 0)
        .map(([label, value]) => `<span class="freshness-reason"><strong>${escapeHtml(label)}</strong>${escapeHtml(String(value))}</span>`)
        .join('');
}

function renderFreshnessScopeCards(scopes = {}) {
    const scopeOrder = [
        'realtime_5m',
        'active_higher_timeframes',
        'watchlist_higher_timeframes',
        'daily_1d',
    ];
    const cards = scopeOrder
        .map((name) => scopes?.[name])
        .filter((scope) => scope && scope.overall)
        .map((scope) => {
            const overall = scope.overall || {};
            const visual = getFreshnessAggregateVisual(overall);
            const label = scope.label || scope.name || 'Freshness Scope';
            const due = Number(overall.due_checks ?? overall.due_symbols ?? 0) || 0;
            const total = Number(overall.total_checks ?? overall.total_symbols ?? 0) || 0;
            const subtitle = [
                scope.best_effort ? 'best-effort' : (scope.critical ? 'critical' : 'monitor'),
                `due ${due}/${total}`,
                `ready ${Number(overall.ready || 0) || 0}`,
                Number(overall.overdue || 0) > 0 ? `overdue ${Number(overall.overdue || 0)}` : '',
                Number(overall.missing || 0) > 0 ? `missing ${Number(overall.missing || 0)}` : '',
                Number(scope.symbols_total || overall.total_symbols || 0) > 0 ? `symbols ${Number(scope.symbols_total || overall.total_symbols || 0)}` : '',
            ].filter(Boolean).join(' · ');
            return `<div class="freshness-card ${visual.chipClass}">
                <div class="freshness-card-head">
                    <span class="freshness-label">${escapeHtml(label)}</span>
                    <span class="freshness-chip ${visual.chipClass}">${escapeHtml(visual.chipText)}</span>
                </div>
                <div class="freshness-meta">
                    <div class="freshness-meta-top">
                        <span class="freshness-percent">${escapeHtml(formatFreshnessReadyPercent(overall))}</span>
                        <span class="freshness-state" style="color:${visual.color}">${escapeHtml(visual.stateText)}</span>
                    </div>
                    <div class="freshness-time">${escapeHtml(subtitle || '--')}</div>
                </div>
                <div class="freshness-bar-bg"><div class="freshness-bar-fill" style="width:${visual.pct}%;background:${visual.color}"></div></div>
                <div class="freshness-reasons">${renderFreshnessReasonPills(overall)}</div>
            </div>`;
        });
    return cards.length ? `<div class="freshness-grid">${cards.join('')}</div>` : '';
}

function renderLegacyFreshness(data) {
    const byInterval = Array.isArray(data)
        ? data.reduce((acc, item) => {
            if (item && item.interval) acc[normalizeIbkrInterval(item.interval, item.interval)] = item;
            return acc;
        }, {})
        : normalizeFreshnessItems(data).reduce((acc, item) => {
            if (item && item.interval) acc[item.interval] = item;
            return acc;
        }, {});
    if (!byInterval || Object.keys(byInterval).length === 0) {
        return '<div class="loading-text">暂无数据</div>';
    }

    const cards = IBKR_FRESHNESS_INTERVALS.map((tf) => {
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
    return `<div class="freshness-grid">${cards.join('')}</div>`;
}

function renderFreshness(data) {
    const el = document.getElementById('freshnessArea');
    const normalized = normalizeFreshnessPayload(data);
    if (!normalized.aggregate) {
        el.innerHTML = renderLegacyFreshness(data);
        return;
    }

    const byInterval = normalized.intervals.reduce((acc, item) => {
        if (item && item.interval) acc[item.interval] = item;
        return acc;
    }, {});
    const scopes = normalized.scopes || {};
    const scopeCards = renderFreshnessScopeCards(scopes);
    const overall = normalized.overall || {};
    const overallVisual = getFreshnessAggregateVisual(overall);
    const cards = IBKR_FRESHNESS_INTERVALS.map((tf) => {
        const item = byInterval[tf] || normalizeFreshnessIntervalItem({ interval: tf }, tf);
        const visual = getFreshnessAggregateVisual(item);
        const expectedLabel = getFreshnessExpectedLabel(item);
        const coverageLabel = formatFreshnessPercent(item.coverage_pct);
        const sampleText = Array.isArray(item.sample_lag_symbols) && item.sample_lag_symbols.length
            ? formatSymbolPreview(item.sample_lag_symbols, 5)
            : '';

        return `<div class="freshness-card ${visual.chipClass}">
            <div class="freshness-card-head">
                <span class="freshness-label">${escapeHtml(tf)}</span>
                <span class="freshness-chip ${visual.chipClass}">${escapeHtml(visual.chipText)}</span>
            </div>
            <div class="freshness-meta">
                <div class="freshness-meta-top">
                    <span class="freshness-percent">${escapeHtml(formatFreshnessReadyPercent(item))}</span>
                    <span class="freshness-state" style="color:${visual.color}">${escapeHtml(visual.stateText)}</span>
                </div>
                <div class="freshness-time freshness-expected">应更新 ${escapeHtml(expectedLabel)}</div>
                <div class="freshness-time">覆盖 ${escapeHtml(coverageLabel)}</div>
            </div>
            <div class="freshness-bar-bg"><div class="freshness-bar-fill" style="width:${visual.pct}%;background:${visual.color}"></div></div>
            <div class="freshness-reasons">${renderFreshnessReasonPills(item)}</div>
            ${sampleText ? `<div class="freshness-samples">异常样例 ${escapeHtml(sampleText)}</div>` : ''}
        </div>`;
    });

    const overallDue = Number(overall.due_checks ?? overall.due_symbols ?? 0) || 0;
    const overallTotal = Number(overall.total_checks ?? overall.total_symbols ?? 0) || 0;
    const overallMeta = [
        `due ${overallDue}/${overallTotal}`,
        `ready ${Number(overall.ready || 0) || 0}`,
        `overdue ${Number(overall.overdue || 0) || 0}`,
        `missing ${Number(overall.missing || 0) || 0}`,
        Number(overall.waiting_5m || 0) > 0 ? `waiting5m ${Number(overall.waiting_5m || 0)}` : '',
        Number(overall.not_due || 0) > 0 ? `not due ${Number(overall.not_due || 0)}` : '',
        Number(overall.quiet_extended || 0) > 0 ? `quiet ${Number(overall.quiet_extended || 0)}` : '',
    ].filter(Boolean);

    el.innerHTML = `<div class="freshness-overall ${overallVisual.chipClass}">
            <div>
                <div class="freshness-overall-kicker">Realtime Freshness</div>
                <div class="freshness-overall-title">
                    <span>${escapeHtml(formatFreshnessReadyPercent(overall))}</span>
                    <span class="freshness-state" style="color:${overallVisual.color}">${escapeHtml(overallVisual.stateText)}</span>
                </div>
            </div>
            <div class="freshness-overall-meta">
                <span>coverage ${escapeHtml(formatFreshnessPercent(overall.coverage_pct))}</span>
                ${overallMeta.map((item) => `<span>${escapeHtml(item)}</span>`).join('')}
            </div>
        </div>
        ${scopeCards}
        <div class="freshness-grid">${cards.join('')}</div>`;
}

function formatStorageBytes(value) {
    const numeric = Number(value || 0);
    if (!Number.isFinite(numeric) || numeric < 0) return '--';
    if (numeric === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let next = numeric;
    let index = 0;
    while (next >= 1024 && index < units.length - 1) {
        next /= 1024;
        index += 1;
    }
    const precision = next >= 100 ? 0 : (next >= 10 ? 1 : 2);
    return `${next.toFixed(precision)} ${units[index]}`;
}

function formatStorageRows(value, source = '') {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '--';
    const label = numeric >= 1000000
        ? `${(numeric / 1000000).toFixed(numeric >= 10000000 ? 1 : 2)}M`
        : numeric >= 1000
            ? `${(numeric / 1000).toFixed(numeric >= 100000 ? 0 : 1)}K`
            : String(numeric);
    return source === 'estimated' ? `~${label}` : label;
}

function getStorageTone(status) {
    const normalized = String(status || '').trim().toLowerCase();
    if (['error', 'unhealthy', 'offline', 'unavailable'].includes(normalized)) return 'danger';
    if (['warning', 'warn', 'degraded', 'partial'].includes(normalized)) return 'warn';
    if (['ok', 'healthy', 'running'].includes(normalized)) return 'ok';
    return 'neutral';
}

function getStorageStatusLabel(status) {
    const normalized = String(status || '').trim().toLowerCase();
    if (normalized === 'ok') return 'OK';
    if (normalized === 'warning') return 'WARN';
    if (normalized === 'error') return 'ERROR';
    if (normalized === 'unavailable') return 'N/A';
    return normalized ? normalized.toUpperCase() : '--';
}

function renderStorageHealth(payload = {}) {
    const el = document.getElementById('storageHealthArea');
    const metaEl = document.getElementById('storageHealthMeta');
    if (!el) return;
    const health = payload && typeof payload === 'object' ? payload : {};
    const status = String(health.status || '').trim().toLowerCase();
    if (!status) {
        el.innerHTML = '<div class="loading-text">暂无 Storage health 数据</div>';
        if (metaEl) metaEl.textContent = '--';
        return;
    }

    const db = health.db_files && typeof health.db_files === 'object' ? health.db_files : {};
    const summary = health.summary && typeof health.summary === 'object' ? health.summary : {};
    const flags = Array.isArray(health.flags) ? health.flags : [];
    const tables = Array.isArray(health.tables) ? health.tables : [];
    const groups = health.groups && typeof health.groups === 'object' ? health.groups : {};
    const tone = getStorageTone(status);
    const statusCounts = summary.table_status_counts && typeof summary.table_status_counts === 'object'
        ? summary.table_status_counts
        : {};
    const existingTables = Number(summary.existing_tables || 0) || tables.filter((item) => item?.exists).length;
    const monitoredTables = Number(summary.monitored_tables || 0) || tables.length;
    const estimatedRows = Number(summary.estimated_rows || 0) || 0;
    const diskFreePct = Number(db.disk_free_pct);
    const walPct = Number(db.wal_to_db_pct || 0) || 0;

    if (metaEl) {
        const collected = String(health.collected_at || '').replace('T', ' ').replace('Z', '');
        metaEl.textContent = `${getStorageStatusLabel(status)} · ${collected || '--'}${health.cached ? ' · cached' : ''}`;
    }

    const summaryCards = [
        {
            label: 'DB',
            value: db.db_size_label || formatStorageBytes(db.db_size_bytes),
            copy: `pb_data ${db.data_size_label || formatStorageBytes(db.data_size_bytes)}`,
            tone: 'neutral',
        },
        {
            label: 'WAL',
            value: db.wal_size_label || formatStorageBytes(db.wal_size_bytes),
            copy: `${Number.isFinite(walPct) ? walPct.toFixed(1) : '--'}% of DB`,
            tone: walPct >= 30 ? 'warn' : 'ok',
        },
        {
            label: 'Disk Free',
            value: Number.isFinite(diskFreePct) ? `${diskFreePct.toFixed(1)}%` : '--',
            copy: db.disk_free_label || formatStorageBytes(db.disk_free_bytes),
            tone: Number.isFinite(diskFreePct) && diskFreePct < 8 ? 'danger' : (Number.isFinite(diskFreePct) && diskFreePct < 15 ? 'warn' : 'ok'),
        },
        {
            label: 'Tables',
            value: `${existingTables}/${monitoredTables}`,
            copy: `${formatStorageRows(estimatedRows, 'estimated')} rows · warn ${Number(statusCounts.warning || 0)} · error ${Number(statusCounts.error || 0)}`,
            tone,
        },
    ];

    const tablesByGroup = tables.reduce((acc, table) => {
        const group = String(table?.group || 'other');
        if (!acc[group]) acc[group] = [];
        acc[group].push(table);
        return acc;
    }, {});
    const groupOrder = ['market', 'trading', 'quality', 'runtime', 'backtest', 'compat'];
    const groupMarkup = groupOrder
        .filter((group) => Array.isArray(tablesByGroup[group]) && tablesByGroup[group].length)
        .map((group) => {
            const items = tablesByGroup[group] || [];
            const issueCount = items.filter((item) => getStorageTone(item?.status) !== 'ok').length;
            return `
                <div class="storage-group-card">
                    <div class="storage-group-head">
                        <span>${escapeHtml(groups[group] || group)}</span>
                        <span class="storage-group-meta">${items.length} tables${issueCount ? ` · ${issueCount} issue` : ''}</span>
                    </div>
                    <div class="storage-table-list">
                        ${items.map((table) => {
                            const tableTone = getStorageTone(table?.status);
                            const latest = table?.latest_us || table?.latest_text || '--';
                            const rows = formatStorageRows(table?.row_count, table?.row_count_source);
                            return `
                                <div class="storage-table-row tone-${escapeHtml(tableTone)}">
                                    <div class="storage-table-main">
                                        <span class="storage-table-name">${escapeHtml(table?.name || '--')}</span>
                                        <span class="storage-table-time">${escapeHtml(latest)}</span>
                                    </div>
                                    <div class="storage-table-side">
                                        <span class="storage-table-rows">${escapeHtml(rows)}</span>
                                        <span class="storage-state-chip tone-${escapeHtml(tableTone)}">${escapeHtml(getStorageStatusLabel(table?.status))}</span>
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        })
        .join('');

    const flagMarkup = flags.length
        ? `
            <div class="storage-flags">
                ${flags.slice(0, 6).map((flag) => {
                    const flagTone = getStorageTone(flag?.severity === 'error' ? 'error' : (flag?.severity === 'warning' ? 'warning' : 'ok'));
                    return `
                        <div class="storage-flag tone-${escapeHtml(flagTone)}">
                            <span class="storage-flag-title">${escapeHtml(flag?.title || flag?.code || 'Storage flag')}</span>
                            <span class="storage-flag-detail">${escapeHtml(flag?.detail || '--')}</span>
                        </div>
                    `;
                }).join('')}
            </div>
        `
        : '<div class="storage-empty-note">未发现严重存储异常；普通容量变化只展示不告警。</div>';

    el.innerHTML = `
        <div class="storage-health-shell tone-${escapeHtml(tone)}">
            <div class="storage-summary-grid">
                ${summaryCards.map((card) => `
                    <div class="storage-summary-card tone-${escapeHtml(card.tone)}">
                        <div class="storage-summary-label">${escapeHtml(card.label)}</div>
                        <div class="storage-summary-value">${escapeHtml(card.value || '--')}</div>
                        <div class="storage-summary-copy">${escapeHtml(card.copy || '--')}</div>
                    </div>
                `).join('')}
            </div>
            ${flagMarkup}
            <div class="storage-group-grid">${groupMarkup || '<div class="loading-text">暂无重点表快照</div>'}</div>
        </div>
    `;
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

function getSystemCronPageModel(key, rows = []) {
    const config = typeof getSystemCronPaginationConfig === 'function'
        ? getSystemCronPaginationConfig(key)
        : { pageSize: 12, pageSizeOptions: [12], label: 'Cron' };
    const state = typeof getSystemCronPaginationState === 'function'
        ? getSystemCronPaginationState(key)
        : { page: 1, pageSize: config.pageSize };
    if (typeof createClientPaginationModel !== 'function') {
        const items = Array.isArray(rows) ? rows : [];
        return {
            page: 1,
            pageSize: items.length || config.pageSize,
            total: items.length,
            totalPages: 1,
            start: 0,
            end: items.length,
            hasPrev: false,
            hasNext: false,
            pageRows: items,
            pageSizeOptions: config.pageSizeOptions || [config.pageSize],
        };
    }
    const model = createClientPaginationModel(rows, state, {
        pageSize: config.pageSize,
        pageSizeOptions: config.pageSizeOptions,
    });
    if (typeof updateSystemCronPaginationState === 'function') {
        updateSystemCronPaginationState(key, {
            page: model.page,
            pageSize: model.pageSize,
        });
    }
    return model;
}

function renderSystemCronPagination(key, pageModel) {
    if (typeof renderClientPaginationBar !== 'function') return '';
    const config = typeof getSystemCronPaginationConfig === 'function'
        ? getSystemCronPaginationConfig(key)
        : { label: 'Cron' };
    return renderClientPaginationBar(pageModel, {
        key,
        label: config.label || 'Cron',
        pageAction: 'setSystemCronPage',
        pageSizeAction: 'setSystemCronPageSize',
        rootClass: 'client-pagination page-pagination system-cron-pagination',
        buttonClass: 'btn system-cron-page-btn',
    });
}

function renderSchedulerOverview(cronPayload = {}, summary = {}) {
    const el = document.getElementById('schedulerArea');
    if (!el) return;
    if (typeof rememberSchedulerCronRenderInputs === 'function') {
        rememberSchedulerCronRenderInputs(cronPayload, summary);
    }
    const definitions = Array.isArray(cronPayload?.items) ? cronPayload.items : [];
    const scheduler = getIbkrSchedulerSummary(cronPayload?.scheduler || {}, currentEnvironment);
    const configMap = buildIbkrConfigMap(summary);
    const cards = definitions.map((definition) => getIbkrSchedulerJobCardData(definition, currentEnvironment));
    const pageModel = getSystemCronPageModel('schedulerOverview', cards);
    const pagedCards = Array.isArray(pageModel.pageRows) ? pageModel.pageRows : cards;
    const paginationHtml = renderSystemCronPagination('schedulerOverview', pageModel);
    const enabledDefinitionCount = cards.filter((card) => card.effectiveEnabled).length;
    const coverageDenominator = scheduler.hasJobState ? (scheduler.jobCount || definitions.length || 0) : (definitions.length || 0);
    const coverageNumerator = scheduler.hasJobState ? (scheduler.enabledJobCount || enabledDefinitionCount || 0) : null;
    const schedulerStateValue = `${String(scheduler.status || '--').toUpperCase()}${scheduler.stale ? ' CACHE' : ''}`;
    const schedulerStateCopy = [
        `loop ${scheduler.loopIntervalLabel}`,
        scheduler.environmentLabel,
        scheduler.stale ? `缓存 ${scheduler.staleAgeLabel}` : '',
    ].filter(Boolean).join(' · ');
    const jobCoverageCopy = scheduler.hasJobState
        ? `native ${scheduler.nativeJobCount || 0} · compat ${scheduler.compatibilityJobCount || 0}`
        : (scheduler.metaError || '状态同步中 · 暂无 job state');

    const summaryCards = [
        {
            label: 'Scheduler State',
            value: schedulerStateValue,
            copy: schedulerStateCopy,
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
            value: coverageNumerator == null ? `--/${coverageDenominator || '--'}` : `${coverageNumerator}/${coverageDenominator || 0}`,
            copy: jobCoverageCopy,
            tone: scheduler.hasJobState ? (scheduler.compatibilityJobCount > 0 ? 'warn' : 'ok') : 'warn',
        },
        {
            label: 'Last Dispatch',
            value: scheduler.lastDispatchLabel,
            copy: scheduler.statusCountSummary,
            tone: scheduler.ok ? (scheduler.stale ? 'warn' : 'ok') : 'error',
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
            ${paginationHtml}
            <div class="cron-grid">
                ${pagedCards.map((card) => `
                    <div class="cron-card">
                        <div class="cron-card-head">
                            <div>
                                <div class="cron-card-title">${escapeHtml(card.title)}</div>
                                <div class="cron-card-key">${escapeHtml(card.configKey)}</div>
                            </div>
                            <span class="cron-state ${card.effectiveEnabled && card.status !== 'syncing' ? 'on' : 'off'}">${escapeHtml(card.statusLabel)}</span>
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
                                <span class="cron-meta-value">${escapeHtml(card.resultLabel || card.lastError || card.resultReason || '--')}</span>
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
    const pageModel = getSystemCronPageModel('configCronSummary', cronCards);
    const pagedCronCards = Array.isArray(pageModel.pageRows) ? pageModel.pageRows : cronCards;
    const paginationHtml = renderSystemCronPagination('configCronSummary', pageModel);
    return `
        ${paginationHtml}
        <div class="cron-grid">
            ${pagedCronCards.map((card) => {
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
    if (typeof rememberConfigCronRenderInputs === 'function') {
        rememberConfigCronRenderInputs(summary, cronDefinitions);
    }
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
