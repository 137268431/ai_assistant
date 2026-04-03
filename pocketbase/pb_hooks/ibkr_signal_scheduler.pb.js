/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_expiry.pb.js
 * 定时检查信号有效期，超时自动标记为 expired
 * 读取 config 表中 signal_validity_minutes 配置
 */

cronAdd("signal_expiry_check", "*/5 * * * *", () => {
    const { getSignalExtra, mergeSignalExtra, notifySignalStatus } = require(`${__hooks}/lib/feishu_signal.js`)
    const { getConfigValue } = require(`${__hooks}/lib/environment.js`)
    const { getRuntimeEnvironments, isEnabledConfigValue } = require(`${__hooks}/lib/runtime_modes.js`)

    let totalCount = 0

    for (const environment of getRuntimeEnvironments()) {
        const schedulerEnabled = String(getConfigValue("pb_scheduler_enabled", "true", environment) || "").trim().toUpperCase()
        if (!isEnabledConfigValue(schedulerEnabled)) {
            console.log(`[SignalExpiry] ${environment}: pb_scheduler_enabled="${schedulerEnabled}", 跳过执行`)
            continue
        }

        let validityMinutes = parseInt(getConfigValue("signal_validity_minutes", "30", environment), 10)
        if (!Number.isFinite(validityMinutes) || validityMinutes <= 0) {
            validityMinutes = 30
        }

        const cutoffMs = Date.now() - validityMinutes * 60 * 1000

        let expired
        try {
            expired = $app.findRecordsByFilter(
                "ibkr_signals",
                `(status = 'pending' || status = 'awaiting_confirm') && environment = {:env} && bar_time_ms <= {:cutoffMs}`,
                "-created", 100, 0,
                { env: environment, cutoffMs: cutoffMs }
            )
        } catch (err) {
            console.error(`[SignalExpiry] ${environment}: 查询信号失败:`, err)
            continue
        }

        if (!expired || expired.length === 0) {
            console.log(`[SignalExpiry] ${environment}: 无过期信号（有效期 ${validityMinutes} 分钟）`)
            continue
        }

        let environmentCount = 0
        for (const record of expired) {
            try {
                const oldStatus = record.get("status")
                record.set("status", "expired")
                $app.save(record)
                const signalExtra = getSignalExtra(record)
                const syncResult = notifySignalStatus("expired", record, {
                    messageId: signalExtra.feishu_signal_message_id || "",
                    message: `信号超时自动失效（有效期 ${validityMinutes} 分钟）`,
                })
                if (syncResult.success && syncResult.message_id && syncResult.message_id !== signalExtra.feishu_signal_message_id) {
                    mergeSignalExtra(record, {
                        feishu_signal_message_id: syncResult.message_id,
                        feishu_signal_card_version: 1,
                    }, true)
                }
                console.log(`[SignalExpiry] ${environment}: 信号已过期: signal_id=${record.get("signal_id")}, previous_status=${oldStatus}, message_id=${signalExtra.feishu_signal_message_id || "-"}`)
                environmentCount++
                totalCount++
            } catch (err) {
                console.error(`[SignalExpiry] ${environment}: 更新信号失败:`, record.id, err)
            }
        }

        if (environmentCount > 0) {
            console.log(`[SignalExpiry] ${environment}: 已将 ${environmentCount} 条信号标记为 expired（有效期 ${validityMinutes} 分钟）`)
        }
    }

    if (totalCount > 0) {
        console.log(`[SignalExpiry] 全环境共处理 ${totalCount} 条过期信号`)
    }
})
