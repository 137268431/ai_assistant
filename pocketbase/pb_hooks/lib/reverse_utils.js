/**
 * reverse_utils.js
 * 反转信号共用解析、归一化与上下文定位能力
 */

function getEnvUtils() {
    if (globalThis.__gloryEnvUtils) return globalThis.__gloryEnvUtils
    if (globalThis.envUtils) return globalThis.envUtils
    const loaded = require(`${__hooks}/lib/environment.js`)
    globalThis.__gloryEnvUtils = loaded
    if (!globalThis.envUtils) {
        globalThis.envUtils = loaded
    }
    return loaded
}

function parseJsonObject(value) {
    if (!value) return {}
    if (typeof value === "object") return value
    if (typeof value === "string") {
        try {
            var parsed = JSON.parse(value)
            return parsed && typeof parsed === "object" ? parsed : {}
        } catch (e) {
            console.error("[ReverseUtils] JSON 解析失败:", e)
        }
    }
    return {}
}

function firstNonEmpty() {
    for (var i = 0; i < arguments.length; i++) {
        var value = arguments[i]
        if (value !== undefined && value !== null && value !== "") return value
    }
    return ""
}

function toNumber(value, fallbackValue) {
    var num = Number(value)
    return isNaN(num) ? (fallbackValue != null ? fallbackValue : 0) : num
}

function getValue(recordOrData, fieldName) {
    if (!recordOrData) return undefined
    if (typeof recordOrData.get === "function") return recordOrData.get(fieldName)
    return recordOrData[fieldName]
}

function getReverseExtra(recordOrData) {
    return parseJsonObject(getValue(recordOrData, "extra"))
}

function mergeReverseExtra(record, patch, saveAfterMerge) {
    var merged = {
        ...getReverseExtra(record),
        ...(patch || {}),
    }
    record.set("extra", merged)
    if (saveAfterMerge) {
        $app.save(record)
    }
    return merged
}

function inferReverseKind(source, extra) {
    if (extra && extra.reverse_kind) return extra.reverse_kind
    return source === "signal" ? "signal_conflict" : "indicator_conflict"
}

function inferTargetState(extra, orderStatus, actionType) {
    if (extra && extra.target_state) return extra.target_state
    if (orderStatus === "Filled") return "filled_position"
    if (orderStatus === "Submitted") return "pending_entry"
    if (actionType === "close" || actionType === "adjust_sl" || actionType === "adjust_tp") {
        return "filled_position"
    }
    if (actionType === "cancel") return "pending_entry"
    return ""
}

function normalizeTriggeredSignals(value, extra) {
    var triggered = value
    if (triggered == null && extra) triggered = extra.triggered_signals
    if (Array.isArray(triggered)) return triggered
    if (typeof triggered === "string" && triggered) return [triggered]
    return []
}

function normalizeReverseRecord(recordOrData) {
    var envUtils = getEnvUtils()
    var get = (typeof recordOrData.get === "function") ? recordOrData.get.bind(recordOrData) : function(k) { return recordOrData[k] }
    var extra = getReverseExtra(recordOrData)
    var source = get("source") || extra.source || ""
    var actionType = get("action_type") || extra.action_type || ""
    var orderStatus = firstNonEmpty(extra.order_status, extra.target_order_status, "")
    var triggeredSignals = normalizeTriggeredSignals(get("triggered_signals"), extra)
    var tradeGroupId = firstNonEmpty(extra.trade_group_id, get("trade_group_id"), "")
    var entryOrderUniqueId = firstNonEmpty(extra.entry_order_unique_id, get("entry_order_unique_id"), "")
    var orderUniqueId = firstNonEmpty(extra.order_unique_id, entryOrderUniqueId, tradeGroupId, "")
    var brokerOrderId = firstNonEmpty(extra.broker_order_id, extra.order_id, "")
    var signalId = firstNonEmpty(extra.signal_id, "")
    var originSignalId = firstNonEmpty(extra.origin_signal_id, extra.signal_id_orig, "")

    return {
        id: get("id") || "",
        environment: get("environment") || extra.environment || envUtils.LIVE_ENVIRONMENT,
        symbol: get("symbol") || extra.symbol || "",
        direction: get("direction") || extra.direction || "",
        source: source,
        reverse_kind: inferReverseKind(source, extra),
        target_state: inferTargetState(extra, orderStatus, actionType),
        target_order_status: orderStatus,
        priority: toNumber(get("priority"), toNumber(extra.priority, 0)),
        strength: get("strength") || extra.strength || "",
        score: toNumber(get("score"), toNumber(extra.score, 0)),
        action_type: actionType,
        status: get("status") || extra.status || "pending",
        reason: get("reason") || extra.reason || "",
        processed_time: get("processed_time") || extra.processed_time || "",
        triggered_signals: triggeredSignals,
        signal_id: signalId,
        origin_signal_id: originSignalId,
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        order_unique_id: orderUniqueId,
        broker_order_id: brokerOrderId,
        order_id: brokerOrderId,
        relation_status: firstNonEmpty(extra.relation_status, ""),
        position_side: firstNonEmpty(extra.position_side, ""),
        current_direction: firstNonEmpty(extra.current_direction, get("direction"), extra.direction, ""),
        new_direction: firstNonEmpty(extra.new_direction, ""),
        entry_price: toNumber(firstNonEmpty(extra.entry_price, extra.limit_price, extra.fill_price, 0), 0),
        quantity: toNumber(firstNonEmpty(extra.quantity, 0), 0),
        take_profit: toNumber(firstNonEmpty(extra.take_profit, extra.tp_price, 0), 0),
        stop_loss: toNumber(firstNonEmpty(extra.stop_loss, extra.sl_price, 0), 0),
        old_sl: toNumber(firstNonEmpty(extra.old_sl, 0), 0),
        new_sl: toNumber(firstNonEmpty(extra.new_sl, 0), 0),
        old_tp: toNumber(firstNonEmpty(extra.old_tp, 0), 0),
        new_tp: toNumber(firstNonEmpty(extra.new_tp, 0), 0),
        executed_action: firstNonEmpty(extra.executed_action, ""),
        result_status: firstNonEmpty(extra.result_status, ""),
        manual_requested: !!extra.manual_requested,
        manual_requested_at: firstNonEmpty(extra.manual_requested_at, ""),
        bar_time_ms: toNumber(firstNonEmpty(get("bar_time_ms"), extra.bar_time_ms, 0), 0),
        us_time: get("us_time") || extra.us_time || "",
        cn_time: get("cn_time") || extra.cn_time || "",
        created: get("created") || extra.created || "",
        updated: get("updated") || extra.updated || "",
        extra: extra,
    }
}

function buildDateRange(dateText) {
    if (!dateText || !/^\d{4}-\d{2}-\d{2}$/.test(String(dateText))) return null
    var start = new Date(String(dateText) + "T00:00:00Z")
    var end = new Date(String(dateText) + "T23:59:59.999Z")
    if (isNaN(start.getTime()) || isNaN(end.getTime())) return null
    return {
        start_ms: start.getTime(),
        end_ms: end.getTime(),
    }
}

function buildOrderContext(orderRecord) {
    var envUtils = getEnvUtils()
    if (!orderRecord) return null
    var orderExtra = parseJsonObject(typeof orderRecord.getString === "function" ? orderRecord.getString("extra") : orderRecord.get("extra"))
    var orderStatus = orderRecord.get("status") || ""
    var direction = firstNonEmpty(orderRecord.get("direction"), orderExtra.direction, "")
    var tradeGroupId = firstNonEmpty(orderRecord.get("trade_group_id"), orderExtra.trade_group_id, orderRecord.get("entry_order_unique_id"), orderRecord.get("unique_id"))
    var entryOrderUniqueId = firstNonEmpty(orderRecord.get("entry_order_unique_id"), orderExtra.entry_order_unique_id, orderRecord.get("unique_id"))
    var uniqueId = firstNonEmpty(orderRecord.get("unique_id"), orderExtra.order_unique_id, entryOrderUniqueId, "")
    var brokerOrderId = firstNonEmpty(orderRecord.get("broker_order_id"), orderRecord.get("order_id"), orderExtra.broker_order_id, orderExtra.order_id, "")

    return {
        environment: orderRecord.get("environment") || orderExtra.environment || envUtils.LIVE_ENVIRONMENT,
        symbol: orderRecord.get("symbol") || "",
        direction: direction,
        target_state: orderStatus === "Filled" ? "filled_position" : "pending_entry",
        order_status: orderStatus,
        relation_status: firstNonEmpty(orderRecord.get("relation_status"), orderExtra.relation_status, ""),
        position_side: firstNonEmpty(orderRecord.get("position_side"), direction, ""),
        signal_id: firstNonEmpty(orderRecord.get("signal_id"), orderExtra.signal_id, ""),
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        order_unique_id: uniqueId,
        broker_order_id: brokerOrderId,
        entry_price: toNumber(firstNonEmpty(orderRecord.get("fill_price"), orderRecord.get("limit_price"), orderExtra.fill_price, orderExtra.limit_price, 0), 0),
        quantity: toNumber(firstNonEmpty(orderRecord.get("filled_qty"), orderRecord.get("quantity"), orderExtra.filled_qty, orderExtra.quantity, 0), 0),
        take_profit: toNumber(firstNonEmpty(orderRecord.get("tp_price"), orderExtra.tp_price, 0), 0),
        stop_loss: toNumber(firstNonEmpty(orderRecord.get("sl_price"), orderExtra.sl_price, 0), 0),
        order_record: orderRecord,
    }
}

function findLatestActiveEntryOrder(symbol, direction, environment) {
    var envUtils = getEnvUtils()
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var records = $app.findRecordsByFilter(
        "orders",
        "symbol = {:symbol} && environment = {:env} && order_type = 'Entry' && (status = 'Submitted' || status = 'Filled')",
        "-created",
        20,
        0,
        { symbol: symbol, env: runtimeEnvironment }
    ) || []

    if (!direction) {
        return records.length > 0 ? records[0] : null
    }

    var normalizedDirection = String(direction || "").toLowerCase()
    for (var i = 0; i < records.length; i++) {
        var orderDirection = String(records[i].get("direction") || "").toLowerCase()
        if (orderDirection === normalizedDirection) {
            return records[i]
        }
    }
    return records.length > 0 ? records[0] : null
}

function findPendingReverseDuplicate(criteria) {
    var envUtils = getEnvUtils()
    if (!criteria || !criteria.symbol) return null

    var records = $app.findRecordsByFilter(
        "reverse_signals",
        "symbol = {:symbol} && environment = {:env} && status = 'pending'",
        "-created",
        50,
        0,
        { symbol: criteria.symbol, env: criteria.environment || envUtils.LIVE_ENVIRONMENT }
    ) || []

    for (var i = 0; i < records.length; i++) {
        var record = records[i]
        var normalized = normalizeReverseRecord(record)
        if (criteria.direction && normalized.direction !== criteria.direction) continue
        if ((criteria.reverse_kind || "") !== (normalized.reverse_kind || "")) continue
        if ((criteria.target_state || "") !== (normalized.target_state || "")) continue
        if ((criteria.action_type || "") !== (normalized.action_type || "")) continue
        if ((criteria.trade_group_id || "") !== (normalized.trade_group_id || "")) continue
        if ((criteria.origin_signal_id || "") !== (normalized.origin_signal_id || "")) continue
        if ((criteria.new_direction || "") !== (normalized.new_direction || "")) continue
        return record
    }
    return null
}

function upsertReverseRecord(payload) {
    var envUtils = getEnvUtils()
    var extra = parseJsonObject(payload && payload.extra)
    var criteria = {
        environment: envUtils.normalizeRuntimeEnvironment(payload.environment || extra.environment || "", envUtils.LIVE_ENVIRONMENT),
        symbol: payload.symbol || "",
        direction: payload.direction || "",
        reverse_kind: firstNonEmpty(extra.reverse_kind, payload.source === "signal" ? "signal_conflict" : "indicator_conflict"),
        target_state: firstNonEmpty(extra.target_state, ""),
        action_type: payload.action_type || "",
        trade_group_id: firstNonEmpty(extra.trade_group_id, ""),
        origin_signal_id: firstNonEmpty(extra.origin_signal_id, extra.signal_id_orig, ""),
        new_direction: firstNonEmpty(extra.new_direction, ""),
    }

    var record = payload.dedupe === false ? null : findPendingReverseDuplicate(criteria)
    var existed = !!record
    if (!record) {
        var collection = $app.findCollectionByNameOrId("reverse_signals")
        record = new Record(collection, {})
    }

    record.set("symbol", payload.symbol || "")
    record.set("environment", criteria.environment)
    record.set("direction", payload.direction || "")
    record.set("source", payload.source || "indicator")
    record.set("priority", toNumber(payload.priority, 5))
    record.set("strength", payload.strength || "weak")
    record.set("score", toNumber(payload.score, 0))
    record.set("triggered_signals", normalizeTriggeredSignals(payload.triggered_signals, extra))
    record.set("action_type", payload.action_type || "cancel")
    record.set("status", payload.status || "pending")
    record.set("reason", payload.reason || "")
    record.set("bar_time_ms", toNumber(payload.bar_time_ms, 0))
    record.set("us_time", payload.us_time || "")
    record.set("cn_time", payload.cn_time || "")
    record.set("extra", {
        ...getReverseExtra(record),
        ...extra,
        environment: criteria.environment,
        reverse_kind: criteria.reverse_kind,
        target_state: criteria.target_state,
        triggered_signals: normalizeTriggeredSignals(payload.triggered_signals, extra),
    })

    $app.save(record)
    return {
        record: record,
        created: !existed,
    }
}

module.exports = {
    parseJsonObject: parseJsonObject,
    firstNonEmpty: firstNonEmpty,
    toNumber: toNumber,
    getReverseExtra: getReverseExtra,
    mergeReverseExtra: mergeReverseExtra,
    normalizeReverseRecord: normalizeReverseRecord,
    buildDateRange: buildDateRange,
    buildOrderContext: buildOrderContext,
    findLatestActiveEntryOrder: findLatestActiveEntryOrder,
    findPendingReverseDuplicate: findPendingReverseDuplicate,
    upsertReverseRecord: upsertReverseRecord,
}
