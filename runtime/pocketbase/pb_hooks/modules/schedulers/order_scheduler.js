/// <reference path="./pb_data/types.d.ts" />

/**
 * order_scheduler.pb.js
 * 定时检查订单有效期，超时自动标记为 Canceled
 * 读取 config 表中 order_validity_minutes 配置（默认 30 分钟）
 */

function collectResultSamples(results, status, limit) {
    const list = Array.isArray(results) ? results : []
    const maxItems = Math.max(1, Number(limit) || 5)
    const samples = []
    for (let i = 0; i < list.length; i++) {
        const item = list[i]
        if (!item || item.status !== status) continue
        const uniqueId = String(item.unique_id || "").trim()
        const symbol = String(item.symbol || "").trim()
        if (!uniqueId && !symbol) continue
        samples.push(symbol ? `${symbol}:${uniqueId || "-"}` : uniqueId)
        if (samples.length >= maxItems) break
    }
    return samples
}

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
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const orderDetailReconcile = require(`${__hooks}/lib/order_detail_reconcile.js`)

    let totalScanned = 0
    let totalRepaired = 0
    let totalFailed = 0

    for (const environment of getRuntimeEnvironments()) {
        const cronState = getPbCronToggleState("order_detail_integrity_guard", environment)
        if (!cronState.effective_enabled) {
            console.log(`[OrderDetailIntegrity] ${environment}: ${cronState.config_key}="${cronState.cron_raw}", pb_scheduler_enabled="${cronState.scheduler_raw}", 跳过执行`)
            continue
        }

        let result
        try {
            result = orderDetailReconcile.reconcileOrderDetails({
                app: $app,
                environment: environment,
                limit: 100,
                pageSize: 100,
                maxScan: 500,
                scanAll: true,
                onlyMissing: true,
                dryRun: false,
                suppressNotification: true,
                source: "order_detail_integrity_guard",
                repairSource: "order_detail_integrity_guard",
            })
        } catch (err) {
            console.error(`[OrderDetailIntegrity] ${environment}: 巡检失败:`, err)
            writeSystemEvent(
                "alert",
                "error",
                "orders",
                "订单明细完整性巡检失败",
                {
                    "环境": environment,
                    "错误": err && err.message ? err.message : String(err),
                },
                environment,
                false
            )
            totalFailed += 1
            continue
        }

        totalScanned += Number(result && result.summary && result.summary.scanned || 0)
        totalRepaired += Number(result && result.summary && result.summary.repaired || 0)
        totalFailed += Number(result && result.summary && result.summary.failed || 0)

        console.log(
            `[OrderDetailIntegrity] ${environment}: scanned=${result.summary.scanned}, repaired=${result.summary.repaired}, skipped=${result.summary.skipped}, failed=${result.summary.failed}`
        )

        if (result.summary.repaired > 0 || result.summary.failed > 0) {
            const repairedSamples = collectResultSamples(result.results, "repaired_detail", 8)
            const failedSamples = collectResultSamples(result.results, "failed_exception", 5)
            writeSystemEvent(
                "alert",
                result.summary.failed > 0 ? "error" : "warning",
                "orders",
                result.summary.failed > 0 ? "订单明细完整性巡检发现异常" : "订单明细完整性巡检已自动修复",
                {
                    "环境": environment,
                    "扫描订单数": String(result.summary.scanned),
                    "自动修复数": String(result.summary.repaired),
                    "失败数": String(result.summary.failed),
                    "修复样本": repairedSamples.join(", ") || "-",
                    "失败样本": failedSamples.join(", ") || "-",
                    "来源": "order_detail_integrity_guard",
                },
                environment,
                false
            )
        }
    }

    if (totalScanned > 0 || totalRepaired > 0 || totalFailed > 0) {
        console.log(`[OrderDetailIntegrity] summary: scanned=${totalScanned}, repaired=${totalRepaired}, failed=${totalFailed}`)
    }
})
