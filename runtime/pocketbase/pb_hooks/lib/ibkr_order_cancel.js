function parseJsonObject(value) {
    if (!value) return {}
    if (typeof value === "object" && !Array.isArray(value)) return value
    try {
        const parsed = JSON.parse(String(value || ""))
        return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}
    } catch (_) {
        return {}
    }
}

function toText(value) {
    return String(value == null ? "" : value).trim()
}

function pickFirstNonEmpty(values) {
    const list = Array.isArray(values) ? values : []
    for (let i = 0; i < list.length; i++) {
        const text = toText(list[i])
        if (text) return text
    }
    return ""
}

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback != null ? fallback : 0)
}

function orderStatusWeight(status) {
    const key = toText(status).toUpperCase()
    if (key === "FILLED" || key === "EXECUTED") return 90
    if (key === "PARTIALLYFILLED" || key === "PARTIAL") return 80
    if (key === "SUBMITTED" || key === "PRESUBMITTED") return 70
    if (key === "INIT") return 50
    if (key === "PENDING" || key === "PENDINGSUBMIT") return 40
    if (key === "CANCELED" || key === "CANCELLED") return 10
    return 20
}

function orderStatusKey(status) {
    return toText(status).toUpperCase()
}

function isOrderClosedStatus(status) {
    const key = orderStatusKey(status)
    return key === "FILLED" || key === "EXECUTED" || key === "CANCELED" || key === "CANCELLED" || key === "CLOSED"
}

function normalizeOrderRecord(record) {
    const extra = parseJsonObject(record.get("extra"))
    const quantity = toNumber(record.get("quantity"), 0)
    const filledQtyRaw = toNumber(record.get("filled_qty"), 0)
    const status = pickFirstNonEmpty([record.get("status"), extra.status, extra.current_status])
    const role = pickFirstNonEmpty([record.get("role"), extra.role]) || "entry"
    const tradeGroupId = pickFirstNonEmpty([
        record.get("trade_group_id"),
        extra.trade_group_id,
        record.get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        record.get("unique_id"),
    ])
    const entryOrderUniqueId = pickFirstNonEmpty([
        record.get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        record.get("unique_id"),
    ])
    const signalId = pickFirstNonEmpty([record.get("signal_id"), extra.signal_id])
    const updated = pickFirstNonEmpty([record.get("updated"), record.get("us_time"), record.get("created")])
    const relationStatus = pickFirstNonEmpty([record.get("relation_status"), extra.relation_status])
    const filledQty = filledQtyRaw > 0
        ? filledQtyRaw
        : ((orderStatusKey(status) === "FILLED" || orderStatusKey(status) === "EXECUTED") ? quantity : 0)

    return {
        id: pickFirstNonEmpty([record.get("id"), record.id]),
        symbol: pickFirstNonEmpty([record.get("symbol"), extra.symbol]).toUpperCase(),
        status: status,
        signal_id: signalId,
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        unique_id: pickFirstNonEmpty([record.get("unique_id"), extra.unique_id]),
        broker_order_id: pickFirstNonEmpty([record.get("broker_order_id"), extra.broker_order_id, record.get("order_id")]),
        role: role,
        relation_status: relationStatus,
        quantity: quantity,
        filled_qty: filledQty,
        updated: updated,
        updated_ms: Date.parse(updated || "") || 0,
        status_weight: orderStatusWeight(status),
    }
}

function resolveTradeGroupId(record) {
    if (!record) return ""
    const extra = parseJsonObject(record.get("extra"))
    return pickFirstNonEmpty([
        record.get("trade_group_id"),
        extra.trade_group_id,
        record.get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        record.get("unique_id"),
        extra.unique_id,
        record.get("order_id"),
    ])
}

function resolveCancelableBrokerOrderId(record) {
    if (!record) return ""
    const extra = parseJsonObject(record.get("extra"))
    const candidates = [
        record.get("broker_order_id"),
        extra.broker_order_id,
        record.get("order_id"),
        extra.order_id,
    ]
    for (let i = 0; i < candidates.length; i++) {
        const value = toText(candidates[i])
        if (/^\d+$/.test(value)) {
            return value
        }
    }
    return ""
}

function pickPrimaryOrderRecord(records, fallbackRecord) {
    const list = Array.isArray(records) ? records : []
    return list.find((record) => toText(record.get("role")) === "entry")
        || list.find((record) => {
            const uniqueId = toText(record.get("unique_id"))
            const entryOrderUniqueId = toText(record.get("entry_order_unique_id"))
            return !!uniqueId && uniqueId === entryOrderUniqueId
        })
        || fallbackRecord
        || list[0]
        || null
}

function uniqueRecords(records) {
    const list = Array.isArray(records) ? records : []
    const seen = {}
    const unique = []
    for (let i = 0; i < list.length; i++) {
        const record = list[i]
        if (!record) continue
        const key = pickFirstNonEmpty([record.get("id"), record.id, record.get("unique_id"), record.get("order_id")]) || `idx_${i}`
        if (seen[key]) continue
        seen[key] = true
        unique.push(record)
    }
    return unique
}

function findTradeGroupRecords(environment, tradeGroupId) {
    const groupId = toText(tradeGroupId)
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
        console.error("[IBKROrderCancel] 查询交易组失败:", groupId, err)
        return []
    }
}

function resolveOrderActionContext(environment, data) {
    const payload = data && typeof data === "object" ? data : {}
    const targetId = pickFirstNonEmpty([
        payload.id,
        payload.unique_id,
        payload.entry_order_unique_id,
        payload.trade_group_id,
    ])
    const brokerOrderId = pickFirstNonEmpty([
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
            console.error("[IBKROrderCancel] 查询订单动作上下文失败:", targetId, err)
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
            console.error("[IBKROrderCancel] 通过 broker_order_id 查询订单失败:", brokerLookupId, err)
        }
    }

    const deduped = uniqueRecords(matchedRecords)
    if (!deduped.length) {
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
        ? deduped.find((record) => resolveCancelableBrokerOrderId(record) === brokerLookupId) || null
        : null
    const exactUniqueMatch = targetId
        ? deduped.find((record) => toText(record.get("unique_id")) === targetId) || null
        : null
    const primaryFromMatches = pickPrimaryOrderRecord(deduped, deduped[0] || null)
    const actionRecord = exactBrokerMatch || exactUniqueMatch || primaryFromMatches
    const initialTradeGroupId = resolveTradeGroupId(primaryFromMatches || actionRecord)
    const relatedRecords = findTradeGroupRecords(environment, initialTradeGroupId)
    const resolvedRelatedRecords = uniqueRecords(relatedRecords.length > 0 ? relatedRecords : deduped)
    const primaryRecord = pickPrimaryOrderRecord(resolvedRelatedRecords, primaryFromMatches || actionRecord)

    return {
        actionRecord: actionRecord,
        primaryRecord: primaryRecord,
        relatedRecords: resolvedRelatedRecords,
        tradeGroupId: resolveTradeGroupId(primaryRecord || actionRecord),
        targetId: targetId,
        brokerOrderId: brokerOrderId,
    }
}

function cancelBrokerOrder(environment, orderId, data) {
    const { getIbkrComputePublicUrl } = require(`${__hooks}/lib/environment.js`)
    const upstream = `${getIbkrComputePublicUrl(environment, "http://127.0.0.1:5100")}/ibkr/orders/cancel`
    const brokerOrderId = toText(orderId)
    const payloadBody = {
        order_id: brokerOrderId,
        environment: environment,
    }
    const accountId = toText(data && data.account_id)
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
        let payload = {}
        try {
            payload = JSON.parse(resp.raw || "{}")
        } catch (_) {
            payload = {}
        }
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

function cancelOrderGroupRecords(options) {
    const opts = options || {}
    const records = uniqueRecords(opts.records)
    const environment = toText(opts.environment || "live").toLowerCase() || "live"
    const appendOrderDetail = opts.appendOrderDetail
    const applyOrderStatusMeta = opts.applyOrderStatusMeta
    const applyOrderRelationship = opts.applyOrderRelationship
    const getOrderExtra = opts.getOrderExtra
    const mergeOrderExtra = opts.mergeOrderExtra
    const notifyOrder = opts.notifyOrder
    const source = opts.source || "ibkr_order_cancel"
    const reason = opts.reason || "manual_cancel"
    const requestData = opts.data && typeof opts.data === "object" ? opts.data : {}

    const cancelIds = []
    records.forEach((record) => {
        const currentStatus = normalizeOrderRecord(record).status
        if (isOrderClosedStatus(currentStatus)) return
        const cancelId = resolveCancelableBrokerOrderId(record)
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
        const result = cancelBrokerOrder(environment, orderId, requestData)
        if (result.upstream && upstreams.indexOf(result.upstream) === -1) {
            upstreams.push(result.upstream)
        }
        if (result.ok) {
            cancelledOrderIds.push(orderId)
        } else {
            failedOrderIds.push({
                order_id: orderId,
                error: pickFirstNonEmpty([
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

    const primaryRecord = pickPrimaryOrderRecord(records, records[0] || null)
    const tradeGroupId = resolveTradeGroupId(primaryRecord || records[0] || null)
    const updatedRecordIds = []
    records.forEach((groupRecord) => {
        const currentStatus = normalizeOrderRecord(groupRecord).status
        if (isOrderClosedStatus(currentStatus)) return
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
        updatedRecordIds.push(pickFirstNonEmpty([groupRecord.get("id"), groupRecord.id, groupRecord.get("unique_id")]))
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
                console.error("[IBKROrderCancel] 写入 ibkr_order_details 失败:", detailErr)
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
            console.error("[IBKROrderCancel] 同步订单卡片失败:", notifyErr)
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

module.exports = {
    toText,
    pickFirstNonEmpty,
    orderStatusKey,
    normalizeOrderRecord,
    resolveOrderActionContext,
    cancelOrderGroupRecords,
}
