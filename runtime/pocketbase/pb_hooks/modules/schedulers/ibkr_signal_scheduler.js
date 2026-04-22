/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_expiry.pb.js
 * 兼容壳：PocketBase 仍注册 cron，但实际执行已迁到 ibkr-scheduler。
 */

function schedulerSlotToken() {
    const now = new Date()
    const year = now.getUTCFullYear()
    const month = String(now.getUTCMonth() + 1).padStart(2, "0")
    const day = String(now.getUTCDate()).padStart(2, "0")
    const hour = String(now.getUTCHours()).padStart(2, "0")
    const minute = String(now.getUTCMinutes()).padStart(2, "0")
    return `${year}-${month}-${day}T${hour}:${minute}Z`
}

cronAdd("signal_expiry_check", "*/5 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const { forwardIbkrSchedulerRequest } = require(`${__hooks}/lib/system/api_proxy.js`)

    for (const environment of getRuntimeEnvironments()) {
        const cronState = getPbCronToggleState("signal_expiry_check", environment)
        if (!cronState.effective_enabled) {
            console.log(`[SignalExpiry] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
            continue
        }
        try {
            const result = forwardIbkrSchedulerRequest("/jobs/run/signal_expiry_check", {
                method: "POST",
                environment: environment,
                timeout: 90,
                body: {
                    environment: environment,
                    trigger_source: "pb_compat",
                    scheduled_slot: schedulerSlotToken(),
                },
            })
            const payload = result.payload && typeof result.payload === "object" ? result.payload : {}
            console.log(
                `[SignalExpiry] ${environment}: scheduler_proxy ok=${payload.ok === true} skipped=${payload.skipped === true} reason=${payload.reason || "-"} upstream=${result.upstream}`
            )
        } catch (err) {
            console.log(`[SignalExpiry] ${environment}: scheduler proxy error: ${err && err.message ? err.message : err}`)
        }
    }
})
