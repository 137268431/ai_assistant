/// <reference path="./pb_data/types.d.ts" />

/**
 * order_scheduler.pb.js
 * 定时检查订单有效期，超时自动标记为 Canceled
 * 读取 config 表中 order_validity_minutes 配置（默认 30 分钟）
 */

function orderSchedulerParseHttpJson(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { raw: raw }
    }
}

function orderSchedulerResolveCancelableOrderId(record) {
    if (!record) return ""
    const extra = record.get("extra") || {}
    const candidates = [
        String(record.get("broker_order_id") || "").trim(),
        String(record.get("order_id") || "").trim(),
        String(extra && extra.broker_order_id || "").trim(),
        String(extra && extra.order_id || "").trim(),
    ]
    for (let i = 0; i < candidates.length; i++) {
        if (/^\d+$/.test(candidates[i])) {
            return candidates[i]
        }
    }
    return ""
}

function orderSchedulerCancelFailureLooksClosed(payload) {
    const errorText = String(
        (payload && (payload.error || payload.message || payload.raw)) || ""
    ).trim().toLowerCase()
    if (!errorText) return false
    return (
        errorText.includes("already canceled")
        || errorText.includes("already cancelled")
        || errorText.includes("already inactive")
        || errorText.includes("not active")
        || errorText.includes("inactive")
        || errorText.includes("not found")
        || errorText.includes("cannot be cancelled")
        || errorText.includes("cannot be canceled")
        || errorText.includes("filled")
    )
}

function orderSchedulerCancelBrokerOrder(environment, orderId) {
    const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/orders/cancel`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify({
                order_id: String(orderId || "").trim(),
                environment: environment,
            }),
            headers: { "Content-Type": "application/json" },
        })
        const payload = orderSchedulerParseHttpJson(resp.raw)
        const ok = !!(payload && payload.ok)
        return {
            ok: ok || orderSchedulerCancelFailureLooksClosed(payload),
            statusCode: Number(resp && resp.statusCode) || 200,
            payload: payload,
            upstream: upstream,
        }
    } catch (err) {
        const payload = { ok: false, error: err.message || String(err) }
        return {
            ok: orderSchedulerCancelFailureLooksClosed(payload),
            statusCode: 502,
            payload: payload,
            upstream: upstream,
        }
    }
}

cronAdd("order_expiry_check", "*/5 * * * *", () => {
    const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
    const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
    const { getConfigValue } = require(`${__hooks}/lib/environment.js`)
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    let totalCount = 0

    for (const environment of getRuntimeEnvironments()) {
        const cronState = getPbCronToggleState("order_expiry_check", environment)
        if (!cronState.effective_enabled) {
            console.log(`[OrderScheduler] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
            continue
        }
        let validityMinutes = parseInt(getConfigValue("order_validity_minutes", "30", environment), 10)
        if (!Number.isFinite(validityMinutes) || validityMinutes <= 0) {
            validityMinutes = 30
        }

        const cutoffMs = Date.now() - validityMinutes * 60 * 1000

        let expiredOrders
        try {
            // Only expire entry orders (DAY orders). TP/SL child orders are GTC and must not be auto-expired.
            expiredOrders = $app.findRecordsByFilter(
                "orders",
                `(status = 'Init' || status = 'Submitted') && environment = {:env} && bar_time_ms <= {:cutoffMs} && (role = 'entry' || role = '')`,
                "-created",
                100,
                0,
                { env: environment, cutoffMs: cutoffMs }
            )
        } catch (err) {
            console.error(`[OrderScheduler] ${environment}: 查询订单失败:`, err)
            continue
        }

        if (!expiredOrders || expiredOrders.length === 0) {
            console.log(`[OrderScheduler] ${environment}: 无过期订单（有效期 ${validityMinutes} 分钟）`)
            continue
        }

        let environmentCount = 0
        const cancelResultByOrderId = {}
        for (const record of expiredOrders) {
            try {
                const oldStatus = record.get("status")
                const uniqueId = record.get("unique_id")
                const cancelOrderId = orderSchedulerResolveCancelableOrderId(record)
                console.log(`[OrderScheduler] ${environment}: 命中过期订单: unique_id=${uniqueId}, status=${oldStatus}, validity_minutes=${validityMinutes}, cutoff_ms=${cutoffMs}, bar_time_ms=${record.get("bar_time_ms")}`)

                if (cancelOrderId) {
                    if (!cancelResultByOrderId[cancelOrderId]) {
                        cancelResultByOrderId[cancelOrderId] = orderSchedulerCancelBrokerOrder(environment, cancelOrderId)
                    }
                    const cancelResult = cancelResultByOrderId[cancelOrderId]
                    if (!cancelResult.ok) {
                        console.error(
                            `[OrderScheduler] ${environment}: IBKR 撤单失败，跳过本地取消: unique_id=${uniqueId}, order_id=${cancelOrderId}, error=${(cancelResult.payload && (cancelResult.payload.error || cancelResult.payload.message)) || "unknown"}`
                        )
                        continue
                    }
                }

                record.set("status", "Canceled")
                applyOrderRelationship(record, {
                    relation_status: "closed",
                    status: "Canceled",
                }, false)
                const metaResult = applyOrderStatusMeta(record, {
                    status: "Canceled",
                    previous_status: oldStatus,
                    source: "order_scheduler",
                    reason: `订单超时自动取消（有效期 ${validityMinutes} 分钟）`,
                }, false)
                const eventTimes = metaResult.eventTimes
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
                        cancelled_broker_order_id: cancelOrderId,
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
                console.log(`[OrderScheduler] ${environment}: 订单已取消: ${record.get("unique_id")}, 原状态: ${oldStatus}`)
                environmentCount++
                totalCount++
            } catch (err) {
                console.error(`[OrderScheduler] ${environment}: 更新订单失败:`, record.id, err)
            }
        }

        if (environmentCount > 0) {
            console.log(`[OrderScheduler] ${environment}: 已将 ${environmentCount} 条订单标记为 Canceled（有效期 ${validityMinutes} 分钟）`)
        }
    }

    if (totalCount > 0) {
        console.log(`[OrderScheduler] 全环境共处理 ${totalCount} 条过期订单`)
    }
})
