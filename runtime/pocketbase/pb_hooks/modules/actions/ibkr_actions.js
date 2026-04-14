/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_actions.pb.js
 * IBKR 数据接收端点 — OHLCV / 指标 / 信号 / 扫描结果
 */

console.log("[IBKRActions] Hook 文件开始加载...");

// ── 工具函数 ──

function ibkrActionsParseHttpJson(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}
globalThis.ibkrActionsParseHttpJson = ibkrActionsParseHttpJson

function ibkrActionsSafeParseHttpJson(rawValue) {
    const parser = typeof globalThis.ibkrActionsParseHttpJson === "function"
        ? globalThis.ibkrActionsParseHttpJson
        : null
    if (parser) {
        return parser(rawValue)
    }
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}
globalThis.ibkrActionsSafeParseHttpJson = ibkrActionsSafeParseHttpJson

const IBKR_ACTIONS_VOLATILE_COMPARE_KEYS = {
    computed_at_ms: true,
    computed_at_us: true,
    computed_at_cn: true,
}
const IBKR_WATCHLIST_ROLE_TRADE = "trade"
const IBKR_WATCHLIST_ROLE_MARKET_MONITOR = "market_monitor"

function ibkrActionsNormalizeForCompare(value) {
    if (Array.isArray(value)) {
        return value.map((item) => ibkrActionsNormalizeForCompare(item))
    }
    if (value && typeof value === "object") {
        const normalized = {}
        Object.keys(value).sort().forEach((key) => {
            if (IBKR_ACTIONS_VOLATILE_COMPARE_KEYS[key]) {
                return
            }
            normalized[key] = ibkrActionsNormalizeForCompare(value[key])
        })
        return normalized
    }
    if (typeof value === "number") {
        return Number.isFinite(value) ? Number(value) : null
    }
    return value == null ? null : value
}
globalThis.ibkrActionsNormalizeForCompare = ibkrActionsNormalizeForCompare

function ibkrActionsValuesEqual(left, right) {
    return JSON.stringify(globalThis.ibkrActionsNormalizeForCompare(left)) === JSON.stringify(globalThis.ibkrActionsNormalizeForCompare(right))
}
globalThis.ibkrActionsValuesEqual = ibkrActionsValuesEqual

function ibkrActionsRecordNeedsUpdate(record, data) {
    return Object.keys(data || {}).some((key) => !globalThis.ibkrActionsValuesEqual(record.get(key), data[key]))
}
globalThis.ibkrActionsRecordNeedsUpdate = ibkrActionsRecordNeedsUpdate

function ibkrActionsUpsertRecord(collectionName, filterStr, filterParams, data) {
    let record = null
    try {
        record = $app.findFirstRecordByFilter(collectionName, filterStr, filterParams)
    } catch (_) {}

    const col = $app.findCollectionByNameOrId(collectionName)
    let action = "updated"
    if (!record) {
        record = new Record(col, {})
        action = "created"
    } else if (!globalThis.ibkrActionsRecordNeedsUpdate(record, data)) {
        return { record: record, action: "skipped" }
    }
    Object.keys(data).forEach((key) => {
        record.set(key, data[key])
    })
    $app.save(record)
    return { record: record, action: action }
}
globalThis.ibkrActionsUpsertRecord = ibkrActionsUpsertRecord

function ibkrActionsNormalizeWatchlistRole(value) {
    const normalized = String(value || "").trim().toLowerCase()
    return normalized === IBKR_WATCHLIST_ROLE_MARKET_MONITOR
        ? IBKR_WATCHLIST_ROLE_MARKET_MONITOR
        : IBKR_WATCHLIST_ROLE_TRADE
}
globalThis.ibkrActionsNormalizeWatchlistRole = ibkrActionsNormalizeWatchlistRole

function ibkrActionsBuildProxyMeta(payload, route, upstream) {
    const base = payload && typeof payload === "object" && !Array.isArray(payload)
        ? { ...payload }
        : { ok: false, raw: String(payload || "") }

    base.proxy_source = "pocketbase_ibkr_hook"
    base.proxy_hook = "ibkr_actions.pb.js"
    base.proxy_route = route
    base.proxy_upstream = upstream
    return base
}
globalThis.ibkrActionsBuildProxyMeta = ibkrActionsBuildProxyMeta

function ibkrActionsInspectRequestedRuntimeEnvironment(environment) {
    const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const requestedEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    const computeBase = getIbkrComputeInternalUrl(requestedEnvironment, "http://127.0.0.1:5100")
    const runtimeUpstream = `${computeBase}/ibkr/status`
    const resp = $http.send({
        url: runtimeUpstream,
        method: "GET",
        timeout: 8,
    })
    const runtimePayload = globalThis.ibkrActionsParseHttpJson(resp.raw)
    const actualRuntimeEnvironment = String(runtimePayload.environment || requestedEnvironment).trim().toLowerCase() || requestedEnvironment
    return {
        requested_environment: requestedEnvironment,
        actual_runtime_environment: actualRuntimeEnvironment,
        runtime_environment_mismatch: actualRuntimeEnvironment !== requestedEnvironment,
        runtime_payload: runtimePayload,
        proxy_upstream_runtime: runtimeUpstream,
    }
}
globalThis.ibkrActionsInspectRequestedRuntimeEnvironment = ibkrActionsInspectRequestedRuntimeEnvironment

function ibkrActionsBuildRuntimeEnvironmentMismatchPayload(environmentInfo, route) {
    const requestedEnvironment = String(environmentInfo && environmentInfo.requested_environment || "live").trim().toLowerCase() || "live"
    const actualRuntimeEnvironment = String(environmentInfo && environmentInfo.actual_runtime_environment || requestedEnvironment).trim().toLowerCase() || requestedEnvironment
    return {
        ok: false,
        error: `当前 ${requestedEnvironment.toUpperCase()} 页面没有独立 runtime；实际运行中的是 ${actualRuntimeEnvironment.toUpperCase()}，请切到对应环境页面执行此动作。`,
        requested_environment: requestedEnvironment,
        actual_runtime_environment: actualRuntimeEnvironment,
        runtime_environment_mismatch: true,
        proxy_source: "pocketbase_ibkr_hook",
        proxy_hook: "ibkr_actions.pb.js",
        proxy_route: route,
        proxy_upstream_runtime: environmentInfo && environmentInfo.proxy_upstream_runtime ? environmentInfo.proxy_upstream_runtime : "",
    }
}
globalThis.ibkrActionsBuildRuntimeEnvironmentMismatchPayload = ibkrActionsBuildRuntimeEnvironmentMismatchPayload

function ibkrActionsToNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback != null ? fallback : 0)
}

function ibkrActionsToText(value) {
    return String(value == null ? "" : value).trim()
}
globalThis.ibkrActionsToText = ibkrActionsToText

function ibkrActionsParseJsonObject(value) {
    if (!value) return {}
    if (typeof value === "object" && !Array.isArray(value)) return value
    try {
        const parsed = JSON.parse(String(value || ""))
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}
    } catch (_) {
        return {}
    }
}

function ibkrActionsQuoteFilterValue(value) {
    return String(value == null ? "" : value).replace(/"/g, '\\"')
}

function ibkrActionsPickFirstNonEmpty(values) {
    for (let i = 0; i < values.length; i++) {
        const text = ibkrActionsToText(values[i])
        if (text) return text
    }
    return ""
}
globalThis.ibkrActionsPickFirstNonEmpty = ibkrActionsPickFirstNonEmpty

function ibkrActionsOrderStatusWeight(status) {
    const key = ibkrActionsToText(status).toUpperCase()
    if (key === "FILLED" || key === "EXECUTED") return 90
    if (key === "PARTIALLYFILLED" || key === "PARTIAL") return 80
    if (key === "SUBMITTED" || key === "PRESUBMITTED") return 70
    if (key === "INIT") return 50
    if (key === "PENDING" || key === "PENDINGSUBMIT") return 40
    if (key === "CANCELED" || key === "CANCELLED") return 10
    return 20
}

function ibkrActionsSignalStatusWeight(status) {
    const key = ibkrActionsToText(status).toLowerCase()
    if (key === "executed") return 90
    if (key === "confirmed") return 80
    if (key === "awaiting_confirm") return 70
    if (key === "pending") return 60
    if (key === "rejected" || key === "expired") return 20
    return 30
}

function ibkrActionsNormalizeOrderRecord(record) {
    const extra = ibkrActionsParseJsonObject(record.get("extra"))
    const quantity = ibkrActionsToNumber(record.get("quantity"), 0)
    const filledQtyRaw = ibkrActionsToNumber(record.get("filled_qty"), 0)
    const status = ibkrActionsPickFirstNonEmpty([record.get("status"), extra.status])
    const role = ibkrActionsPickFirstNonEmpty([record.get("role"), extra.role]) || "entry"
    const tradeGroupId = ibkrActionsPickFirstNonEmpty([
        record.get("trade_group_id"),
        extra.trade_group_id,
        record.get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        record.get("unique_id"),
    ])
    const entryOrderUniqueId = ibkrActionsPickFirstNonEmpty([
        record.get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        record.get("unique_id"),
    ])
    const signalId = ibkrActionsPickFirstNonEmpty([
        record.get("signal_id"),
        extra.signal_id,
    ])
    const updated = ibkrActionsPickFirstNonEmpty([
        record.get("updated"),
        record.get("us_time"),
        record.get("created"),
    ])
    const relationStatus = ibkrActionsPickFirstNonEmpty([
        record.get("relation_status"),
        extra.relation_status,
    ])
    const filledQty = filledQtyRaw > 0
        ? filledQtyRaw
        : ((String(status).toUpperCase() === "FILLED" || String(status).toUpperCase() === "EXECUTED") ? quantity : 0)

    return {
        id: ibkrActionsPickFirstNonEmpty([record.get("id"), record.id]),
        symbol: ibkrActionsPickFirstNonEmpty([record.get("symbol"), extra.symbol]).toUpperCase(),
        status: status,
        signal_id: signalId,
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        unique_id: ibkrActionsPickFirstNonEmpty([record.get("unique_id"), extra.unique_id]),
        broker_order_id: ibkrActionsPickFirstNonEmpty([record.get("broker_order_id"), extra.broker_order_id, record.get("order_id")]),
        role: role,
        relation_status: relationStatus,
        quantity: quantity,
        filled_qty: filledQty,
        updated: updated,
        updated_ms: Date.parse(updated || "") || 0,
        status_weight: ibkrActionsOrderStatusWeight(status),
    }
}
globalThis.ibkrActionsNormalizeOrderRecord = ibkrActionsNormalizeOrderRecord

function ibkrActionsNormalizeSignalRecord(record) {
    const extra = ibkrActionsParseJsonObject(record.get("extra"))
    const updated = ibkrActionsPickFirstNonEmpty([
        record.get("updated"),
        record.get("us_time"),
        record.get("created"),
    ])
    const status = ibkrActionsPickFirstNonEmpty([record.get("status"), extra.status])
    return {
        id: ibkrActionsPickFirstNonEmpty([record.get("id"), record.id]),
        signal_id: ibkrActionsPickFirstNonEmpty([record.get("signal_id"), extra.signal_id]),
        symbol: ibkrActionsPickFirstNonEmpty([record.get("symbol"), extra.symbol]).toUpperCase(),
        status: status,
        note: ibkrActionsPickFirstNonEmpty([record.get("note"), extra.status_reason, extra.note]),
        updated: updated,
        updated_ms: Date.parse(updated || "") || 0,
        status_weight: ibkrActionsSignalStatusWeight(status),
    }
}

function ibkrActionsOrderStatusKey(status) {
    return ibkrActionsToText(status).toUpperCase()
}
globalThis.ibkrActionsOrderStatusKey = ibkrActionsOrderStatusKey

function ibkrActionsIsOrderClosedStatus(status) {
    const key = ibkrActionsOrderStatusKey(status)
    return key === "FILLED" || key === "EXECUTED" || key === "CANCELED" || key === "CANCELLED" || key === "CLOSED"
}
globalThis.ibkrActionsIsOrderClosedStatus = ibkrActionsIsOrderClosedStatus

function ibkrActionsResolveTradeGroupId(record) {
    if (!record) return ""
    const extra = ibkrActionsParseJsonObject(record.get("extra"))
    return ibkrActionsPickFirstNonEmpty([
        record.get("trade_group_id"),
        extra.trade_group_id,
        record.get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        record.get("unique_id"),
        extra.unique_id,
        record.get("order_id"),
    ])
}
globalThis.ibkrActionsResolveTradeGroupId = ibkrActionsResolveTradeGroupId

function ibkrActionsResolveCancelableBrokerOrderId(record) {
    if (!record) return ""
    const extra = ibkrActionsParseJsonObject(record.get("extra"))
    const candidates = [
        record.get("broker_order_id"),
        extra.broker_order_id,
        record.get("order_id"),
        extra.order_id,
    ]
    for (let i = 0; i < candidates.length; i++) {
        const value = ibkrActionsToText(candidates[i])
        if (/^\d+$/.test(value)) {
            return value
        }
    }
    return ""
}
globalThis.ibkrActionsResolveCancelableBrokerOrderId = ibkrActionsResolveCancelableBrokerOrderId

function ibkrActionsPickPrimaryOrderRecord(records, fallbackRecord) {
    const list = Array.isArray(records) ? records : []
    return list.find((record) => ibkrActionsToText(record.get("role")) === "entry")
        || list.find((record) => {
            const uniqueId = ibkrActionsToText(record.get("unique_id"))
            const entryOrderUniqueId = ibkrActionsToText(record.get("entry_order_unique_id"))
            return !!uniqueId && uniqueId === entryOrderUniqueId
        })
        || fallbackRecord
        || list[0]
        || null
}
globalThis.ibkrActionsPickPrimaryOrderRecord = ibkrActionsPickPrimaryOrderRecord

function ibkrActionsUniqueRecords(records) {
    const list = Array.isArray(records) ? records : []
    const seen = {}
    const unique = []
    for (let i = 0; i < list.length; i++) {
        const record = list[i]
        if (!record) continue
        const key = ibkrActionsPickFirstNonEmpty([record.get("id"), record.id, record.get("unique_id"), record.get("order_id")]) || `idx_${i}`
        if (seen[key]) continue
        seen[key] = true
        unique.push(record)
    }
    return unique
}
globalThis.ibkrActionsUniqueRecords = ibkrActionsUniqueRecords

function ibkrActionsFindTradeGroupRecords(environment, tradeGroupId) {
    const groupId = ibkrActionsToText(tradeGroupId)
    if (!groupId) return []
    try {
        return $app.findRecordsByFilter(
            "orders",
            "(trade_group_id = {:gid} || entry_order_unique_id = {:gid} || unique_id = {:gid}) && environment = {:env}",
            "-created",
            100,
            0,
            { gid: groupId, env: environment }
        ) || []
    } catch (err) {
        console.error("[IBKRActions] 查询交易组失败:", groupId, err)
        return []
    }
}
globalThis.ibkrActionsFindTradeGroupRecords = ibkrActionsFindTradeGroupRecords

function ibkrActionsResolveOrderActionContext(environment, data) {
    const payload = data && typeof data === "object" ? data : {}
    const targetId = ibkrActionsPickFirstNonEmpty([
        payload.id,
        payload.unique_id,
        payload.entry_order_unique_id,
        payload.trade_group_id,
    ])
    const brokerOrderId = ibkrActionsPickFirstNonEmpty([
        payload.broker_order_id,
        payload.order_id,
    ])

    let matchedRecords = []
    if (targetId) {
        try {
            matchedRecords = $app.findRecordsByFilter(
                "orders",
                "(unique_id = {:id} || entry_order_unique_id = {:id} || trade_group_id = {:id}) && environment = {:env}",
                "-created",
                100,
                0,
                { id: targetId, env: environment }
            ) || []
        } catch (err) {
            console.error("[IBKRActions] 查询订单动作上下文失败:", targetId, err)
        }
    }

    const brokerLookupId = brokerOrderId || (/^\d+$/.test(targetId) ? targetId : "")
    if ((!matchedRecords || matchedRecords.length === 0) && brokerLookupId) {
        try {
            matchedRecords = $app.findRecordsByFilter(
                "orders",
                "(broker_order_id = {:oid} || order_id = {:oid}) && environment = {:env}",
                "-created",
                100,
                0,
                { oid: brokerLookupId, env: environment }
            ) || []
        } catch (err) {
            console.error("[IBKRActions] 通过 broker_order_id 查询订单失败:", brokerLookupId, err)
        }
    }

    const uniqueRecords = ibkrActionsUniqueRecords(matchedRecords)
    if (!uniqueRecords.length) {
        return {
            actionRecord: null,
            primaryRecord: null,
            relatedRecords: [],
            tradeGroupId: "",
            targetId: targetId,
            brokerOrderId: brokerOrderId,
        }
    }

    const exactBrokerMatch = brokerLookupId
        ? uniqueRecords.find((record) => ibkrActionsResolveCancelableBrokerOrderId(record) === brokerLookupId) || null
        : null
    const exactUniqueMatch = targetId
        ? uniqueRecords.find((record) => ibkrActionsToText(record.get("unique_id")) === targetId) || null
        : null
    const primaryFromMatches = ibkrActionsPickPrimaryOrderRecord(uniqueRecords, uniqueRecords[0] || null)
    const actionRecord = exactBrokerMatch || exactUniqueMatch || primaryFromMatches
    const initialTradeGroupId = ibkrActionsResolveTradeGroupId(primaryFromMatches || actionRecord)
    const relatedRecords = ibkrActionsFindTradeGroupRecords(environment, initialTradeGroupId)
    const resolvedRelatedRecords = ibkrActionsUniqueRecords(relatedRecords.length > 0 ? relatedRecords : uniqueRecords)
    const primaryRecord = ibkrActionsPickPrimaryOrderRecord(resolvedRelatedRecords, primaryFromMatches || actionRecord)

    return {
        actionRecord: actionRecord,
        primaryRecord: primaryRecord,
        relatedRecords: resolvedRelatedRecords,
        tradeGroupId: ibkrActionsResolveTradeGroupId(primaryRecord || actionRecord),
        targetId: targetId,
        brokerOrderId: brokerOrderId,
    }
}
globalThis.ibkrActionsResolveOrderActionContext = ibkrActionsResolveOrderActionContext

function ibkrActionsCancelBrokerOrder(environment, orderId, data) {
    const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/cancel`
    const brokerOrderId = ibkrActionsToText(orderId)
    const payloadBody = {
        order_id: brokerOrderId,
        environment: environment,
    }
    const accountId = ibkrActionsToText(data && data.account_id)
    if (accountId) {
        payloadBody.account_id = accountId
    }
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(payloadBody),
            headers: { "Content-Type": "application/json" },
        })
        const payload = ibkrActionsParseHttpJson(resp.raw)
        return {
            ok: !!(payload && payload.ok),
            statusCode: Number(resp && resp.statusCode) || 200,
            payload: payload,
            upstream: upstream,
            order_id: brokerOrderId,
        }
    } catch (err) {
        return {
            ok: false,
            statusCode: 502,
            payload: { ok: false, error: err.message || String(err) },
            upstream: upstream,
            order_id: brokerOrderId,
        }
    }
}
globalThis.ibkrActionsCancelBrokerOrder = ibkrActionsCancelBrokerOrder

function ibkrActionsCancelOrderGroupRecords(options) {
    const opts = options || {}
    const records = ibkrActionsUniqueRecords(opts.records)
    const environment = ibkrActionsToText(opts.environment || "live").toLowerCase() || "live"
    const appendOrderDetail = opts.appendOrderDetail
    const applyOrderStatusMeta = opts.applyOrderStatusMeta
    const applyOrderRelationship = opts.applyOrderRelationship
    const getOrderExtra = opts.getOrderExtra
    const mergeOrderExtra = opts.mergeOrderExtra
    const notifyOrder = opts.notifyOrder
    const source = opts.source || "ibkr_actions_cancel_sync"
    const reason = opts.reason || "manual_cancel"
    const requestData = opts.data && typeof opts.data === "object" ? opts.data : {}

    const cancelIds = []
    records.forEach((record) => {
        const currentStatus = ibkrActionsNormalizeOrderRecord(record).status
        if (ibkrActionsIsOrderClosedStatus(currentStatus)) {
            return
        }
        const cancelId = ibkrActionsResolveCancelableBrokerOrderId(record)
        if (cancelId && cancelIds.indexOf(cancelId) === -1) {
            cancelIds.push(cancelId)
        }
    })

    if (!cancelIds.length) {
        return {
            ok: false,
            error: "未找到可取消的 IBKR 订单号",
            cancelled_order_ids: [],
            failed_order_ids: [],
            updated_record_ids: [],
        }
    }

    const cancelledOrderIds = []
    const failedOrderIds = []
    const upstreams = []
    for (let i = 0; i < cancelIds.length; i++) {
        const orderId = cancelIds[i]
        const result = ibkrActionsCancelBrokerOrder(environment, orderId, requestData)
        if (result.upstream && upstreams.indexOf(result.upstream) === -1) {
            upstreams.push(result.upstream)
        }
        if (result.ok) {
            cancelledOrderIds.push(orderId)
        } else {
            failedOrderIds.push({
                order_id: orderId,
                error: ibkrActionsPickFirstNonEmpty([
                    result.payload && result.payload.error,
                    result.payload && result.payload.message,
                    result.payload && result.payload.result && result.payload.result.error,
                    `status_${result.statusCode || 500}`,
                ]),
            })
        }
    }

    if (failedOrderIds.length > 0) {
        return {
            ok: false,
            error: `IBKR 撤单失败 ${failedOrderIds.length} 条`,
            cancelled_order_ids: cancelledOrderIds,
            failed_order_ids: failedOrderIds,
            updated_record_ids: [],
            upstreams: upstreams,
        }
    }

    const primaryRecord = ibkrActionsPickPrimaryOrderRecord(records, records[0] || null)
    const tradeGroupId = ibkrActionsResolveTradeGroupId(primaryRecord || records[0] || null)
    const updatedRecordIds = []
    records.forEach((groupRecord) => {
        const currentStatus = ibkrActionsNormalizeOrderRecord(groupRecord).status
        if (ibkrActionsIsOrderClosedStatus(currentStatus)) {
            return
        }
        groupRecord.set("status", "Canceled")
        if (typeof applyOrderRelationship === "function") {
            applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: "Canceled",
            }, false)
        }
        let eventTimes = {
            us_time: groupRecord.get("us_time") || "",
            cn_time: groupRecord.get("cn_time") || "",
            bar_time_ms: groupRecord.get("bar_time_ms") || 0,
        }
        if (typeof applyOrderStatusMeta === "function") {
            const metaResult = applyOrderStatusMeta(groupRecord, {
                status: "Canceled",
                previous_status: currentStatus,
                source: source,
                reason: reason,
            }, false)
            if (metaResult && metaResult.eventTimes) {
                eventTimes = metaResult.eventTimes
            }
        }
        $app.save(groupRecord)
        updatedRecordIds.push(ibkrActionsPickFirstNonEmpty([groupRecord.get("id"), groupRecord.id, groupRecord.get("unique_id")]))
        if (typeof appendOrderDetail === "function") {
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
                        action: "cancel_sync",
                        trade_group_id: tradeGroupId,
                        cancelled_order_ids: cancelledOrderIds,
                    },
                })
            } catch (detailErr) {
                console.error("[IBKRActions] 写入 ibkr_order_details 失败:", detailErr)
            }
        }
    })

    if (primaryRecord && typeof notifyOrder === "function" && typeof getOrderExtra === "function" && typeof mergeOrderExtra === "function") {
        try {
            const orderExtra = getOrderExtra(primaryRecord)
            const syncResult = notifyOrder("canceled", primaryRecord, {
                messageId: orderExtra.feishu_order_message_id || "",
                message: cancelledOrderIds.length > 0 ? "主单与系统订单已同步撤销" : "交易组已取消",
            })
            if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
                mergeOrderExtra(primaryRecord, {
                    feishu_order_message_id: syncResult.message_id,
                    feishu_order_card_version: 2,
                }, true)
            }
        } catch (notifyErr) {
            console.error("[IBKRActions] 同步订单卡片失败:", notifyErr)
        }
    }

    return {
        ok: true,
        trade_group_id: tradeGroupId,
        cancelled_order_ids: cancelledOrderIds,
        failed_order_ids: [],
        updated_record_ids: updatedRecordIds,
        upstreams: upstreams,
    }
}
globalThis.ibkrActionsCancelOrderGroupRecords = ibkrActionsCancelOrderGroupRecords

function ibkrActionsBuildAccountRelationContext(environment, symbols) {
    const normalizedSymbols = []
    const seenSymbols = {}
    for (let i = 0; i < (symbols || []).length; i++) {
        const symbol = ibkrActionsToText(symbols[i]).toUpperCase()
        if (!symbol || seenSymbols[symbol]) continue
        seenSymbols[symbol] = true
        normalizedSymbols.push(symbol)
    }
    if (!normalizedSymbols.length) {
        return {
            ordersBySymbol: {},
            activeGroupBySymbol: {},
            signalMap: {},
        }
    }

    const orderClauses = []
    const orderParams = { env: environment }
    for (let i = 0; i < normalizedSymbols.length; i++) {
        const key = `sym${i}`
        orderClauses.push(`symbol = {:${key}}`)
        orderParams[key] = normalizedSymbols[i]
    }

    const orderFilter = `environment = {:env} && (${orderClauses.join(" || ")})`
    let orderRecords = []
    try {
        orderRecords = $app.findRecordsByFilter("orders", orderFilter, "-updated", 400, 0, orderParams) || []
    } catch (_) {
        orderRecords = []
    }

    const ordersBySymbol = {}
    const groupsBySymbol = {}
    const signalIds = {}

    for (let i = 0; i < orderRecords.length; i++) {
        const normalized = ibkrActionsNormalizeOrderRecord(orderRecords[i])
        if (!normalized.symbol) continue
        if (!ordersBySymbol[normalized.symbol]) ordersBySymbol[normalized.symbol] = []
        ordersBySymbol[normalized.symbol].push(normalized)

        const groupKey = normalized.trade_group_id || normalized.entry_order_unique_id || normalized.unique_id || normalized.broker_order_id
        if (!groupKey) continue
        if (!groupsBySymbol[normalized.symbol]) groupsBySymbol[normalized.symbol] = {}
        if (!groupsBySymbol[normalized.symbol][groupKey]) {
            groupsBySymbol[normalized.symbol][groupKey] = {
                trade_group_id: normalized.trade_group_id || groupKey,
                entry_order_unique_id: normalized.entry_order_unique_id || normalized.unique_id || groupKey,
                signal_id: normalized.signal_id,
                symbol: normalized.symbol,
                latest_updated_ms: 0,
                latest_updated: "",
                latest_order_status: "",
                best_status_weight: -1,
                entry_filled_qty: 0,
                exit_filled_qty: 0,
                has_active_order: false,
                has_open_exposure: false,
                orders: [],
            }
        }

        const group = groupsBySymbol[normalized.symbol][groupKey]
        group.orders.push(normalized)
        if (normalized.signal_id && !group.signal_id) group.signal_id = normalized.signal_id
        if (normalized.updated_ms >= group.latest_updated_ms) {
            group.latest_updated_ms = normalized.updated_ms
            group.latest_updated = normalized.updated
        }
        if (normalized.status_weight >= group.best_status_weight) {
            group.best_status_weight = normalized.status_weight
            group.latest_order_status = normalized.status
        }

        const isEntry = normalized.role === "entry"
        const isExit = normalized.role === "take_profit" || normalized.role === "stop_loss"
        if (isEntry) group.entry_filled_qty += Math.abs(normalized.filled_qty)
        if (isExit) group.exit_filled_qty += Math.abs(normalized.filled_qty)
        if (normalized.relation_status === "active" || normalized.relation_status === "planned") group.has_active_order = true
        if (normalized.status_weight >= 70 && ibkrActionsToText(normalized.status).toUpperCase() !== "FILLED") group.has_active_order = true
    }

    const activeGroupBySymbol = {}
    const groupSignalIds = {}
    Object.keys(groupsBySymbol).forEach((symbol) => {
        const groups = Object.keys(groupsBySymbol[symbol]).map((key) => {
            const group = groupsBySymbol[symbol][key]
            group.has_open_exposure = group.entry_filled_qty > group.exit_filled_qty
            if (group.signal_id) groupSignalIds[group.signal_id] = true
            return group
        })

        groups.sort((left, right) => {
            const leftScore = (left.has_open_exposure ? 1000 : 0) + (left.has_active_order ? 100 : 0) + (left.best_status_weight || 0)
            const rightScore = (right.has_open_exposure ? 1000 : 0) + (right.has_active_order ? 100 : 0) + (right.best_status_weight || 0)
            if (leftScore !== rightScore) return rightScore - leftScore
            return (right.latest_updated_ms || 0) - (left.latest_updated_ms || 0)
        })

        if (groups.length) activeGroupBySymbol[symbol] = groups[0]
    })

    const signalIdList = Object.keys(groupSignalIds)
    const signalMap = {}
    if (signalIdList.length) {
        const signalClauses = []
        const signalParams = { env: environment }
        for (let i = 0; i < signalIdList.length; i++) {
            const key = `sid${i}`
            signalClauses.push(`signal_id = {:${key}}`)
            signalParams[key] = signalIdList[i]
        }

        try {
            const signalRecords = $app.findRecordsByFilter(
                "ibkr_signals",
                `environment = {:env} && (${signalClauses.join(" || ")})`,
                "-updated",
                400,
                0,
                signalParams
            ) || []
            for (let i = 0; i < signalRecords.length; i++) {
                const normalized = ibkrActionsNormalizeSignalRecord(signalRecords[i])
                if (!normalized.signal_id) continue
                const existing = signalMap[normalized.signal_id]
                if (!existing || normalized.status_weight >= existing.status_weight || normalized.updated_ms >= existing.updated_ms) {
                    signalMap[normalized.signal_id] = normalized
                }
            }
        } catch (_) {}
    }

    return {
        ordersBySymbol: ordersBySymbol,
        activeGroupBySymbol: activeGroupBySymbol,
        signalMap: signalMap,
    }
}

function ibkrActionsEnrichAccountSnapshot(payload, environment) {
    if (!payload || typeof payload !== "object") return payload

    const positions = Array.isArray(payload.positions) ? payload.positions : []
    const symbols = positions.map((item) => ibkrActionsToText(item && item.symbol).toUpperCase()).filter(Boolean)
    const context = ibkrActionsBuildAccountRelationContext(environment, symbols)

    let systemManagedCount = 0
    let externalCount = 0
    let flatCount = 0

    payload.positions = positions.map((position) => {
        const normalizedSymbol = ibkrActionsToText(position && position.symbol).toUpperCase()
        const quantity = ibkrActionsToNumber(position && position.quantity, 0)
        const activeGroup = context.activeGroupBySymbol[normalizedSymbol] || null
        const relatedSignal = activeGroup && activeGroup.signal_id ? context.signalMap[activeGroup.signal_id] || null : null

        let relation = null
        if (quantity === 0) {
            flatCount += 1
            relation = {
                status: "flat_legacy",
                reason: "gateway_flat_position_record",
                signal_id: activeGroup ? activeGroup.signal_id || "" : "",
                signal_status: relatedSignal ? relatedSignal.status || "" : "",
                trade_group_id: activeGroup ? activeGroup.trade_group_id || "" : "",
                entry_order_unique_id: activeGroup ? activeGroup.entry_order_unique_id || "" : "",
                last_order_status: activeGroup ? activeGroup.latest_order_status || "" : "",
                order_updated: activeGroup ? activeGroup.latest_updated || "" : "",
            }
        } else if (activeGroup && activeGroup.has_open_exposure) {
            systemManagedCount += 1
            relation = {
                status: "system_managed",
                reason: "matched_open_trade_group",
                signal_id: activeGroup.signal_id || "",
                signal_status: relatedSignal ? relatedSignal.status || "" : "",
                signal_note: relatedSignal ? relatedSignal.note || "" : "",
                trade_group_id: activeGroup.trade_group_id || "",
                entry_order_unique_id: activeGroup.entry_order_unique_id || "",
                last_order_status: activeGroup.latest_order_status || "",
                order_updated: activeGroup.latest_updated || "",
                order_count: Array.isArray(activeGroup.orders) ? activeGroup.orders.length : 0,
            }
        } else {
            externalCount += 1
            relation = {
                status: "external_position",
                reason: "no_system_order_link",
                signal_id: "",
                signal_status: "",
                trade_group_id: "",
                entry_order_unique_id: "",
                last_order_status: "",
                order_updated: "",
            }
        }

        return {
            ...position,
            relation: relation,
        }
    })

    payload.counts = {
        ...(payload.counts || {}),
        system_managed_positions: systemManagedCount,
        external_positions: externalCount,
        flat_positions: flatCount,
    }

    return payload
}
globalThis.ibkrActionsEnrichAccountSnapshot = ibkrActionsEnrichAccountSnapshot

function ibkrActionsHttpStatusCode(resp, fallback) {
    const code = Number(resp && resp.statusCode)
    return Number.isFinite(code) && code > 0 ? Math.trunc(code) : (fallback || 200)
}

function ibkrActionsSendJson(c, statusCode, payload) {
    return c.json(Number(statusCode) || 200, payload || {})
}
globalThis.ibkrActionsSendJson = ibkrActionsSendJson

function ibkrActionsFetchProxyPayload(route, upstream, method, body, timeoutSec) {
    const requestOptions = {
        url: upstream,
        method: method || "GET",
        timeout: timeoutSec || 20,
    }
    if (body !== undefined && body !== null) {
        requestOptions.body = JSON.stringify(body)
        requestOptions.headers = { "Content-Type": "application/json" }
    }

    const resp = $http.send(requestOptions)
    return {
        statusCode: (Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200),
        payload: ibkrActionsBuildProxyMeta(ibkrActionsParseHttpJson(resp.raw), route, upstream),
    }
}
globalThis.ibkrActionsFetchProxyPayload = ibkrActionsFetchProxyPayload

function ibkrActionsProxyRequest(c, route, upstream, method, body, timeoutSec) {
    try {
        const result = globalThis.ibkrActionsFetchProxyPayload(route, upstream, method, body, timeoutSec)
        return ibkrActionsSendJson(c, result.statusCode, result.payload)
    } catch (err) {
        console.error(`[IBKRActions] proxy error route=${route}: ${err.message || err}`)
        return c.json(
            502,
            globalThis.ibkrActionsBuildProxyMeta(
                { ok: false, status: "offline", error: err.message || String(err) },
                route,
                upstream
            )
        )
    }
}
globalThis.ibkrActionsProxyRequest = ibkrActionsProxyRequest

function ibkrActionsBuildIndicatorData(d, environment) {
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const interval = String(d.interval || "").trim()
    const barTimeMs = Number(d.bar_time_ms)

    if (!symbol || !interval || !Number.isFinite(barTimeMs) || barTimeMs <= 0) {
        return { ok: false, error: "Missing symbol/interval/bar_time_ms" }
    }

    return {
        ok: true,
        symbol: symbol,
        interval: interval,
        bar_time_ms: Math.trunc(barTimeMs),
        filter: "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}",
        params: { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment },
        data: {
            symbol: symbol,
            environment: environment,
            exchange: String(d.exchange || "").trim().toUpperCase(),
            interval: interval,
            script_tag: String(d.script_tag || "").trim(),
            us_time: String(d.us_time || "").trim(),
            cn_time: String(d.cn_time || "").trim(),
            bar_time_ms: Math.trunc(barTimeMs),
            bar_index: d.bar_index != null ? Number(d.bar_index) : null,
            extra: {
                ...(d.extra || {}),
                environment: environment,
                source: (d.extra && d.extra.source) ? d.extra.source : "ibkr_compute",
            },
        },
    }
}
globalThis.ibkrActionsBuildIndicatorData = ibkrActionsBuildIndicatorData

function ibkrActionsBuildSignalData(d, environment) {
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const signalId = String(d.signal_id || "").trim()

    if (!symbol || !signalId) {
        return { ok: false, error: "Missing symbol or signal_id" }
    }

    return {
        ok: true,
        symbol: symbol,
        signal_id: signalId,
        filter: "signal_id = {:sid} && environment = {:env}",
        params: { sid: signalId, env: environment },
        data: {
            symbol: symbol,
            environment: environment,
            direction: String(d.direction || "").trim(),
            signal: String(d.signal || "").trim(),
            limit_price: Number(d.limit_price) || 0,
            entry: Number(d.entry) || 0,
            stop_loss: Number(d.stop_loss) || 0,
            take_profit: Number(d.take_profit) || 0,
            rr: String(d.rr || ""),
            shares: Number(d.shares) || 0,
            signal_id: signalId,
            exchange: String(d.exchange || "").trim().toUpperCase(),
            interval: String(d.interval || "").trim(),
            reason: String(d.reason || ""),
            us_time: String(d.us_time || "").trim(),
            cn_time: String(d.cn_time || "").trim(),
            date: String(d.date || "").trim(),
            bar_time_ms: d.bar_time_ms ? Math.trunc(Number(d.bar_time_ms)) : 0,
            bar_index: d.bar_index != null ? Number(d.bar_index) : null,
            script_tag: String(d.script_tag || "").trim(),
            chart_tf: String(d.chart_tf || "").trim(),
            extra: {
                ...(d.extra || {}),
                source: (d.extra && d.extra.source) ? d.extra.source : "ibkr_compute",
                environment: environment,
            },
            status: String(d.status || "pending"),
            note: String(d.note || ""),
        },
    }
}
globalThis.ibkrActionsBuildSignalData = ibkrActionsBuildSignalData

function ibkrActionsUpsertConfigValue(key, value, environment, extras) {
    const runtimeEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    let record = null
    try {
        record = $app.findFirstRecordByFilter(
            "config",
            "key = {:key} && environment = {:env}",
            { key: String(key || ""), env: runtimeEnvironment }
        )
    } catch (_) {}

    const collection = $app.findCollectionByNameOrId("config")
    if (!record) {
        record = new Record(collection, {})
        record.set("key", String(key || ""))
        record.set("environment", runtimeEnvironment)
    }

    const meta = extras || {}
    if (meta.display_name) record.set("display_name", String(meta.display_name))
    if (meta.description) record.set("description", String(meta.description))
    if (meta.group_name) record.set("group_name", String(meta.group_name))
    if (meta.default_value != null) record.set("default_value", String(meta.default_value))
    if (meta.sort_order != null) record.set("sort_order", Number(meta.sort_order) || 0)
    record.set("value", String(value == null ? "" : value))
    $app.save(record)
    return record
}
globalThis.ibkrActionsUpsertConfigValue = ibkrActionsUpsertConfigValue

function ibkrActionsParseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") {
        return fallback
    }
    if (typeof value === "boolean") return value
    const normalized = String(value || "").trim().toLowerCase()
    if (["true", "1", "yes", "y"].indexOf(normalized) !== -1) return true
    if (["false", "0", "no", "n"].indexOf(normalized) !== -1) return false
    return fallback
}
globalThis.ibkrActionsParseBoolean = ibkrActionsParseBoolean

function ibkrActionsCloneObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return {}
    }
    return { ...value }
}
globalThis.ibkrActionsCloneObject = ibkrActionsCloneObject

function ibkrActionsBuildStatuszComputePayload(computePayload, includeEngines) {
    const payload = ibkrActionsCloneObject(computePayload)
    const engineMap = payload.engines && typeof payload.engines === "object" && !Array.isArray(payload.engines)
        ? payload.engines
        : {}
    const totalEngines = Number(payload.total_engines || 0) || Object.keys(engineMap).length

    payload.total_engines = totalEngines
    payload.ready_engines = Number(payload.ready_engines || 0) || 0
    payload.engines_available = totalEngines > 0
    payload.engines_included = Boolean(includeEngines)
    payload.statusz_mode = includeEngines ? "full" : "lite"

    if (includeEngines) {
        payload.engines = engineMap
    } else {
        delete payload.engines
    }

    return payload
}
globalThis.ibkrActionsBuildStatuszComputePayload = ibkrActionsBuildStatuszComputePayload

function ibkrActionsTrimArray(values, limit) {
    if (!Array.isArray(values)) return []
    const maxItems = Math.max(0, Number(limit) || 0)
    return maxItems > 0 ? values.slice(0, maxItems) : []
}
globalThis.ibkrActionsTrimArray = ibkrActionsTrimArray

function ibkrActionsTrimObjectEntries(value, limit) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        return {}
    }
    const maxItems = Math.max(0, Number(limit) || 0)
    const entries = Object.entries(value)
    if (!maxItems || entries.length <= maxItems) {
        return { ...value }
    }
    const trimmed = {}
    entries.slice(0, maxItems).forEach(([key, item]) => {
        trimmed[key] = item
    })
    return trimmed
}
globalThis.ibkrActionsTrimObjectEntries = ibkrActionsTrimObjectEntries

function ibkrActionsBuildStatuszRuntimePayload(runtimePayload, includeWarmupDetails) {
    const payload = ibkrActionsCloneObject(runtimePayload)
    const gateway = ibkrActionsCloneObject(payload.gateway)
    const session = ibkrActionsCloneObject(payload.session)
    const websocket = ibkrActionsCloneObject(payload.websocket)
    const dataBackfill = ibkrActionsCloneObject(payload.data_backfill)
    const orderTracker = ibkrActionsCloneObject(payload.order_tracker)
    const warmup = ibkrActionsCloneObject(payload.warmup)
    const realtimeCompute = ibkrActionsCloneObject(payload.realtime_compute)
    const realtimeResult = ibkrActionsCloneObject(realtimeCompute.last_result)
    const marketUniverse = ibkrActionsCloneObject(payload.market_universe)

    const activeTradeSymbols = ibkrActionsTrimArray(
        marketUniverse.active_trade_symbols,
        Array.isArray(marketUniverse.active_trade_symbols) ? marketUniverse.active_trade_symbols.length : 0
    )
    const pendingSymbols = ibkrActionsTrimArray(warmup.pending_symbols, 12)
    const fullPendingSymbols = ibkrActionsTrimArray(
        warmup.pending_symbols,
        Array.isArray(warmup.pending_symbols) ? warmup.pending_symbols.length : 0
    )
    const activeRepairSymbols = ibkrActionsTrimArray(marketUniverse.last_active_repair_symbols, 12)
    const fullSymbolStatus = includeWarmupDetails
        ? ibkrActionsTrimArray(
            warmup.symbol_status,
            Array.isArray(warmup.symbol_status) ? warmup.symbol_status.length : 0
        )
        : []
    const integrityPendingSymbols = includeWarmupDetails
        ? ibkrActionsTrimArray(
            warmup.integrity_pending_symbols,
            Array.isArray(warmup.integrity_pending_symbols) ? warmup.integrity_pending_symbols.length : 0
        )
        : []
    const readySymbolsList = includeWarmupDetails
        ? ibkrActionsTrimArray(
            warmup.ready_symbols_list,
            Array.isArray(warmup.ready_symbols_list) ? warmup.ready_symbols_list.length : 0
        )
        : []
    const warmupSymbols = includeWarmupDetails
        ? ibkrActionsTrimArray(
            warmup.symbols,
            Array.isArray(warmup.symbols) ? warmup.symbols.length : 0
        )
        : []
    const warmupTradeSymbols = includeWarmupDetails
        ? ibkrActionsTrimArray(
            warmup.trade_symbols,
            Array.isArray(warmup.trade_symbols) ? warmup.trade_symbols.length : 0
        )
        : []
    const warmupMonitorSymbols = includeWarmupDetails
        ? ibkrActionsTrimArray(
            warmup.monitor_symbols,
            Array.isArray(warmup.monitor_symbols) ? warmup.monitor_symbols.length : 0
        )
        : []
    const integrityRepairReasons = includeWarmupDetails
        ? ibkrActionsCloneObject(warmup.integrity_repair_reasons)
        : {}
    const preflightRepair = includeWarmupDetails
        ? ibkrActionsCloneObject(warmup.preflight_repair)
        : {}

    return {
        ok: payload.ok,
        starting: Boolean(payload.starting),
        startup_complete: Boolean(payload.startup_complete),
        runtime_phase: String(payload.runtime_phase || ""),
        environment: String(payload.environment || ""),
        warmup_details_included: Boolean(includeWarmupDetails),
        gateway: {
            running: Boolean(gateway.running),
            reachable: Boolean(gateway.reachable),
            managed_by: String(gateway.managed_by || ""),
            status_code: Number(gateway.status_code || 0) || 0,
            pid: Number(gateway.pid || 0) || 0,
            uptime_s: Number(gateway.uptime_s || 0) || 0,
        },
        session: {
            authenticated: Boolean(session.authenticated),
            running: Boolean(session.running),
            consecutive_failures: Number(session.consecutive_failures || 0) || 0,
            last_tickle: session.last_tickle || "",
        },
        websocket: {
            connected: Boolean(websocket.connected),
            ready: Boolean(websocket.ready),
            running: Boolean(websocket.running),
            last_message: websocket.last_message || "",
            message_count: Number(websocket.message_count || 0) || 0,
            subscribed_count: Array.isArray(websocket.subscribed_conids) ? websocket.subscribed_conids.length : (Number(websocket.subscribed_count || 0) || 0),
            pending_count: Array.isArray(websocket.pending_conids) ? websocket.pending_conids.length : (Number(websocket.pending_count || 0) || 0),
        },
        data_backfill: {
            total_backfilled: Number(dataBackfill.total_backfilled || 0) || 0,
        },
        order_tracker: {
            running: Boolean(orderTracker.running),
            last_poll: orderTracker.last_poll || "",
            tracked_orders: Number(orderTracker.tracked_orders || 0) || 0,
        },
        warmup: {
            phase: String(warmup.phase || ""),
            trading_gate_open: Boolean(warmup.trading_gate_open),
            trading_gate_reason: String(warmup.trading_gate_reason || ""),
            required_interval: String(warmup.required_interval || ""),
            symbols_total: Number(warmup.symbols_total || 0) || 0,
            trade_symbols_total: Number(warmup.trade_symbols_total || 0) || 0,
            monitor_symbols_total: Number(warmup.monitor_symbols_total || 0) || 0,
            ready_symbols: Number(warmup.ready_symbols || 0) || 0,
                ready_trade_symbols: Number(warmup.ready_trade_symbols || 0) || 0,
                ready_monitor_symbols: Number(warmup.ready_monitor_symbols || 0) || 0,
                pending_symbols: includeWarmupDetails ? fullPendingSymbols : pendingSymbols,
                pending_symbols_total: Array.isArray(warmup.pending_symbols) ? warmup.pending_symbols.length : (Number(warmup.pending_symbols_total || 0) || 0),
                requested_at: warmup.requested_at || "",
                started_at: warmup.started_at || "",
                finished_at: warmup.finished_at || "",
                last_success_at: warmup.last_success_at || "",
                last_error: String(warmup.last_error || ""),
                reason: String(warmup.reason || ""),
                target_date: String(warmup.target_date || ""),
                symbols: warmupSymbols,
                trade_symbols: warmupTradeSymbols,
                monitor_symbols: warmupMonitorSymbols,
                ready_symbols_list: readySymbolsList,
                symbol_status: fullSymbolStatus,
                integrity_pending_symbols: integrityPendingSymbols,
                integrity_pending_symbols_total: Array.isArray(warmup.integrity_pending_symbols) ? warmup.integrity_pending_symbols.length : 0,
                integrity_repair_reasons: integrityRepairReasons,
                preflight_repair: preflightRepair,
            },
            realtime_compute: {
                runs: Number(realtimeCompute.runs || 0) || 0,
                queue_size: Number(realtimeCompute.queue_size || 0) || 0,
                last_run: realtimeCompute.last_run || "",
            last_bar_close: realtimeCompute.last_bar_close || "",
            last_elapsed_s: Number(realtimeResult.elapsed_s || 0) || 0,
            last_processed: Number(realtimeResult.processed || 0) || 0,
            last_signals: Number(realtimeResult.signals || 0) || 0,
            last_errors: Number(realtimeResult.errors || 0) || 0,
        },
        market_universe: {
            market_date: String(marketUniverse.market_date || ""),
            last_daily_reset: marketUniverse.last_daily_reset || "",
            watchlist_pool_count: Number(marketUniverse.watchlist_pool_count || 0) || 0,
            active_target_date: String(marketUniverse.active_target_date || ""),
            active_target_count: Number(marketUniverse.active_target_count || 0) || 0,
            active_trade_symbols: activeTradeSymbols,
            active_trade_symbols_total: Array.isArray(marketUniverse.active_trade_symbols) ? marketUniverse.active_trade_symbols.length : (Number(marketUniverse.active_trade_symbols_total || 0) || 0),
            last_target_refresh: marketUniverse.last_target_refresh || "",
            active_repair_interval_min: Number(marketUniverse.active_repair_interval_min || 0) || 0,
            last_active_repair: marketUniverse.last_active_repair || "",
            last_active_repair_symbols: activeRepairSymbols,
            last_active_repair_symbols_total: Array.isArray(marketUniverse.last_active_repair_symbols) ? marketUniverse.last_active_repair_symbols.length : (Number(marketUniverse.last_active_repair_symbols_total || 0) || 0),
            last_active_repair_reasons: ibkrActionsTrimObjectEntries(marketUniverse.last_active_repair_reasons, 12),
            watchlist_backfill_interval_min: Number(marketUniverse.watchlist_backfill_interval_min || 0) || 0,
            last_watchlist_backfill: marketUniverse.last_watchlist_backfill || "",
        },
        runtime_control: ibkrActionsCloneObject(payload.runtime_control),
    }
}
globalThis.ibkrActionsBuildStatuszRuntimePayload = ibkrActionsBuildStatuszRuntimePayload

function ibkrActionsSerializeBarIntegrityRecord(record) {
    if (!record) return null
    return {
        id: record.id || "",
        environment: record.get("environment") || "",
        market_date: record.get("market_date") || "",
        symbol: record.get("symbol") || "",
        interval: record.get("interval") || "5m",
        scan_scope: record.get("scan_scope") || "",
        status: record.get("status") || "",
        needs_repair: Boolean(record.get("needs_repair")),
        safe_repair: Boolean(record.get("safe_repair")),
        bar_count: Number(record.get("bar_count") || 0) || 0,
        latest_bar_time_ms: Number(record.get("latest_bar_time_ms") || 0) || 0,
        latest_bar_us_time: record.get("latest_bar_us_time") || "",
        oldest_loaded_ms: Number(record.get("oldest_loaded_ms") || 0) || 0,
        gap_count: Number(record.get("gap_count") || 0) || 0,
        duplicate_count: Number(record.get("duplicate_count") || 0) || 0,
        bad_ohlc_count: Number(record.get("bad_ohlc_count") || 0) || 0,
        missing_intervals: record.get("missing_intervals") || [],
        stale_intervals: record.get("stale_intervals") || [],
        gap_examples: record.get("gap_examples") || [],
        duplicate_examples: record.get("duplicate_examples") || [],
        bad_ohlc_examples: record.get("bad_ohlc_examples") || [],
        repair_attempts: Number(record.get("repair_attempts") || 0) || 0,
        last_scan_at: record.get("last_scan_at") || "",
        last_repair_at: record.get("last_repair_at") || "",
        last_repair_result: record.get("last_repair_result") || {},
        extra: record.get("extra") || {},
        created: record.get("created") || "",
        updated: record.get("updated") || "",
    }
}
globalThis.ibkrActionsSerializeBarIntegrityRecord = ibkrActionsSerializeBarIntegrityRecord

routerAdd("GET", "/api/custom/ibkr/ping_write", (c) => {
    try {
        const signalId = "PING_" + Date.now()
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_signals",
                "signal_id = {:sid} && environment = {:env}",
                { sid: signalId, env: "live" }
            )
        } catch (_) {}

        const col = $app.findCollectionByNameOrId("ibkr_signals")
        if (!record) {
            record = new Record(col, {})
        }

        record.set("symbol", "AAPL")
        record.set("environment", "live")
        record.set("direction", "long")
        record.set("signal", "ping_write")
        record.set("limit_price", 0)
        record.set("entry", 100)
        record.set("stop_loss", 99)
        record.set("take_profit", 101)
        record.set("rr", "1.00")
        record.set("shares", 1)
        record.set("signal_id", signalId)
        record.set("exchange", "NASDAQ")
        record.set("interval", "5")
        record.set("reason", "ping_write")
        record.set("us_time", "2026-04-02 11:24:00")
        record.set("cn_time", "2026-04-02 23:24:00")
        record.set("date", "2026-04-02")
        record.set("bar_time_ms", Date.now())
        record.set("bar_index", 1)
        record.set("script_tag", "ping")
        record.set("chart_tf", "5")
        record.set("extra", { source: "ping_write", environment: "live" })
        record.set("status", "pending")
        record.set("note", "")
        $app.save(record)

        return c.json(200, { ok: true, signal_id: signalId, id: record.id || "" })
    } catch (err) {
        console.error(`[IBKRActions] ping_write error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

// ══════════════════════════════════════
// 批量OHLCV接收 → ibkr_bars
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/bars", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const bars = d.bars || []
    const { getRuntimeEnvironmentFromData, getConfigValue, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { isEnabledConfigValue } = require(`${__hooks}/lib/runtime_modes.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const defaultEnvironment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    if (!bars.length) {
        return c.json(400, { ok: false, error: "Empty bars array" })
    }

    const enabled = getConfigValue("ibkr_bar_publish_enabled", "true", defaultEnvironment)
    if (!isEnabledConfigValue(enabled)) {
        return c.json(200, {
            ok: true,
            skipped: true,
            reason: "ibkr_bar_publish_enabled=false",
            config_value: String(enabled || ""),
        })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let errors = 0

    for (let i = 0; i < bars.length; i++) {
        const bar = bars[i]
        const symbol = String(bar.symbol || "").trim().toUpperCase()
        const interval = String(bar.interval || "").trim()
        const barTimeMs = Number(bar.bar_time_ms)
        const environment = getRuntimeEnvironmentFromData(bar, defaultEnvironment)

        if (!symbol || !interval || !Number.isFinite(barTimeMs) || barTimeMs <= 0) {
            errors++
            continue
        }

        try {
            const result = actionHelpers.upsertRecord(
                "ibkr_bars",
                "symbol = {:sym} && interval = {:tf} && bar_time_ms = {:ms} && environment = {:env}",
                { sym: symbol, tf: interval, ms: Math.trunc(barTimeMs), env: environment },
                {
                    symbol: symbol,
                    environment: environment,
                    exchange: String(bar.exchange || "").trim().toUpperCase(),
                    interval: interval,
                    open: Number(bar.open) || 0,
                    high: Number(bar.high) || 0,
                    low: Number(bar.low) || 0,
                    close: Number(bar.close) || 0,
                    volume: Number(bar.volume) || 0,
                    session_type: String(bar.session_type || "").trim(),
                    us_time: String(bar.us_time || "").trim(),
                    cn_time: String(bar.cn_time || "").trim(),
                    bar_time_ms: Math.trunc(barTimeMs),
                    extra: bar.extra || {},
                },
            )
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] bars upsert error: ${symbol}/${interval}/${barTimeMs}: ${err.message}`)
        }
    }

    console.log(`[IBKRActions] bars: received=${bars.length}, created=${created}, updated=${updated}, skipped=${skipped}, errors=${errors}`)
    return c.json(200, { ok: true, received: bars.length, created, updated, skipped, errors })
})

// ══════════════════════════════════════
// 指标写入 -> ibkr_indicators
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/indicator", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const prepared = actionHelpers.buildIndicatorData(d, environment)
    if (!prepared.ok) {
        return c.json(400, { ok: false, error: prepared.error || "invalid_indicator_payload" })
    }

    try {
        const result = actionHelpers.upsertRecord("ibkr_indicators", prepared.filter, prepared.params, prepared.data)
        return c.json(200, {
            ok: true,
            symbol: prepared.symbol,
            interval: prepared.interval,
            collection: "ibkr_indicators",
            action: result.action,
        })
    } catch (err) {
        console.error(`[IBKRActions] indicator upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

routerAdd("POST", "/api/custom/ibkr/indicators", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const items = Array.isArray(d.items) ? d.items : []
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const defaultEnvironment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    if (!items.length) {
        return c.json(400, { ok: false, error: "Empty indicators array" })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let errors = 0
    for (let i = 0; i < items.length; i++) {
        const environment = getRuntimeEnvironmentFromData(items[i], defaultEnvironment)
        const prepared = actionHelpers.buildIndicatorData(items[i], environment)
        if (!prepared.ok) {
            errors++
            continue
        }
        try {
            const result = actionHelpers.upsertRecord("ibkr_indicators", prepared.filter, prepared.params, prepared.data)
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] indicators upsert error: ${prepared.symbol}/${prepared.interval}/${prepared.bar_time_ms}: ${err.message}`)
        }
    }

    return c.json(200, {
        ok: errors === 0,
        received: items.length,
        success: created + updated,
        created: created,
        updated: updated,
        skipped: skipped,
        errors: errors,
        collection: "ibkr_indicators",
    })
})

// ══════════════════════════════════════
// 信号写入 -> ibkr_signals
routerAdd("POST", "/api/custom/ibkr/signal", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const signalLifecycle = require(`${__hooks}/lib/ibkr_signal_lifecycle.js`)
    const environment = String(d.environment || "live").trim().toLowerCase() || "live"
    const prepared = actionHelpers.buildSignalData(d, environment)
    if (!prepared.ok) {
        return c.json(400, { ok: false, error: prepared.error || "invalid_signal_payload" })
    }

    try {
        let existing = null
        try {
            existing = $app.findFirstRecordByFilter("ibkr_signals", prepared.filter, prepared.params)
        } catch (_) {}
        if (!existing) {
            const duplicate = actionHelpers.findSignalDuplicateByBarKey(prepared.data, environment, prepared.signal_id)
            if (duplicate) {
                actionHelpers.annotateSignalDuplicate(duplicate, prepared.data, environment)
                return c.json(200, {
                    ok: true,
                    signal_id: String(duplicate.get("signal_id") || "").trim() || prepared.signal_id,
                    duplicate_signal_id: prepared.signal_id,
                    target: "ibkr_signals",
                    id: duplicate.id,
                    action: "skipped_duplicate_bar_signal",
                    status: String(duplicate.get("status") || "").trim(),
                    dedupe_key: actionHelpers.buildSignalBarDedupeKey(prepared.data, environment),
                })
            }
        }
        signalLifecycle.prepareSignalLifecycle(prepared, existing, environment)
        const result = actionHelpers.upsertRecord("ibkr_signals", prepared.filter, prepared.params, prepared.data)
        if (result.action !== "skipped") {
            signalLifecycle.syncSignalNotification(result.record, existing ? String(existing.get("status") || "").trim() : "")
        }
        return c.json(200, {
            ok: true,
            signal_id: prepared.signal_id,
            target: "ibkr_signals",
            id: (result.record && result.record.id) || "",
            action: result.action,
            status: (result.record && result.record.get("status")) || prepared.data.status,
        })
    } catch (err) {
        console.error(`[IBKRActions] signal upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message || String(err), target: "ibkr_signals" })
    }
})

routerAdd("POST", "/api/custom/ibkr/signals", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const items = Array.isArray(d.items) ? d.items : []
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const signalLifecycle = require(`${__hooks}/lib/ibkr_signal_lifecycle.js`)
    const defaultEnvironment = String(d.environment || "live").trim().toLowerCase() || "live"

    if (!items.length) {
        return c.json(400, { ok: false, error: "Empty signals array" })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let duplicates = 0
    let errors = 0
    for (let i = 0; i < items.length; i++) {
        const environment = String((items[i] && items[i].environment) || defaultEnvironment).trim().toLowerCase() || defaultEnvironment
        const prepared = actionHelpers.buildSignalData(items[i] || {}, environment)
        if (!prepared.ok) {
            errors++
            continue
        }
        try {
            let existing = null
            try {
                existing = $app.findFirstRecordByFilter("ibkr_signals", prepared.filter, prepared.params)
            } catch (_) {}
            if (!existing) {
                const duplicate = actionHelpers.findSignalDuplicateByBarKey(prepared.data, environment, prepared.signal_id)
                if (duplicate) {
                    actionHelpers.annotateSignalDuplicate(duplicate, prepared.data, environment)
                    duplicates++
                    skipped++
                    continue
                }
            }
            signalLifecycle.prepareSignalLifecycle(prepared, existing, environment)
            const result = actionHelpers.upsertRecord("ibkr_signals", prepared.filter, prepared.params, prepared.data)
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
            if (result.action !== "skipped") {
                signalLifecycle.syncSignalNotification(result.record, existing ? String(existing.get("status") || "").trim() : "")
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] signals upsert error: ${prepared.signal_id}: ${err.message}`)
        }
    }

    return c.json(200, {
        ok: errors === 0,
        received: items.length,
        success: created + updated,
        created: created,
        updated: updated,
        skipped: skipped,
        duplicates: duplicates,
        errors: errors,
        target: "ibkr_signals",
    })
})

// ══════════════════════════════════════
// IBKR 盘前扫描结果 → ibkr_targets
// ══════════════════════════════════════
routerAdd("POST", "/api/custom/ibkr/scan", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upsertRecord = function(collectionName, filterStr, filterParams, data) {
        let record = null
        try {
            record = $app.findFirstRecordByFilter(collectionName, filterStr, filterParams)
        } catch (_) {}

        const col = $app.findCollectionByNameOrId(collectionName)
        if (!record) {
            record = new Record(col, {})
        }
        Object.keys(data).forEach((key) => {
            record.set(key, data[key])
        })
        $app.save(record)
        return record
    }

    const symbol = String(d.symbol || "").trim().toUpperCase()
    const date = String(d.date || "").trim()

    if (!symbol || !date) {
        return c.json(400, { ok: false, error: "Missing symbol or date" })
    }

    const targetData = {
        symbol: symbol,
        environment: environment,
        exchange: String(d.exchange || "").trim().toUpperCase(),
        date: date,
        direction_bias: d.direction_bias || "neutral",
        score: Number(d.score) || 0,
        scan_reason: String(d.scan_reason || ""),
        status: d.status || "candidate",
        us_time: String(d.us_time || "").trim(),
        cn_time: String(d.cn_time || "").trim(),
        bar_time_ms: d.bar_time_ms ? Math.trunc(Number(d.bar_time_ms)) : 0,
        extra: d.extra || {},
    }

    try {
        upsertRecord(
            "ibkr_targets",
            "symbol = {:sym} && date = {:d} && environment = {:env}",
            { sym: symbol, d: date, env: environment },
            targetData
        )
        return c.json(200, { ok: true, symbol, date })
    } catch (err) {
        console.error(`[IBKRActions] scan upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

routerAdd("GET", "/api/custom/ibkr/today-targets", (c) => {
    try {
        const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
        const { buildTodayTargetPayload } = require(`${__hooks}/lib/ibkr_today_targets.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const query = c.request.url.query()
        const times = getTimeStrings()
        const marketDate = String(query.get("market_date") || query.get("date") || "").trim() || times.date

        return c.json(200, buildTodayTargetPayload({
            environment: environment,
            marketDate: marketDate,
        }))
    } catch (err) {
        console.error(`[IBKRActions] today-targets error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("GET", "/api/custom/ibkr/contracts/search", (c) => {
    try {
        const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const queryParams = c.request.url.query()
        const q = String(queryParams.get("q") || queryParams.get("query") || "").trim()
        const rawLimit = Number(queryParams.get("limit") || 12)
        const limit = Math.max(1, Math.min(24, Number.isFinite(rawLimit) ? Math.trunc(rawLimit) : 12))

        if (!q) {
            return c.json(400, { ok: false, error: "Missing q" })
        }

        const encodedQuery = String(q).replace(/%/g, "%25").replace(/ /g, "%20").replace(/\+/g, "%2B").replace(/#/g, "%23").replace(/&/g, "%26")
        const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/contracts/search?q=${encodedQuery}&limit=${limit}`
        const parsePayload = function(rawValue) {
            const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
            if (!raw) return {}
            try {
                return JSON.parse(raw)
            } catch (_) {
                return { ok: false, raw: raw }
            }
        }
        const response = $http.send({
            url: upstream,
            method: "GET",
            timeout: 20,
        })
        const payload = parsePayload(response.raw)
        if (payload && typeof payload === "object" && !Array.isArray(payload)) {
            payload.proxy_source = "pocketbase_ibkr_hook"
            payload.proxy_hook = "ibkr_actions.pb.js"
            payload.proxy_route = "/api/custom/ibkr/contracts/search"
            payload.proxy_upstream = upstream
        }
        return c.html((Number(response && response.statusCode) > 0 ? Number(response.statusCode) : 200), JSON.stringify(payload || {}))
    } catch (err) {
        console.error(`[IBKRActions] contracts search error: ${err.message || err}`)
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("POST", "/api/custom/ibkr/watchlist/upsert", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environmentUtils = require(`${__hooks}/lib/environment.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)

    const runtimeEnvironment = environmentUtils.getRuntimeEnvironmentFromData(d, environmentUtils.LIVE_ENVIRONMENT)
    const recordEnvironment = environmentUtils.normalizeConfigEnvironment(
        d.scope || d.target_environment || d.environment || runtimeEnvironment,
        runtimeEnvironment
    )
    const symbol = String(d.symbol || "").trim().toUpperCase()
    if (!symbol) {
        return c.json(400, { ok: false, error: "Missing symbol" })
    }

    const times = getTimeStrings()
    const note = String(d.note || "").trim()
    const exchange = String(d.exchange || "").trim().toUpperCase()
    const industry = String(
        d.industry
        || d.asset_class
        || (Array.isArray(d.sec_types) ? d.sec_types.join("/") : "")
        || d.description
        || ""
    ).trim()
    const requestedBarTimeMs = Number(d.bar_time_ms)
    const barTimeMs = Number.isFinite(requestedBarTimeMs) && requestedBarTimeMs > 0
        ? Math.trunc(requestedBarTimeMs)
        : Date.now()
    const usTime = String(d.us_time || times.us).trim()
    const cnTime = String(d.cn_time || times.cn).trim()

    let existing = null
    try {
        existing = $app.findFirstRecordByFilter(
            "watchlist",
            "symbol = {:sym} && environment = {:env}",
            { sym: symbol, env: recordEnvironment }
        )
    } catch (_) {}
    const symbolRole = globalThis.ibkrActionsNormalizeWatchlistRole(
        d.symbol_role || d.role || (existing ? existing.get("symbol_role") : "")
    )

    const compareData = {
        symbol: symbol,
        environment: recordEnvironment,
        exchange: exchange,
        industry: industry,
        note: note,
        symbol_role: symbolRole,
    }

    if (existing && !actionHelpers.recordNeedsUpdate(existing, compareData)) {
        return c.json(200, {
            ok: true,
            action: "skipped",
            id: existing.id || "",
            symbol: symbol,
            environment: recordEnvironment,
            symbol_role: symbolRole,
        })
    }

    const data = {
        ...compareData,
        created_us: String(
            existing ? (existing.get("created_us") || d.created_us || usTime) : (d.created_us || usTime)
        ).trim(),
        created_cn: String(
            existing ? (existing.get("created_cn") || d.created_cn || cnTime) : (d.created_cn || cnTime)
        ).trim(),
        updated_us: String(d.updated_us || usTime).trim(),
        updated_cn: String(d.updated_cn || cnTime).trim(),
        us_time: usTime,
        cn_time: cnTime,
        bar_time_ms: barTimeMs,
    }

    try {
        const result = actionHelpers.upsertRecord(
            "watchlist",
            "symbol = {:sym} && environment = {:env}",
            { sym: symbol, env: recordEnvironment },
            data
        )
        return c.json(200, {
            ok: true,
            action: result.action,
            id: result.record && result.record.id ? result.record.id : "",
            symbol: symbol,
            environment: recordEnvironment,
            symbol_role: symbolRole,
        })
    } catch (err) {
        console.error(`[IBKRActions] watchlist upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

routerAdd("POST", "/api/custom/ibkr/targets/upsert", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)

    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const symbol = String(d.symbol || "").trim().toUpperCase()
    const times = getTimeStrings()
    const date = String(d.date || times.date).trim()

    if (!symbol || !date) {
        return c.json(400, { ok: false, error: "Missing symbol or date" })
    }

    const exchange = String(d.exchange || "").trim().toUpperCase()
    const directionBias = String(d.direction_bias || "neutral").trim() || "neutral"
    const score = Number(d.score)
    const scanReason = String(d.scan_reason || "").trim()
    const status = String(d.status || "candidate").trim() || "candidate"
    const extra = d.extra && typeof d.extra === "object" && !Array.isArray(d.extra) ? d.extra : {}
    const requestedBarTimeMs = Number(d.bar_time_ms)
    const barTimeMs = Number.isFinite(requestedBarTimeMs) && requestedBarTimeMs > 0
        ? Math.trunc(requestedBarTimeMs)
        : Date.now()
    const usTime = String(d.us_time || times.us).trim()
    const cnTime = String(d.cn_time || times.cn).trim()

    let existing = null
    try {
        existing = $app.findFirstRecordByFilter(
            "ibkr_targets",
            "symbol = {:sym} && date = {:d} && environment = {:env}",
            { sym: symbol, d: date, env: environment }
        )
    } catch (_) {}

    const compareData = {
        symbol: symbol,
        environment: environment,
        exchange: exchange,
        date: date,
        direction_bias: directionBias,
        score: Number.isFinite(score) ? score : 0,
        scan_reason: scanReason,
        status: status,
        extra: extra,
    }

    if (existing && !actionHelpers.recordNeedsUpdate(existing, compareData)) {
        return c.json(200, {
            ok: true,
            action: "skipped",
            id: existing.id || "",
            symbol: symbol,
            date: date,
            environment: environment,
        })
    }

    try {
        const result = actionHelpers.upsertRecord(
            "ibkr_targets",
            "symbol = {:sym} && date = {:d} && environment = {:env}",
            { sym: symbol, d: date, env: environment },
            {
                ...compareData,
                us_time: usTime,
                cn_time: cnTime,
                bar_time_ms: barTimeMs,
            }
        )
        return c.json(200, {
            ok: true,
            action: result.action,
            id: result.record && result.record.id ? result.record.id : "",
            symbol: symbol,
            date: date,
            environment: environment,
        })
    } catch (err) {
        console.error(`[IBKRActions] targets upsert error: ${err.message}`)
        return c.json(500, { ok: false, error: err.message })
    }
})

routerAdd("POST", "/api/custom/ibkr/data_quality/upsert", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const items = Array.isArray(d.items) ? d.items : []
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const defaultEnvironment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)

    if (!items.length) {
        return c.json(400, { ok: false, error: "Empty data quality items array" })
    }

    let created = 0
    let updated = 0
    let skipped = 0
    let errors = 0

    for (let i = 0; i < items.length; i++) {
        const environment = getRuntimeEnvironmentFromData(items[i], defaultEnvironment)
        const prepared = actionHelpers.buildBarIntegrityData(items[i] || {}, environment)
        if (!prepared.ok) {
            errors++
            continue
        }

        try {
            let existing = null
            try {
                existing = $app.findFirstRecordByFilter("ibkr_bar_integrity", prepared.filter, prepared.params)
            } catch (_) {}

            if (existing) {
                const existingAttempts = Number(existing.get("repair_attempts") || 0) || 0
                if (prepared.increment_repair_attempts) {
                    prepared.data.repair_attempts = Math.max(Number(prepared.data.repair_attempts) || 0, existingAttempts + 1)
                } else {
                    prepared.data.repair_attempts = Math.max(Number(prepared.data.repair_attempts) || 0, existingAttempts)
                    if (!prepared.data.last_repair_at) {
                        prepared.data.last_repair_at = String(existing.get("last_repair_at") || "")
                        prepared.data.last_repair_result = existing.get("last_repair_result") || {}
                    }
                }
            }

            const result = actionHelpers.upsertRecord("ibkr_bar_integrity", prepared.filter, prepared.params, prepared.data)
            if (result.action === "created") {
                created++
            } else if (result.action === "updated") {
                updated++
            } else {
                skipped++
            }
        } catch (err) {
            errors++
            console.error(`[IBKRActions] data quality upsert error: ${prepared.symbol}/${prepared.market_date}: ${err.message}`)
        }
    }

    return c.json(200, {
        ok: errors === 0,
        received: items.length,
        created,
        updated,
        skipped,
        errors,
    })
})

routerAdd("GET", "/api/custom/ibkr/data_quality/summary", (c) => {
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const query = c.request.url.query()
    const marketDate = String(query.get("market_date") || "").trim()
    const scanScope = String(query.get("scan_scope") || "").trim()
    const filterParts = ['environment = {:env}']
    const filterParams = { env: environment }

    if (marketDate) {
        filterParts.push('market_date = {:date}')
        filterParams.date = marketDate
    }
    if (scanScope) {
        filterParts.push('scan_scope = {:scope}')
        filterParams.scope = scanScope
    }

    try {
        const serialize = (record) => ({
            id: record.id || "",
            environment: record.get("environment") || "",
            market_date: record.get("market_date") || "",
            symbol: record.get("symbol") || "",
            interval: record.get("interval") || "5m",
            scan_scope: record.get("scan_scope") || "",
            status: record.get("status") || "",
            needs_repair: Boolean(record.get("needs_repair")),
            safe_repair: Boolean(record.get("safe_repair")),
            bar_count: Number(record.get("bar_count") || 0) || 0,
            latest_bar_time_ms: Number(record.get("latest_bar_time_ms") || 0) || 0,
            latest_bar_us_time: record.get("latest_bar_us_time") || "",
            oldest_loaded_ms: Number(record.get("oldest_loaded_ms") || 0) || 0,
            gap_count: Number(record.get("gap_count") || 0) || 0,
            duplicate_count: Number(record.get("duplicate_count") || 0) || 0,
            bad_ohlc_count: Number(record.get("bad_ohlc_count") || 0) || 0,
            repair_attempts: Number(record.get("repair_attempts") || 0) || 0,
            last_scan_at: record.get("last_scan_at") || "",
            last_repair_at: record.get("last_repair_at") || "",
        })
        const rows = $app.findRecordsByFilter(
            "ibkr_bar_integrity",
            filterParts.join(" && "),
            "-updated",
            0,
            0,
            filterParams,
        ) || []

        const summary = {
            environment,
            market_date: marketDate,
            total: rows.length,
            needs_repair: 0,
            manual_review: 0,
            latest_scan_at: "",
            latest_repair_at: "",
            status_counts: { ok: 0, warn: 0, error: 0, repaired: 0, repair_failed: 0, repairing: 0 },
            scan_scope_counts: { active_target: 0, watchlist: 0, manual: 0 },
            symbols: [],
        }

        const symbolSet = {}
        for (let i = 0; i < rows.length; i++) {
            const item = serialize(rows[i])
            const status = String(item.status || "").trim()
            const scope = String(item.scan_scope || "").trim()
            if (summary.status_counts[status] == null) {
                summary.status_counts[status] = 0
            }
            summary.status_counts[status]++
            if (summary.scan_scope_counts[scope] == null) {
                summary.scan_scope_counts[scope] = 0
            }
            summary.scan_scope_counts[scope]++
            if (item.needs_repair) summary.needs_repair++
            if ((Number(item.duplicate_count) || 0) > 0 || (Number(item.bad_ohlc_count) || 0) > 0) {
                summary.manual_review++
            }
            if (item.last_scan_at && (!summary.latest_scan_at || item.last_scan_at > summary.latest_scan_at)) {
                summary.latest_scan_at = item.last_scan_at
            }
            if (item.last_repair_at && (!summary.latest_repair_at || item.last_repair_at > summary.latest_repair_at)) {
                summary.latest_repair_at = item.last_repair_at
            }
            if (item.symbol && !symbolSet[item.symbol]) {
                symbolSet[item.symbol] = true
                summary.symbols.push(item.symbol)
            }
        }
        summary.symbols.sort()
        return c.json(200, { ok: true, summary })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("GET", "/api/custom/ibkr/data_quality/list", (c) => {
    try {
        const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const query = c.request.url.query()
        const marketDate = String(query.get("market_date") || "").trim()
        const scanScope = String(query.get("scan_scope") || "").trim()
        const status = String(query.get("status") || "").trim()
        const symbol = String(query.get("symbol") || "").trim().toUpperCase()
        const parseBoolean = (value, fallback) => {
            if (value === undefined || value === null || value === "") return fallback
            const normalized = String(value || "").trim().toLowerCase()
            if (["true", "1", "yes", "y"].indexOf(normalized) !== -1) return true
            if (["false", "0", "no", "n"].indexOf(normalized) !== -1) return false
            return fallback
        }
        const serialize = (record) => ({
            id: record.id || "",
            environment: record.get("environment") || "",
            market_date: record.get("market_date") || "",
            symbol: record.get("symbol") || "",
            interval: record.get("interval") || "5m",
            scan_scope: record.get("scan_scope") || "",
            status: record.get("status") || "",
            needs_repair: Boolean(record.get("needs_repair")),
            safe_repair: Boolean(record.get("safe_repair")),
            bar_count: Number(record.get("bar_count") || 0) || 0,
            latest_bar_time_ms: Number(record.get("latest_bar_time_ms") || 0) || 0,
            latest_bar_us_time: record.get("latest_bar_us_time") || "",
            oldest_loaded_ms: Number(record.get("oldest_loaded_ms") || 0) || 0,
            gap_count: Number(record.get("gap_count") || 0) || 0,
            duplicate_count: Number(record.get("duplicate_count") || 0) || 0,
            bad_ohlc_count: Number(record.get("bad_ohlc_count") || 0) || 0,
            missing_intervals: record.get("missing_intervals") || [],
            stale_intervals: record.get("stale_intervals") || [],
            gap_examples: record.get("gap_examples") || [],
            duplicate_examples: record.get("duplicate_examples") || [],
            bad_ohlc_examples: record.get("bad_ohlc_examples") || [],
            repair_attempts: Number(record.get("repair_attempts") || 0) || 0,
            last_scan_at: record.get("last_scan_at") || "",
            last_repair_at: record.get("last_repair_at") || "",
            last_repair_result: record.get("last_repair_result") || {},
            extra: record.get("extra") || {},
            created: record.get("created") || "",
            updated: record.get("updated") || "",
        })
        const needsRepair = parseBoolean(query.get("needs_repair"), null)
        const page = Math.max(1, Number(query.get("page") || 1) || 1)
        const perPage = Math.max(1, Math.min(200, Number(query.get("per_page") || 50) || 50))
        const sort = String(query.get("sort") || "-updated").trim() || "-updated"

        const filterParts = ['environment = {:env}']
        const filterParams = { env: environment }
        if (marketDate) {
            filterParts.push('market_date = {:date}')
            filterParams.date = marketDate
        }
        if (scanScope) {
            filterParts.push('scan_scope = {:scope}')
            filterParams.scope = scanScope
        }
        if (status) {
            filterParts.push('status = {:status}')
            filterParams.status = status
        }
        if (symbol) {
            filterParts.push('symbol = {:symbol}')
            filterParams.symbol = symbol
        }
        if (needsRepair !== null) {
            filterParts.push(`needs_repair = ${needsRepair ? 'true' : 'false'}`)
        }

        const filterStr = filterParts.join(" && ")
        const allRows = $app.findRecordsByFilter("ibkr_bar_integrity", filterStr, sort, 0, 0, filterParams) || []
        const offset = (page - 1) * perPage
        const items = []
        for (let i = offset; i < allRows.length && items.length < perPage; i++) {
            items.push(serialize(allRows[i]))
        }
        return c.json(200, {
            ok: true,
            environment,
            market_date: marketDate,
            page,
            per_page: perPage,
            total: allRows.length,
            items,
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("POST", "/api/custom/ibkr/data_quality/rescan", (c) => {
    try {
        const reqInfo = c.requestInfo()
        const d = reqInfo.body || reqInfo.data || {}
        const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/data-quality/scan`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 60,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = resp.raw ? JSON.parse(resp.raw) : {}
        } catch (_) {
            payload = { ok: false, raw: String(resp.raw || "") }
        }
        if (Array.isArray(payload.rows) && payload.rows.length) {
            try {
                const persistResp = $http.send({
                    url: "http://127.0.0.1:8090/api/custom/ibkr/data_quality/upsert",
                    method: "POST",
                    timeout: 20,
                    body: JSON.stringify({ environment, items: payload.rows }),
                    headers: { "Content-Type": "application/json" },
                })
                try {
                    payload.persistence = persistResp.raw ? JSON.parse(persistResp.raw) : {}
                } catch (_) {
                    payload.persistence = { ok: false, raw: String(persistResp.raw || "") }
                }
            } catch (persistErr) {
                payload.persistence = { ok: false, error: persistErr.message || String(persistErr) }
            }
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_route = "/api/custom/ibkr/data_quality/rescan"
        payload.proxy_upstream = upstream
        return c.json(Number(resp.statusCode) || 200, payload)
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("POST", "/api/custom/ibkr/data_quality/repair", (c) => {
    try {
        const reqInfo = c.requestInfo()
        const d = reqInfo.body || reqInfo.data || {}
        const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/data-quality/repair`
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 180,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = resp.raw ? JSON.parse(resp.raw) : {}
        } catch (_) {
            payload = { ok: false, raw: String(resp.raw || "") }
        }
        if (Array.isArray(payload.rows) && payload.rows.length) {
            try {
                const persistResp = $http.send({
                    url: "http://127.0.0.1:8090/api/custom/ibkr/data_quality/upsert",
                    method: "POST",
                    timeout: 20,
                    body: JSON.stringify({ environment, items: payload.rows }),
                    headers: { "Content-Type": "application/json" },
                })
                try {
                    payload.persistence = persistResp.raw ? JSON.parse(persistResp.raw) : {}
                } catch (_) {
                    payload.persistence = { ok: false, raw: String(persistResp.raw || "") }
                }
            } catch (persistErr) {
                payload.persistence = { ok: false, error: persistErr.message || String(persistErr) }
            }
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_route = "/api/custom/ibkr/data_quality/repair"
        payload.proxy_upstream = upstream
        return c.json(Number(resp.statusCode) || 200, payload)
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("GET", "/api/custom/ibkr/screener", (c) => {
    try {
        const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const query = c.request.url.query()
        const normalizeSymbols = (value) => {
            const source = Array.isArray(value) ? value : String(value || "").split(",")
            const items = []
            const seen = {}
            for (let i = 0; i < source.length; i++) {
                const symbol = String(source[i] || "").trim().toUpperCase()
                if (!symbol || seen[symbol]) continue
                seen[symbol] = true
                items.push(symbol)
            }
            return items
        }
        const toNumber = (value, fallback) => {
            const number = Number(value)
            return Number.isFinite(number) ? number : (fallback || 0)
        }
        const toInt = (value, fallback) => {
            return Math.trunc(toNumber(value, fallback || 0))
        }
        const asObject = (value) => {
            if (value && typeof value === "object" && !Array.isArray(value)) {
                return value
            }
            if (typeof value === "string") {
                try {
                    const parsed = JSON.parse(value)
                    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                        return parsed
                    }
                } catch (_) {}
            }
            return {}
        }
        const escapeFilter = (value) => String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')
        const buildSymbolFilter = (symbols) => {
            const items = normalizeSymbols(symbols)
            if (!items.length) return ""
            return "(" + items.map((symbol) => `symbol = "${escapeFilter(symbol)}"`).join(" || ") + ")"
        }
        const buildBarEnvironmentFilter = (runtimeEnvironment) => {
            const env = String(runtimeEnvironment || "live").trim().toLowerCase() || "live"
            const clauses = [`environment = "${escapeFilter(env)}"`]
            if (env === "live") clauses.push('environment = ""')
            return clauses.length > 1 ? `(${clauses.join(" || ")})` : clauses[0]
        }
        const pad2 = (value) => String(value || 0).padStart(2, "0")
        const shiftDate = (ms, offsetHours) => new Date(Number(ms) + offsetHours * 60 * 60 * 1000)
        const formatOffsetDate = (ms, offsetHours) => {
            const date = shiftDate(ms, offsetHours)
            return `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`
        }
        const formatOffsetDateTime = (ms, offsetHours) => {
            const date = shiftDate(ms, offsetHours)
            return (
                `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())} ` +
                `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`
            )
        }
        const formatEtDate = (ms) => formatOffsetDate(ms, -4)
        const formatEtDateTime = (ms) => formatOffsetDateTime(ms, -4)
        const formatCnDateTime = (ms) => formatOffsetDateTime(ms, 8)
        const currentMarketDate = () => formatEtDate(Date.now())
        const classifySession = (barTimeMs) => {
            const date = shiftDate(barTimeMs, -4)
            const minutes = date.getUTCHours() * 60 + date.getUTCMinutes()
            if (minutes < 570) return "premarket"
            if (minutes >= 960) return "afterhours"
            return "regular"
        }
        const buildDailyChangeFields = (history, currentClose) => {
            const prevClose = history.length >= 1 ? toNumber(history[history.length - 1].close) : 0
            const prevPrevClose = history.length >= 2 ? toNumber(history[history.length - 2].close) : 0
            const close5 = history.length >= 5 ? toNumber(history[history.length - 5].close) : 0
            const dayChangePct = prevClose > 0 ? ((currentClose - prevClose) / prevClose) * 100 : 0
            const prevCloseChangePct = prevPrevClose > 0 ? ((prevClose - prevPrevClose) / prevPrevClose) * 100 : 0
            const change7d = close5 > 0 ? ((currentClose - close5) / close5) * 100 : 0
            return {
                day_change_pct: Math.round(dayChangePct * 100) / 100,
                prev_close_change_pct: Math.round(prevCloseChangePct * 100) / 100,
                change_7d: Math.round(change7d * 100) / 100,
            }
        }
        const buildTradabilityAssessment = (row) => {
            let score = 0
            const notes = []
            const price = Math.abs(toNumber(row.price))
            const avg10dVolume = Math.abs(toNumber(row.avg_10d_volume))
            const premarketVolume = Math.abs(toNumber(row.premarket_volume))
            const todayVolume = Math.abs(toNumber(row.today_volume))
            const atrPct = Math.abs(toNumber(row.atr_pct))
            const dayChangePct = Math.abs(toNumber(row.day_change_pct))
            const freshnessMin = Number.isFinite(Number(row.freshness_min)) ? Number(row.freshness_min) : null
            const targetScore = Math.abs(toNumber(row.target_score))

            if (price >= 2 && price <= 80) {
                score += 12
                notes.push("价位适中")
            } else if (price >= 1 && price <= 150) {
                score += 6
            }
            if (avg10dVolume >= 5000000) {
                score += 18
                notes.push("10日均量>500万")
            } else if (avg10dVolume >= 1000000) {
                score += 12
                notes.push("10日均量>100万")
            } else if (avg10dVolume >= 500000) {
                score += 6
            }
            if (premarketVolume >= 500000) {
                score += 18
                notes.push("盘前量能>50万")
            } else if (premarketVolume >= 100000) {
                score += 12
                notes.push("盘前量能>10万")
            } else if (todayVolume >= 300000) {
                score += 8
                notes.push("当日成交活跃")
            }
            if (atrPct >= 2 && atrPct <= 12) {
                score += 16
                notes.push("ATR波动充足")
            } else if (atrPct >= 1 && atrPct <= 20) {
                score += 8
            }
            if (dayChangePct >= 2) {
                score += 12
                notes.push("日内波动>2%")
            } else if (dayChangePct >= 0.8) {
                score += 6
            }
            if (freshnessMin !== null) {
                if (freshnessMin <= 20) {
                    score += 14
                    notes.push("bars新鲜")
                } else if (freshnessMin <= 60) {
                    score += 8
                } else if (freshnessMin <= 180) {
                    score += 3
                }
            }
            if (targetScore >= 10) {
                score += 10
                notes.push("已入目标池")
            } else if (targetScore >= 5) {
                score += 6
            }
            if (["long", "short"].indexOf(String(row.direction_bias || "").trim().toLowerCase()) !== -1) {
                score += 4
            }
            return { score: Math.min(100, score), notes }
        }

        const requestedSymbols = normalizeSymbols(query.get("symbols") || "")
        const marketDate = String(query.get("market_date") || "").trim() || currentMarketDate()
        const limit = Math.max(0, toInt(query.get("limit"), 0))
        const marketStartMs = Date.parse(`${marketDate}T04:00:00.000Z`)
        if (!Number.isFinite(marketStartMs)) {
            return c.json(400, { ok: false, error: "invalid_market_date", market_date: marketDate })
        }
        const marketEndMs = marketStartMs + 24 * 60 * 60 * 1000
        const nowMs = Date.now()

        const watchRecords = $app.findRecordsByFilter(
            "watchlist",
            'environment = {:env} || environment = "global" || environment = ""',
            "-updated",
            500,
            0,
            { env: environment }
        ) || []
        const watchPriority = { "": 0, global: 1 }
        watchPriority[environment] = 2
        const watchMeta = {}
        const watchRank = {}
        const watchRole = {}
        for (let i = 0; i < watchRecords.length; i++) {
            const record = watchRecords[i]
            const symbol = String(record.get("symbol") || "").trim().toUpperCase()
            if (!symbol) continue
            const recordEnvironment = String(record.get("environment") || "").trim().toLowerCase()
            const rank = watchPriority[recordEnvironment] != null ? watchPriority[recordEnvironment] : -1
            if (rank < 0) continue
            if (watchRank[symbol] != null && watchRank[symbol] > rank) continue
            watchRank[symbol] = rank
            watchRole[symbol] = globalThis.ibkrActionsNormalizeWatchlistRole(record.get("symbol_role"))
            watchMeta[symbol] = {
                exchange: String(record.get("exchange") || "").trim().toUpperCase(),
                industry: String(record.get("industry") || "").trim(),
                note: String(record.get("note") || "").trim(),
                symbol_role: watchRole[symbol],
            }
        }
        const tradeWatchMeta = {}
        Object.keys(watchMeta).forEach((symbol) => {
            if ((watchRole[symbol] || IBKR_WATCHLIST_ROLE_TRADE) === IBKR_WATCHLIST_ROLE_TRADE) {
                tradeWatchMeta[symbol] = watchMeta[symbol]
            }
        })

        const targetRecords = $app.findRecordsByFilter(
            "ibkr_targets",
            "environment = {:env} && date = {:date}",
            "-updated",
            500,
            0,
            { env: environment, date: marketDate }
        ) || []
        const targetBySymbol = {}
        for (let i = 0; i < targetRecords.length; i++) {
            const record = targetRecords[i]
            const symbol = String(record.get("symbol") || "").trim().toUpperCase()
            if ((watchRole[symbol] || IBKR_WATCHLIST_ROLE_TRADE) !== IBKR_WATCHLIST_ROLE_TRADE) {
                continue
            }
            if (symbol && !targetBySymbol[symbol]) {
                targetBySymbol[symbol] = record
            }
        }

        const universeSymbols = requestedSymbols.length
            ? requestedSymbols
            : Object.keys(tradeWatchMeta).concat(
                Object.keys(targetBySymbol).filter((symbol) => !tradeWatchMeta[symbol])
            )
        const normalizedUniverse = normalizeSymbols(universeSymbols)
        if (!normalizedUniverse.length) {
            const computedAtMs = Date.now()
            return c.json(200, {
                ok: true,
                environment,
                market_date: marketDate,
                computed_at_ms: computedAtMs,
                computed_at_us: formatEtDateTime(computedAtMs),
                computed_at_cn: formatCnDateTime(computedAtMs),
                proxy_source: "pocketbase_local_screener",
                proxy_route: "/api/custom/ibkr/screener",
                summary: {
                    total: 0,
                    with_live_bars: 0,
                    operable: 0,
                    candidate_targets: 0,
                    active_targets: 0,
                    avg_premarket_volume: 0,
                },
                filters: {
                    exchanges: [],
                    industries: [],
                    target_statuses: [],
                    direction_biases: [],
                },
                items: [],
            })
        }

        const symbolFilter = buildSymbolFilter(normalizedUniverse)
        const barEnvironmentFilter = buildBarEnvironmentFilter(environment)
        const lookbackDailyMs = marketStartMs - 20 * 24 * 60 * 60 * 1000
        const indicatorLookbackMs = marketStartMs - 5 * 24 * 60 * 60 * 1000
        const dailyFilter = [
            'interval = "1d"',
            barEnvironmentFilter,
            `bar_time_ms >= ${lookbackDailyMs}`,
            `bar_time_ms < ${marketEndMs}`,
            symbolFilter,
        ].join(" && ")
        const intradayFilter = [
            'interval = "5m"',
            barEnvironmentFilter,
            `bar_time_ms >= ${marketStartMs}`,
            `bar_time_ms < ${marketEndMs}`,
            symbolFilter,
        ].join(" && ")
        const indicatorFilter = [
            'interval = "5"',
            `environment = "${escapeFilter(environment)}"`,
            `bar_time_ms >= ${indicatorLookbackMs}`,
            symbolFilter,
        ].join(" && ")

        const dailyRecords = $app.findRecordsByFilter("ibkr_bars", dailyFilter, "bar_time_ms", 20000, 0) || []
        const intradayRecords = $app.findRecordsByFilter("ibkr_bars", intradayFilter, "bar_time_ms", 50000, 0) || []
        const indicatorRecords = $app.findRecordsByFilter("ibkr_indicators", indicatorFilter, "-bar_time_ms", 10000, 0) || []

        const dailyHistoryBySymbol = {}
        const fallbackDailyBySymbol = {}
        for (let i = 0; i < dailyRecords.length; i++) {
            const record = dailyRecords[i]
            const symbol = String(record.get("symbol") || "").trim().toUpperCase()
            if (!symbol) continue
            const barTimeMs = toInt(record.get("bar_time_ms"), 0)
            const close = toNumber(record.get("close"), 0)
            if (barTimeMs <= 0 || close <= 0) continue
            const row = {
                bar_time_ms: barTimeMs,
                close: close,
                volume: toNumber(record.get("volume"), 0),
                us_time: String(record.get("us_time") || "").trim(),
                date: formatEtDate(barTimeMs),
            }
            if (!dailyHistoryBySymbol[symbol]) dailyHistoryBySymbol[symbol] = []
            dailyHistoryBySymbol[symbol].push(row)
            if (row.date < marketDate) {
                fallbackDailyBySymbol[symbol] = row
            }
        }

        const latestIntradayBySymbol = {}
        const volumeStatsBySymbol = {}
        for (let i = 0; i < intradayRecords.length; i++) {
            const record = intradayRecords[i]
            const symbol = String(record.get("symbol") || "").trim().toUpperCase()
            if (!symbol) continue
            const barTimeMs = toInt(record.get("bar_time_ms"), 0)
            if (barTimeMs <= 0) continue
            const row = {
                bar_time_ms: barTimeMs,
                close: toNumber(record.get("close"), 0),
                exchange: String(record.get("exchange") || "").trim().toUpperCase(),
                session_type: String(record.get("session_type") || "").trim().toLowerCase() || classifySession(barTimeMs),
                us_time: String(record.get("us_time") || "").trim(),
                volume: toNumber(record.get("volume"), 0),
            }
            latestIntradayBySymbol[symbol] = row
            if (!volumeStatsBySymbol[symbol]) {
                volumeStatsBySymbol[symbol] = { premarket: 0, today: 0 }
            }
            volumeStatsBySymbol[symbol].today += row.volume
            if (row.session_type === "premarket") {
                volumeStatsBySymbol[symbol].premarket += row.volume
            }
        }

        const latestIndicatorBySymbol = {}
        for (let i = 0; i < indicatorRecords.length; i++) {
            const record = indicatorRecords[i]
            const symbol = String(record.get("symbol") || "").trim().toUpperCase()
            if (symbol && !latestIndicatorBySymbol[symbol]) {
                latestIndicatorBySymbol[symbol] = record
            }
        }

        const exchangeValues = {}
        const industryValues = {}
        const targetStatusValues = {}
        const directionValues = {}
        let candidateTargets = 0
        let activeTargets = 0
        const items = []

        for (let i = 0; i < normalizedUniverse.length; i++) {
            const symbol = normalizedUniverse[i]
            const meta = tradeWatchMeta[symbol] || {}
            const target = targetBySymbol[symbol]
            const intraday = latestIntradayBySymbol[symbol]
            const fallbackDaily = fallbackDailyBySymbol[symbol]
            const indicatorRecord = latestIndicatorBySymbol[symbol]
            const indicatorExtra = asObject(indicatorRecord ? indicatorRecord.get("extra") : {})
            const history = (dailyHistoryBySymbol[symbol] || []).filter((row) => row.date < marketDate)
            const last10 = history.slice(-10)
            const avg10dVolume = last10.length
                ? last10.reduce((sum, row) => sum + toNumber(row.volume), 0) / last10.length
                : 0
            let price = intraday ? toNumber(intraday.close, 0) : 0
            let priceSource = "5m"
            if (price <= 0) {
                price = fallbackDaily ? toNumber(fallbackDaily.close, 0) : 0
                priceSource = price > 0 ? "1d_close" : ""
            }
            const compareHistory = price > 0 ? buildDailyChangeFields(history, price) : {
                day_change_pct: 0,
                prev_close_change_pct: 0,
                change_7d: 0,
            }
            const targetStatus = String(target ? target.get("status") : "").trim().toLowerCase()
            const directionBias = String(target ? target.get("direction_bias") : "neutral").trim().toLowerCase() || "neutral"
            const targetScore = Math.round(toNumber(target ? target.get("score") : 0, 0) * 100) / 100
            const scanReason = String(target ? target.get("scan_reason") : "").trim()
            if (targetStatus === "candidate") candidateTargets++
            if (targetStatus === "active") activeTargets++
            const intradayBarTimeMs = intraday ? toInt(intraday.bar_time_ms, 0) : 0
            const latestBarTimeMs = intradayBarTimeMs || (fallbackDaily ? toInt(fallbackDaily.bar_time_ms, 0) : 0)
            const freshnessMin = intradayBarTimeMs > 0 ? Math.max(0, Math.floor((nowMs - intradayBarTimeMs) / 60000)) : null
            const volumeStats = volumeStatsBySymbol[symbol] || { premarket: 0, today: 0 }
            const row = {
                symbol: symbol,
                exchange: String(meta.exchange || (intraday ? intraday.exchange : "") || (target ? target.get("exchange") : "") || "").trim().toUpperCase(),
                industry: String(meta.industry || "").trim(),
                note: String(meta.note || "").trim(),
                price: price > 0 ? Math.round(price * 10000) / 10000 : 0,
                price_source: priceSource,
                atr_pct: Math.round(toNumber(indicatorExtra.atr_pct, 0) * 100) / 100,
                avg_10d_volume: Math.round(avg10dVolume * 100) / 100,
                premarket_volume: Math.round(toNumber(volumeStats.premarket, 0) * 100) / 100,
                today_volume: Math.round(toNumber(volumeStats.today, 0) * 100) / 100,
                latest_bar_time_ms: latestBarTimeMs,
                latest_intraday_bar_time_ms: intradayBarTimeMs,
                latest_us_time: intraday ? intraday.us_time : (fallbackDaily ? fallbackDaily.us_time : ""),
                latest_session_type: intraday ? intraday.session_type : "",
                freshness_min: freshnessMin,
                has_live_bar: intradayBarTimeMs > 0,
                target_status: targetStatus,
                target_score: targetScore,
                direction_bias: directionBias,
                scan_reason: scanReason,
                day_change_pct: compareHistory.day_change_pct,
                prev_close_change_pct: compareHistory.prev_close_change_pct,
                change_7d: compareHistory.change_7d,
            }
            const assessment = buildTradabilityAssessment(row)
            row.tradability_score = assessment.score
            row.operable_reasons = assessment.notes
            row.is_operable = Boolean(
                row.has_live_bar &&
                row.price > 0 &&
                row.avg_10d_volume >= 500000 &&
                row.tradability_score >= 60 &&
                row.freshness_min !== null &&
                row.freshness_min <= 90
            )
            items.push(row)

            if (row.exchange) exchangeValues[row.exchange] = true
            if (row.industry) industryValues[row.industry] = true
            if (row.target_status) targetStatusValues[row.target_status] = true
            if (row.direction_bias) directionValues[row.direction_bias] = true
        }

        items.sort((left, right) => {
            if (Boolean(left.is_operable) !== Boolean(right.is_operable)) {
                return left.is_operable ? -1 : 1
            }
            const scoreDiff = toNumber(right.tradability_score, 0) - toNumber(left.tradability_score, 0)
            if (scoreDiff !== 0) return scoreDiff
            const targetDiff = toNumber(right.target_score, 0) - toNumber(left.target_score, 0)
            if (targetDiff !== 0) return targetDiff
            const preDiff = toNumber(right.premarket_volume, 0) - toNumber(left.premarket_volume, 0)
            if (preDiff !== 0) return preDiff
            const volumeDiff = toNumber(right.avg_10d_volume, 0) - toNumber(left.avg_10d_volume, 0)
            if (volumeDiff !== 0) return volumeDiff
            return String(left.symbol || "").localeCompare(String(right.symbol || ""))
        })

        const visibleItems = limit > 0 ? items.slice(0, limit) : items
        const computedAtMs = Date.now()
        return c.json(200, {
            ok: true,
            environment,
            market_date: marketDate,
            computed_at_ms: computedAtMs,
            computed_at_us: formatEtDateTime(computedAtMs),
            computed_at_cn: formatCnDateTime(computedAtMs),
            proxy_source: "pocketbase_local_screener",
            proxy_route: "/api/custom/ibkr/screener",
            summary: {
                total: items.length,
                with_live_bars: items.filter((item) => item.has_live_bar).length,
                operable: items.filter((item) => item.is_operable).length,
                candidate_targets: candidateTargets,
                active_targets: activeTargets,
                avg_premarket_volume: items.length
                    ? Math.round(
                        items.reduce((sum, item) => sum + toNumber(item.premarket_volume, 0), 0)
                        / items.length
                        * 100
                    ) / 100
                    : 0,
            },
            filters: {
                exchanges: Object.keys(exchangeValues).sort(),
                industries: Object.keys(industryValues).sort(),
                target_statuses: Object.keys(targetStatusValues).sort(),
                direction_biases: Object.keys(directionValues).sort(),
            },
            items: visibleItems,
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

routerAdd("POST", "/api/custom/ibkr/screener/targets", (c) => {
    try {
        const reqInfo = c.requestInfo()
        const d = reqInfo.body || reqInfo.data || {}
        const items = Array.isArray(d.items) ? d.items : []
        const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
        const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
        const marketDate = String(d.market_date || d.date || "").trim()

        if (!marketDate) {
            return c.json(400, { ok: false, error: "Missing market_date" })
        }
        if (!items.length) {
            return c.json(400, { ok: false, error: "Empty screener items array" })
        }

        const asObject = (value) => {
            if (value && typeof value === "object" && !Array.isArray(value)) {
                return value
            }
            if (typeof value === "string") {
                try {
                    const parsed = JSON.parse(value)
                    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                        return parsed
                    }
                } catch (_) {}
            }
            return {}
        }
        const pickScore = (item, existing) => {
            const candidates = [
                Number(item.target_score),
                Number(item.score),
                Number(item.tradability_score),
                Number(existing && existing.get("score")),
            ]
            for (let i = 0; i < candidates.length; i++) {
                const value = candidates[i]
                if (Number.isFinite(value) && value > 0) {
                    return value
                }
            }
            return 0
        }

        let created = 0
        let updated = 0
        let skipped = 0
        let errors = 0
        const symbols = []

        for (let i = 0; i < items.length; i++) {
            const item = items[i] || {}
            const symbol = String(item.symbol || "").trim().toUpperCase()
            if (!symbol) {
                errors++
                continue
            }

            let existing = null
            try {
                existing = $app.findFirstRecordByFilter(
                    "ibkr_targets",
                    "symbol = {:sym} && date = {:d} && environment = {:env}",
                    { sym: symbol, d: marketDate, env: environment }
                )
            } catch (_) {}

            const existingExtra = asObject(existing ? existing.get("extra") : {})
            const itemExtra = asObject(item.extra)
            const score = pickScore(item, existing)
            const mergedExtra = {
                ...existingExtra,
                ...itemExtra,
                source: "ibkr_screener",
                screener_snapshot: {
                    symbol: symbol,
                    price: Number(item.price) || 0,
                    atr_pct: Number(item.atr_pct) || 0,
                    avg_10d_volume: Number(item.avg_10d_volume) || 0,
                    premarket_volume: Number(item.premarket_volume) || 0,
                    today_volume: Number(item.today_volume) || 0,
                    tradability_score: Number(item.tradability_score) || 0,
                    operable_reasons: Array.isArray(item.operable_reasons) ? item.operable_reasons : [],
                    freshness_min: Number(item.freshness_min) || 0,
                    is_operable: Boolean(item.is_operable),
                },
                pushed_from: "ibkr_screener_page",
                pushed_at: new Date().toISOString(),
                score_source: (
                    Number.isFinite(Number(item.target_score)) && Number(item.target_score) > 0
                        ? "target_score"
                        : Number.isFinite(Number(item.score)) && Number(item.score) > 0
                            ? "score"
                            : "tradability_score"
                ),
                market_date: marketDate,
                environment: environment,
            }

            const payload = {
                symbol: symbol,
                environment: environment,
                exchange: String(item.exchange || (existing ? existing.get("exchange") : "") || "").trim().toUpperCase(),
                date: marketDate,
                direction_bias: String(
                    item.direction_bias || (existing ? existing.get("direction_bias") : "") || "neutral"
                ).trim().toLowerCase() || "neutral",
                score: score,
                scan_reason: String(
                    item.scan_reason || (existing ? existing.get("scan_reason") : "") || "manual_screener_selection"
                ).trim(),
                status: String(
                    item.target_status || item.status || (existing ? existing.get("status") : "") || "candidate"
                ).trim().toLowerCase() || "candidate",
                us_time: String(item.latest_us_time || (existing ? existing.get("us_time") : "") || "").trim(),
                cn_time: String(item.latest_cn_time || (existing ? existing.get("cn_time") : "") || "").trim(),
                bar_time_ms: Math.trunc(
                    Number(item.latest_intraday_bar_time_ms || item.latest_bar_time_ms || (existing ? existing.get("bar_time_ms") : 0)) || 0
                ),
                extra: mergedExtra,
            }

            try {
                const result = actionHelpers.upsertRecord(
                    "ibkr_targets",
                    "symbol = {:sym} && date = {:d} && environment = {:env}",
                    { sym: symbol, d: marketDate, env: environment },
                    payload
                )
                symbols.push(symbol)
                if (result.action === "created") {
                    created++
                } else if (result.action === "updated") {
                    updated++
                } else {
                    skipped++
                }
            } catch (err) {
                errors++
                console.error(`[IBKRActions] screener target upsert error: ${symbol}/${marketDate}: ${err.message}`)
            }
        }

        return c.json(200, {
            ok: errors === 0,
            market_date: marketDate,
            environment: environment,
            received: items.length,
            created,
            updated,
            skipped,
            errors,
            symbols,
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
})

// ══════════════════════════════════════
// IBKR Compute 代理端点 (PB页面调用 → 转发到Python服务)
// ══════════════════════════════════════
function parseHookJson(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}

function inspectRequestedRuntimeEnvironment(environment) {
    const { getIbkrComputeInternalUrl } = require(`${__hooks}/lib/environment.js`)
    const requestedEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    const computeBase = getIbkrComputeInternalUrl(requestedEnvironment, "http://127.0.0.1:5100")
    const runtimeUpstream = `${computeBase}/ibkr/status`
    const resp = $http.send({
        url: runtimeUpstream,
        method: "GET",
        timeout: 8,
    })
    const runtimePayload = parseHookJson(resp.raw)
    const actualRuntimeEnvironment = String(runtimePayload.environment || requestedEnvironment).trim().toLowerCase() || requestedEnvironment
    return {
        requested_environment: requestedEnvironment,
        actual_runtime_environment: actualRuntimeEnvironment,
        runtime_environment_mismatch: actualRuntimeEnvironment !== requestedEnvironment,
        runtime_payload: runtimePayload,
        proxy_upstream_runtime: runtimeUpstream,
    }
}

function buildRuntimeEnvironmentMismatchPayload(environmentInfo, route) {
    const requestedEnvironment = String(environmentInfo && environmentInfo.requested_environment || "live").trim().toLowerCase() || "live"
    const actualRuntimeEnvironment = String(environmentInfo && environmentInfo.actual_runtime_environment || requestedEnvironment).trim().toLowerCase() || requestedEnvironment
    return {
        ok: false,
        error: `当前 ${requestedEnvironment.toUpperCase()} 页面没有独立 runtime；实际运行中的是 ${actualRuntimeEnvironment.toUpperCase()}，请切到对应环境页面执行此动作。`,
        requested_environment: requestedEnvironment,
        actual_runtime_environment: actualRuntimeEnvironment,
        runtime_environment_mismatch: true,
        proxy_source: "pocketbase_ibkr_hook",
        proxy_hook: "ibkr_actions.pb.js",
        proxy_route: route,
        proxy_upstream_runtime: environmentInfo && environmentInfo.proxy_upstream_runtime ? environmentInfo.proxy_upstream_runtime : "",
    }
}

routerAdd("POST", "/api/custom/ibkr/proxy", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const action = String(d.action || "").trim()
    // PB hooks run in a shared JS runtime; keep critical proxy helpers route-local.
    const { getRuntimeEnvironmentFromData, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const computeBaseUrl = getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")
    const parsePayload = function(rawValue) {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }
    const withMeta = function(payload, upstream) {
        const base = payload && typeof payload === "object" && !Array.isArray(payload)
            ? { ...payload }
            : { ok: false, raw: String(payload || "") }
        base.proxy_source = "pocketbase_ibkr_hook"
        base.proxy_hook = "ibkr_actions.pb.js"
        base.proxy_route = "/api/custom/ibkr/proxy"
        base.proxy_upstream = upstream
        return base
    }

    const validActions = ["compute", "scan", "recompute", "chart/timeline", "chart/compare"]
    if (!validActions.includes(action)) {
        return c.json(400, { ok: false, error: "Invalid action, must be: " + validActions.join("/") })
    }

    try {
        const upstream = `${computeBaseUrl}/${action}`
        const timeoutSeconds = action === "compute" ? 180 : 60
        const resp = $http.send({
            url: upstream,
            method: "POST",
            body: JSON.stringify({ source: "pb_proxy", ...d }),
            headers: { "Content-Type": "application/json" },
            timeout: timeoutSeconds,
        })
        return c.html((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), JSON.stringify(withMeta(parsePayload(resp.raw), upstream)))
    } catch (err) {
        console.error(`[IBKRActions] proxy ${action} error: ${err.message}`)
        const upstream = `${computeBaseUrl}/${action}`
        return c.json(
            502,
            withMeta(
                { ok: false, status: "offline", error: `ibkr_compute unreachable: ${err.message}` },
                upstream
            )
        )
    }
})

routerAdd("GET", "/api/custom/ibkr/quotes", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const reqInfo = c.requestInfo()
    const q = reqInfo.query || {}
    const symbols = String(q.symbols || "").trim()
    const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/quotes${symbols ? `?symbols=${encodeURIComponent(symbols)}` : ""}`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/quotes"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/quotes",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/quotes/forming_bar", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const reqInfo = c.requestInfo()
    const q = reqInfo.query || {}
    const symbol = String(q.symbol || "").trim().toUpperCase()
    const params = []
    if (symbol) params.push(`symbol=${encodeURIComponent(symbol)}`)
    const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/quotes/forming_bar${params.length ? `?${params.join("&")}` : ""}`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/quotes/forming_bar"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/quotes/forming_bar",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/ingest/close", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/ingest/close`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 60,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/ingest/close"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/ingest/close",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/history/rebuild/start", (c) => {
    const route = "/api/custom/ibkr/history/rebuild/start"
    try {
        const { getRuntimeEnvironmentFromData, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const reqInfo = c.requestInfo()
        const data = reqInfo.body || reqInfo.data || {}
        const environment = getRuntimeEnvironmentFromData(data, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/history/rebuild/start`
        const response = $http.send({
            url: upstream,
            method: "POST",
            timeout: 120,
            body: JSON.stringify({
                ...data,
                environment: environment,
            }),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(response.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = route
        payload.proxy_upstream = upstream
        return c.json((Number(response && response.statusCode) > 0 ? Number(response.statusCode) : 200), payload)
    } catch (err) {
        console.error(`[IBKRActions] history rebuild start error: ${err.message || err}`)
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: route,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/history/rebuild/status", (c) => {
    const route = "/api/custom/ibkr/history/rebuild/status"
    try {
        const { getRuntimeEnvironmentFromRequest, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const upstream = `${getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")}/ibkr/history/rebuild/status?environment=${encodeURIComponent(environment)}`
        const response = $http.send({
            url: upstream,
            method: "GET",
            timeout: 20,
        })
        let payload = {}
        try {
            payload = JSON.parse(response.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = route
        payload.proxy_upstream = upstream
        return c.json((Number(response && response.statusCode) > 0 ? Number(response.statusCode) : 200), payload)
    } catch (err) {
        console.error(`[IBKRActions] history rebuild status error: ${err.message || err}`)
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: route,
        })
    }
})

// ══════════════════════════════════════
// IBKR Compute 状态查询 (PB页面用)
// ══════════════════════════════════════
routerAdd("GET", "/api/custom/ibkr/statusz", (c) => {
    try {
        const parseBoolean = function(value, fallback) {
            if (value === undefined || value === null || value === "") {
                return fallback
            }
            if (typeof value === "boolean") return value
            const normalized = String(value || "").trim().toLowerCase()
            if (["true", "1", "yes", "y"].indexOf(normalized) !== -1) return true
            if (["false", "0", "no", "n"].indexOf(normalized) !== -1) return false
            return fallback
        }
        const parseHttpJson = function(rawValue) {
            const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
            if (!raw) return {}
            try {
                return JSON.parse(raw)
            } catch (_) {
                return { ok: false, raw: raw }
            }
        }
        const cloneObject = function(value) {
            if (!value || typeof value !== "object" || Array.isArray(value)) {
                return {}
            }
            return { ...value }
        }
        const trimArray = function(values, limit) {
            if (!Array.isArray(values)) return []
            const maxItems = Math.max(0, Number(limit) || 0)
            return maxItems > 0 ? values.slice(0, maxItems) : []
        }
        const trimObjectEntries = function(value, limit) {
            if (!value || typeof value !== "object" || Array.isArray(value)) {
                return {}
            }
            const maxItems = Math.max(0, Number(limit) || 0)
            const entries = Object.entries(value)
            if (!maxItems || entries.length <= maxItems) {
                return { ...value }
            }
            const trimmed = {}
            entries.slice(0, maxItems).forEach(([key, item]) => {
                trimmed[key] = item
            })
            return trimmed
        }
        const buildStatuszComputePayload = function(computePayload, includeEngines) {
            const payload = cloneObject(computePayload)
            const engineMap = payload.engines && typeof payload.engines === "object" && !Array.isArray(payload.engines)
                ? payload.engines
                : {}
            const totalEngines = Number(payload.total_engines || 0) || Object.keys(engineMap).length

            payload.total_engines = totalEngines
            payload.ready_engines = Number(payload.ready_engines || 0) || 0
            payload.engines_available = totalEngines > 0
            payload.engines_included = Boolean(includeEngines)
            payload.statusz_mode = includeEngines ? "full" : "lite"

            if (includeEngines) {
                payload.engines = engineMap
            } else {
                delete payload.engines
            }

            return payload
        }
        const buildStatuszRuntimePayload = function(runtimePayload, includeWarmupDetails) {
            const payload = cloneObject(runtimePayload)
            const gateway = cloneObject(payload.gateway)
            const session = cloneObject(payload.session)
            const websocket = cloneObject(payload.websocket)
            const realtimeQuotes = cloneObject(payload.realtime_quotes)
            const canonical5m = cloneObject(payload.canonical_5m)
            const dataBackfill = cloneObject(payload.data_backfill)
            const orderTracker = cloneObject(payload.order_tracker)
            const warmup = cloneObject(payload.warmup)
            const realtimeCompute = cloneObject(payload.realtime_compute)
            const realtimeResult = cloneObject(realtimeCompute.last_result)
            const marketUniverse = cloneObject(payload.market_universe)

            const activeTradeSymbols = trimArray(
                marketUniverse.active_trade_symbols,
                Array.isArray(marketUniverse.active_trade_symbols) ? marketUniverse.active_trade_symbols.length : 0
            )
            const pendingSymbols = trimArray(warmup.pending_symbols, 12)
            const fullPendingSymbols = trimArray(
                warmup.pending_symbols,
                Array.isArray(warmup.pending_symbols) ? warmup.pending_symbols.length : 0
            )
            const activeRepairSymbols = trimArray(marketUniverse.last_active_repair_symbols, 12)
            const fullSymbolStatus = includeWarmupDetails
                ? trimArray(
                    warmup.symbol_status,
                    Array.isArray(warmup.symbol_status) ? warmup.symbol_status.length : 0
                )
                : []
            const integrityPendingSymbols = includeWarmupDetails
                ? trimArray(
                    warmup.integrity_pending_symbols,
                    Array.isArray(warmup.integrity_pending_symbols) ? warmup.integrity_pending_symbols.length : 0
                )
                : []
            const readySymbolsList = includeWarmupDetails
                ? trimArray(
                    warmup.ready_symbols_list,
                    Array.isArray(warmup.ready_symbols_list) ? warmup.ready_symbols_list.length : 0
                )
                : []
            const warmupSymbols = includeWarmupDetails
                ? trimArray(
                    warmup.symbols,
                    Array.isArray(warmup.symbols) ? warmup.symbols.length : 0
                )
                : []
            const warmupTradeSymbols = includeWarmupDetails
                ? trimArray(
                    warmup.trade_symbols,
                    Array.isArray(warmup.trade_symbols) ? warmup.trade_symbols.length : 0
                )
                : []
            const warmupMonitorSymbols = includeWarmupDetails
                ? trimArray(
                    warmup.monitor_symbols,
                    Array.isArray(warmup.monitor_symbols) ? warmup.monitor_symbols.length : 0
                )
                : []
            const integrityRepairReasons = includeWarmupDetails
                ? cloneObject(warmup.integrity_repair_reasons)
                : {}
            const preflightRepair = includeWarmupDetails
                ? cloneObject(warmup.preflight_repair)
                : {}

            return {
                ok: payload.ok,
                starting: Boolean(payload.starting),
                startup_complete: Boolean(payload.startup_complete),
                runtime_phase: String(payload.runtime_phase || ""),
                environment: String(payload.environment || ""),
                warmup_details_included: Boolean(includeWarmupDetails),
                gateway: {
                    running: Boolean(gateway.running),
                    reachable: Boolean(gateway.reachable),
                    managed_by: String(gateway.managed_by || ""),
                    status_code: Number(gateway.status_code || 0) || 0,
                    pid: Number(gateway.pid || 0) || 0,
                    uptime_s: Number(gateway.uptime_s || 0) || 0,
                },
                session: {
                    authenticated: Boolean(session.authenticated),
                    running: Boolean(session.running),
                    consecutive_failures: Number(session.consecutive_failures || 0) || 0,
                    last_tickle: session.last_tickle || "",
                },
                websocket: {
                    connected: Boolean(websocket.connected),
                    ready: Boolean(websocket.ready),
                    running: Boolean(websocket.running),
                    last_message: websocket.last_message || "",
                    message_count: Number(websocket.message_count || 0) || 0,
                    subscribed_count: Array.isArray(websocket.subscribed_conids) ? websocket.subscribed_conids.length : (Number(websocket.subscribed_count || 0) || 0),
                    pending_count: Array.isArray(websocket.pending_conids) ? websocket.pending_conids.length : (Number(websocket.pending_count || 0) || 0),
                },
                realtime_quotes: {
                    total_quotes: Number(realtimeQuotes.total_quotes || 0) || 0,
                    stale_quotes: Number(realtimeQuotes.stale_quotes || 0) || 0,
                    tick_count: Number(realtimeQuotes.tick_count || 0) || 0,
                    update_count: Number(realtimeQuotes.update_count || 0) || 0,
                },
                canonical_5m: {
                    enabled: Boolean(canonical5m.enabled !== false),
                    driver: String(canonical5m.driver || ""),
                    close_delay_sec: Number(canonical5m.close_delay_sec || 0) || 0,
                    request_period: String(canonical5m.request_period || ""),
                    last_run: canonical5m.last_run || "",
                    last_due_bucket_ms: Number(canonical5m.last_due_bucket_ms || 0) || 0,
                    last_completed_bucket_ms: Number(canonical5m.last_completed_bucket_ms || 0) || 0,
                    lag_s: Number(canonical5m.lag_s || 0) || 0,
                    last_written_bars: Number(canonical5m.last_written_bars || 0) || 0,
                    written_symbols: trimArray(canonical5m.written_symbols, 24),
                    written_symbols_total: Array.isArray(canonical5m.written_symbols) ? canonical5m.written_symbols.length : (Number(canonical5m.written_symbols_total || 0) || 0),
                    pending_symbols: trimArray(canonical5m.pending_symbols, 24),
                    pending_symbols_total: Array.isArray(canonical5m.pending_symbols) ? canonical5m.pending_symbols.length : (Number(canonical5m.pending_symbols_total || 0) || 0),
                    last_error: String(canonical5m.last_error || ""),
                },
                data_backfill: {
                    total_backfilled: Number(dataBackfill.total_backfilled || 0) || 0,
                },
                order_tracker: {
                    running: Boolean(orderTracker.running),
                    last_poll: orderTracker.last_poll || "",
                    tracked_orders: Number(orderTracker.tracked_orders || 0) || 0,
                },
                warmup: {
                    phase: String(warmup.phase || ""),
                    trading_gate_open: Boolean(warmup.trading_gate_open),
                    trading_gate_reason: String(warmup.trading_gate_reason || ""),
                    required_interval: String(warmup.required_interval || ""),
                    symbols_total: Number(warmup.symbols_total || 0) || 0,
                    trade_symbols_total: Number(warmup.trade_symbols_total || 0) || 0,
                    monitor_symbols_total: Number(warmup.monitor_symbols_total || 0) || 0,
                    ready_symbols: Number(warmup.ready_symbols || 0) || 0,
                        ready_trade_symbols: Number(warmup.ready_trade_symbols || 0) || 0,
                        ready_monitor_symbols: Number(warmup.ready_monitor_symbols || 0) || 0,
                        pending_symbols: includeWarmupDetails ? fullPendingSymbols : pendingSymbols,
                        pending_symbols_total: Array.isArray(warmup.pending_symbols) ? warmup.pending_symbols.length : (Number(warmup.pending_symbols_total || 0) || 0),
                        requested_at: warmup.requested_at || "",
                        started_at: warmup.started_at || "",
                        finished_at: warmup.finished_at || "",
                        last_success_at: warmup.last_success_at || "",
                        last_error: String(warmup.last_error || ""),
                        reason: String(warmup.reason || ""),
                        target_date: String(warmup.target_date || ""),
                        symbols: warmupSymbols,
                        trade_symbols: warmupTradeSymbols,
                        monitor_symbols: warmupMonitorSymbols,
                        ready_symbols_list: readySymbolsList,
                        symbol_status: fullSymbolStatus,
                        integrity_pending_symbols: integrityPendingSymbols,
                        integrity_pending_symbols_total: Array.isArray(warmup.integrity_pending_symbols) ? warmup.integrity_pending_symbols.length : 0,
                        integrity_repair_reasons: integrityRepairReasons,
                        preflight_repair: preflightRepair,
                },
                realtime_compute: {
                    runs: Number(realtimeCompute.runs || 0) || 0,
                    queue_size: Number(realtimeCompute.queue_size || 0) || 0,
                    last_run: realtimeCompute.last_run || "",
                    last_bar_close: realtimeCompute.last_bar_close || "",
                    last_elapsed_s: Number(realtimeResult.elapsed_s || 0) || 0,
                    last_processed: Number(realtimeResult.processed || 0) || 0,
                    last_signals: Number(realtimeResult.signals || 0) || 0,
                    last_errors: Number(realtimeResult.errors || 0) || 0,
                },
                market_universe: {
                    market_date: String(marketUniverse.market_date || ""),
                    last_daily_reset: marketUniverse.last_daily_reset || "",
                    watchlist_pool_count: Number(marketUniverse.watchlist_pool_count || 0) || 0,
                    active_target_date: String(marketUniverse.active_target_date || ""),
                    active_target_count: Number(marketUniverse.active_target_count || 0) || 0,
                    active_trade_symbols: activeTradeSymbols,
                    active_trade_symbols_total: Array.isArray(marketUniverse.active_trade_symbols) ? marketUniverse.active_trade_symbols.length : (Number(marketUniverse.active_trade_symbols_total || 0) || 0),
                    last_target_refresh: marketUniverse.last_target_refresh || "",
                    active_repair_interval_min: Number(marketUniverse.active_repair_interval_min || 0) || 0,
                    last_active_repair: marketUniverse.last_active_repair || "",
                    last_active_repair_symbols: activeRepairSymbols,
                    last_active_repair_symbols_total: Array.isArray(marketUniverse.last_active_repair_symbols) ? marketUniverse.last_active_repair_symbols.length : (Number(marketUniverse.last_active_repair_symbols_total || 0) || 0),
                    last_active_repair_reasons: trimObjectEntries(marketUniverse.last_active_repair_reasons, 12),
                    watchlist_backfill_interval_min: Number(marketUniverse.watchlist_backfill_interval_min || 0) || 0,
                    last_watchlist_backfill: marketUniverse.last_watchlist_backfill || "",
                },
                runtime_control: cloneObject(payload.runtime_control),
            }
        }

        const { getRuntimeEnvironmentFromRequest, getIbkrComputeInternalUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
        const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
        const query = c.request.url.query()
        const includeEngines = parseBoolean(query.get("full"), false)
            || !parseBoolean(query.get("lite"), true)
        const includeWarmupDetails = parseBoolean(query.get("warmup"), false)
            || parseBoolean(query.get("warmup_full"), false)
            || includeEngines
        const computeBase = getIbkrComputeInternalUrl(environment, "http://127.0.0.1:5100")
        const computeUpstream = `${computeBase}/status`
        const runtimeUpstream = `${computeBase}/ibkr/status`

        let computePayload = {}
        let runtimePayload = {}
        let computeError = ""
        let runtimeError = ""

        try {
            const computeResp = $http.send({
                url: computeUpstream,
                method: "GET",
                timeout: 10,
            })
            computePayload = parseHttpJson(computeResp.raw)
        } catch (err) {
            computeError = err.message || String(err)
        }

        try {
            const runtimeResp = $http.send({
                url: runtimeUpstream,
                method: "GET",
                timeout: 10,
            })
            runtimePayload = parseHttpJson(runtimeResp.raw)
        } catch (err) {
            runtimeError = err.message || String(err)
        }

        const computeData = buildStatuszComputePayload(computePayload, includeEngines)
        const runtimeData = buildStatuszRuntimePayload(runtimePayload, includeWarmupDetails)
        const actualRuntimeEnvironment = String(runtimeData.environment || computeData.environment || environment).trim().toLowerCase() || environment
        const response = {
            ...computeData,
            ...(runtimeData && runtimeData.ok !== false ? runtimeData : {}),
            compute: computeData,
            runtime: runtimeData,
            warmup_details_included: Boolean(includeWarmupDetails),
            requested_environment: environment,
            actual_runtime_environment: actualRuntimeEnvironment,
            runtime_environment_mismatch: actualRuntimeEnvironment !== environment,
            ok: !computeError && !runtimeError && computeData.ok !== false && (runtimeData.ok !== false || Object.keys(runtimeData).length === 0),
            status: !computeError && !runtimeError
                ? "running"
                : (!computeError || !runtimeError ? "degraded" : "offline"),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/statusz",
            proxy_upstream_compute: computeUpstream,
            proxy_upstream_runtime: runtimeUpstream,
        }

        if (computeError || runtimeError) {
            response.errors = {}
            if (computeError) response.errors.compute = computeError
            if (runtimeError) response.errors.runtime = runtimeError
            response.error = Object.keys(response.errors)
                .map((key) => `${key}: ${response.errors[key]}`)
                .join("; ")
        }

        return c.json(200, response)
    } catch (err) {
        console.error(`[IBKRActions] statusz route error: ${err.message || err}`)
        return c.json(200, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/statusz",
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/healthz", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    try {
        const resp = $http.send({
            url: `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/health`,
            method: "GET",
            timeout: 5,
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        return c.json(200, payload)
    } catch (err) {
        return c.json(200, { ok: false, status: "offline", error: err.message || String(err) })
    }
})

routerAdd("GET", "/api/custom/ibkr/account_snapshot", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { enrichAccountSnapshot } = require(`${__hooks}/lib/account_snapshot.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/account`
    const fallbackUpstream = "http://127.0.0.1:5100/ibkr/account"
    try {
        let payload = {}
        let selectedUpstream = upstream
        let lastError = ""
        const candidates = [upstream, fallbackUpstream]

        for (let i = 0; i < candidates.length; i++) {
            const candidate = candidates[i]
            try {
                const resp = $http.send({
                    url: candidate,
                    method: "GET",
                    timeout: 20,
                })
                const statusCode = Number(resp && resp.statusCode) || 200
                const raw = String(resp && resp.raw || "")
                if (statusCode >= 400) {
                    lastError = `upstream_http_${statusCode}`
                    continue
                }
                try {
                    payload = raw ? JSON.parse(raw) : {}
                } catch (_) {
                    payload = {}
                }
                if (payload && typeof payload === "object" && (payload.ok !== undefined || payload.account_id || payload.summary || payload.positions)) {
                    selectedUpstream = candidate
                    break
                }
                lastError = "empty_account_payload"
            } catch (err) {
                lastError = err.message || String(err)
            }
        }

        if (!payload || typeof payload !== "object" || (!payload.ok && !payload.account_id && !payload.summary && !payload.positions)) {
            return c.json(502, {
                ok: false,
                status: "offline",
                error: lastError || "account_snapshot_upstream_unavailable",
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: "/api/custom/ibkr/account_snapshot",
                proxy_upstream: upstream,
                proxy_fallback_upstream: fallbackUpstream,
            })
        }
        payload = enrichAccountSnapshot(payload, environment)
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/account_snapshot"
        payload.proxy_upstream = selectedUpstream
        payload.proxy_fallback_upstream = fallbackUpstream
        return c.json(200, payload)
    } catch (err) {
        return c.json(200, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/account_snapshot",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/runtime/config", (c) => {
    const { getRuntimeEnvironmentFromRequest, listEffectiveConfigRecords, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const items = []
    try {
        const records = listEffectiveConfigRecords(environment)

        for (let i = 0; i < records.length; i++) {
            items.push({
                key: records[i].get("key") || "",
                value: records[i].get("value") || "",
                environment: records[i].get("environment") || "",
                updated: records[i].get("updated") || "",
            })
        }
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err) })
    }
    return c.json(200, { ok: true, environment: environment, items: items })
})

routerAdd("POST", "/api/custom/ibkr/start", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/start`
    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/start"))
        }
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/start"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/start",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/startup/progress", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const startupProgress = require(`${__hooks}/lib/feishu/feishu_startup.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    try {
        const payload = {
            environment: environment,
            action: d.action || "update",
            status: d.status || "",
            title: d.title || "",
            summary: d.summary || "",
            current_step: d.current_step || "",
            current_blocker: d.current_blocker || "",
            operator_action: d.operator_action || "",
            reason: d.reason || "",
            source: d.source || "",
            runtime_phase: d.runtime_phase || "",
            runtime_url: d.runtime_url || "",
            steps: d.steps && typeof d.steps === "object" ? d.steps : {},
            fields: d.fields && typeof d.fields === "object" ? d.fields : {},
            create_if_missing: d.create_if_missing === true,
            record_event: d.record_event === true,
            event_type: d.event_type || "",
            event_title: d.event_title || "",
            event_detail: d.event_detail && typeof d.event_detail === "object" ? d.event_detail : {},
            level: d.level || "",
            event_source: d.event_source || d.source || "ibkr_compute",
        }
        if (Object.prototype.hasOwnProperty.call(d, "trigger_login")) {
            payload.trigger_login = d.trigger_login === true
        }
        const result = startupProgress.syncStartupProgress(payload)
        return c.json(result.ok ? 200 : 500, {
            ok: !!result.ok,
            environment: result.environment || environment,
            date: result.date || "",
            cycle_id: result.cycle_id || "",
            message_id: result.message_id || "",
            state: result.state || {},
            error: result.result && result.result.success ? "" : String((result.result && result.result.error) || ""),
        })
    } catch (err) {
        return c.json(500, {
            ok: false,
            environment: environment,
            error: err.message || String(err),
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/startup/status", (c) => {
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const startupProgress = require(`${__hooks}/lib/feishu/feishu_startup.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)

    try {
        const payload = startupProgress.getStatePayload(environment)
        const state = payload && payload.data ? payload.data : {}
        return c.json(200, {
            ok: true,
            environment: payload.environment || environment,
            date: payload.date || "",
            state: state,
        })
    } catch (err) {
        return c.json(500, {
            ok: false,
            environment: environment,
            error: err.message || String(err),
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/stop", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/stop`
    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/stop"))
        }
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/stop"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/stop",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/account", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/account`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/account"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/account",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/positions", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/positions`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/positions"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/positions",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/orders/live", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/live`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 20 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/live"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/live",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/orders/history", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    let days = "1"
    try {
        days = String(c.request.url.query().get("days") || "1").trim() || "1"
    } catch (_) {
        days = "1"
    }
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/history?days=${encodeURIComponent(days)}`
    try {
        const resp = $http.send({ url: upstream, method: "GET", timeout: 25 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/history"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/history",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/cancel", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/cancel`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/cancel"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/cancel",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/cancel_sync", (c) => {
    const route = "/api/custom/ibkr/orders/cancel_sync"
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
    const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
    const cancelUtils = require(`${__hooks}/lib/ibkr_order_cancel.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    try {
        const orderContext = cancelUtils.resolveOrderActionContext(environment, d)
        if (!orderContext || !orderContext.primaryRecord) {
            return c.json(404, {
                ok: false,
                error: "找不到订单或交易组",
                environment: environment,
                requested_id: cancelUtils.pickFirstNonEmpty([d.id, d.unique_id, d.entry_order_unique_id, d.trade_group_id]),
                requested_order_id: cancelUtils.pickFirstNonEmpty([d.order_id, d.broker_order_id]),
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
            })
        }

        const actionRecord = orderContext.actionRecord || orderContext.primaryRecord
        const primaryRecord = orderContext.primaryRecord
        const relatedRecords = orderContext.relatedRecords && orderContext.relatedRecords.length > 0
            ? orderContext.relatedRecords
            : [primaryRecord]
        const normalizedAction = cancelUtils.normalizeOrderRecord(actionRecord)
        const normalizedPrimary = cancelUtils.normalizeOrderRecord(primaryRecord)
        const primaryStatus = normalizedPrimary.status
        const primaryStatusKey = cancelUtils.orderStatusKey(primaryStatus)
        const symbol = normalizedPrimary.symbol || actionRecord.get("symbol") || ""
        const filledQty = Number(normalizedPrimary.filled_qty || 0)

        if (normalizedAction.role && normalizedAction.role !== "entry") {
            return c.json(400, {
                ok: false,
                error: "只能取消主入场单，子单请随主单一起取消",
                environment: environment,
                symbol: symbol,
                role: normalizedAction.role,
                trade_group_id: orderContext.tradeGroupId,
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
            })
        }
        if (filledQty > 0) {
            return c.json(400, {
                ok: false,
                error: "主单已部分成交，不能直接取消，请改用平仓整组",
                environment: environment,
                symbol: symbol,
                status: primaryStatus,
                trade_group_id: orderContext.tradeGroupId,
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
            })
        }
        if (primaryStatusKey === "CANCELED" || primaryStatusKey === "CANCELLED") {
            return c.json(200, {
                ok: true,
                already_canceled: true,
                message: "订单已取消",
                environment: environment,
                symbol: symbol,
                trade_group_id: orderContext.tradeGroupId,
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
            })
        }
        if (primaryStatusKey === "CLOSED") {
            return c.json(400, {
                ok: false,
                error: "订单已平仓，无法取消",
                environment: environment,
                symbol: symbol,
                trade_group_id: orderContext.tradeGroupId,
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
            })
        }
        if (primaryStatusKey === "FILLED" || primaryStatusKey === "EXECUTED") {
            return c.json(400, {
                ok: false,
                error: "订单已成交，无法取消",
                environment: environment,
                symbol: symbol,
                trade_group_id: orderContext.tradeGroupId,
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
            })
        }

        const cancelSummary = cancelUtils.cancelOrderGroupRecords({
            records: relatedRecords,
            environment: environment,
            data: d,
            appendOrderDetail: appendOrderDetail,
            applyOrderStatusMeta: applyOrderStatusMeta,
            applyOrderRelationship: applyOrderRelationship,
            getOrderExtra: getOrderExtra,
            mergeOrderExtra: mergeOrderExtra,
            notifyOrder: notifyOrder,
            source: route,
            reason: cancelUtils.toText(d.reason) || "页面取消主单",
        })
        if (!cancelSummary.ok) {
            return c.json(cancelSummary.failed_order_ids && cancelSummary.failed_order_ids.length > 0 ? 502 : 400, {
                ok: false,
                error: cancelSummary.error || "取消失败",
                environment: environment,
                symbol: symbol,
                trade_group_id: orderContext.tradeGroupId,
                cancelled_order_ids: cancelSummary.cancelled_order_ids || [],
                failed_order_ids: cancelSummary.failed_order_ids || [],
                updated_record_ids: cancelSummary.updated_record_ids || [],
                proxy_source: "pocketbase_ibkr_hook",
                proxy_hook: "ibkr_actions.pb.js",
                proxy_route: route,
                proxy_upstreams: cancelSummary.upstreams || [],
            })
        }

        return c.json(200, {
            ok: true,
            message: "主单与系统订单已同步取消",
            environment: environment,
            symbol: symbol,
            trade_group_id: cancelSummary.trade_group_id || orderContext.tradeGroupId,
            primary_unique_id: normalizedPrimary.unique_id,
            primary_broker_order_id: normalizedPrimary.broker_order_id,
            cancelled_order_ids: cancelSummary.cancelled_order_ids || [],
            failed_order_ids: [],
            updated_record_ids: cancelSummary.updated_record_ids || [],
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: route,
            proxy_upstreams: cancelSummary.upstreams || [],
        })
    } catch (err) {
        console.error("[IBKRActions] cancel_sync 失败:", err)
        return c.json(500, {
            ok: false,
            error: err && (err.message || String(err)) || "unknown_error",
            environment: environment,
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: route,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/cancel_all", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/cancel_all`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/cancel_all"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/cancel_all",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/modify", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/modify`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/modify"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/modify",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/orders/place", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/orders/place`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/orders/place"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/orders/place",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/positions/close", (c) => {
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const upstream = `${getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")}/ibkr/positions/close`
    try {
        const resp = $http.send({
            url: upstream,
            method: "POST",
            timeout: 30,
            body: JSON.stringify(d),
            headers: { "Content-Type": "application/json" },
        })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/positions/close"
        payload.proxy_upstream = upstream
        return c.json((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), payload)
    } catch (err) {
        return c.json(502, {
            ok: false,
            status: "offline",
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/positions/close",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("POST", "/api/custom/ibkr/emergency-stop", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const action = String(d.action || "all").trim().toLowerCase() || "all"
    const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")

    const configActions = {
        compute: [
            ["ibkr_compute_enabled", "FALSE", "Compute 调度开关", "紧急停止后关闭自动 compute / scan"],
        ],
        trading: [
            ["ibkr_trading_enabled", "FALSE", "交易总开关", "紧急停止后禁止继续下单"],
        ],
        scheduler: [
            ["pb_scheduler_enabled", "FALSE", "PB 调度开关", "紧急停止后暂停 PB cron 调度"],
        ],
        publish: [
            ["ibkr_bar_publish_enabled", "FALSE", "IBKR K线发布开关", "紧急停止后暂停 bars 写入 PocketBase"],
        ],
        all: [
            ["ibkr_compute_enabled", "FALSE", "Compute 调度开关", "紧急停止后关闭自动 compute / scan"],
            ["ibkr_trading_enabled", "FALSE", "交易总开关", "紧急停止后禁止继续下单"],
            ["pb_scheduler_enabled", "FALSE", "PB 调度开关", "紧急停止后暂停 PB cron 调度"],
            ["ibkr_bar_publish_enabled", "FALSE", "IBKR K线发布开关", "紧急停止后暂停 bars 写入 PocketBase"],
        ],
        runtime: [],
    }

    const selected = configActions[action]
    if (!selected) {
        return c.json(400, { ok: false, error: "Unsupported emergency action", action: action })
    }

    if (["runtime", "compute", "all"].indexOf(action) !== -1) {
        try {
            const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
            if (environmentInfo.runtime_environment_mismatch) {
                return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/emergency-stop"))
            }
        } catch (_) {}
    }

    const updated = []
    for (let i = 0; i < selected.length; i++) {
        const item = selected[i]
        const record = actionHelpers.upsertConfigValue(item[0], item[1], environment, {
            display_name: item[2],
            description: item[3],
            group_name: "PB / IBKR 服务",
        })
        updated.push({
            key: item[0],
            value: item[1],
            id: record && record.id ? record.id : "",
        })
    }

    let stopPayload = { ok: true, skipped: true }
    if (action === "runtime" || action === "all" || action === "compute") {
        try {
            const resp = $http.send({ url: `${computeBaseUrl}/ibkr/stop`, method: "POST", timeout: 20 })
            try {
                stopPayload = JSON.parse(resp.raw || "{}")
            } catch (_) {
                stopPayload = {}
            }
            stopPayload.status_code = (Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200)
        } catch (err) {
            stopPayload = { ok: false, error: err.message || String(err) }
        }
    }

    writeSystemEvent(
        "status_change",
        "warning",
        "manual",
        "触发紧急停止",
        {
            action: action,
            updated_keys: updated.map((item) => item.key).join(","),
            runtime_stop: stopPayload.ok !== false ? "requested" : "failed",
        },
        environment,
        false,
    )

    return c.json(200, {
        ok: stopPayload.ok !== false,
        environment: environment,
        action: action,
        updated: updated,
        runtime_stop: stopPayload,
    })
})

routerAdd("POST", "/api/custom/ibkr/recover", (c) => {
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)
    const actionHelpers = require(`${__hooks}/lib/ibkr_action_helpers.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const action = String(d.action || "all").trim().toLowerCase() || "all"

    const configActions = {
        compute: [
            ["ibkr_compute_enabled", "TRUE", "Compute 调度开关", "恢复自动 compute / scan"],
        ],
        trading: [
            ["ibkr_trading_enabled", "TRUE", "交易总开关", "恢复自动交易执行"],
        ],
        scheduler: [
            ["pb_scheduler_enabled", "TRUE", "PB 调度开关", "恢复 PB cron 调度"],
        ],
        publish: [
            ["ibkr_bar_publish_enabled", "TRUE", "IBKR K线发布开关", "恢复 bars 写入 PocketBase"],
        ],
        all: [
            ["ibkr_compute_enabled", "TRUE", "Compute 调度开关", "恢复自动 compute / scan"],
            ["ibkr_trading_enabled", "TRUE", "交易总开关", "恢复自动交易执行"],
            ["pb_scheduler_enabled", "TRUE", "PB 调度开关", "恢复 PB cron 调度"],
            ["ibkr_bar_publish_enabled", "TRUE", "IBKR K线发布开关", "恢复 bars 写入 PocketBase"],
        ],
    }

    const selected = configActions[action]
    if (!selected) {
        return c.json(400, { ok: false, error: "Unsupported recover action", action: action })
    }

    const updated = []
    for (let i = 0; i < selected.length; i++) {
        const item = selected[i]
        const record = actionHelpers.upsertConfigValue(item[0], item[1], environment, {
            display_name: item[2],
            description: item[3],
            group_name: "PB / IBKR 服务",
        })
        updated.push({
            key: item[0],
            value: item[1],
            id: record && record.id ? record.id : "",
        })
    }

    writeSystemEvent(
        "status_change",
        "info",
        "manual",
        "恢复运行开关",
        {
            action: action,
            updated_keys: updated.map((item) => item.key).join(","),
        },
        environment,
        false,
    )

    return c.json(200, {
        ok: true,
        environment: environment,
        action: action,
        updated: updated,
    })
})

routerAdd("POST", "/api/custom/ibkr/reauth", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/reauth"))
        }
    } catch (_) {}
    try {
        $http.send({ url: `${computeBaseUrl}/ibkr/stop`, method: "POST", timeout: 30 })
    } catch (_) {}
    const upstream = `${computeBaseUrl}/ibkr/start`
    try {
        const resp = $http.send({ url: upstream, method: "POST", timeout: 30 })
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
        payload.proxy_source = "pocketbase_ibkr_hook"
        payload.proxy_hook = "ibkr_actions.pb.js"
        payload.proxy_route = "/api/custom/ibkr/reauth"
        payload.proxy_upstream = upstream
        return c.html((Number(resp && resp.statusCode) > 0 ? Number(resp.statusCode) : 200), JSON.stringify(payload))
    } catch (err) {
        return c.json(502, {
            ok: false,
            error: err.message || String(err),
            proxy_source: "pocketbase_ibkr_hook",
            proxy_hook: "ibkr_actions.pb.js",
            proxy_route: "/api/custom/ibkr/reauth",
            proxy_upstream: upstream,
        })
    }
})

routerAdd("GET", "/api/custom/ibkr/2fa/status", (c) => {
    const { getRuntimeEnvironmentFromRequest, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { getStatePayload, normalizeStateWithRuntime } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const payload = getStatePayload(environment)
    let state = { ...(payload.data || {}) }

    try {
        const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
        const runtimeResp = $http.send({
            url: `${computeBaseUrl}/ibkr/status`,
            method: "GET",
            timeout: 8,
        })
        let runtime = {}
        try {
            runtime = JSON.parse(runtimeResp.raw || "{}")
        } catch (_) {
            runtime = {}
        }
        state = normalizeStateWithRuntime(state, runtime)
        const actualRuntimeEnvironment = String(runtime.environment || environment).trim().toLowerCase() || environment
        state.requested_environment = environment
        state.actual_runtime_environment = actualRuntimeEnvironment
        state.runtime_environment_mismatch = actualRuntimeEnvironment !== environment
        if (state.runtime_environment_mismatch) {
            state.message = `当前 ${environment.toUpperCase()} 页面没有独立 runtime；实际运行中的是 ${actualRuntimeEnvironment.toUpperCase()}，2FA 动作已阻止。`
            state.last_result = `当前显示的是 ${actualRuntimeEnvironment.toUpperCase()} 运行态。`
        }
    } catch (err) {
        state.runtime_status_error = err.message || String(err)
    }

    return c.json(200, {
        ok: true,
        environment: payload.environment,
        date: payload.date,
        state: state,
    })
})

routerAdd("POST", "/api/custom/ibkr/2fa/request", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const { getStatePayload, normalizeStateWithRuntime, request2faApproval, trigger2faFlow } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const forceReset = d.force_reset === true || ["1", "true", "yes", "on"].includes(String(d.force_reset || "").trim().toLowerCase())
    const forceNew = d.force_new === true || ["1", "true", "yes", "on"].includes(String(d.force_new || "").trim().toLowerCase())
    const triggerNow = d.trigger_now === true || ["1", "true", "yes", "on"].includes(String(d.trigger_now || "").trim().toLowerCase())
    const forceRestart = d.force_restart === true || ["1", "true", "yes", "on"].includes(String(d.force_restart || "").trim().toLowerCase()) || triggerNow

    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/2fa/request"))
        }
        let runtime = {}
        let runtimeStatusError = ""
        try {
            const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
            const runtimeResp = $http.send({
                url: `${computeBaseUrl}/ibkr/status`,
                method: "GET",
                timeout: 8,
            })
            try {
                runtime = JSON.parse(runtimeResp.raw || "{}")
            } catch (_) {
                runtime = {}
            }
        } catch (err) {
            runtimeStatusError = err.message || String(err)
        }

        const currentState = normalizeStateWithRuntime((getStatePayload(environment).data || {}), runtime)
        currentState.requested_environment = environment
        currentState.actual_runtime_environment = String((runtime && runtime.environment) || environment).trim().toLowerCase() || environment
        currentState.runtime_environment_mismatch = currentState.actual_runtime_environment !== environment
        if (
            currentState.runtime_authenticated
            && currentState.gateway_reachable
            && Number(currentState.gateway_status_code || 0) !== 401
        ) {
            if (runtimeStatusError) currentState.runtime_status_error = runtimeStatusError
            return c.json(200, {
                ok: true,
                environment: environment,
                date: getStatePayload(environment).date || "",
                status: currentState.status || "success",
                message: "当前 Gateway 会话已认证，无需再次确认。",
                message_id: currentState.message_id || "",
                state: currentState,
                skipped: true,
                skipped_reason: "runtime_already_authenticated",
                renotify_remaining_ms: 0,
                error: "",
            })
        }

        const reason = String(d.reason || "").trim() || "manual_reauth"
        const source = String(d.source || "").trim() || "ibkr_compute"
        const messageText = String(d.message || "").trim()
        const detail = d.detail && typeof d.detail === "object" ? d.detail : {}
        const result = triggerNow
            ? trigger2faFlow({
                environment: environment,
                reason: reason,
                source: source,
                detail: detail,
                forceRestart: forceRestart,
                forceNewCard: forceNew,
            })
            : request2faApproval({
                environment: environment,
                reason: reason,
                source: source,
                message: messageText,
                detail: detail,
                forceReset: forceReset,
                forceNew: forceNew,
            })
        const state = normalizeStateWithRuntime(result.state || {}, runtime)
        state.requested_environment = environment
        state.actual_runtime_environment = String((runtime && runtime.environment) || environment).trim().toLowerCase() || environment
        state.runtime_environment_mismatch = state.actual_runtime_environment !== environment
        if (runtimeStatusError) state.runtime_status_error = runtimeStatusError
        let message = ""
        const activeStatus = String(state.status || "").trim().toLowerCase()
        const currentCycleActive = ["triggered", "waiting_confirm", "waiting_response"].includes(activeStatus)
        if (state.runtime_authenticated && state.gateway_reachable && Number(state.gateway_status_code || 0) !== 401) {
            message = "当前 Gateway 会话已认证，无需再次确认。"
        } else if (currentCycleActive || result.already_active) {
            if (activeStatus === "waiting_response") {
                if (state.reset_recommended) {
                    message = "当前旧 2FA / Session 状态很可能已失配，请打开 Runtime 页面执行“全量清空并重新验证”，不要重复触发。"
                } else if (state.response_status === "submitted") {
                    message = "当前 Response Code 已提交，正在等待 Gateway 恢复认证；请继续当前轮次，不要重复触发。"
                } else if (state.response_status === "gateway_rejected") {
                    message = "Gateway 已拒绝当前 Response Code，请打开 Runtime 页面核对当前 Challenge 后重新提交，不要重复触发。"
                } else if (state.response_status === "submit_failed") {
                    message = "浏览器提交 Response Code 失败，请打开 Runtime 页面重试当前 Challenge，不要重复触发。"
                } else if (state.response_status === "received") {
                    message = "Runtime 已收到 Response Code，正在等待浏览器提交流程；请继续当前轮次，不要重复触发。"
                } else {
                    message = "当前已进入 Challenge/Response，请继续当前轮次并在 Runtime 页面提交 Response Code，不要重复触发。"
                }
            } else {
                message = "当前已有一轮 2FA 正在进行，请继续当前轮次，不要重复触发。"
            }
        } else if (triggerNow && result.ok) {
            message = forceNew
                ? "已强制开启新一轮 2FA，并刷新卡片。请立即查看手机通知；若稍后切到 Challenge/Response，再去 Runtime 页面提交 Response Code。"
                : "已重新触发 2FA。请立即查看手机通知；若稍后切到 Challenge/Response，再去 Runtime 页面提交 Response Code。"
        } else if (result.skipped_reason === "active_card_reused") {
            const remainingMs = Number(result.renotify_remaining_ms || 0) || 0
            const remainingMin = remainingMs > 0 ? Math.ceil(remainingMs / 60000) : 0
            message = remainingMin > 0
                ? `已复用现有飞书 2FA 卡片，请直接去飞书点击开始验证（约 ${remainingMin} 分钟内不会再新发提醒）。`
                : "已复用现有飞书 2FA 卡片，请直接去飞书点击开始验证。"
        } else if (result.skipped_reason === "cooldown") {
            message = "2FA 卡片刚更新过，请直接使用飞书中的当前卡片。"
        } else if (result.skipped_reason === "delivery_locked") {
            message = "2FA 卡片发送仍在处理中，请直接查看飞书中的当前卡片。"
        } else if (result.ok) {
            message = forceNew
                ? "已强制发送新的 2FA 卡片，请在飞书点击按钮触发验证。"
                : "已请求 2FA 卡片，请在飞书点击按钮触发验证。"
        } else {
            message = result.error ? `2FA 请求失败：${result.error}` : "2FA 请求失败。"
        }
        return c.json(result.ok ? 200 : 500, {
            ok: !!result.ok,
            environment: result.environment || environment,
            date: result.date || "",
            status: state.status || result.status || "",
            message: message,
            message_id: result.message_id || "",
            state: state,
            skipped: !!result.skipped,
            skipped_reason: result.skipped_reason || "",
            renotify_remaining_ms: Number(result.renotify_remaining_ms || 0) || 0,
            error: result.error || "",
            already_active: !!result.already_active,
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/result", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { report2faResult } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    try {
        const result = report2faResult({
            environment: environment,
            status: String(d.status || "").trim() || "requested",
            source: String(d.source || "").trim() || "ibkr_compute",
            message: String(d.message || "").trim(),
            last_result: String(d.last_result || "").trim(),
            error: d.error != null ? String(d.error) : "",
            detail: d.detail && typeof d.detail === "object" ? d.detail : {},
            state_patch: d.state_patch && typeof d.state_patch === "object" ? d.state_patch : {},
        })
        return c.json(result.ok ? 200 : 500, {
            ok: !!result.ok,
            environment: result.environment || environment,
            status: result.status || "",
            message_id: result.message_id || "",
            state: result.state || {},
            error: result.error || "",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/respond", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const { submit2faResponse } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/2fa/respond"))
        }
        const result = submit2faResponse({
            environment: environment,
            response_code: String(d.response_code || "").trim(),
            challenge_code: String(d.challenge_code || "").trim(),
            source: String(d.source || "").trim() || "runtime_page",
        })
        return c.json(result.ok ? 200 : 400, {
            ok: !!result.ok,
            environment: result.environment || environment,
            status: result.status || "",
            message_id: result.message_id || "",
            state: result.state || {},
            error: result.error || "",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/takeover", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const { getStatePayload, normalizeStateWithRuntime } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const parseHttpJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }

    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/2fa/takeover"))
        }
        const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
        const resp = $http.send({
            url: `${computeBaseUrl}/ibkr/2fa/takeover`,
            method: "POST",
            body: JSON.stringify({
                environment: environment,
                enabled: d.enabled !== false,
                ttl_sec: Number(d.ttl_sec || 0) || 600,
                reason: String(d.reason || "manual_takeover"),
                source: String(d.source || "runtime_page"),
            }),
            headers: { "Content-Type": "application/json" },
            timeout: 20,
        })
        const payload = parseHttpJson(resp.raw)
        const runtimeResp = $http.send({ url: `${computeBaseUrl}/ibkr/status`, method: "GET", timeout: 8 })
        const runtime = parseHttpJson(runtimeResp.raw)
        const state = normalizeStateWithRuntime((getStatePayload(environment).data || {}), runtime)
        return c.json((resp.statusCode || 200), {
            ok: payload.ok !== false,
            environment: environment,
            enabled: d.enabled !== false,
            state: state,
            payload: payload,
            message: (d.enabled !== false)
                ? "已开启人工接管；系统会继续静默探测，但不会把当前轮次误判为已锁死。"
                : "已结束人工接管，并立即恢复静默探测。",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/probe", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const { getStatePayload, normalizeStateWithRuntime } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const parseHttpJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }

    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/2fa/probe"))
        }
        const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
        const resp = $http.send({
            url: `${computeBaseUrl}/ibkr/2fa/probe`,
            method: "POST",
            body: JSON.stringify({
                environment: environment,
                reason: String(d.reason || "manual_probe"),
                source: String(d.source || "runtime_page"),
            }),
            headers: { "Content-Type": "application/json" },
            timeout: 20,
        })
        const payload = parseHttpJson(resp.raw)
        const runtimeResp = $http.send({ url: `${computeBaseUrl}/ibkr/status`, method: "GET", timeout: 8 })
        const runtime = parseHttpJson(runtimeResp.raw)
        const state = normalizeStateWithRuntime((getStatePayload(environment).data || {}), runtime)
        return c.json((resp.statusCode || 200), {
            ok: payload.ok !== false,
            environment: environment,
            state: state,
            payload: payload,
            message: "已触发静默探测；若当前会话其实已在真实账户侧恢复，系统会自动转为 success。",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

routerAdd("POST", "/api/custom/ibkr/2fa/panic-reset", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, getIbkrComputePublicUrl, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const { inspectRequestedRuntimeEnvironment, buildRuntimeEnvironmentMismatchPayload } = require(`${__hooks}/lib/runtime_guard.js`)
    const { getStatePayload, normalizeStateWithRuntime } = require(`${__hooks}/lib/feishu_2fa.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    const parseHttpJson = (rawValue) => {
        const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
        if (!raw) return {}
        try {
            return JSON.parse(raw)
        } catch (_) {
            return { ok: false, raw: raw }
        }
    }

    try {
        const environmentInfo = inspectRequestedRuntimeEnvironment(environment)
        if (environmentInfo.runtime_environment_mismatch) {
            return c.json(409, buildRuntimeEnvironmentMismatchPayload(environmentInfo, "/api/custom/ibkr/2fa/panic-reset"))
        }
        const computeBaseUrl = getIbkrComputePublicUrl(environment, "https://qc.lzw-glory.top")
        const resp = $http.send({
            url: `${computeBaseUrl}/ibkr/panic-reset`,
            method: "POST",
            body: JSON.stringify({
                environment: environment,
                restart_gateway: d.restart_gateway !== false,
                restart_runtime: d.restart_runtime !== false,
                trigger_login: d.trigger_login !== false,
                reason: String(d.reason || "panic_reset_2fa"),
                source: String(d.source || "runtime_page"),
            }),
            headers: { "Content-Type": "application/json" },
            timeout: 60,
        })
        const payload = parseHttpJson(resp.raw)
        const runtimeResp = $http.send({ url: `${computeBaseUrl}/ibkr/status`, method: "GET", timeout: 8 })
        const runtime = parseHttpJson(runtimeResp.raw)
        const state = normalizeStateWithRuntime((getStatePayload(environment).data || {}), runtime)
        return c.json((resp.statusCode || 200), {
            ok: payload.ok !== false,
            environment: environment,
            state: state,
            payload: payload,
            message: "已全量清空旧 2FA / Session 状态，并重新拉起新的验证周期。",
        })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message || String(err), environment: environment })
    }
})

// ══════════════════════════════════════
// IBKR 状态持久化 — 替代 ObjectStore
// ══════════════════════════════════════

// GET /api/custom/ibkr/state/signals?date=YYYY-MM-DD
routerAdd("GET", "/api/custom/ibkr/state/signals", (c) => {
    const date = c.request.url.query().get("date") || ""
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    try {
        const record = $app.findFirstRecordByFilter(
            "ibkr_state", "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "ibkr_signals", d: date, env: environment }
        )
        if (record) {
            return c.json(200, { ok: true, date: date, environment: environment, data: record.get("data") || {} })
        }
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    } catch (_) {
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    }
})

// POST /api/custom/ibkr/state/signals
routerAdd("POST", "/api/custom/ibkr/state/signals", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const date = d.date || ""
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    const stateData = {
        processed_ids: d.processed_ids || [],
        confirmed_ids: d.confirmed_ids || [],
        active_signals: d.active_signals || []
    }

    try {
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_state",
                "state_key = {:k} && date = {:d} && environment = {:env}",
                { k: "ibkr_signals", d: date, env: environment }
            )
        } catch (_) {}
        const col = $app.findCollectionByNameOrId("ibkr_state")
        if (!record) {
            record = new Record(col, {})
        }
        record.set("state_key", "ibkr_signals")
        record.set("date", date)
        record.set("environment", environment)
        record.set("data", stateData)
        $app.save(record)
        return c.json(200, { ok: true, date: date, environment: environment })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message })
    }
})

// GET /api/custom/ibkr/state/orders?date=YYYY-MM-DD
routerAdd("GET", "/api/custom/ibkr/state/orders", (c) => {
    const date = c.request.url.query().get("date") || ""
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    try {
        const record = $app.findFirstRecordByFilter(
            "ibkr_state", "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: "orders", d: date, env: environment }
        )
        if (record) {
            return c.json(200, { ok: true, date: date, environment: environment, data: record.get("data") || {} })
        }
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    } catch (_) {
        return c.json(200, { ok: true, date: date, environment: environment, data: {} })
    }
})

// POST /api/custom/ibkr/state/orders
routerAdd("POST", "/api/custom/ibkr/state/orders", (c) => {
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const date = d.date || ""
    const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)
    if (!date) {
        return c.json(400, { ok: false, error: "date required" })
    }

    const stateData = {
        closed_today: d.closed_today || [],
        stop_loss_count_today: d.stop_loss_count_today || 0,
        pending: d.pending || {},
        positions: d.positions || {},
        order_id_map: d.order_id_map || {},
        completed_signal_ids: d.completed_signal_ids || [],
        completed_signal_outcomes: d.completed_signal_outcomes || {}
    }

    try {
        let record = null
        try {
            record = $app.findFirstRecordByFilter(
                "ibkr_state",
                "state_key = {:k} && date = {:d} && environment = {:env}",
                { k: "orders", d: date, env: environment }
            )
        } catch (_) {}
        const col = $app.findCollectionByNameOrId("ibkr_state")
        if (!record) {
            record = new Record(col, {})
        }
        record.set("state_key", "orders")
        record.set("date", date)
        record.set("environment", environment)
        record.set("data", stateData)
        $app.save(record)
        return c.json(200, { ok: true, date: date, environment: environment })
    } catch (err) {
        return c.json(500, { ok: false, error: err.message })
    }
})

// POST /api/custom/ibkr/health-report — IBKR Compute 健康数据上报
routerAdd("POST", "/api/custom/ibkr/health-report", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    // 写 system_events
    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        record.set("event_type", "heartbeat")
        record.set("level", "info")
        record.set("source", "ibkr_compute")
        record.set("environment", environment)
        record.set("title", labelTitleWithEnvironment("IBKR 健康上报", environment))
        record.set("detail", addEnvironmentToDetail(d, environment))
        record.set("us_time", d.et_time || "")
        record.set("cn_time", d.bj_time || "")
        record.set("notified", false)
        $app.save(record)
    } catch (_) {}

    return c.json(200, { ok: true })
})

// POST /api/custom/ibkr/notify — IBKR Compute 通知转发
routerAdd("POST", "/api/custom/ibkr/notify", (c) => {
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const { getRuntimeEnvironmentFromData, labelTitleWithEnvironment, addEnvironmentToDetail, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const environment = getRuntimeEnvironmentFromData(d, LIVE_ENVIRONMENT)

    const notifyType = d.type || "status"
    const title = labelTitleWithEnvironment(d.title || "", environment)
    const detail = addEnvironmentToDetail(d.data || d.detail || {}, environment)

    if (!title) {
        return c.json(400, { ok: false, error: "title required" })
    }

    var level = "info"
    if (notifyType === "alert" || notifyType === "error") level = "error"
    if (notifyType === "warning") level = "warning"

    // 写 system_events
    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        record.set("event_type", "status_change")
        record.set("level", level)
        record.set("source", "ibkr_compute")
        record.set("environment", environment)
        record.set("title", title)
        record.set("detail", detail)
        record.set("notified", false)
        $app.save(record)
    } catch (_) {}

    // 发飞书
    var notified = feishuSystem.notifySystemEvent("status_change", level, "ibkr_compute", title, detail, environment)

    return c.json(200, { ok: true, notified: notified })
})

console.log("[IBKRActions] Hook 文件加载完成");
