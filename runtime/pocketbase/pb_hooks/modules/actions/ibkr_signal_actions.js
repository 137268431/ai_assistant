/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_signal_actions.pb.js
 * 信号 / 订单 webhook 与 group action 兼容入口；
 * 真实处理已迁到 ibkr-api，这里只保留 PocketBase 注册壳。
 */

console.log("[SignalActions] Hook 文件开始加载...");

var getSignalActionsRequestEnvironment = function(c) {
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
}

var getSignalActionsDataEnvironment = function(data) {
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return getRuntimeEnvironmentFromData(data, LIVE_ENVIRONMENT)
}

var signalActionsParseHttpJson = function(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { raw: raw }
    }
}

var signalActionsResolveCancelableOrderId = function(record) {
    if (!record) return ""
    const candidates = [
        String(record.get("broker_order_id") || "").trim(),
        String(record.get("order_id") || "").trim(),
    ]
    for (let i = 0; i < candidates.length; i++) {
        const value = candidates[i]
        if (/^\d+$/.test(value)) {
            return value
        }
    }
    return ""
}

var signalActionsResolveTradeGroupId = function(record) {
    if (!record) return ""
    return String(record.get("trade_group_id") || record.get("entry_order_unique_id") || record.get("unique_id") || "").trim()
}

var signalActionsPickPrimaryRecord = function(records, fallbackRecord) {
    const list = Array.isArray(records) ? records : []
    return list.find((record) => String(record.get("role") || "").trim() === "entry")
        || list.find((record) => {
            const uniqueId = String(record.get("unique_id") || "").trim()
            const entryOrderUniqueId = String(record.get("entry_order_unique_id") || "").trim()
            return !!uniqueId && uniqueId === entryOrderUniqueId
        })
        || fallbackRecord
        || list[0]
        || null
}

var signalActionsFindTradeGroupRecords = function(environment, tradeGroupId) {
    const groupId = String(tradeGroupId || "").trim()
    if (!groupId) return []
    try {
        return $app.findRecordsByFilter(
            "orders",
            "(trade_group_id = {:gid} || entry_order_unique_id = {:gid}) && environment = {:env}",
            "-created",
            100,
            0,
            { gid: groupId, env: environment }
        ) || []
    } catch (err) {
        console.error("[SignalActions] 查询交易组失败:", groupId, err)
        return []
    }
}

var signalActionsResolveOrderActionContext = function(environment, targetId) {
    const normalizedTargetId = String(targetId || "").trim()
    if (!normalizedTargetId) {
        return {
            actionRecord: null,
            primaryRecord: null,
            relatedRecords: [],
            tradeGroupId: "",
        }
    }

    const matchedRecords = $app.findRecordsByFilter(
        "orders",
        "(unique_id = {:id} || entry_order_unique_id = {:id} || trade_group_id = {:id}) && environment = {:env}",
        "-created",
        100,
        0,
        { id: normalizedTargetId, env: environment }
    ) || []
    if (matchedRecords.length === 0) {
        return {
            actionRecord: null,
            primaryRecord: null,
            relatedRecords: [],
            tradeGroupId: "",
        }
    }

    const exactUniqueMatch = matchedRecords.find((record) => String(record.get("unique_id") || "").trim() === normalizedTargetId) || null
    const primaryFromMatches = signalActionsPickPrimaryRecord(matchedRecords, matchedRecords[0] || null)
    const actionRecord = exactUniqueMatch || primaryFromMatches
    const initialTradeGroupId = signalActionsResolveTradeGroupId(primaryFromMatches || actionRecord)
    const relatedRecords = signalActionsFindTradeGroupRecords(environment, initialTradeGroupId)
    const resolvedRelatedRecords = relatedRecords.length > 0 ? relatedRecords : matchedRecords
    const primaryRecord = signalActionsPickPrimaryRecord(resolvedRelatedRecords, primaryFromMatches || actionRecord)

    return {
        actionRecord: actionRecord,
        primaryRecord: primaryRecord,
        relatedRecords: resolvedRelatedRecords,
        tradeGroupId: signalActionsResolveTradeGroupId(primaryRecord || actionRecord),
    }
}

var signalActionsCancelBrokerOrder = function(environment, orderId) {
    const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
    const upstream = `${getIbkrComputePublicUrl(environment, "http://127.0.0.1:5100")}/ibkr/orders/cancel`
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
        const payload = signalActionsParseHttpJson(resp.raw)
        return {
            ok: !!(payload && payload.ok),
            statusCode: Number(resp && resp.statusCode) || 200,
            payload: payload,
            upstream: upstream,
        }
    } catch (err) {
        return {
            ok: false,
            statusCode: 502,
            payload: { ok: false, error: err.message || String(err) },
            upstream: upstream,
        }
    }
}

var signalActionsCancelOrderGroupRecords = function(options) {
    const opts = options || {}
    const records = Array.isArray(opts.records) ? opts.records : []
    const environment = String(opts.environment || "live").trim().toLowerCase() || "live"
    const appendOrderDetail = opts.appendOrderDetail
    const applyOrderStatusMeta = opts.applyOrderStatusMeta
    const applyOrderRelationship = opts.applyOrderRelationship
    const getOrderExtra = opts.getOrderExtra
    const mergeOrderExtra = opts.mergeOrderExtra
    const notifyOrder = opts.notifyOrder
    const source = opts.source || "signal_actions"
    const reason = opts.reason || "manual_cancel"

    const cancelIds = []
    records.forEach((record) => {
        const status = String(record.get("status") || "")
        if (status === "Canceled" || status === "Closed" || status === "Filled") {
            return
        }
        const cancelId = signalActionsResolveCancelableOrderId(record)
        if (cancelId && cancelIds.indexOf(cancelId) === -1) {
            cancelIds.push(cancelId)
        }
    })

    const cancelledOrderIds = []
    const failedOrderIds = []
    for (let i = 0; i < cancelIds.length; i++) {
        const orderId = cancelIds[i]
        const result = signalActionsCancelBrokerOrder(environment, orderId)
        if (result.ok) {
            cancelledOrderIds.push(orderId)
        } else {
            failedOrderIds.push({
                order_id: orderId,
                error: (result.payload && (result.payload.error || result.payload.message)) || `status_${result.statusCode || 500}`,
            })
        }
    }

    if (failedOrderIds.length > 0) {
        return {
            ok: false,
            cancelled_order_ids: cancelledOrderIds,
            failed_order_ids: failedOrderIds,
        }
    }

    const primaryRecord = records.find((record) => String(record.get("role") || "") === "entry") || records[0] || null
    const tradeGroupId = primaryRecord
        ? (primaryRecord.get("trade_group_id") || primaryRecord.get("entry_order_unique_id") || primaryRecord.get("unique_id") || "")
        : ""

    records.forEach((groupRecord) => {
        const currentStatus = groupRecord.get("status")
        if (currentStatus === "Canceled" || currentStatus === "Closed" || currentStatus === "Filled") {
            return
        }
        groupRecord.set("status", "Canceled")
        applyOrderRelationship(groupRecord, {
            relation_status: "closed",
            status: "Canceled",
        }, false)
        const metaResult = applyOrderStatusMeta(groupRecord, {
            status: "Canceled",
            previous_status: currentStatus,
            source: source,
            reason: reason,
        }, false)
        const eventTimes = metaResult.eventTimes
        $app.save(groupRecord)
        try {
            appendOrderDetail(groupRecord, {
                environment: environment,
                status: "Canceled",
                source: source,
                reason: reason,
                us_time: eventTimes.us_time,
                cn_time: eventTimes.cn_time,
                bar_time_ms: eventTimes.bar_time_ms,
                extra: {
                    previous_status: currentStatus,
                    action: "cancel",
                    trade_group_id: tradeGroupId,
                    cancelled_order_ids: cancelledOrderIds,
                },
            })
        } catch (detailErr) {
            console.error("[SignalActions] 写入 ibkr_order_details 失败:", detailErr)
        }
    })

    if (primaryRecord && typeof notifyOrder === "function" && typeof getOrderExtra === "function" && typeof mergeOrderExtra === "function") {
        try {
            const orderExtra = getOrderExtra(primaryRecord)
            const syncResult = notifyOrder("canceled", primaryRecord, {
                messageId: orderExtra.feishu_order_message_id || "",
                message: cancelledOrderIds.length > 0 ? "主单已撤销，关联挂单已收尾" : "交易组已取消",
            })
            if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
                mergeOrderExtra(primaryRecord, {
                    feishu_order_message_id: syncResult.message_id,
                    feishu_order_card_version: 2,
                }, true)
            }
        } catch (notifyErr) {
            console.error("[SignalActions] 同步订单卡片失败:", notifyErr)
        }
    }

    return {
        ok: true,
        trade_group_id: tradeGroupId,
        cancelled_order_ids: cancelledOrderIds,
        failed_order_ids: failedOrderIds,
    }
}

var signalActionsHandleOrderCancelRequest = function(c, uniqueId, environment) {
    try {
        const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
        const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
        const { ok, warn, fail } = require(`${__hooks}/lib/_page.js`)
        if (!uniqueId) {
            return c.html(400, fail("参数错误", "缺少订单ID"))
        }
        const orderContext = signalActionsResolveOrderActionContext(environment, uniqueId)
        if (!orderContext || !orderContext.actionRecord) {
            return c.html(404, fail("订单不存在", "找不到订单", uniqueId))
        }
        const record = orderContext.actionRecord
        const primaryRecord = orderContext.primaryRecord || record
        const status = primaryRecord.get("status")
        const symbol = primaryRecord.get("symbol") || record.get("symbol") || uniqueId
        const filledQty = Number(primaryRecord.get("filled_qty") || 0)
        const tradeGroupId = orderContext.tradeGroupId || signalActionsResolveTradeGroupId(primaryRecord || record)
        const role = String(record.get("role") || "").trim()
        console.log(`[OrderAction] 收到取消请求: target_id=${uniqueId}, resolved_unique_id=${primaryRecord.get("unique_id") || record.get("unique_id") || "-"}, symbol=${symbol}, current_status=${status}`)
        if (role && role !== "entry") {
            return c.html(200, warn("只能取消主单", "止盈/止损等子单不能直接取消，请操作主入场单", symbol))
        }
        if (filledQty > 0) {
            return c.html(200, warn("主单已部分成交", "主单已部分成交，不能直接取消，请改用平仓整组", symbol))
        }
        const statusHints = {
            Filled:   { fn: warn, title: "订单已成交", msg: "订单已成交，无法取消" },
            Canceled: { fn: warn, title: "订单已取消", msg: "无需重复操作" },
            Closed:   { fn: warn, title: "订单已平仓", msg: "无法取消" },
        }
        if (statusHints[status]) {
            const h = statusHints[status]
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        const relatedRecords = orderContext.relatedRecords && orderContext.relatedRecords.length > 0
            ? orderContext.relatedRecords
            : [primaryRecord]
        const cancelSummary = signalActionsCancelOrderGroupRecords({
            records: relatedRecords,
            environment: environment,
            appendOrderDetail: appendOrderDetail,
            applyOrderStatusMeta: applyOrderStatusMeta,
            applyOrderRelationship: applyOrderRelationship,
            getOrderExtra: getOrderExtra,
            mergeOrderExtra: mergeOrderExtra,
            notifyOrder: notifyOrder,
            source: "webhook/order/cancel",
            reason: "页面取消主单",
        })
        if (!cancelSummary.ok) {
            console.error("[OrderAction] IBKR 撤单失败:", JSON.stringify(cancelSummary.failed_order_ids || []))
            return c.html(500, fail("订单取消失败", `账户撤单失败 ${cancelSummary.failed_order_ids.length} 条`, symbol))
        }
        console.log("[OrderAction] 交易组已取消:", tradeGroupId, "cancelled_order_ids=", (cancelSummary.cancelled_order_ids || []).join(",") || "-")
        return c.html(200, ok("订单已取消", "状态已更新", symbol))
    } catch (err) {
        console.error("[OrderAction] 取消失败:", err)
        return c.json(500, { ok: false, error: err && (err.message || String(err)) || "unknown_error", action: "cancel_group" })
    }
}

var signalActionsHandleOrderCloseRequest = function(c, uniqueId, environment) {
    try {
        const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
        const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
        const { ok, warn, fail } = require(`${__hooks}/lib/_page.js`)
        if (!uniqueId) {
            return c.html(400, fail("参数错误", "缺少订单ID"))
        }
        const orderContext = signalActionsResolveOrderActionContext(environment, uniqueId)
        if (!orderContext || !orderContext.primaryRecord) {
            return c.html(404, fail("订单不存在", "找不到订单", uniqueId))
        }
        const record = orderContext.primaryRecord
        const status = record.get("status")
        const symbol = record.get("symbol") || uniqueId
        const tradeGroupId = orderContext.tradeGroupId || signalActionsResolveTradeGroupId(record)
        const entryOrderUniqueId = record.get("entry_order_unique_id") || record.get("unique_id")
        const statusHints = {
            Init:      { fn: warn, title: "订单未成交", msg: "只有成交的订单才能平仓" },
            Submitted: { fn: warn, title: "订单未成交", msg: "请先取消挂单" },
            Canceled: { fn: warn, title: "订单已取消", msg: "无法平仓" },
            Closed:   { fn: warn, title: "订单已平仓", msg: "无需重复操作" },
        }
        if (statusHints[status]) {
            const h = statusHints[status]
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        if (status !== "Filled") {
            return c.html(200, warn("订单未成交", "只有成交的订单才能平仓", symbol))
        }
        const relatedRecords = orderContext.relatedRecords && orderContext.relatedRecords.length > 0
            ? orderContext.relatedRecords
            : [record]
        relatedRecords.forEach((groupRecord) => {
            const currentGroupStatus = groupRecord.get("status")
            if (currentGroupStatus === "Canceled" || currentGroupStatus === "Closed") {
                return
            }
            const nextStatus = groupRecord.get("unique_id") === entryOrderUniqueId ? "Closed" : "Canceled"
            groupRecord.set("status", nextStatus)
            applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: nextStatus,
            }, false)
            const metaResult = applyOrderStatusMeta(groupRecord, {
                status: nextStatus,
                previous_status: currentGroupStatus,
                source: "webhook/order/close",
                reason: `页面平仓交易组 ${tradeGroupId}`,
            }, false)
            const eventTimes = metaResult.eventTimes
            $app.save(groupRecord)
            try {
                appendOrderDetail(groupRecord, {
                    environment: environment,
                    status: nextStatus,
                    source: "webhook/order/close",
                    reason: `页面平仓交易组 ${tradeGroupId}`,
                    us_time: eventTimes.us_time,
                    cn_time: eventTimes.cn_time,
                    bar_time_ms: eventTimes.bar_time_ms,
                    extra: {
                        action: "close_group",
                        previous_status: currentGroupStatus,
                        trade_group_id: tradeGroupId,
                    },
                })
            } catch (detailErr) {
                console.error("[OrderAction] 平仓写入 ibkr_order_details 失败:", detailErr)
            }
        })
        const orderExtra = getOrderExtra(record)
        const syncResult = notifyOrder("closed", record, {
            messageId: orderExtra.feishu_order_message_id || "",
            message: `交易组已平仓 (${tradeGroupId})`,
        })
        if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
            mergeOrderExtra(record, {
                feishu_order_message_id: syncResult.message_id,
                feishu_order_card_version: 2,
            }, true)
        }
        console.log("[OrderAction] 交易组已平仓:", tradeGroupId)
        return c.html(200, ok("交易组已平仓", "状态已更新", symbol))
    } catch (err) {
        console.error("[OrderAction] 平仓失败:", err)
        return c.json(500, { ok: false, error: err && (err.message || String(err)) || "unknown_error", action: "close_group" })
    }
}

routerAdd("GET", "/webhook/signal/confirm", (c) => {
    const { proxyIbkrApiHtml } = require(`${__hooks}/lib/system/api_proxy.js`)
    const environment = getSignalActionsRequestEnvironment(c)
    return proxyIbkrApiHtml(c, "/webhook/signal/confirm", {
        method: "GET",
        environment: environment,
        query: {
            id: c.request.url.query().get("id") || "",
            environment: environment,
        },
        timeout: 20,
    })
})

routerAdd("GET", "/webhook/signal/cancel", (c) => {
    const { proxyIbkrApiHtml } = require(`${__hooks}/lib/system/api_proxy.js`)
    const environment = getSignalActionsRequestEnvironment(c)
    return proxyIbkrApiHtml(c, "/webhook/signal/cancel", {
        method: "GET",
        environment: environment,
        query: {
            id: c.request.url.query().get("id") || "",
            environment: environment,
        },
        timeout: 30,
    })
})

// ── IBKR 信号拉取 & 确认 ──

routerAdd("GET", "/api/custom/ibkr/signals/pending", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const environment = getSignalActionsRequestEnvironment(c)
  return proxyIbkrApiJson(c, "/api/custom/ibkr/signals/pending", {
    method: "GET",
    environment: environment,
    query: {
      date: c.request.url.query().get("date") || "",
    },
    timeout: 20,
  })
})

routerAdd("POST", "/api/custom/ibkr/signals/ack", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const reqInfo = c.requestInfo()
  const body = reqInfo.body || reqInfo.data || {}
  return proxyIbkrApiJson(c, "/api/custom/ibkr/signals/ack", {
    method: "POST",
    environment: getSignalActionsDataEnvironment(body),
    body: body,
    timeout: 45,
  })
})

// ── 订单操作（飞书按钮直接更新状态） ──

routerAdd("GET", "/webhook/order/cancel", (c) => {
    const { proxyIbkrApiHtml } = require(`${__hooks}/lib/system/api_proxy.js`)
    const environment = getSignalActionsRequestEnvironment(c)
    return proxyIbkrApiHtml(c, "/webhook/order/cancel", {
        method: "GET",
        environment: environment,
        query: {
            id: c.request.url.query().get("id") || "",
            environment: environment,
        },
        timeout: 30,
    })
})

routerAdd("GET", "/webhook/order/close", (c) => {
    const { proxyIbkrApiHtml } = require(`${__hooks}/lib/system/api_proxy.js`)
    const environment = getSignalActionsRequestEnvironment(c)
    return proxyIbkrApiHtml(c, "/webhook/order/close", {
        method: "GET",
        environment: environment,
        query: {
            id: c.request.url.query().get("id") || "",
            environment: environment,
        },
        timeout: 30,
    })
})

routerAdd("POST", "/api/custom/ibkr/orders/cancel_group", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const reqInfo = c.requestInfo()
    const body = reqInfo.body || reqInfo.data || {}
    return proxyIbkrApiJson(c, "/api/custom/ibkr/orders/cancel_group", {
        method: "POST",
        environment: getSignalActionsDataEnvironment(body),
        body: body,
        timeout: 30,
    })
})

routerAdd("POST", "/api/custom/ibkr/orders/close_group", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const reqInfo = c.requestInfo()
    const body = reqInfo.body || reqInfo.data || {}
    return proxyIbkrApiJson(c, "/api/custom/ibkr/orders/close_group", {
        method: "POST",
        environment: getSignalActionsDataEnvironment(body),
        body: body,
        timeout: 30,
    })
})

console.log('[SignalActions] Hook 文件加载完成');
