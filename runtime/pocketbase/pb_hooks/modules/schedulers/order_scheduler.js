/// <reference path="./pb_data/types.d.ts" />

/**
 * order_scheduler.pb.js
 * `order_expiry_check` / `order_detail_integrity_guard` 已迁到 ibkr-scheduler，
 * 这里仅保留兼容转发。
 */

cronAdd("order_expiry_check", "*/5 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)

    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("order_expiry_check", {
            environment: environment,
            timeout: 120,
            logPrefix: "[OrderScheduler]",
        })
    }
})

cronAdd("order_detail_integrity_guard", "*/10 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { proxySchedulerCronJob } = require(`${__hooks}/lib/system/scheduler_proxy.js`)

    for (const environment of getRuntimeEnvironments()) {
        proxySchedulerCronJob("order_detail_integrity_guard", {
            environment: environment,
            timeout: 120,
            logPrefix: "[OrderDetailIntegrity]",
        })
    }
})
