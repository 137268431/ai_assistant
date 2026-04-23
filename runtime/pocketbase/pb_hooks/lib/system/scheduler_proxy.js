function schedulerSlotToken(nowValue) {
    const now = nowValue instanceof Date ? nowValue : new Date()
    const year = now.getUTCFullYear()
    const month = String(now.getUTCMonth() + 1).padStart(2, "0")
    const day = String(now.getUTCDate()).padStart(2, "0")
    const hour = String(now.getUTCHours()).padStart(2, "0")
    const minute = String(now.getUTCMinutes()).padStart(2, "0")
    return `${year}-${month}-${day}T${hour}:${minute}Z`
}

function proxySchedulerCronJob(jobId, options) {
    const opts = options && typeof options === "object" ? options : {}
    const environment = String(opts.environment || "live").trim().toLowerCase() || "live"
    const timeout = Number(opts.timeout) > 0 ? Number(opts.timeout) : 90
    const logPrefix = String(opts.logPrefix || `[${jobId}]`)
    const triggerSource = String(opts.triggerSource || "pb_compat").trim() || "pb_compat"
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const { forwardIbkrSchedulerRequest } = require(`${__hooks}/lib/system/api_proxy.js`)

    const cronState = opts.cronState && typeof opts.cronState === "object"
        ? opts.cronState
        : getPbCronToggleState(jobId, environment)
    if (!cronState.effective_enabled) {
        console.log(`${logPrefix} ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
        return {
            ok: true,
            skipped: true,
            reason: "disabled",
            environment: environment,
            cron_state: cronState,
        }
    }

    try {
        const result = forwardIbkrSchedulerRequest(`/jobs/run/${jobId}`, {
            method: "POST",
            environment: environment,
            timeout: timeout,
            body: {
                environment: environment,
                trigger_source: triggerSource,
                scheduled_slot: String(opts.scheduledSlot || schedulerSlotToken()).trim() || schedulerSlotToken(),
                ...(opts.body && typeof opts.body === "object" ? opts.body : {}),
            },
        })
        const payload = result.payload && typeof result.payload === "object" ? result.payload : {}
        console.log(
            `${logPrefix} ${environment}: scheduler_proxy ok=${payload.ok === true} skipped=${payload.skipped === true} reason=${payload.reason || "-"} upstream=${result.upstream}`
        )
        return {
            ok: result.ok,
            statusCode: result.statusCode,
            payload: payload,
            upstream: result.upstream,
            environment: environment,
            cron_state: cronState,
        }
    } catch (err) {
        const message = err && err.message ? err.message : String(err || "")
        console.log(`${logPrefix} ${environment}: scheduler proxy error: ${message}`)
        return {
            ok: false,
            statusCode: 502,
            error: message,
            environment: environment,
            cron_state: cronState,
        }
    }
}

module.exports = {
    schedulerSlotToken,
    proxySchedulerCronJob,
}
