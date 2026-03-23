/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_expiry.pb.js
 * 定时检查信号有效期，超时自动标记为 expired
 * 读取 config 表中 signal_validity_minutes 配置
 */

cronAdd("signal_expiry_check", "* * * * *", () => {
    // 读取有效期配置（分钟）
    let validityMinutes = 30  // 默认 30 分钟
    try {
        const configRecord = $app.findFirstRecordByFilter("config", "key = 'signal_validity_minutes'")
        const val = parseInt(configRecord.get("value"))
        if (!isNaN(val) && val > 0) {
            validityMinutes = val
        }
    } catch (_) {
        // 配置不存在，使用默认值
    }

    // 计算截止时间（毫秒时间戳）
    const cutoff = new Date(Date.now() - validityMinutes * 60 * 1000)
    const cutoffMs = cutoff.getTime()

    // 查找所有超时的 pending / awaiting_confirm 信号
    let expired
    try {
        expired = $app.findRecordsByFilter(
            "signals",
            `(status = 'pending' || status = 'awaiting_confirm') && bar_time_ms <= {:cutoffMs}`,
            "-created", 100, 0,
            { cutoffMs: cutoffMs }
        )
    } catch (err) {
        console.error("[SignalExpiry] 查询信号失败:", err)
        return
    }

    if (!expired || expired.length === 0) {
        console.log(`[SignalExpiry] 无过期信号（有效期 ${validityMinutes} 分钟）`)
        return
    }

    let count = 0
    for (const record of expired) {
        try {
            record.set("status", "expired")
            $app.save(record)
            count++
        } catch (err) {
            console.error("[SignalExpiry] 更新信号失败:", record.id, err)
        }
    }

    if (count > 0) {
        console.log(`[SignalExpiry] 已将 ${count} 条信号标记为 expired（有效期 ${validityMinutes} 分钟）`)
    }
})
