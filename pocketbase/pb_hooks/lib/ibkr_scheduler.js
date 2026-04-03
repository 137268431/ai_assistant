function getIbkrSchedulerEnvironments() {
    const { getConfigValue, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { getActiveRuntimeEnvironments, isEnabledConfigValue } = require(`${__hooks}/lib/runtime_modes.js`)
    const environments = getActiveRuntimeEnvironments(["ibkr_compute_enabled", "pb_scheduler_enabled"])

    return environments.filter((environment) => {
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

function runIbkrScheduledAction(action, timeoutSeconds, logPrefix) {
    const prefix = logPrefix || "[IBKRComputeCron]"
    let environments = []

    try {
        environments = getIbkrSchedulerEnvironments()
    } catch (err) {
        console.error(`${prefix} ${action}: environment resolve error: ${err.message}`)
        return { ok: false, error: err.message || String(err), environments: [] }
    }

    if (!environments.length) {
        console.log(`${prefix} ${action}: no enabled environments, skip`)
        return { ok: true, skipped: true, environments: [] }
    }

    try {
        const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
        const computeBaseUrl = getIbkrComputePublicUrl(environments[0], "https://qc.lzw-glory.top")
        const upstream = `${computeBaseUrl}/${action}`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify({ source: "cron", environments: environments }),
            headers: { "Content-Type": "application/json" },
            timeout: timeoutSeconds || 30,
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
