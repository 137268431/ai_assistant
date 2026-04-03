/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_scheduler.pb.js
 * PocketBase 定时调度 -> 触发 ibkr_compute Python 服务
 */

console.log("[IBKRScheduler] Hook file loading...")

function getEnabledEnvironments() {
    const { getEnabledRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    return getEnabledRuntimeEnvironments("ibkr_compute_enabled", "true", ["ibkr_compute_enabled", "pb_scheduler_enabled"])
}

function postToCompute(action, timeoutSeconds) {
    const environments = getEnabledEnvironments()
    if (!environments.length) {
        console.log(`[IBKRScheduler] ${action}: no enabled environments, skip`)
        return
    }
    const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
    const computeBaseUrl = getIbkrComputePublicUrl(environments[0], "https://qc.lzw-glory.top")

    try {
        const resp = $http.send({
            url: `${computeBaseUrl}/${action}`,
            method: "POST",
            body: JSON.stringify({ source: "cron", environments: environments }),
            headers: { "Content-Type": "application/json" },
            timeout: timeoutSeconds,
        })
        if (resp.statusCode !== 200) {
            console.error(`[IBKRScheduler] ${action} returned ${resp.statusCode}: ${resp.raw}`)
            return
        }

        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        console.log(`[IBKRScheduler] ${action}: environments=${(payload.environments || environments).join(",")}, ok=${payload.ok !== false}`)
    } catch (err) {
        console.error(`[IBKRScheduler] ${action} error: ${err.message}`)
    }
}

cronAdd("ibkr_compute", "* 4-20 * * 1-5", () => {
    postToCompute("compute", 30)
})

cronAdd("ibkr_scan", "*/5 7-9 * * 1-5", () => {
    postToCompute("scan", 60)
})

console.log("[IBKRScheduler] Hook file loaded")
