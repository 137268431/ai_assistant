// Shared IBKR config and scheduler presentation models.

const IBKR_CONFIG_DETAIL_KEYS = [
    'ibkr_compute_internal_url',
    'ibkr_backtest_internal_url',
    'ibkr_runtime_internal_url',
    'ibkr_api_internal_url',
    'ibkr_scheduler_internal_url',
    'ibkr_compute_enabled',
    'pb_scheduler_enabled',
    'ibkr_bar_publish_enabled',
    'ibkr_trading_enabled',
    'ibkr_signal_source',
    'signal_manual_confirm_enabled',
    'position_limit_max',
    'max_strategy_open_positions',
    'fixed_position_symbols',
    'consecutive_stop_loss_limit',
    'live_exit_policy_stop_update_enabled',
    'eod_close_time',
    'eod_keep_symbols',
    'watchlist_interval_min',
    'ibkr_target_refresh_sec',
    'ibkr_daily_scan_time_et',
    'ibkr_target_subscription_limit',
    'ibkr_total_subscription_limit',
    'ibkr_active_repair_interval_min',
    'ibkr_watchlist_backfill_interval_min',
    'ibkr_watchlist_backfill_batch_size',
    'ibkr_watchlist_backfill_stale_min',
    'trade_window_start_time',
    'trade_window_end_time',
    'order_window_end_time',
    'signal_validity_minutes',
    'signal_window_max_bars',
    'exit_policy_profile',
    'exit_policy_overrides',
    'cooldown_bars_after_sl',
    'cooldown_bars_after_reverse',
    'atr_dynamic_stop_enabled',
    'atr_stop_min_profit_r',
    'atr_stop_deviation_threshold',
    'atr_stop_min_change',
    'order_validity_minutes',
    'signal_poll_interval_sec',
    'reverse_signal_threshold',
    'reverse_flip_enabled'
];

const IBKR_SYSTEM_PRIMARY_DETAIL_KEYS = new Set([
    'ibkr_signal_source',
    'ibkr_target_refresh_sec',
    'trade_window_start_time',
    'trade_window_end_time',
    'order_window_end_time'
]);

function buildIbkrConfigMap(summary = {}, runtimeConfig = []) {
    const configMap = {};
    const runtimeItems = Array.isArray(runtimeConfig) ? runtimeConfig : [];

    runtimeItems.forEach((item) => {
        if (item?.key && !(item.key in configMap)) {
            configMap[item.key] = item.value;
        }
    });

    Object.entries(summary?.config || {}).forEach(([key, value]) => {
        if (!(key in configMap)) {
            configMap[key] = value;
        }
    });

    if (summary?.ibkr_trading_enabled !== undefined && !('ibkr_trading_enabled' in configMap)) {
        configMap.ibkr_trading_enabled = summary.ibkr_trading_enabled;
    }
    if (summary?.compute_enabled !== undefined && !('ibkr_compute_enabled' in configMap)) {
        configMap.ibkr_compute_enabled = summary.compute_enabled;
    }

    return configMap;
}

function getOrderedIbkrConfigEntries(configMap, keys = IBKR_CONFIG_DETAIL_KEYS) {
    if (!configMap || typeof configMap !== 'object') return [];
    return keys
        .filter((key) => configMap[key] !== undefined && configMap[key] !== '')
        .map((key) => ({ key, value: configMap[key] }));
}

function formatIbkrConfigKeyLabel(key) {
    return String(key || '').trim().toUpperCase().replace(/_/g, ' ');
}

function getIbkrConfigDisplayState(value, emptyDisplay = '--') {
    const raw = value === undefined || value === null ? '' : String(value).trim();
    if (!raw) {
        return {
            display: emptyDisplay,
            tone: 'neutral',
        };
    }

    const normalized = raw.toLowerCase();
    if (normalized === 'true') {
        return {
            display: 'ON',
            tone: 'on',
        };
    }
    if (normalized === 'false') {
        return {
            display: 'OFF',
            tone: 'off',
        };
    }

    return {
        display: raw,
        tone: 'neutral',
    };
}

function getIbkrTradeWindowLabel(configMap = {}) {
    const parts = [
        configMap?.trade_window_start_time,
        configMap?.trade_window_end_time
    ].filter((value) => value !== undefined && value !== null && String(value).trim() !== '');
    return parts.length ? parts.join(' - ') : '--';
}

function getIbkrSystemPrimaryConfigItems(summary = {}, configMap = {}) {
    return [
        { label: 'TRADING', value: summary?.ibkr_trading_enabled ?? configMap?.ibkr_trading_enabled ?? '--' },
        { label: 'COMPUTE', value: summary?.compute_enabled ?? configMap?.ibkr_compute_enabled ?? '--' },
        { label: 'IBKR SCHED', value: configMap?.pb_scheduler_enabled ?? '--' },
        { label: 'BAR PUBLISH', value: configMap?.ibkr_bar_publish_enabled ?? '--' },
        { label: 'SIGNAL SOURCE', value: configMap?.ibkr_signal_source ?? '--' },
        {
            label: 'TARGET REFRESH',
            value: configMap?.ibkr_target_refresh_sec ? `${configMap.ibkr_target_refresh_sec}s` : '--'
        },
        { label: 'TRADE WINDOW', value: getIbkrTradeWindowLabel(configMap) },
        { label: 'ORDER CUT', value: configMap?.order_window_end_time ?? '--' }
    ];
}

function getIbkrSystemSecondaryConfigItems(configMap = {}) {
    const items = [];

    if (configMap.ibkr_compute_internal_url) {
        items.push({ label: 'COMPUTE INTERNAL', value: configMap.ibkr_compute_internal_url });
    }
    if (configMap.ibkr_backtest_internal_url) {
        items.push({ label: 'BACKTEST INTERNAL', value: configMap.ibkr_backtest_internal_url });
    }
    if (configMap.ibkr_runtime_internal_url) {
        items.push({ label: 'RUNTIME INTERNAL', value: configMap.ibkr_runtime_internal_url });
    }
    if (configMap.ibkr_api_internal_url) {
        items.push({ label: 'API INTERNAL', value: configMap.ibkr_api_internal_url });
    }
    if (configMap.ibkr_scheduler_internal_url) {
        items.push({ label: 'SCHEDULER INTERNAL', value: configMap.ibkr_scheduler_internal_url });
    }

    getOrderedIbkrConfigEntries(configMap).forEach(({ key, value }) => {
        if (
            key === 'ibkr_compute_internal_url'
            || key === 'ibkr_backtest_internal_url'
            || key === 'ibkr_runtime_internal_url'
            || key === 'ibkr_api_internal_url'
            || key === 'ibkr_scheduler_internal_url'
            || IBKR_SYSTEM_PRIMARY_DETAIL_KEYS.has(key)
        ) return;
        items.push({
            label: formatIbkrConfigKeyLabel(key),
            value
        });
    });

    return items;
}

function getIbkrCronCardData(definition = {}, configMap = {}, environment = '') {
    const state = getCronEffectiveState(definition, configMap);
    return {
        title: definition.display_name || definition.config_display_name || definition.id || 'Scheduler Job',
        configKey: definition.config_key || definition.id || '-',
        effectiveEnabled: state.effectiveEnabled,
        schedulerEnabled: state.schedulerEnabled,
        cronEnabled: state.cronEnabled,
        functionSummary: definition.function_summary || '--',
        primaryCycleLabel: definition.beijing_cycle_label || definition.cycle_label || '--',
        utcCycleLabel: definition.cycle_label || '--',
        etCycleLabel: definition.et_cycle_label || definition.cycle_label || '--',
        cronExpr: definition.cron_expr || '--',
        windowLabel: definition.window_label || '--',
        environmentLabel: getEnvironmentLabel(environment),
    };
}

function getIbkrCronCardDataList(definitions, configMap = {}, environment = '') {
    if (!Array.isArray(definitions)) return [];
    return definitions.map((definition) => getIbkrCronCardData(definition, configMap, environment));
}

function getIbkrServiceHealthTone(status, fallback = 'neutral') {
    const text = String(status || '').trim().toLowerCase();
    if (!text) return fallback;
    if (['running', 'ok', 'online', 'ready', 'success', 'peer', 'external'].includes(text)) return 'ok';
    if (['idle'].includes(text)) return 'neutral';
    if (['warning', 'warn', 'pending', 'degraded', 'disabled', 'embedded', 'expected_remote'].includes(text)) return 'warn';
    if (['error', 'offline', 'failed'].includes(text)) return 'error';
    return fallback;
}

function formatIbkrLagMinutesLabel(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return '--';
    if (number < 1) return `${(number * 60).toFixed(number < 0.1 ? 1 : 0)}s`;
    return `${number.toFixed(number >= 10 ? 0 : 1)}m`;
}

function getIbkrSchedulerSummary(payload = {}, environment = '') {
    const source = payload?.scheduler && typeof payload.scheduler === 'object'
        ? payload.scheduler
        : (payload && typeof payload === 'object' ? payload : {});
    const status = String(source.status || (source.ok ? 'running' : 'offline')).trim().toLowerCase() || 'offline';
    const loopIntervalSeconds = Number(source.loop_interval_seconds || 0) || 0;
    const latestIngestedBarTimeMs = Number(source.latest_ingested_bar_time_ms || 0) || 0;
    const latestDispatchedBarTimeMs = Number(source.latest_dispatched_bar_time_ms || 0) || 0;
    const lastDispatchAtMs = Number(source.last_dispatch_at_ms || 0) || 0;
    const jobStatusCounts = source.job_status_counts && typeof source.job_status_counts === 'object'
        ? source.job_status_counts
        : {};
    const meta = source._meta && typeof source._meta === 'object' ? source._meta : {};
    const stale = source.stale === true || meta.from_cache === true;
    const staleAgeSeconds = Number(source.stale_age_s || meta.stale_age_s || 0) || 0;
    const metaError = String(meta.error || source.error || '').trim();
    const statusCountSummary = Object.entries(jobStatusCounts)
        .filter(([, count]) => Number(count || 0) > 0)
        .sort((left, right) => String(left[0]).localeCompare(String(right[0])))
        .map(([key, count]) => `${String(key).toUpperCase()} ${Number(count || 0)}`);
    const jobCount = Number(source.job_count || 0) || 0;

    return {
        ok: Boolean(source.ok),
        status,
        tone: stale ? 'warn' : getIbkrServiceHealthTone(status, 'neutral'),
        stale,
        staleAgeSeconds,
        staleAgeLabel: staleAgeSeconds > 0 ? formatIbkrLagMinutesLabel(staleAgeSeconds / 60) : '--',
        meta,
        metaError,
        environment: String(source.environment || environment || '').trim().toLowerCase(),
        environmentLabel: getEnvironmentLabel(source.environment || environment || ''),
        loopIntervalSeconds,
        loopIntervalLabel: loopIntervalSeconds > 0 ? `${Math.round(loopIntervalSeconds)}s` : '--',
        jobCount,
        hasJobState: jobCount > 0 || statusCountSummary.length > 0,
        enabledJobCount: Number(source.enabled_job_count || 0) || 0,
        nativeJobCount: Number(source.native_job_count || 0) || 0,
        compatibilityJobCount: Number(source.compatibility_job_count || 0) || 0,
        latestIngestedBarTimeMs,
        latestDispatchedBarTimeMs,
        lastDispatchAtMs,
        latestIngestedBarLabel: latestIngestedBarTimeMs ? formatTimeLabel(latestIngestedBarTimeMs) : '--',
        latestDispatchedBarLabel: latestDispatchedBarTimeMs ? formatTimeLabel(latestDispatchedBarTimeMs) : '--',
        lastDispatchLabel: lastDispatchAtMs ? formatTimeLabel(lastDispatchAtMs) : '--',
        dispatchLagMin: Number(source.dispatch_lag_min || 0) || 0,
        dispatchLagLabel: latestIngestedBarTimeMs ? formatIbkrLagMinutesLabel(source.dispatch_lag_min || 0) : '--',
        ingestCursor: source.ingest_cursor && typeof source.ingest_cursor === 'object' ? source.ingest_cursor : {},
        computeDispatchCursor: source.compute_dispatch_cursor && typeof source.compute_dispatch_cursor === 'object' ? source.compute_dispatch_cursor : {},
        jobStatusCounts,
        statusCountSummary: statusCountSummary.length ? statusCountSummary.join(' · ') : (metaError ? '状态同步中' : '暂无 job state'),
    };
}

function getIbkrSchedulerJobCardData(definition = {}, environment = '') {
    const state = definition?.job_state && typeof definition.job_state === 'object' ? definition.job_state : {};
    const hasJobState = Object.keys(state).length > 0;
    const runnerKind = String(definition.runner_kind || '').trim().toLowerCase() || 'compatibility_pending';
    const stateStatus = String(state.status || '').trim().toLowerCase();
    const effectiveEnabled = Boolean(definition.effective_enabled);
    const status = !effectiveEnabled ? 'disabled' : (hasJobState ? (stateStatus || 'idle') : 'syncing');
    const tone = !effectiveEnabled
        ? 'warn'
        : (status === 'syncing' ? 'warn' : getIbkrServiceHealthTone(status, runnerKind.startsWith('native_') ? 'ok' : 'neutral'));
    const modeLabel = runnerKind === 'compatibility_pending'
        ? 'COMPAT'
        : (runnerKind === 'native_compute_dispatch' ? 'NATIVE DISPATCH' : 'NATIVE');
    const lastResult = state.last_result && typeof state.last_result === 'object' ? state.last_result : {};
    const asyncOperation = lastResult.async_operation && typeof lastResult.async_operation === 'object'
        ? lastResult.async_operation
        : {};
    const asyncStatus = String(asyncOperation.status || lastResult.status || '').trim().toLowerCase();
    const asyncId = String(asyncOperation.run_id || asyncOperation.operation_id || '').trim();
    const resultReason = String(lastResult.reason || '').trim();
    const rawError = String(lastResult.error || '').trim();
    const terminalAsyncError = asyncStatus && ['failed', 'cancelled'].includes(asyncStatus);
    const lastError = status === 'error' || terminalAsyncError ? rawError : '';
    let resultLabel = lastError || resultReason || '--';
    if (asyncId || asyncStatus) {
        if (['accepted', 'pending', 'submitted'].includes(asyncStatus)) {
            resultLabel = `异步已提交${asyncId ? ` · ${asyncId}` : ''}`;
        } else if (['running', 'in_progress', 'processing'].includes(asyncStatus)) {
            resultLabel = `运行中 · 轮询中${asyncId ? ` · ${asyncId}` : ''}`;
        } else if (asyncStatus === 'completed') {
            resultLabel = `异步完成${asyncId ? ` · ${asyncId}` : ''}`;
        } else if (terminalAsyncError) {
            resultLabel = `${lastError || asyncStatus}${asyncId ? ` · ${asyncId}` : ''}`;
        }
    }

    return {
        id: String(definition.id || '').trim(),
        title: definition.display_name || definition.config_display_name || definition.id || 'Scheduler Job',
        configKey: definition.config_key || definition.id || '-',
        effectiveEnabled,
        hasJobState,
        schedulerEnabled: Boolean(definition.scheduler_enabled),
        cronEnabled: Boolean(definition.cron_enabled),
        functionSummary: definition.function_summary || '--',
        primaryCycleLabel: definition.beijing_cycle_label || definition.cycle_label || '--',
        utcCycleLabel: definition.cycle_label || '--',
        etCycleLabel: definition.et_cycle_label || definition.cycle_label || '--',
        cronExpr: definition.cron_expr || '--',
        windowLabel: definition.window_label || '--',
        environmentLabel: getEnvironmentLabel(definition.environment || environment),
        note: definition.note || '',
        runnerKind,
        modeLabel,
        status,
        statusLabel: String(status || '--').toUpperCase(),
        tone,
        lastRunStartedAtMs: Number(state.last_run_started_at_ms || 0) || 0,
        lastRunFinishedAtMs: Number(state.last_run_finished_at_ms || 0) || 0,
        lastSuccessAtMs: Number(state.last_success_at_ms || 0) || 0,
        lastRunStartedLabel: state.last_run_started_at_ms ? formatTimeLabel(state.last_run_started_at_ms) : '--',
        lastRunFinishedLabel: state.last_run_finished_at_ms ? formatTimeLabel(state.last_run_finished_at_ms) : '--',
        lastSuccessLabel: state.last_success_at_ms ? formatTimeLabel(state.last_success_at_ms) : '--',
        resultReason: resultReason || '--',
        resultLabel,
        lastError,
        skipped: Boolean(lastResult.skipped),
    };
}
