/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_expiry.pb.js
 * 兼容壳：PocketBase 仍注册 cron，但实际执行已迁到 ibkr-scheduler。
 */

cronAdd("signal_expiry_check", "*/5 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)

    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("signal_expiry_check", {
            environment: environment,
            timeout: 90,
            logPrefix: "[SignalExpiry]",
        })
    }
})
