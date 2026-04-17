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

function parseHttpJson(rawValue) {
    const raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        const parsed = JSON.parse(raw)
        return parsed && typeof parsed === "object" ? parsed : {}
    } catch (_) {
        return { raw: raw }
    }
}

function resolveCancelableOrderId(record) {
    if (!record || typeof record.get !== "function") return ""
    const extra = parseJsonObject(record.get("extra"))
    const candidates = [
        record.get("broker_order_id"),
        record.get("order_id"),
        extra.broker_order_id,
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

function orderStatusKey(status) {
    return toText(status).toUpperCase()
}

function isOrderClosedStatus(status) {
    const key = orderStatusKey(status)
    return key === "FILLED" || key === "EXECUTED" || key === "CANCELED" || key === "CANCELLED" || key === "CLOSED"
}

function resolveTradeGroupId(record) {
    if (!record || typeof record.get !== "function") return ""
    const extra = parseJsonObject(record.get("extra"))
    return toText(
        record.get("trade_group_id")
        || extra.trade_group_id
        || record.get("entry_order_unique_id")
        || extra.entry_order_unique_id
        || record.get("unique_id")
        || extra.unique_id
        || record.get("order_id")
        || extra.order_id
    )
}

function uniqueRecords(records) {
    const list = Array.isArray(records) ? records : []
    const seen = {}
    const unique = []
    for (let i = 0; i < list.length; i++) {
        const record = list[i]
        if (!record || typeof record.get !== "function") continue
        const key = toText(record.id) || toText(record.get("id")) || toText(record.get("unique_id")) || toText(record.get("order_id")) || `idx_${i}`
        if (seen[key]) continue
        seen[key] = true
        unique.push(record)
    }
    return unique
}

function pickPrimaryOrderRecord(records, fallbackRecord) {
    const list = Array.isArray(records) ? records : []
    for (let i = 0; i < list.length; i++) {
        if (toText(list[i].get("role")) === "entry") return list[i]
    }
    for (let i = 0; i < list.length; i++) {
        const uniqueId = toText(list[i].get("unique_id"))
        const entryOrderUniqueId = toText(list[i].get("entry_order_unique_id"))
        if (uniqueId && uniqueId === entryOrderUniqueId) return list[i]
    }
    return fallbackRecord || list[0] || null
}

function cancelFailureLooksClosed(payload) {
    const errorText = toText(
        payload && (payload.error || payload.message || payload.raw)
    ).toLowerCase()
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

function logInfo(logger, message) {
    if (logger && typeof logger.info === "function") {
        logger.info(message)
        return
    }
    if (logger && typeof logger.log === "function") {
        logger.log(message)
    }
}

function logError(logger, message, err) {
    if (logger && typeof logger.error === "function") {
        if (err !== undefined) {
            logger.error(message, err)
        } else {
            logger.error(message)
        }
        return
    }
    if (logger && typeof logger.log === "function") {
        if (err !== undefined) {
            logger.log(message, err)
        } else {
            logger.log(message)
        }
    }
}

function processExpiredOrders(options) {
    const opts = options || {}
    const records = Array.isArray(opts.records) ? opts.records : []
    const environment = toText(opts.environment || "live").toLowerCase() || "live"
    const validityMinutes = Math.max(1, Math.floor(Number(opts.validityMinutes) || 30))
    const cutoffMs = Math.trunc(Number(opts.cutoffMs) || 0)
    const app = opts.app
    const cancelBrokerOrder = typeof opts.cancelBrokerOrder === "function"
        ? opts.cancelBrokerOrder
        : (() => ({ ok: true, statusCode: 200, payload: {} }))
    const appendOrderDetail = opts.appendOrderDetail
    const getOrderExtra = opts.getOrderExtra
    const mergeOrderExtra = opts.mergeOrderExtra
    const applyOrderStatusMeta = opts.applyOrderStatusMeta
    const applyOrderRelationship = opts.applyOrderRelationship
    const notifyOrder = opts.notifyOrder
    const findRelatedRecords = typeof opts.findRelatedRecords === "function"
        ? opts.findRelatedRecords
        : null
    const logger = opts.logger || console

    let processedCount = 0
    let updateErrorCount = 0
    let cancelFailureCount = 0
    const cancelResultByOrderId = {}
    const processedGroupIds = {}

    for (const record of records) {
        let uniqueId = ""
        let recordId = ""
        let cancelOrderId = ""
        let tradeGroupId = ""
        try {
            uniqueId = toText(record && record.get && record.get("unique_id"))
            recordId = toText(record && record.id) || toText(record && record.get && record.get("id"))
            tradeGroupId = resolveTradeGroupId(record) || uniqueId || recordId
            if (tradeGroupId && processedGroupIds[tradeGroupId]) {
                continue
            }
            const relatedRecords = uniqueRecords(
                tradeGroupId && findRelatedRecords
                    ? findRelatedRecords(tradeGroupId, record)
                    : [record]
            )
            const groupRecords = relatedRecords.length > 0 ? relatedRecords : [record]
            const primaryRecord = pickPrimaryOrderRecord(groupRecords, record)
            const oldStatus = toText(primaryRecord && primaryRecord.get && primaryRecord.get("status"))
            cancelOrderId = resolveCancelableOrderId(primaryRecord || record)
            logInfo(
                logger,
                `[OrderScheduler] ${environment}: 命中过期订单: trade_group_id=${tradeGroupId || "-"}, record_id=${recordId || "-"}, unique_id=${uniqueId || "-"}, broker_order_id=${cancelOrderId || "-"}, status=${oldStatus || "-"}, validity_minutes=${validityMinutes}, cutoff_ms=${cutoffMs}, bar_time_ms=${toText(primaryRecord && primaryRecord.get && primaryRecord.get("bar_time_ms")) || "0"}, group_records=${groupRecords.length}`
            )

            if (cancelOrderId) {
                if (!cancelResultByOrderId[cancelOrderId]) {
                    cancelResultByOrderId[cancelOrderId] = cancelBrokerOrder(environment, cancelOrderId)
                }
                const cancelResult = cancelResultByOrderId[cancelOrderId] || {}
                if (!cancelResult.ok) {
                    cancelFailureCount += 1
                    logError(
                        logger,
                        `[OrderScheduler] ${environment}: IBKR 撤单失败，跳过本地取消: trade_group_id=${tradeGroupId || "-"}, record_id=${recordId || "-"}, unique_id=${uniqueId || "-"}, order_id=${cancelOrderId}, upstream=${toText(cancelResult.upstream) || "-"}, error=${toText(cancelResult.payload && (cancelResult.payload.error || cancelResult.payload.message)) || "unknown"}`
                    )
                    continue
                }
            }

            let updatedInGroup = 0
            for (let index = 0; index < groupRecords.length; index++) {
                const groupRecord = groupRecords[index]
                const currentStatus = toText(groupRecord.get("status"))
                if (isOrderClosedStatus(currentStatus)) {
                    continue
                }
                groupRecord.set("status", "Canceled")
                if (typeof applyOrderRelationship === "function") {
                    applyOrderRelationship(groupRecord, {
                        relation_status: "closed",
                        status: "Canceled",
                    }, false)
                }
                let eventTimes = {
                    us_time: toText(groupRecord.get("us_time")),
                    cn_time: toText(groupRecord.get("cn_time")),
                    bar_time_ms: Math.trunc(Number(groupRecord.get("bar_time_ms")) || 0),
                }
                if (typeof applyOrderStatusMeta === "function") {
                    const metaResult = applyOrderStatusMeta(groupRecord, {
                        status: "Canceled",
                        previous_status: currentStatus,
                        source: "order_scheduler",
                        reason: `订单超时自动取消（有效期 ${validityMinutes} 分钟）`,
                    }, false)
                    if (metaResult && metaResult.eventTimes) {
                        eventTimes = metaResult.eventTimes
                    }
                }
                if (app && typeof app.save === "function") {
                    app.save(groupRecord)
                }
                if (typeof appendOrderDetail === "function") {
                    appendOrderDetail(groupRecord, {
                        status: "Canceled",
                        source: "order_scheduler",
                        reason: `订单超时自动取消（有效期 ${validityMinutes} 分钟）`,
                        us_time: eventTimes.us_time,
                        cn_time: eventTimes.cn_time,
                        bar_time_ms: eventTimes.bar_time_ms,
                        extra: {
                            previous_status: currentStatus,
                            validity_minutes: validityMinutes,
                            cutoff_ms: cutoffMs,
                            cancelled_broker_order_id: cancelOrderId,
                            trade_group_id: tradeGroupId,
                        },
                    })
                }
                if (
                    groupRecord === primaryRecord
                    && typeof getOrderExtra === "function"
                    && typeof notifyOrder === "function"
                ) {
                    const orderExtra = getOrderExtra(groupRecord) || {}
                    const syncResult = notifyOrder("canceled", groupRecord, {
                        messageId: orderExtra.feishu_order_message_id || "",
                        message: `订单超时自动取消（有效期 ${validityMinutes} 分钟）`,
                    })
                    if (
                        syncResult
                        && syncResult.success
                        && syncResult.message_id
                        && syncResult.message_id !== orderExtra.feishu_order_message_id
                        && typeof mergeOrderExtra === "function"
                    ) {
                        mergeOrderExtra(groupRecord, {
                            feishu_order_message_id: syncResult.message_id,
                            feishu_order_card_version: 1,
                        }, true)
                    }
                }
                updatedInGroup += 1
            }

            processedGroupIds[tradeGroupId || recordId || uniqueId] = true
            processedCount += updatedInGroup
            logInfo(
                logger,
                `[OrderScheduler] ${environment}: 订单已取消: trade_group_id=${tradeGroupId || "-"}, primary_unique_id=${toText(primaryRecord && primaryRecord.get && primaryRecord.get("unique_id")) || uniqueId || "-"}, broker_order_id=${cancelOrderId || "-"}, 更新记录数=${updatedInGroup}`
            )
        } catch (err) {
            updateErrorCount += 1
            logError(
                logger,
                `[OrderScheduler] ${environment}: 更新订单失败: trade_group_id=${tradeGroupId || "-"}, record_id=${recordId || "-"}, unique_id=${uniqueId || "-"}, broker_order_id=${cancelOrderId || "-"} ${err && err.message ? err.message : toText(err)}`,
                err
            )
        }
    }

    return {
        processed_count: processedCount,
        update_error_count: updateErrorCount,
        cancel_failure_count: cancelFailureCount,
        cancel_result_by_order_id: cancelResultByOrderId,
    }
}

module.exports = {
    cancelFailureLooksClosed,
    parseHttpJson,
    processExpiredOrders,
    resolveCancelableOrderId,
}
