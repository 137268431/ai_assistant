/// <reference path="./pb_data/types.d.ts" />

/**
 * order_scheduler.pb.js
 * `order_expiry_check` 仍在 PB 侧兼容运行；
 * `order_detail_integrity_guard` 已迁到 ibkr-scheduler，这里只保留兼容转发。
 */

cronAdd("order_expiry_check", "*/5 * * * *", () => {
    const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
    const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
    const { getConfigValue, getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const orderExpiry = require(`${__hooks}/lib/order_expiry_scheduler.js`)
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

        const result = orderExpiry.processExpiredOrders({
            records: expiredOrders,
            environment: environment,
            validityMinutes: validityMinutes,
            cutoffMs: cutoffMs,
            app: $app,
            findRelatedRecords: (tradeGroupId) => {
                try {
                    return $app.findRecordsByFilter(
                        "orders",
                        "(trade_group_id = {:gid} || entry_order_unique_id = {:gid} || unique_id = {:gid}) && environment = {:env}",
                        "-created",
                        100,
                        0,
                        { gid: tradeGroupId, env: environment }
                    ) || []
                } catch (err) {
                    console.error(`[OrderScheduler] ${environment}: 查询交易组失败: trade_group_id=${tradeGroupId}`, err)
                    return []
                }
            },
            appendOrderDetail: appendOrderDetail,
            getOrderExtra: getOrderExtra,
            mergeOrderExtra: mergeOrderExtra,
            applyOrderStatusMeta: applyOrderStatusMeta,
            applyOrderRelationship: applyOrderRelationship,
            notifyOrder: notifyOrder,
            logger: console,
            cancelBrokerOrder: (currentEnvironment, orderId) => {
                const upstream = `${getIbkrComputeInternalUrl(currentEnvironment, "http://127.0.0.1:5100")}/ibkr/orders/cancel`
                try {
                    const resp = $http.send({
                        url: upstream,
                        method: "POST",
                        timeout: 30,
                        body: JSON.stringify({
                            order_id: String(orderId || "").trim(),
                            environment: currentEnvironment,
                        }),
                        headers: { "Content-Type": "application/json" },
                    })
                    const payload = orderExpiry.parseHttpJson(resp.raw)
                    const ok = !!(payload && payload.ok)
                    return {
                        ok: ok || orderExpiry.cancelFailureLooksClosed(payload),
                        statusCode: Number(resp && resp.statusCode) || 200,
                        payload: payload,
                        upstream: upstream,
                    }
                } catch (err) {
                    const payload = { ok: false, error: err.message || String(err) }
                    return {
                        ok: orderExpiry.cancelFailureLooksClosed(payload),
                        statusCode: 502,
                        payload: payload,
                        upstream: upstream,
                    }
                }
            }
        })

        if (result.processed_count > 0) {
            console.log(`[OrderScheduler] ${environment}: 已将 ${result.processed_count} 条订单标记为 Canceled（有效期 ${validityMinutes} 分钟）`)
        }
        totalCount += result.processed_count
    }

    if (totalCount > 0) {
        console.log(`[OrderScheduler] 全环境共处理 ${totalCount} 条过期订单`)
    }
})

cronAdd("order_detail_integrity_guard", "*/10 * * * *", () => {
    const { getRuntimeEnvironments } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getPbCronToggleState } = require(`${__hooks}/lib/pb_cron_registry.js`)
    const { forwardIbkrSchedulerRequest } = require(`${__hooks}/lib/system/api_proxy.js`)
    const schedulerSlotToken = () => {
        const now = new Date()
        const year = now.getUTCFullYear()
        const month = String(now.getUTCMonth() + 1).padStart(2, "0")
        const day = String(now.getUTCDate()).padStart(2, "0")
        const hour = String(now.getUTCHours()).padStart(2, "0")
        const minute = String(now.getUTCMinutes()).padStart(2, "0")
        return `${year}-${month}-${day}T${hour}:${minute}Z`
    }

    for (const environment of getRuntimeEnvironments()) {
        const cronState = getPbCronToggleState("order_detail_integrity_guard", environment)
        if (!cronState.effective_enabled) {
            console.log(`[OrderDetailIntegrity] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
            continue
        }

        try {
            const result = forwardIbkrSchedulerRequest("/jobs/run/order_detail_integrity_guard", {
                method: "POST",
                environment: environment,
                timeout: 120,
                body: {
                    environment: environment,
                    trigger_source: "pb_compat",
                    scheduled_slot: schedulerSlotToken(),
                },
            })
            const payload = result.payload && typeof result.payload === "object" ? result.payload : {}
            console.log(
                `[OrderDetailIntegrity] ${environment}: scheduler_proxy ok=${payload.ok === true} skipped=${payload.skipped === true} reason=${payload.reason || "-"} upstream=${result.upstream}`
            )
        } catch (err) {
            console.error(`[OrderDetailIntegrity] ${environment}: scheduler proxy failed:`, err)
        }
    }
})
