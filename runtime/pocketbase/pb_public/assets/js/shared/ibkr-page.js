function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatTimeLabel(value) {
    if (value === undefined || value === null || value === '') return '--';
    const text = String(value).trim();
    if (!text) return '--';

    if (/^\d{10,13}$/.test(text)) {
        const timestamp = text.length === 10 ? Number(text) * 1000 : Number(text);
        return formatBarTimeMsToET(timestamp);
    }

    if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
        return formatTime(text, 'America/New_York', 'default');
    }

    if (/^\d{4}-\d{2}-\d{2}/.test(text)) return text;
    return text;
}

function formatIbkrTimeLabelWithFallback(value) {
    const formatted = formatTimeLabel(value);
    if (formatted && formatted !== '--') return formatted;
    const raw = String(value || '').trim();
    if (!raw) return '--';
    return raw.replace('T', ' ').slice(0, 19);
}

function formatIbkrSecondsLabel(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '--';
    if (number < 1) return `${number.toFixed(3)}s`;
    if (number < 10) return `${number.toFixed(2)}s`;
    if (number < 60) return `${number.toFixed(1)}s`;
    return `${Math.round(number)}s`;
}

function getIbkrExtraObject(record) {
    return record && typeof record.extra === 'object' && record.extra ? record.extra : {};
}

function getIbkrComputedTimeLabel(record) {
    const extra = getIbkrExtraObject(record);
    return extra.computed_at_us || extra.computed_at_cn || record?.updated || record?.created || '--';
}

function getIbkrRecordBarLabel(record) {
    if (!record) return '--';
    return record.bar_time_ms ? formatBarTimeMsToET(record.bar_time_ms) : (record.us_time || '--');
}

function normalizeIbkrInterval(value, fallback = '') {
    const text = String(value ?? '').trim().toLowerCase();
    if (!text) return String(fallback ?? '').trim().toLowerCase();
    const mapping = {
        '1': '1m',
        '1m': '1m',
        '5': '5m',
        '5m': '5m',
        '15': '15m',
        '15m': '15m',
        '30': '30m',
        '30m': '30m',
        '60': '1h',
        '1h': '1h',
        '240': '4h',
        '4h': '4h',
        'd': '1d',
        '1d': '1d',
    };
    return mapping[text] || text;
}

function formatSymbolPreview(values, limit = 4) {
    if (!Array.isArray(values)) return '';
    const normalized = [];
    const seen = new Set();
    for (let i = 0; i < values.length; i++) {
        const symbol = String(values[i] ?? '').trim().toUpperCase();
        if (!symbol || seen.has(symbol)) continue;
        seen.add(symbol);
        normalized.push(symbol);
    }
    if (!normalized.length) return '';
    const clipped = normalized.slice(0, Math.max(1, limit));
    const more = normalized.length - clipped.length;
    return more > 0 ? `${clipped.join(', ')} +${more}` : clipped.join(', ');
}

function formatIbkrIntervalLabel(value, fallback = '--') {
    const normalized = normalizeIbkrInterval(value);
    return normalized || String(fallback ?? '--');
}

function normalizeBooleanText(value, fallback = 'TRUE') {
    const text = String(value ?? fallback).trim().toUpperCase();
    if (!text) return String(fallback || 'TRUE').trim().toUpperCase();
    return text;
}

function isTruthyConfigValue(value, fallback = 'TRUE') {
    const text = normalizeBooleanText(value, fallback);
    return !(text === 'FALSE' || text === '0' || text === 'OFF' || text === 'NO');
}

function getAuthHeaders(extra = {}) {
    const headers = { ...extra };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
}

function withTimeout(promise, ms, label) {
    const timeoutMs = Math.max(1000, Number(ms) || 0);
    return Promise.race([
        promise,
        new Promise((_, reject) => {
            setTimeout(() => {
                reject(new Error(`${label || 'request'} timed out after ${timeoutMs}ms`));
            }, timeoutMs);
        })
    ]);
}

async function requestIbkrPageJson(path, {
    environment = '',
    method = 'GET',
    body = null,
    retryAttempts = 3,
    retryDelayMs = 500
} = {}) {
    const options = {
        method,
        headers: getAuthHeaders(body ? { 'Content-Type': 'application/json' } : {})
    };
    if (body) options.body = JSON.stringify(body);

    const response = await fetchWithRetry(
        buildPageUrl(path, {}, environment ? { environment } : {}),
        options,
        {
            attempts: retryAttempts,
            retryDelayMs
        }
    );
    if (response.status === 401 || response.status === 403) {
        handleAuthError();
        throw new Error('Authentication failed');
    }

    const text = await response.text();
    let data = {};
    try {
        data = text ? JSON.parse(text) : {};
    } catch (_) {
        data = { ok: false, raw: text };
    }

    if (!response.ok) {
        throw new Error(data.message || data.error || `Request failed (${response.status})`);
    }
    return data;
}

function setIbkrPageLoading(active, title, copy, {
    overlayId = 'pageLoading',
    titleId = 'pageLoadingTitle',
    copyId = 'pageLoadingCopy',
} = {}) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return;
    if (title) {
        const titleEl = document.getElementById(titleId);
        if (titleEl) titleEl.textContent = title;
    }
    if (copy) {
        const copyEl = document.getElementById(copyId);
        if (copyEl) copyEl.textContent = copy;
    }
    overlay.classList.toggle('is-hidden', !active);
}

function ensureIbkrPageAuth(returnPath = `${location.pathname}${location.search}`) {
    const token = getToken();
    if (!token) {
        redirectToLogin(returnPath);
        return false;
    }
    return true;
}

async function requestIbkrEnvironmentJson(path, environment, options = {}) {
    return requestIbkrPageJson(path, {
        environment,
        ...options,
    });
}

function getCronEffectiveState(definition, configMap) {
    const schedulerValue = configMap?.pb_scheduler_enabled ?? 'TRUE';
    const cronValue = configMap?.[definition?.config_key] ?? definition?.default_value ?? 'TRUE';
    const schedulerEnabled = isTruthyConfigValue(schedulerValue, 'TRUE');
    const cronEnabled = isTruthyConfigValue(cronValue, definition?.default_value ?? 'TRUE');
    return {
        schedulerEnabled,
        cronEnabled,
        effectiveEnabled: schedulerEnabled && cronEnabled,
    };
}

function getSortedEngineEntries(engines) {
    if (!engines || typeof engines !== 'object') return [];
    return Object.entries(engines).sort((a, b) => {
        const readyDiff = Number(Boolean(b[1]?.is_ready)) - Number(Boolean(a[1]?.is_ready));
        if (readyDiff) return readyDiff;
        const barDiff = Number(b[1]?.bar_count || 0) - Number(a[1]?.bar_count || 0);
        if (barDiff) return barDiff;
        return a[0].localeCompare(b[0]);
    });
}

function getIbkrDataHealthStatus(ageMin, { noDataStatus = 'no_data' } = {}) {
    const numericAge = Number(ageMin);
    if (!Number.isFinite(numericAge) || numericAge < 0) return noDataStatus;
    if (numericAge <= 5) return 'online';
    if (numericAge <= 15) return 'delayed';
    return 'offline';
}

function buildIbkrDataHealth(lastBarTimeMs, { symbol = '', noDataStatus = 'no_data' } = {}) {
    const numericTime = Number(lastBarTimeMs || 0) || 0;
    if (numericTime <= 0) {
        return {
            status: noDataStatus,
            last_bar_age_min: null,
            last_symbol: String(symbol || ''),
            last_bar_time_ms: 0,
            last_bar_label: '--',
        };
    }

    const ageMin = Math.max(0, Math.round((Date.now() - numericTime) / 60000));
    return {
        status: getIbkrDataHealthStatus(ageMin, { noDataStatus }),
        last_bar_age_min: ageMin,
        last_symbol: String(symbol || ''),
        last_bar_time_ms: numericTime,
        last_bar_label: formatBarTimeMsToET(numericTime),
    };
}

function getIbkrFreshnessVisualState(ageMin) {
    const numericAge = Number(ageMin);
    if (!Number.isFinite(numericAge) || numericAge < 0) {
        return {
            color: '#64748b',
            pct: 0,
            chipClass: 'is-empty',
            stateText: '缺失',
            ageLabel: '--',
        };
    }

    if (numericAge <= 2) {
        return {
            color: '#22c55e',
            pct: 100,
            chipClass: 'is-fresh',
            stateText: '正常',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= 5) {
        return {
            color: '#eab308',
            pct: 72,
            chipClass: 'is-warn',
            stateText: '延迟',
            ageLabel: `${numericAge}m`,
        };
    }

    if (numericAge <= 15) {
        return {
            color: '#f97316',
            pct: 45,
            chipClass: 'is-warn',
            stateText: '偏慢',
            ageLabel: `${numericAge}m`,
        };
    }

    return {
        color: '#ef4444',
        pct: 18,
        chipClass: 'is-stale',
        stateText: '滞后',
        ageLabel: `${numericAge}m`,
    };
}

function getIbkrEngineViewModel(key, engine = {}, defaultEnvironment = '') {
    const safeKey = String(key || '');
    const [environment, symbol = '', interval = ''] = safeKey.split(':');
    const effectiveEnvironment = environment || defaultEnvironment || '';
    const environmentLabel = getEnvironmentLabel(effectiveEnvironment);
    const hasSymbol = Boolean(symbol);
    const normalizedInterval = normalizeIbkrInterval(interval, '5m') || '5m';
    const intervalLabel = formatIbkrIntervalLabel(interval, interval || '5m');
    return {
        key: safeKey,
        environment: effectiveEnvironment,
        environmentLabel,
        symbol,
        interval,
        displayName: hasSymbol ? `${symbol}${interval ? ` · ${intervalLabel}` : ''}` : safeKey,
        subtitle: hasSymbol ? environmentLabel : `${environmentLabel} · ${safeKey}`,
        chartHref: hasSymbol
            ? buildPageUrl('/ibkr_chart.html', { symbol, interval: normalizedInterval }, { environment: effectiveEnvironment })
            : '',
        ready: Boolean(engine?.is_ready),
        readyLabel: engine?.is_ready ? 'READY' : 'WARMING',
        barCount: Number(engine?.bar_count || 0) || 0,
        lastCloseLabel: engine?.last_close != null ? `$${Number(engine.last_close).toFixed(2)}` : '--',
        lastBarLabel: engine?.last_bar_time_ms ? formatBarTimeMsToET(engine.last_bar_time_ms) : '--',
    };
}

function isIbkrGatewayActive(runtime = {}) {
    return Boolean(runtime?.gateway?.running || runtime?.gateway?.reachable);
}

function getIbkrRuntimeStarted(runtime = {}) {
    return Boolean(
        runtime?.starting
        || runtime?.session?.running
        || runtime?.websocket?.running
        || runtime?.order_tracker?.running
    );
}

function normalizeIbkrComputeHealth(health = {}) {
    return {
        status: health?.ibkr_compute?.status || health?.status || 'unknown',
        last_compute: health?.ibkr_compute?.last_compute || health?.last_compute || null,
        last_scan: health?.ibkr_compute?.last_scan || health?.last_scan || null,
        error_count: health?.ibkr_compute?.error_count ?? health?.error_count ?? 0,
        uptime_s: health?.ibkr_compute?.uptime_s ?? health?.uptime_s ?? 0
    };
}

function getIbkrDataStatusCardModel(dataHealth = {}) {
    let dotClass = 'dot-gray';
    let mainText = dataHealth?.status || '无数据';

    if (dataHealth?.status === 'online') {
        dotClass = 'dot-green';
        mainText = '在线';
    } else if (dataHealth?.status === 'delayed') {
        dotClass = 'dot-yellow';
        mainText = '延迟';
    } else if (dataHealth?.status === 'offline') {
        dotClass = 'dot-red';
        mainText = '离线';
    }

    if (dataHealth?.last_bar_age_min != null && dataHealth.last_bar_age_min !== '') {
        mainText += ` · ${String(dataHealth.last_bar_age_min)}m`;
    }

    const metaParts = [];
    if (dataHealth?.last_bar_time_ms) {
        metaParts.push(formatTimeLabel(dataHealth.last_bar_time_ms));
    }
    if (dataHealth?.last_symbol) {
        metaParts.push(String(dataHealth.last_symbol));
    }

    const bucketMeta = [];
    if (dataHealth?.bar_bucket_status) bucketMeta.push(`bar bucket ${String(dataHealth.bar_bucket_status)}`);
    if (Number(dataHealth?.pending_symbols_total || 0) > 0) bucketMeta.push(`pending ${Number(dataHealth.pending_symbols_total || 0)}`);
    if (Array.isArray(dataHealth?.pending_symbols) && dataHealth.pending_symbols.length) {
        bucketMeta.push(`symbols ${formatSymbolPreview(dataHealth.pending_symbols, 4)}`);
    }
    if (Number(dataHealth?.lag_s || 0) > 0) bucketMeta.push(`lag ${Math.round(Number(dataHealth.lag_s || 0))}s`);
    if (dataHealth?.last_due_bucket_us) bucketMeta.push(`due ${String(dataHealth.last_due_bucket_us)}`);
    if (dataHealth?.last_completed_bucket_us) bucketMeta.push(`done ${String(dataHealth.last_completed_bucket_us)}`);
    if (bucketMeta.length) metaParts.push(bucketMeta.join(' · '));

    return {
        dotClass,
        mainText,
        subText: metaParts.join(' · ')
    };
}

function getIbkrComputeStatusCardModel(compute = {}) {
    const metaParts = [];
    if (Number(compute?.total_engines || 0) > 0) {
        metaParts.push(`${Number(compute.ready_engines || 0)}/${Number(compute.total_engines || 0)} ready`);
    }
    if (Number(compute?.last_realtime_elapsed_s || 0) > 0) metaParts.push(`${Number(compute.last_realtime_elapsed_s || 0).toFixed(2)}s`);
    if (Number(compute?.last_realtime_signals || 0) > 0) metaParts.push(`sig ${Number(compute.last_realtime_signals || 0)}`);
    if (Number(compute?.last_realtime_errors || 0) > 0) metaParts.push(`err ${Number(compute.last_realtime_errors || 0)}`);
    if (Number(compute?.queue_size || 0) > 0) metaParts.push(`queue ${Number(compute.queue_size || 0)}`);
    if (!metaParts.length && compute?.last_realtime_run) metaParts.push(`run ${formatTimeLabel(compute.last_realtime_run)}`);

    if (compute?.status === 'running') {
        return {
            dotClass: 'dot-green',
            mainText: '运行',
            subText: metaParts.join(' · ')
        };
    }

    if (compute?.status === 'offline') {
        return {
            dotClass: 'dot-red',
            mainText: '离线',
            subText: metaParts.join(' · ')
        };
    }

    return {
        dotClass: compute?.status === 'error' ? 'dot-red' : 'dot-gray',
        mainText: compute?.status || '--',
        subText: metaParts.join(' · ')
    };
}

function getIbkrRuntimeStatusCardModel(runtime = {}) {
    const started = getIbkrRuntimeStarted(runtime);
    const gatewayActive = isIbkrGatewayActive(runtime);
    const authenticated = Boolean(runtime?.session?.authenticated);
    const metaParts = [];

    if (gatewayActive) metaParts.push('gateway');
    if (runtime?.session?.running) metaParts.push(authenticated ? 'session 已认证' : 'session 待认证');
    if (runtime?.websocket?.running) metaParts.push('ws 运行');
    if (runtime?.order_tracker?.running) metaParts.push('orders 运行');

    if (started && authenticated) {
        return {
            dotClass: 'dot-green',
            mainText: '认证',
            subText: metaParts.join(' · '),
            started,
            gatewayActive,
            authenticated
        };
    }

    if (started) {
        return {
            dotClass: 'dot-yellow',
            mainText: '待认证',
            subText: metaParts.join(' · '),
            started,
            gatewayActive,
            authenticated
        };
    }

    if (gatewayActive) {
        return {
            dotClass: 'dot-red',
            mainText: '未启动',
            subText: metaParts.join(' · '),
            started,
            gatewayActive,
            authenticated
        };
    }

    return {
        dotClass: 'dot-gray',
        mainText: '离线',
        subText: metaParts.join(' · '),
        started,
        gatewayActive,
        authenticated
    };
}

function getIbkrWarmupSummaryText(warmup = {}, { prefix = 'warmup', elapsedText = '' } = {}) {
    const safePrefix = String(prefix || '').trim();
    const base = warmup?.gate_open
        ? `${safePrefix ? `${safePrefix} ` : ''}gate open`
        : `${safePrefix ? `${safePrefix} ` : ''}${String(warmup?.phase || 'idle')} ${Number(warmup?.ready_trade_symbols || 0)}/${Number(warmup?.trade_symbols_total || 0)}`;
    const safeElapsed = String(elapsedText || '').trim();
    return safeElapsed ? `${base} · ${safeElapsed}` : base;
}

function getIbkrRuntimeHeroAuthSummaryText({
    runtimeMismatch = null,
    runtimeStatus = {},
    startupActive = false,
    startupStepLabel = 'startup active',
    warmupSummaryText = 'warmup idle 0/0',
    effectiveReasonLabel = '手动验证',
    twoFactorStatus = 'idle',
    twoFactorMode = '',
    recoveryPhase = '',
    lastRequestAtLabel = '',
    keepCurrentCycle = false
} = {}) {
    if (runtimeStatus?.authenticated) {
        if (runtimeMismatch) {
            return `runtime mismatch · actual ${String(runtimeMismatch.actual || '--').toUpperCase()}`;
        }
        return `session authenticated · ${startupActive ? startupStepLabel : warmupSummaryText}`;
    }

    if (!runtimeStatus?.started) {
        return 'runtime stopped · waiting manual start';
    }

    if (runtimeMismatch) {
        return `runtime mismatch · actual ${String(runtimeMismatch.actual || '--').toUpperCase()}`;
    }

    const parts = [`${String(effectiveReasonLabel || '手动验证')} · 2FA ${String(twoFactorStatus || 'idle')}`];
    if (twoFactorMode) parts.push(String(twoFactorMode));
    if (recoveryPhase) parts.push(String(recoveryPhase));
    if (lastRequestAtLabel) parts.push(`requested ${String(lastRequestAtLabel)}`);
    if (keepCurrentCycle) parts.push('keep current cycle');
    return parts.join(' · ');
}

function getIbkrTwoFactorResponsePhase(twoFactorState = {}) {
    const status = String(twoFactorState?.status || '').trim().toLowerCase();
    const responseStatus = String(twoFactorState?.response_status || '').trim().toLowerCase();
    const challengeCode = String(twoFactorState?.challenge_code || '').trim();
    const resetRecommended = twoFactorState?.reset_recommended === true;
    const active = status === 'waiting_response';

    let phase = 'inactive';
    if (active) {
        if (resetRecommended) {
            phase = responseStatus === 'gateway_rejected' ? 'reset_gateway_rejected' : 'reset_pending';
        } else if (responseStatus === 'received') {
            phase = 'received';
        } else if (responseStatus === 'submitted') {
            phase = 'submitted';
        } else if (responseStatus === 'gateway_rejected') {
            phase = 'gateway_rejected';
        } else if (responseStatus === 'submit_failed') {
            phase = 'submit_failed';
        } else {
            phase = challengeCode ? 'challenge_ready' : 'waiting';
        }
    }

    return {
        active,
        phase,
        status,
        responseStatus,
        challengeCode,
        resetRecommended,
        canSubmit: active && Boolean(challengeCode) && !resetRecommended && ['', 'gateway_rejected', 'submit_failed'].includes(responseStatus),
        showResetCta: active && resetRecommended,
        inputPlaceholder: responseStatus === 'gateway_rejected' ? '重新输入 Response Code' : '输入 Response Code'
    };
}

function normalizeIbkrTwoFactorStatus(value) {
    const text = String(value || '').trim().toLowerCase();
    if (!text) return 'requested';
    if (['pending', 'waiting_mobile_approval', 'mobile_approval', 'awaiting_mobile_approval'].includes(text)) return 'waiting_confirm';
    if (['complete', 'completed', 'authenticated'].includes(text)) return 'success';
    if (text === 'error') return 'failed';
    return text;
}

function getIbkrTwoFactorStatusKey(twoFactorState = {}) {
    return normalizeIbkrTwoFactorStatus(twoFactorState?.status || '');
}

function isIbkrTwoFactorCycleActive(twoFactorState = {}) {
    return ['triggered', 'waiting_confirm', 'waiting_response'].includes(getIbkrTwoFactorStatusKey(twoFactorState));
}

function getIbkrTwoFactorCyclePhase(twoFactorState = {}) {
    const status = getIbkrTwoFactorStatusKey(twoFactorState);
    const responsePhase = getIbkrTwoFactorResponsePhase(twoFactorState);
    if (status === 'waiting_response') {
        if (responsePhase.showResetCta) return 'waiting_response_reset';
        if (responsePhase.phase === 'received') return 'waiting_response_received';
        if (responsePhase.phase === 'submitted') return 'waiting_response_submitted';
        if (responsePhase.phase === 'gateway_rejected') return 'waiting_response_gateway_rejected';
        if (responsePhase.phase === 'submit_failed') return 'waiting_response_submit_failed';
        if (responsePhase.canSubmit) return 'waiting_response_ready';
        return 'waiting_response_waiting';
    }
    if (status === 'waiting_confirm') return 'waiting_confirm';
    if (status === 'triggered') return 'triggered';
    if (status === 'requested') return 'requested';
    if (status === 'success') return 'success';
    if (status === 'failed') return 'failed';
    if (status === 'timeout') return 'timeout';
    return status || 'requested';
}

function getIbkrTwoFactorCycleActionLockReason(action, cyclePhase) {
    const safeAction = String(action || '').trim();
    const safePhase = String(cyclePhase || '').trim();

    if (safeAction === 'probe') {
        if (safePhase === 'waiting_response_reset') {
            return '当前旧 2FA / Session 状态很可能已失配，先执行“放弃当前轮次并干净重开”；此时再做静默探测只会增加判断噪音。';
        }
        return '当前已有一轮 2FA 正在进行，先等这一轮收口；此时再做静默探测只会增加判断噪音。';
    }

    if (safeAction === 'reauth_force_new') {
        if (safePhase === 'waiting_response_reset') {
            return '当前旧 2FA / Session 状态很可能已失配。如要放弃当前轮次，请只使用“放弃当前轮次并干净重开”。';
        }
        return '当前已有一轮 2FA 正在进行。如要放弃当前轮次，请只使用“放弃当前轮次并干净重开”。';
    }

    if (safePhase === 'waiting_response_reset') {
        return '当前旧 2FA / Session 状态很可能已失配，请直接执行“放弃当前轮次并干净重开”，不要重新触发。';
    }
    if (safePhase === 'waiting_response_submitted') {
        return '当前 Response Code 已提交，正在等待 Gateway 恢复认证；不要重新触发。';
    }
    if (safePhase === 'waiting_response_gateway_rejected') {
        return 'Gateway 已拒绝当前 Response Code，请先在本页按当前 Challenge 重试，不要重新触发。';
    }
    if (safePhase === 'waiting_response_submit_failed') {
        return '浏览器提交 Response Code 失败，请先在本页重试，不要重新触发。';
    }
    if (safePhase === 'waiting_response_received') {
        return 'Runtime 已收到 Response Code，正在等待浏览器提交流程；不要重新触发。';
    }
    if (safePhase === 'waiting_response_ready' || safePhase === 'waiting_response_waiting') {
        return '当前已进入 Challenge/Response，请继续当前轮次并提交 Response Code，不要重复触发。';
    }
    if (safePhase === 'waiting_confirm') {
        return '当前正在等待手机确认，请继续当前轮次，不要重复触发。';
    }
    return '当前已有一轮 2FA 正在进行，请继续当前轮次，不要重复触发。';
}

function getIbkrTwoFactorCycleActionSummary(action, cyclePhase) {
    const safeAction = String(action || '').trim();
    const safePhase = String(cyclePhase || '').trim();

    if (safePhase === 'waiting_response_reset') {
        return '当前旧 2FA / Session 状态很可能已失配，请打开 Runtime 页面执行“放弃当前轮次并干净重开”，不要重复触发。';
    }
    if (safePhase === 'waiting_response_submitted') {
        return '当前 Response Code 已提交，正在等待 Gateway 恢复认证；请继续当前轮次，不要重复触发。';
    }
    if (safePhase === 'waiting_response_gateway_rejected') {
        return 'Gateway 已拒绝当前 Response Code，请打开 Runtime 页面核对当前 Challenge 后重新提交，不要重复触发。';
    }
    if (safePhase === 'waiting_response_submit_failed') {
        return '浏览器提交 Response Code 失败，请打开 Runtime 页面重试当前 Challenge，不要重复触发。';
    }
    if (safePhase === 'waiting_response_received') {
        return 'Runtime 已收到 Response Code，正在等待浏览器提交流程；请继续当前轮次，不要重复触发。';
    }
    if (safePhase === 'waiting_response_ready' || safePhase === 'waiting_response_waiting') {
        return '当前已进入 Challenge/Response，请继续当前轮次并在 Runtime 页面提交 Response Code，不要重复触发。';
    }
    if (safePhase === 'triggered' || safePhase === 'waiting_confirm') {
        return '当前已有一轮 2FA 正在进行，请继续当前轮次，不要重复触发。';
    }
    return safeAction === 'reauth_force_new'
        ? '已开始新一轮 2FA，请立即查看手机通知或飞书卡片。'
        : '已请求 2FA 卡片，请在飞书点击按钮触发验证。';
}

function getIbkrTwoFactorWaitingResponseHelperText(twoFactorState = {}) {
    const state = twoFactorState && typeof twoFactorState === 'object'
        ? twoFactorState
        : {};
    const responsePhase = getIbkrTwoFactorResponsePhase(state);
    const feedback = String(state.challenge_feedback || 'Authentication failed').trim() || 'Authentication failed';

    if (responsePhase.phase === 'reset_gateway_rejected') {
        return 'Gateway 已拒绝当前 Response Code，且旧轮次长时间未恢复。当前旧 2FA / Session 状态很可能已失配，请直接执行“放弃当前轮次并干净重开”。';
    }
    if (responsePhase.phase === 'reset_pending') {
        return 'Response Code 已提交较久但 Gateway 仍未恢复认证。当前旧 2FA / Session 状态很可能已失配，请直接执行“放弃当前轮次并干净重开”。';
    }
    if (responsePhase.phase === 'received') {
        return 'Runtime 已收到 Response Code，正在等待 compute 浏览器提交流程。此时不要重复输入，也不要触发新一轮。';
    }
    if (responsePhase.phase === 'submitted') {
        return '浏览器已提交 Response Code，正在等待 Gateway 恢复认证。此时不要重复提交旧 Response，也不要触发新一轮。';
    }
    if (responsePhase.phase === 'gateway_rejected') {
        return `Gateway 已拒绝当前 Response Code（${feedback}）。请核对当前 Challenge，用 IBKR App 重新生成后在这里重提。`;
    }
    if (responsePhase.phase === 'submit_failed') {
        return '浏览器提交动作失败。请在这里重新提交当前 Challenge 的 Response Code，不要重新触发。';
    }
    return '当前已切到 Challenge/Response。仅在手机上点确认不会完成验证；请在 IBKR App 的 Two-Factor Authentication 中输入当前 Challenge，拿到 Response Code 后回到这里提交。不要重复触发新一轮。';
}

function getIbkrTwoFactorCycleHelperText({
    cyclePhase = '',
    runtimeMismatchMessage = '',
    recoveryPhase = '',
    manualTakeoverActive = false,
    successMessage = '',
    lastResult = '',
    waitingResponseHelperText = '',
    twoFactorState = {},
} = {}) {
    const safeCyclePhase = String(cyclePhase || '').trim();
    const safeRecoveryPhase = String(recoveryPhase || '').trim().toLowerCase();
    const safeRuntimeMismatchMessage = String(runtimeMismatchMessage || '').trim();
    const safeSuccessMessage = String(successMessage || '').trim();
    const safeLastResult = String(lastResult || '').trim();
    const safeWaitingResponseHelperText = String(waitingResponseHelperText || '').trim();

    if (safeRuntimeMismatchMessage) return safeRuntimeMismatchMessage;
    if (safeRecoveryPhase === 'panic_resetting') {
        return '系统正在全量清空旧 2FA / Session 状态，并准备拉起一轮新的干净验证。当前旧 Challenge / Response 不应再继续使用。';
    }
    if (manualTakeoverActive) {
        return '当前处于人工接管中。你可以继续去真实账户里确认挂单；系统不会直接把当前轮次判死，但后台仍会持续静默探测会话是否已恢复。';
    }
    if (safeCyclePhase === 'success') {
        return safeSuccessMessage || '当前 Gateway 会话已认证，无需提交 Response Code。';
    }
    if (safeCyclePhase === 'requested') {
        return '这一步只是把 2FA 卡片发到或刷新到飞书，还没有真正开始验证。请直接用页面顶部主入口或去飞书点击“开始 2FA 验证”。';
    }
    if (safeCyclePhase === 'triggered') {
        return safeLastResult || '登录流程已经触发，等待 IBKR 返回手机确认或 Challenge/Response。当前已有 active 轮次，请不要重复触发。';
    }
    if (safeCyclePhase.startsWith('waiting_response')) {
        return safeWaitingResponseHelperText || getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
    }
    if (safeCyclePhase === 'waiting_confirm') {
        return '当前是手机推送模式，只需要点手机通知确认；如果页面后续切成 Challenge/Response，手机确认将不再够用，这里会出现 Challenge 与响应码输入框。当前已有 active 轮次，请不要重复触发。';
    }
    return safeLastResult || '当前没有等待中的 Challenge。';
}

function getIbkrTwoFactorChallengeDisplayText({
    challengeCode = '',
    runtimeMismatchActual = '',
    cyclePhase = '',
} = {}) {
    const safeChallengeCode = String(challengeCode || '').trim();
    if (safeChallengeCode) return safeChallengeCode;

    const safeRuntimeMismatchActual = String(runtimeMismatchActual || '').trim();
    if (safeRuntimeMismatchActual) {
        return `当前实际 runtime: ${safeRuntimeMismatchActual.toUpperCase()}`;
    }

    return String(cyclePhase || '').trim() === 'success'
        ? '当前无需 Challenge'
        : '当前未检测到 Challenge';
}

function getIbkrTwoFactorPanelViewModel({
    twoFactorState = {},
    runtimeMismatch = null,
    manualAuthReasonLabel = '--',
    waitingResponseHelperText = '',
} = {}) {
    const state = twoFactorState && typeof twoFactorState === 'object'
        ? twoFactorState
        : {};
    const statusKey = getIbkrTwoFactorStatusKey(state) || 'requested';
    const cyclePhase = getIbkrTwoFactorCyclePhase(state);
    const challengeCode = String(state?.challenge_code || '').trim();
    const recoveryPhase = String(state?.recovery_phase || '').trim().toLowerCase();
    const responsePhase = getIbkrTwoFactorResponsePhase(state);
    const responseStatus = responsePhase.responseStatus;
    const previousCycle = state?.previous_cycle && typeof state.previous_cycle === 'object'
        ? state.previous_cycle
        : null;

    let helperText = getIbkrTwoFactorCycleHelperText({
        cyclePhase,
        runtimeMismatchMessage: runtimeMismatch?.message || '',
        recoveryPhase,
        manualTakeoverActive: state?.manual_takeover_active === true,
        successMessage: state?.message || '',
        lastResult: state?.last_result || '',
        twoFactorState: state,
        waitingResponseHelperText,
    });

    if (previousCycle?.superseded_at) {
        const previousStatus = String(previousCycle.status || 'unknown').trim() || 'unknown';
        helperText = `上一轮 ${previousStatus} 已在 ${formatIbkrTimeLabelWithFallback(previousCycle.superseded_at)} 被替换。后续请只跟当前这一轮。 ${helperText}`;
    }

    const metaItems = [`当前用途 ${String(manualAuthReasonLabel || '--').trim() || '--'}`];
    if (responseStatus) metaItems.push(`响应状态 ${responseStatus}`);
    if (state?.challenge_feedback) metaItems.push(`Gateway反馈 ${state.challenge_feedback}`);
    if (state?.operator_action) metaItems.push(`建议动作 ${state.operator_action}`);
    if (responsePhase.showResetCta) metaItems.push('建议干净重开');

    return {
        statusKey,
        statusText: String(state?.status || '--').toUpperCase(),
        cyclePhase,
        modeText: String(state?.mode || '').trim() || '--',
        recoveryText: String(state?.recovery_phase || '--'),
        sourceText: String(state?.source || '--'),
        responseText: responseStatus || '--',
        challengeCode,
        challengeDisplayText: getIbkrTwoFactorChallengeDisplayText({
            challengeCode,
            runtimeMismatchActual: runtimeMismatch?.actual || '',
            cyclePhase,
        }),
        helperText,
        metaItems,
        canSubmit: responsePhase.canSubmit,
        showResetCta: responsePhase.showResetCta,
        inputPlaceholder: responsePhase.inputPlaceholder,
    };
}

function getIbkrRuntimeAuthGuidanceModel({
    runtimeMismatch = null,
    sessionAuthenticated = false,
    runtimeStarted = false,
    startup = {},
    reason = 'manual_start',
    reasonLabel = '手动验证',
    startupStepLabel = '等待下一步',
    twoFactorCyclePhase = '',
    waitingResponseHelperText = '',
    twoFactorState = {},
    environment = '',
    source = 'runtime_page_banner',
} = {}) {
    const safeReason = String(reason || '').trim();
    const safeReasonLabel = String(reasonLabel || '手动验证').trim() || '手动验证';
    const safeStartupStepLabel = String(startupStepLabel || '等待下一步').trim() || '等待下一步';
    const safeWaitingResponseHelperText = String(waitingResponseHelperText || '').trim()
        || (String(twoFactorCyclePhase || '').trim().startsWith('waiting_response')
            ? getIbkrTwoFactorWaitingResponseHelperText(twoFactorState)
            : '');

    if (runtimeMismatch) {
        return {
            visible: true,
            tone: 'error',
            badge: '环境错配',
            stepLabel: '切换环境',
            title: `当前实际运行环境是 ${String(runtimeMismatch.actual || '--').toUpperCase()}`,
            copy: runtimeMismatch.message,
            meta: ['动作已阻止', '请切到对应环境页面'],
            buttonLabel: '刷新状态',
            behavior: 'refresh'
        };
    }

    if (twoFactorCyclePhase === 'waiting_response_reset') {
        return {
            visible: true,
            tone: 'error',
            badge: safeReasonLabel,
            stepLabel: '重开验证',
            title: '旧 2FA 轮次已失配',
            copy: safeWaitingResponseHelperText,
            meta: [safeStartupStepLabel, '不要重复发起新一轮'],
            buttonLabel: '干净重开 2FA',
            behavior: 'action',
            actionName: 'panic_reset_2fa'
        };
    }

    if (['waiting_response_ready', 'waiting_response_gateway_rejected', 'waiting_response_submit_failed'].includes(twoFactorCyclePhase)) {
        return {
            visible: true,
            tone: 'warn',
            badge: safeReasonLabel,
            stepLabel: '提交响应码',
            title: '继续当前 2FA 轮次',
            copy: '当前已经进入 Challenge/Response。请直接在下方 2FA 控制台提交 Response Code，不要重新发起。',
            meta: [safeStartupStepLabel, '继续当前轮次'],
            buttonLabel: '定位到响应码输入',
            behavior: 'focus_response'
        };
    }

    if (['waiting_response_waiting', 'waiting_response_received', 'waiting_response_submitted'].includes(twoFactorCyclePhase)) {
        return {
            visible: true,
            tone: 'warn',
            badge: safeReasonLabel,
            stepLabel: '等待响应码流程',
            title: '当前轮次仍在收口',
            copy: safeWaitingResponseHelperText,
            meta: [safeStartupStepLabel, '不要重新触发'],
            buttonLabel: '刷新状态',
            behavior: 'refresh'
        };
    }

    if (twoFactorCyclePhase === 'waiting_confirm') {
        return {
            visible: true,
            tone: 'warn',
            badge: safeReasonLabel,
            stepLabel: '手机确认',
            title: '等待手机确认',
            copy: '这一步只看手机通知，不要再发起新一轮。确认完成后回到这里刷新状态。',
            meta: [safeStartupStepLabel, '继续当前轮次'],
            buttonLabel: '刷新状态',
            behavior: 'refresh'
        };
    }

    if (twoFactorCyclePhase === 'triggered') {
        return {
            visible: true,
            tone: 'info',
            badge: safeReasonLabel,
            stepLabel: '等待验证模式',
            title: '当前轮次已手动触发',
            copy: '飞书按钮已经点下。现在等待手机确认，或稍后切到响应码模式。',
            meta: [safeStartupStepLabel, '不要重复触发'],
            buttonLabel: '刷新状态',
            behavior: 'refresh'
        };
    }

    if (twoFactorCyclePhase === 'requested') {
        return {
            visible: true,
            tone: safeReason === 'weekly_reauth' ? 'warn' : 'info',
            badge: safeReasonLabel,
            stepLabel: '去飞书开始',
            title: safeReason === 'weekly_reauth' ? '本周重登提醒已发出' : '去飞书开始 2FA 验证',
            copy: startup?.operator_action || '这一步还没有真正开始验证。请去飞书点击“开始 2FA 验证”，点完后回到这里刷新状态。',
            meta: [safeStartupStepLabel, startup?.current_blocker || '当前停在待手动触发'],
            buttonLabel: '请求 / 刷新 2FA 卡片',
            behavior: 'action',
            actionName: 'reauth',
            requestTarget: getIbkrRuntimeAuthGuidanceRequestTarget({
                requestTargetKind: 'reauth',
                environment,
                reason: safeReason,
                source
            })
        };
    }

    if (!sessionAuthenticated && !runtimeStarted && !startup?.active) {
        return {
            visible: true,
            tone: 'info',
            badge: '顶部主入口',
            stepLabel: '先拉起服务',
            title: '先启动 IBKR 服务',
            copy: '这一步只拉起服务，不会自动触发手机 Push。启动后顶部入口会切到飞书手动验证。',
            meta: ['启动后再进入手动验证', '不会自动往下推进'],
            buttonLabel: '启动 IBKR 服务',
            behavior: 'action',
            actionName: 'start',
            requestTarget: getIbkrRuntimeAuthGuidanceRequestTarget({
                requestTargetKind: 'start',
                environment,
                reason: 'manual_start',
                source
            })
        };
    }

    if (!sessionAuthenticated && (startup?.active || ['weekly_reauth', 'manual_start', 'panic_reset_2fa'].includes(safeReason))) {
        return {
            visible: true,
            tone: safeReason === 'weekly_reauth' ? 'warn' : 'info',
            badge: safeReasonLabel,
            stepLabel: safeStartupStepLabel,
            title: safeReason === 'weekly_reauth' ? '继续本周重登提醒' : '继续手动验证',
            copy: startup?.operator_action || startup?.current_blocker || '请先把同一张 2FA 卡片刷到飞书，然后去飞书点击开始验证。',
            meta: [startup?.summary || '按顶部入口一步一步推进', '只有人工确认后才继续'],
            buttonLabel: '请求 / 刷新 2FA 卡片',
            behavior: 'action',
            actionName: 'reauth',
            requestTarget: getIbkrRuntimeAuthGuidanceRequestTarget({
                requestTargetKind: 'reauth',
                environment,
                reason: safeReason,
                source
            })
        };
    }

    if (sessionAuthenticated && startup?.active && startup?.status === 'active') {
        return {
            visible: true,
            tone: 'ok',
            badge: '启动推进中',
            stepLabel: safeStartupStepLabel,
            title: 'Runtime 正在继续恢复',
            copy: startup?.current_blocker || startup?.summary || '当前不需要额外手动操作，等待 Runtime 完成剩余恢复与检查。',
            meta: [startup?.operator_action || '等待系统继续推进'],
            buttonLabel: '刷新状态',
            behavior: 'refresh'
        };
    }

    return { visible: false };
}

function getIbkrRuntimeAuthGuidanceRequestTarget({
    requestTargetKind = '',
    environment = '',
    reason = 'manual_start',
    source = 'runtime_page_banner',
} = {}) {
    const safeKind = String(requestTargetKind || '').trim();
    const safeEnvironment = String(environment || '').trim();
    const safeSource = String(source || 'runtime_page_banner').trim() || 'runtime_page_banner';

    if (safeKind === 'reauth') {
        const normalizedReason = String(reason || '').trim() === 'weekly_reauth'
            ? 'weekly_reauth'
            : 'manual_start';
        return {
            path: '/api/custom/ibkr/2fa/request',
            body: {
                environment: safeEnvironment,
                reason: normalizedReason,
                source: safeSource,
                force_reset: true,
                message: normalizedReason === 'weekly_reauth'
                    ? '本周重登等待你在飞书手动点开始验证。'
                    : '启动验证等待你在飞书手动点开始验证。'
            }
        };
    }

    if (safeKind === 'start') {
        return {
            path: '/api/custom/ibkr/start',
            body: {
                environment: safeEnvironment,
                trigger_login: false,
                reason: 'manual_start',
                source: safeSource
            }
        };
    }

    return null;
}

function getIbkrRuntimePrimaryBlockerPhase({
    runtimeMismatch = null,
    startup = {},
    sessionAuthenticated = false,
    gatewayActive = false,
    runtimeStarted = false,
    responsePhase = {},
    twoFactorStatus = '',
    recoveryPhase = '',
    warmup = {},
    dataHealth = {},
    canonical = {},
    realtimeState = {},
    readyEngines = 0,
    totalEngines = 0,
} = {}) {
    const normalizedTwoFactorStatus = String(twoFactorStatus || '').trim().toLowerCase();
    const normalizedRecoveryPhase = String(recoveryPhase || '').trim().toLowerCase();
    const normalizedWarmupPhase = String(warmup?.phase || '').trim().toLowerCase();
    const canonicalPending = Number(canonical?.pending_symbols_total || 0) || 0;
    const canonicalLag = Number(canonical?.lag_s || 0) || 0;

    if (runtimeMismatch) return 'runtime_mismatch';
    if (!sessionAuthenticated && startup?.active && startup?.current_step === 'manual_trigger') return 'startup_manual_trigger';
    if (!sessionAuthenticated && startup?.active && startup?.current_step === 'manual_confirm') return 'startup_manual_confirm';
    if (normalizedRecoveryPhase === 'panic_resetting') return 'panic_resetting';
    if (normalizedRecoveryPhase === 'manual_takeover') return 'manual_takeover';
    if (!gatewayActive) return 'gateway_offline';
    if (!runtimeStarted) return 'runtime_stopped';
    if (normalizedTwoFactorStatus === 'waiting_response') {
        if (responsePhase?.showResetCta) return 'waiting_response_reset';
        if (responsePhase?.phase === 'gateway_rejected') return 'waiting_response_gateway_rejected';
        if (responsePhase?.phase === 'submit_failed') return 'waiting_response_submit_failed';
        if (responsePhase?.phase === 'submitted') return 'waiting_response_submitted';
        if (responsePhase?.phase === 'received') return 'waiting_response_received';
        return 'waiting_response';
    }
    if (!sessionAuthenticated) {
        return normalizedTwoFactorStatus === 'waiting_confirm'
            ? 'waiting_confirm'
            : 'session_unauthenticated';
    }
    if (normalizedWarmupPhase === 'failed') return 'warmup_failed';
    if (normalizedWarmupPhase === 'pending' || normalizedWarmupPhase === 'running') return 'warmup_running';
    if (Number(warmup?.trade_symbols_total || 0) > 0 && !warmup?.gate_open) return 'trading_gate_closed';
    if (dataHealth?.last_bar_age_min != null && Number(dataHealth.last_bar_age_min) > 30) return 'bars_stale';
    if (canonicalPending > 0 || canonicalLag > 0) return 'canonical_lagging';
    if (realtimeState?.phase === 'stalled') return 'realtime_stalled';
    if (realtimeState?.phase === 'running') return 'realtime_running';
    if (realtimeState?.phase === 'queued') return 'realtime_queued';
    if (Number(totalEngines || 0) > 0 && Number(readyEngines || 0) < Number(totalEngines || 0)) return 'engines_warming';
    return 'clear';
}

function getIbkrRuntimePrimaryBlockerCardModel({
    runtimeMismatch = null,
    startup = {},
    sessionAuthenticated = false,
    gatewayActive = false,
    runtimeStarted = false,
    responsePhase = {},
    twoFactorStatus = '',
    challengeCode = '',
    twoFactorState = {},
    recoveryPhase = '',
    warmup = {},
    dataHealth = {},
    canonical = {},
    realtimeState = {},
    readyEngines = 0,
    totalEngines = 0,
} = {}) {
    const blocker = {
        tone: 'info',
        kicker: 'Primary Blocker',
        title: '运行链路可控',
        copy: '当前没有硬阻塞，继续观察 bars → indicators → signals 是否连续刷新。'
    };
    const blockerPhase = getIbkrRuntimePrimaryBlockerPhase({
        runtimeMismatch,
        startup,
        sessionAuthenticated,
        gatewayActive,
        runtimeStarted,
        responsePhase,
        twoFactorStatus,
        recoveryPhase,
        warmup,
        dataHealth,
        canonical,
        realtimeState,
        readyEngines,
        totalEngines,
    });

    if (blockerPhase === 'runtime_mismatch') {
        blocker.tone = 'error';
        blocker.title = `Runtime 环境错配 · ${getEnvironmentLabel(runtimeMismatch?.actual)}`;
        blocker.copy = runtimeMismatch?.message || '环境错配';
    } else if (blockerPhase === 'startup_manual_trigger') {
        blocker.tone = startup?.reason === 'weekly_reauth' ? 'warn' : 'info';
        blocker.title = startup?.reason === 'weekly_reauth' ? '本周重登提醒待手动开始' : '启动流程等待手动触发';
        blocker.copy = startup?.operator_action || '现在只需要去飞书点击“开始 2FA 验证”，系统不会自动往下继续。';
    } else if (blockerPhase === 'startup_manual_confirm') {
        blocker.tone = 'warn';
        blocker.title = '等待当前 2FA 轮次完成';
        blocker.copy = startup?.current_blocker || '请继续当前轮次的手机确认或响应码提交流程，不要重复触发。';
    } else if (blockerPhase === 'panic_resetting') {
        blocker.tone = 'warn';
        blocker.title = '正在全量清空旧 2FA 状态';
        blocker.copy = '系统正在停止旧 runtime、清理 cookie 并准备拉起一轮新的干净验证；这期间旧 challenge / response 不再可信。';
    } else if (blockerPhase === 'manual_takeover') {
        blocker.tone = 'warn';
        blocker.title = '人工接管中';
        blocker.copy = '当前视为你正在真实账户里确认挂单；系统不会把这轮直接判死，但后台仍会持续探测认证是否已恢复。';
    } else if (blockerPhase === 'gateway_offline') {
        blocker.tone = 'error';
        blocker.title = 'Gateway 当前离线';
        blocker.copy = '先恢复网关或重启 IBKR 服务，否则 2FA、bars、订单和信号链路都会停住。';
    } else if (blockerPhase === 'runtime_stopped') {
        blocker.tone = 'warn';
        blocker.title = 'Runtime 当前未启动';
        blocker.copy = '这次更像是 runtime service 没有拉起，不是单纯 session pending；重启 compute 后如果没有自动恢复，需要重新触发一次 IBKR start / 2FA。';
    } else if (blockerPhase === 'waiting_response_reset') {
        blocker.tone = 'error';
        blocker.title = '旧 2FA / Session 状态已失配';
        blocker.copy = '当前旧 Challenge / Response 状态已经不可信。不要继续围绕旧轮次重试，请直接执行“放弃当前轮次并干净重开”。';
    } else if (blockerPhase === 'waiting_response_gateway_rejected') {
        blocker.tone = 'error';
        blocker.title = `Gateway 已拒绝当前 Response${challengeCode ? ` · ${challengeCode}` : ''}`;
        blocker.copy = getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
    } else if (blockerPhase === 'waiting_response_submit_failed') {
        blocker.tone = 'error';
        blocker.title = `Response Code 浏览器提交失败${challengeCode ? ` · ${challengeCode}` : ''}`;
        blocker.copy = getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
    } else if (blockerPhase === 'waiting_response_submitted') {
        blocker.tone = 'warn';
        blocker.title = `Response Code 已提交${challengeCode ? ` · ${challengeCode}` : ''}`;
        blocker.copy = getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
    } else if (blockerPhase === 'waiting_response_received') {
        blocker.tone = 'warn';
        blocker.title = `Response Code 已收到${challengeCode ? ` · ${challengeCode}` : ''}`;
        blocker.copy = getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
    } else if (blockerPhase === 'waiting_response') {
        blocker.tone = 'warn';
        blocker.title = `等待 Response Code${challengeCode ? ` · ${challengeCode}` : ''}`;
        blocker.copy = getIbkrTwoFactorWaitingResponseHelperText(twoFactorState);
    } else if (blockerPhase === 'waiting_confirm' || blockerPhase === 'session_unauthenticated') {
        blocker.tone = blockerPhase === 'waiting_confirm' ? 'warn' : 'error';
        blocker.title = blockerPhase === 'waiting_confirm' ? '等待手机确认 2FA' : 'Session 仍未认证';
        blocker.copy = blockerPhase === 'waiting_confirm'
            ? '浏览器已经停在手机确认阶段，确认完成后会自动继续；请继续当前轮次，不要重复点重新验证 / 开始新一轮。'
            : '当前还没有拿到可用 Session，新的 bars / backfill / 订单链路都不会继续刷新。';
    } else if (blockerPhase === 'warmup_failed') {
        blocker.tone = 'error';
        blocker.title = 'Warmup 执行失败';
        blocker.copy = warmup?.last_error || '启动后的预热链路失败，交易闸门保持关闭，需要检查回填与 compute 日志。';
    } else if (blockerPhase === 'warmup_running') {
        blocker.tone = 'warn';
        blocker.title = `Startup Warmup ${Number(warmup?.ready_trade_symbols || 0)}/${Number(warmup?.trade_symbols_total || 0)}`;
        blocker.copy = `正在为 ${String(warmup?.required_interval || '--')} 建立交易预热，blocking ${Number(warmup?.blocking_pending_symbols_total || 0)} · monitor ${Number(warmup?.ready_monitor_symbols || 0)}/${Number(warmup?.monitor_symbols_total || 0)}。`;
    } else if (blockerPhase === 'trading_gate_closed') {
        blocker.tone = 'warn';
        blocker.title = `Trading Gate Closed · ${Number(warmup?.ready_trade_symbols || 0)}/${Number(warmup?.trade_symbols_total || 0)}`;
        blocker.copy = `目标池还没有全部预热完成，pending: ${(Array.isArray(warmup?.blocking_pending_symbols) ? warmup.blocking_pending_symbols : []).slice(0, 4).join(', ') || 'n/a'}。`;
    } else if (blockerPhase === 'bars_stale') {
        blocker.tone = 'error';
        blocker.title = `bars 停在 ${String(dataHealth?.last_bar_label || '--')}`;
        blocker.copy = `最新 live bar 已经落后 ${Number(dataHealth?.last_bar_age_min || 0)} 分钟，需要优先检查订阅、写入器和行情桥。`;
    } else if (blockerPhase === 'canonical_lagging') {
        blocker.tone = 'warn';
        blocker.title = `Canonical 5m 仍在补齐 · ${String(canonical?.last_completed_bucket_us || '--')}`;
        const pendingPreview = formatSymbolPreview(canonical?.pending_symbols, 4);
        blocker.copy = `due ${String(canonical?.last_due_bucket_us || '--')} · completed ${String(canonical?.last_completed_bucket_us || '--')} · pending ${Number(canonical?.pending_symbols_total || 0)}${pendingPreview ? ` · ${pendingPreview}` : ''}。`;
    } else if (blockerPhase === 'realtime_stalled') {
        blocker.tone = 'error';
        blocker.title = realtimeState?.title || 'Indicators 计算已卡住';
        blocker.copy = `bar 已写到 ${String(canonical?.last_completed_bucket_us || '--')}，但 realtime compute 没有完成：${String(realtimeState?.summary || '--')}。`;
    } else if (blockerPhase === 'realtime_running') {
        blocker.tone = realtimeState?.tone || 'warn';
        blocker.title = realtimeState?.title || 'Indicators 正在计算';
        blocker.copy = `bar 已写到 ${String(canonical?.last_completed_bucket_us || '--')}，指标仍在追赶：${String(realtimeState?.summary || '--')}。`;
    } else if (blockerPhase === 'realtime_queued') {
        blocker.tone = 'warn';
        blocker.title = realtimeState?.title || 'Indicators 等待计算';
        blocker.copy = `canonical bar 已写入，但 compute 还在排队：${String(realtimeState?.summary || '--')}。`;
    } else if (blockerPhase === 'engines_warming') {
        blocker.tone = 'warn';
        blocker.title = `Warmup 未完成 ${Number(readyEngines || 0)}/${Number(totalEngines || 0)}`;
        blocker.copy = '指标链路还在预热，ready engines 没起来前，signals 偏少通常是正常现象。';
    }
    return blocker;
}

function getIbkrRuntimeDataChainCardModel({
    latestBar = null,
    latestIndicator = null,
    latestSignal = null,
    dataHealth = {},
    realtimeMetrics = {},
    realtimeState = {},
    canonical = {},
    warmupSummaryText = '',
    inflightAgeS = null,
} = {}) {
    const latestBarExtra = getIbkrExtraObject(latestBar);
    const lastBarAgeMin = Number(dataHealth?.last_bar_age_min);
    const barAgeLabel = Number.isFinite(lastBarAgeMin) ? lastBarAgeMin : 0;

    return {
        tone: Number.isFinite(lastBarAgeMin) && lastBarAgeMin > 30 ? 'warn' : 'info',
        kicker: 'Data Chain',
        title: latestBar
            ? `${latestBar.symbol || '--'} ${formatIbkrIntervalLabel(latestBar.interval)} · ${barAgeLabel}m`
            : '当前没有 live bars',
        copy: [
            latestBar ? `bar ${getIbkrRecordBarLabel(latestBar)}` : 'bars missing',
            latestBar ? `write ${String(getIbkrComputedTimeLabel(latestBar)).slice(0, 19)}` : 'write --',
            realtimeMetrics?.last_bar_close ? `close ${formatTimeLabel(realtimeMetrics.last_bar_close)}` : 'close --',
            realtimeMetrics?.close_delay_s != null ? `close delay ${formatIbkrSecondsLabel(realtimeMetrics.close_delay_s)}` : '',
            realtimeMetrics?.compute_after_close_s != null ? `compute ${formatIbkrSecondsLabel(realtimeMetrics.compute_after_close_s)}` : '',
            canonical?.last_completed_bucket_us ? `canonical ${String(canonical.last_completed_bucket_us)}` : '',
            realtimeState?.phase !== 'idle'
                ? `compute ${String(realtimeState?.phase || '--')} ${String(realtimeState?.phase || '').trim() === 'running' ? formatIbkrSecondsLabel(inflightAgeS) : ''}`.trim()
                : 'compute ready',
            latestBar && !latestBarExtra.computed_at_us && !latestBarExtra.computed_at_cn ? 'bar extra 缺失' : '',
            String(warmupSummaryText || '').trim(),
            latestIndicator
                ? `indicator ${latestIndicator.symbol || '--'} ${formatIbkrIntervalLabel(latestIndicator.interval)} · ${String(getIbkrComputedTimeLabel(latestIndicator)).slice(0, 19)}`
                : 'indicator missing',
            latestSignal
                ? `signal ${latestSignal.symbol || '--'} ${String(latestSignal.direction || '--').toUpperCase()} · ${latestSignal.status || '--'}`
                : 'signal 暂无',
        ].filter(Boolean).join(' · ')
    };
}

function getIbkrRuntimeQuickViewCardModel({
    barsCountLabel = '0',
    indicatorsCountLabel = '0',
    signalsCountLabel = '0',
    latestIndicator = null,
    latestSignal = null,
    environment = '',
} = {}) {
    const latestIndicatorExtra = getIbkrExtraObject(latestIndicator);
    const latestSignalExtra = getIbkrExtraObject(latestSignal);

    return {
        tone: 'ok',
        kicker: 'Quick View',
        title: '把排查入口放在一层',
        copy: [
            `bars ${String(barsCountLabel || '0')}`,
            `indicators ${String(indicatorsCountLabel || '0')}`,
            `signals ${String(signalsCountLabel || '0')}`,
            latestIndicatorExtra.computed_at_us ? `latest calc ${String(getIbkrComputedTimeLabel(latestIndicator)).slice(0, 19)}` : '',
            latestSignalExtra.computed_at_us ? `signal calc ${String(getIbkrComputedTimeLabel(latestSignal)).slice(0, 19)}` : '',
        ].filter(Boolean).join(' · '),
        links: [
            { path: '/ibkr_signals.html', label: '看信号' },
            { path: '/ibkr_indicators.html', label: '看指标' },
            { path: '/orders.html', label: '看订单' },
            { path: '/ibkr_config.html', label: '改配置', options: { allowGlobal: true, environment } },
        ]
    };
}

const IBKR_CONFIG_DETAIL_KEYS = [
    'ibkr_compute_public_url',
    'ibkr_compute_enabled',
    'pb_scheduler_enabled',
    'ibkr_bar_publish_enabled',
    'ibkr_trading_enabled',
    'ibkr_signal_source',
    'signal_manual_confirm_enabled',
    'position_limit_max',
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
    'order_validity_minutes',
    'signal_poll_interval_sec',
    'reverse_signal_threshold'
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
        { label: 'PB SCHEDULER', value: configMap?.pb_scheduler_enabled ?? '--' },
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

    if (configMap.ibkr_compute_public_url) {
        items.push({ label: 'COMPUTE URL', value: configMap.ibkr_compute_public_url });
    }

    getOrderedIbkrConfigEntries(configMap).forEach(({ key, value }) => {
        if (key === 'ibkr_compute_public_url' || IBKR_SYSTEM_PRIMARY_DETAIL_KEYS.has(key)) return;
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
        title: definition.display_name || definition.config_display_name || definition.id || 'PB Cron',
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
