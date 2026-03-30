/// <reference path="./pb_data/types.d.ts" />

/**
 * order_scheduler.pb.js
 * 定时检查订单有效期，超时自动标记为 Canceled
 * 读取 config 表中 order_validity_minutes 配置（默认 30 分钟）
 */

const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderEventTimes } = require(`${__hooks}/lib/order_events.js`)
const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)

cronAdd("order_expiry_check", "* * * * *", () => {
    // 检查 PB 定时调度开关
    try {
        const schedulerConfig = $app.findFirstRecordByFilter("config", "key = 'pb_scheduler_enabled'")
        const rawValue = schedulerConfig ? schedulerConfig.get("value") : ""
        const normalizedValue = String(rawValue || "").trim().toUpperCase()
        if (normalizedValue === "FALSE" || normalizedValue === "0" || normalizedValue === "OFF") {
            console.log(`[OrderScheduler] pb_scheduler_enabled="${normalizedValue}", 跳过执行`)
            return
        }
    } catch (err) {
        console.log("[OrderScheduler] pb_scheduler_enabled 配置读取失败，继续执行")
    }

    // 读取订单有效期配置（分钟）
    let validityMinutes = 30
    try {
        const configRecord = $app.findFirstRecordByFilter("config", "key = 'order_validity_minutes'")
        if (configRecord) {
            const val = parseInt(configRecord.get("value"))
            if (!isNaN(val) && val > 0) {
                validityMinutes = val
                console.log(`[OrderScheduler] order_validity_minutes 配置值: ${validityMinutes}`)
            }
        } else {
            console.log(`[OrderScheduler] order_validity_minutes 配置不存在，使用默认值: ${validityMinutes}`)
        }
    } catch (err) {
        console.log(`[OrderScheduler] order_validity_minutes 配置读取失败: ${err}`)
    }

    // 计算截止时间（毫秒时间戳）
    const cutoffMs = Date.now() - validityMinutes * 60 * 1000

    // 查找所有超时的 Init / Submitted 订单（仅 Entry 类型）
    let expiredOrders
    try {
        expiredOrders = $app.findRecordsByFilter(
            "orders",
            `(status = 'Init' || status = 'Submitted') && bar_time_ms <= {:cutoffMs}`,
            "-created",
            100,
            0,
            { cutoffMs: cutoffMs }
        )
    } catch (err) {
        console.error("[OrderScheduler] 查询订单失败:", err)
        return
    }

    if (!expiredOrders || expiredOrders.length === 0) {
        console.log(`[OrderScheduler] 无过期订单（有效期 ${validityMinutes} 分钟）`)
        return
    }

    let count = 0
    for (const record of expiredOrders) {
        try {
            const oldStatus = record.get("status")
            const uniqueId = record.get("unique_id")
            console.log(`[OrderScheduler] 命中过期订单: unique_id=${uniqueId}, status=${oldStatus}, validity_minutes=${validityMinutes}, cutoff_ms=${cutoffMs}, bar_time_ms=${record.get("bar_time_ms")}`)
            const eventTimes = applyOrderEventTimes(record)
            record.set("status", "Canceled")
            $app.save(record)
            appendOrderDetail(record, {
                status: "Canceled",
                source: "order_scheduler",
                reason: `订单超时自动取消（有效期 ${validityMinutes} 分钟）`,
                us_time: eventTimes.us_time,
                cn_time: eventTimes.cn_time,
                bar_time_ms: eventTimes.bar_time_ms,
                extra: {
                    previous_status: oldStatus,
                    validity_minutes: validityMinutes,
                    cutoff_ms: cutoffMs,
                },
            })
            const orderExtra = getOrderExtra(record)
            const syncResult = notifyOrder("canceled", record, {
                messageId: orderExtra.feishu_order_message_id || "",
                message: `订单超时自动取消（有效期 ${validityMinutes} 分钟）`,
            })
            if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
                mergeOrderExtra(record, {
                    feishu_order_message_id: syncResult.message_id,
                    feishu_order_card_version: 1,
                }, true)
            }
            console.log(`[OrderScheduler] 订单已取消: ${record.get("unique_id")}, 原状态: ${oldStatus}`)
            count++
        } catch (err) {
            console.error("[OrderScheduler] 更新订单失败:", record.id, err)
        }
    }

    if (count > 0) {
        console.log(`[OrderScheduler] 已将 ${count} 条订单标记为 Canceled（有效期 ${validityMinutes} 分钟）`)
    }
})
