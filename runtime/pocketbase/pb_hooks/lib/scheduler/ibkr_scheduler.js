const COMPUTE_STARTUP_GRACE_MS = 3 * 60 * 1000
const CRON_REALTIME_SKIP_MS = 2 * 60 * 1000

function getIbkrSchedulerEnvironments(cronId) {
    const { getConfigValue, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { getActiveRuntimeEnvironments, isEnabledConfigValue } = require(`${__hooks}/lib/runtime_modes.js`)
    const { isPbCronEnabled } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const environments = getActiveRuntimeEnvironments(["ibkr_compute_enabled", "pb_scheduler_enabled"])

    return environments.filter((environment) => {
        if (cronId && !isPbCronEnabled(cronId, environment)) {
            return false
        }
        const schedulerEnabled = isEnabledConfigValue(getConfigValue("pb_scheduler_enabled", "true", environment))
        const defaultCompute = environment === BACKTEST_ENVIRONMENT ? "false" : "true"
        const computeEnabled = isEnabledConfigValue(getConfigValue("ibkr_compute_enabled", defaultCompute, environment))
        return schedulerEnabled && computeEnabled
    })
}

function parseSchedulerPayload(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}

function fetchSchedulerJson(url, timeoutSeconds) {
    try {
        const resp = $http.send({
            url: url,
            method: "GET",
            timeout: timeoutSeconds || 5,
        })
        return {
            ok: resp.statusCode === 200,
            status: resp.statusCode,
            payload: parseSchedulerPayload(resp.raw),
        }
    } catch (err) {
        return {
            ok: false,
            status: 0,
            error: err.message || String(err),
            payload: {},
        }
    }
}

function getRealtimePriorityState(runtimePayload) {
    const payload = runtimePayload && typeof runtimePayload === "object" ? runtimePayload : {}
    const session = payload.session && typeof payload.session === "object" ? payload.session : {}
    const websocket = payload.websocket && typeof payload.websocket === "object" ? payload.websocket : {}
    const realtime = payload.realtime_compute && typeof payload.realtime_compute === "object" ? payload.realtime_compute : {}
    const canonical5m = payload.canonical_5m && typeof payload.canonical_5m === "object" ? payload.canonical_5m : {}
    const marketUniverse = payload.market_universe && typeof payload.market_universe === "object" ? payload.market_universe : {}
    const activeSymbols = Number(marketUniverse.active_target_count || 0) || 0
    const queueSize = Number(realtime.queue_size || 0) || 0
    const lastRunText = String(realtime.last_run || "").trim()
    const lastRunMs = lastRunText ? Date.parse(lastRunText) : NaN
    const recentRealtimeRun = Number.isFinite(lastRunMs) && (Date.now() - lastRunMs) <= CRON_REALTIME_SKIP_MS
    const canonicalEnabled = canonical5m.enabled !== false
    const pendingSymbols = Number(canonical5m.pending_symbols_total || 0) || 0
    const lastCanonicalRunText = String(canonical5m.last_run || "").trim()
    const lastCanonicalRunMs = lastCanonicalRunText ? Date.parse(lastCanonicalRunText) : NaN
    const recentCanonicalRun = Number.isFinite(lastCanonicalRunMs) && (Date.now() - lastCanonicalRunMs) <= CRON_REALTIME_SKIP_MS
    const runtimeStreamingActive = Boolean(session.authenticated && websocket.connected && activeSymbols > 0)
    const canonicalHealthy = canonicalEnabled && pendingSymbols === 0 && recentCanonicalRun
    return {
        skip: runtimeStreamingActive && (queueSize > 0 || recentRealtimeRun || canonicalHealthy),
        reason: runtimeStreamingActive ? "realtime_priority_active" : "",
        queue_size: queueSize,
        active_symbols_visible: activeSymbols,
        recent_realtime_run: recentRealtimeRun,
        recent_canonical_run: recentCanonicalRun,
        canonical_enabled: canonicalEnabled,
        canonical_pending_symbols: pendingSymbols,
        websocket_connected: Boolean(websocket.connected),
        authenticated: Boolean(session.authenticated),
        last_run: lastRunText || "",
        last_canonical_run: lastCanonicalRunText || "",
    }
}

function runIbkrScheduledAction(action, timeoutSeconds, logPrefix, cronId) {
    const prefix = logPrefix || "[IBKRComputeCron]"
    let environments = []

    try {
        environments = getIbkrSchedulerEnvironments(cronId)
    } catch (err) {
        console.error(`${prefix} ${action}: environment resolve error: ${err.message}`)
        return { ok: false, error: err.message || String(err), environments: [] }
    }

    if (!environments.length) {
        console.log(`${prefix} ${action}: no enabled environments, skip`)
        return { ok: true, skipped: true, environments: [] }
    }

    try {
        const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
        const computeBaseUrl = getIbkrComputeInternalUrl(environments[0], "http://127.0.0.1:5100")
        const effectiveTimeout = action === "compute"
            ? Math.max(Number(timeoutSeconds || 0) || 0, 90)
            : (timeoutSeconds || 30)

        if (action === "compute") {
            const healthSnapshot = fetchSchedulerJson(`${computeBaseUrl}/health`, 5)
            const runtimeSnapshot = fetchSchedulerJson(`${computeBaseUrl}/ibkr/status`, 5)
            const uptimeS = Number(healthSnapshot.payload && healthSnapshot.payload.uptime_s || 0) || 0
            const runtimeStarting = Boolean(runtimeSnapshot.payload && runtimeSnapshot.payload.starting === true)
            const warmupPhase = String(runtimeSnapshot.payload && runtimeSnapshot.payload.warmup && runtimeSnapshot.payload.warmup.phase || "").trim().toLowerCase()
            const startupGraceActive = (
                runtimeStarting
                || warmupPhase === "pending"
                || warmupPhase === "running"
                || (uptimeS > 0 && (uptimeS * 1000) < COMPUTE_STARTUP_GRACE_MS)
            )
            if (startupGraceActive) {
                console.log(
                    `${prefix} ${action}: startup grace active, skip dispatch, uptime_s=${Math.round(uptimeS)}, runtime_starting=${runtimeStarting}, warmup_phase=${warmupPhase || "-"}`
                )
                return {
                    ok: true,
                    skipped: true,
                    reason: "startup_grace",
                    environments: environments,
                    upstream: `${computeBaseUrl}/${action}`,
                    uptime_s: uptimeS,
                    runtime_starting: runtimeStarting,
                    warmup_phase: warmupPhase,
                }
            }

            const realtimePriority = getRealtimePriorityState(runtimeSnapshot.payload)
            if (realtimePriority.skip) {
                console.log(
                    `${prefix} ${action}: realtime priority active, skip dispatch, queue_size=${realtimePriority.queue_size}, active_symbols=${realtimePriority.active_symbols_visible}, last_run=${realtimePriority.last_run || "-"}, websocket=${realtimePriority.websocket_connected}, authenticated=${realtimePriority.authenticated}`
                )
                return {
                    ok: true,
                    skipped: true,
                    reason: realtimePriority.reason,
                    environments: environments,
                    upstream: `${computeBaseUrl}/${action}`,
                    realtime_priority: realtimePriority,
                }
            }
        }

        const upstream = `${computeBaseUrl}/${action}`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify({ source: "cron", environments: environments }),
            headers: { "Content-Type": "application/json" },
            timeout: effectiveTimeout,
        })
        const payload = parseSchedulerPayload(resp.raw)

        if (resp.statusCode !== 200) {
            console.error(`${prefix} ${action}: upstream=${upstream}, status=${resp.statusCode}, raw=${resp.raw}`)
            return {
                ok: false,
                status: resp.statusCode,
                upstream: upstream,
                environments: environments,
                payload: payload,
            }
        }

        console.log(
            `${prefix} ${action}: upstream=${upstream}, environments=${(payload.environments || environments).join(",")}, ok=${payload.ok !== false}`
        )
        return {
            ok: payload.ok !== false,
            upstream: upstream,
            environments: payload.environments || environments,
            payload: payload,
        }
    } catch (err) {
        console.error(`${prefix} ${action}: ${err.message}`)
        return { ok: false, error: err.message || String(err), environments: environments }
    }
}

module.exports = {
    getIbkrSchedulerEnvironments,
    runIbkrScheduledAction,
}
