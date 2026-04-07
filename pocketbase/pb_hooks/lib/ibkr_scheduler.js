const COMPUTE_STARTUP_GRACE_MS = 3 * 60 * 1000

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
