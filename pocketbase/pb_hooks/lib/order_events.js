/// <reference path="../pb_data/types.d.ts" />

/**
 * order_events.js
 * 共享订单事件日志与 order_details 写入工具
 */

const ORDER_INTERNAL_EXTRA_KEYS = {
    feishu_order_message_id: true,
    feishu_order_card_version: true,
}

const ORDER_STATUS_TEXT_MAP = {
    Init: "初始化",
    Submitted: "待成交",
    Filled: "已成交",
    Canceled: "已取消",
    Closed: "已平仓",
}

function pad2(value) {
    return String(value).padStart(2, "0")
}

function nthWeekdayOfMonth(year, monthIndex, weekday, nth) {
    const firstDayWeekday = new Date(Date.UTC(year, monthIndex, 1)).getUTCDay()
    const delta = (weekday - firstDayWeekday + 7) % 7
    return 1 + delta + (nth - 1) * 7
}

function isUsEasternDst(date) {
    const year = date.getUTCFullYear()
    const dstStartDay = nthWeekdayOfMonth(year, 2, 0, 2)
    const dstEndDay = nthWeekdayOfMonth(year, 10, 0, 1)
    const dstStartUtcMs = Date.UTC(year, 2, dstStartDay, 7, 0, 0)
    const dstEndUtcMs = Date.UTC(year, 10, dstEndDay, 6, 0, 0)
    const ts = date.getTime()
    return ts >= dstStartUtcMs && ts < dstEndUtcMs
}

function formatDateTimeByOffset(utcMs, offsetHours) {
    const shifted = new Date(utcMs + offsetHours * 60 * 60 * 1000)
    return {
        year: shifted.getUTCFullYear(),
        month: pad2(shifted.getUTCMonth() + 1),
        day: pad2(shifted.getUTCDate()),
        hour: pad2(shifted.getUTCHours()),
        minute: pad2(shifted.getUTCMinutes()),
        second: pad2(shifted.getUTCSeconds()),
    }
}

function formatTimestampMs(timestampMs) {
    const barTimeMs = parseInt(timestampMs, 10) || Date.now()
    const utcDate = new Date(barTimeMs)
    const easternOffset = isUsEasternDst(utcDate) ? -4 : -5
    const usParts = formatDateTimeByOffset(barTimeMs, easternOffset)
    const cnParts = formatDateTimeByOffset(barTimeMs, 8)
    return {
        usTime: `${usParts.year}-${usParts.month}-${usParts.day} ${usParts.hour}:${usParts.minute}:${usParts.second}`,
        cnTime: `${cnParts.year}-${cnParts.month}-${cnParts.day} ${cnParts.hour}:${cnParts.minute}:${cnParts.second}`,
        barTimeMs: barTimeMs,
    }
}

function formatNowStrings() {
    return formatTimestampMs(Date.now())
}

function firstNonEmpty() {
    for (let i = 0; i < arguments.length; i++) {
        const value = arguments[i]
        if (value !== undefined && value !== null && value !== "") {
            return value
        }
    }
    return ""
}

function nextSequence(orderUniqueId) {
    try {
        const existingDetails = $app.findRecordsByFilter(
            "order_details",
            "order_id = {:orderId}",
            "-bar_time_ms",
            1000,
            0,
            { orderId: orderUniqueId }
        )
        return existingDetails.length + 1
    } catch (err) {
        console.error(`[OrderEvents] 查询 order_details 失败: order_id=${orderUniqueId}`, err)
        return 1
    }
}

function getJsonField(record, fieldName) {
    if (!record) return {}

    if (typeof record.getString === "function") {
        const raw = record.getString(fieldName) || ""
        if (raw) {
            try {
                return JSON.parse(raw)
            } catch (err) {
                console.error(`[OrderEvents] 解析 ${fieldName} JSON 失败:`, err)
            }
        }
    }

    const value = typeof record.get === "function" ? record.get(fieldName) : record[fieldName]
    if (value && typeof value === "object") return value
    if (typeof value === "string" && value) {
        try {
            return JSON.parse(value)
        } catch (err) {
            console.error(`[OrderEvents] 解析 ${fieldName} 字符串失败:`, err)
        }
    }
    return {}
}

function getOrderExtra(record) {
    return getJsonField(record, "extra")
}

function mergeOrderExtra(record, patch, saveAfterMerge) {
    const merged = {
        ...getOrderExtra(record),
        ...(patch || {}),
    }
    record.set("extra", merged)
    if (saveAfterMerge) {
        $app.save(record)
    }
    return merged
}

function stripInternalExtra(extra) {
    const safe = {}
    const source = extra || {}
    Object.keys(source).forEach((key) => {
        if (!ORDER_INTERNAL_EXTRA_KEYS[key]) {
            safe[key] = source[key]
        }
    })
    return safe
}

function normalizeOrderRole(role, orderType) {
    if (role) return role
    const type = String(orderType || "").toLowerCase()
    if (type === "entry") return "entry"
    if (type === "takeprofit") return "take_profit"
    if (type === "stoploss") return "stop_loss"
    return ""
}

function normalizeRelationStatus(relationStatus, status) {
    if (relationStatus) return relationStatus
    const normalizedStatus = String(status || "").toLowerCase()
    if (normalizedStatus === "canceled" || normalizedStatus === "closed") {
        return "closed"
    }
    return "active"
}

function normalizePositionSide(positionSide, direction) {
    return firstNonEmpty(positionSide, direction, "")
}

function resolveOrderRelationship(recordOrData, fallback) {
    const source = recordOrData || {}
    const fallbackData = fallback || {}
    const get = (typeof source.get === "function")
        ? source.get.bind(source)
        : function(fieldName) { return source[fieldName] }

    const extra = getJsonField(source, "extra")
    const fallbackExtra = (fallbackData && typeof fallbackData.get === "function")
        ? getJsonField(fallbackData, "extra")
        : (fallbackData.extra || {})

    const uniqueId = firstNonEmpty(
        get("unique_id"),
        extra.unique_id,
        fallbackData.unique_id,
        fallbackExtra.unique_id,
        ""
    )
    const orderType = firstNonEmpty(
        get("order_type"),
        extra.order_type,
        fallbackData.order_type,
        fallbackExtra.order_type,
        ""
    )
    const role = normalizeOrderRole(
        firstNonEmpty(get("role"), extra.role, fallbackData.role, fallbackExtra.role, ""),
        orderType
    )
    const direction = firstNonEmpty(
        get("direction"),
        extra.direction,
        fallbackData.direction,
        fallbackExtra.direction,
        ""
    )
    const positionSide = normalizePositionSide(
        firstNonEmpty(
            get("position_side"),
            extra.position_side,
            fallbackData.position_side,
            fallbackExtra.position_side,
            ""
        ),
        direction
    )
    const entryOrderUniqueId = firstNonEmpty(
        get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        fallbackData.entry_order_unique_id,
        fallbackExtra.entry_order_unique_id,
        role === "entry" ? uniqueId : "",
        uniqueId
    )
    const tradeGroupId = firstNonEmpty(
        get("trade_group_id"),
        extra.trade_group_id,
        fallbackData.trade_group_id,
        fallbackExtra.trade_group_id,
        entryOrderUniqueId,
        uniqueId
    )
    const parentOrderUniqueId = firstNonEmpty(
        get("parent_order_unique_id"),
        extra.parent_order_unique_id,
        fallbackData.parent_order_unique_id,
        fallbackExtra.parent_order_unique_id,
        role && role !== "entry" ? entryOrderUniqueId : "",
        ""
    )
    const siblingOrderUniqueId = firstNonEmpty(
        get("sibling_order_unique_id"),
        extra.sibling_order_unique_id,
        fallbackData.sibling_order_unique_id,
        fallbackExtra.sibling_order_unique_id,
        ""
    )
    const brokerOrderId = firstNonEmpty(
        get("broker_order_id"),
        extra.broker_order_id,
        fallbackData.broker_order_id,
        fallbackExtra.broker_order_id,
        get("order_id"),
        extra.order_id,
        fallbackData.order_id,
        fallbackExtra.order_id,
        ""
    )
    const relationStatus = normalizeRelationStatus(
        firstNonEmpty(
            get("relation_status"),
            extra.relation_status,
            fallbackData.relation_status,
            fallbackExtra.relation_status,
            ""
        ),
        firstNonEmpty(get("status"), extra.status, fallbackData.status, fallbackExtra.status, "")
    )

    return {
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        parent_order_unique_id: parentOrderUniqueId,
        sibling_order_unique_id: siblingOrderUniqueId,
        role: role,
        relation_status: relationStatus,
        broker_order_id: brokerOrderId,
        position_side: positionSide,
    }
}

function applyOrderRelationship(record, options, saveAfterApply) {
    const relation = resolveOrderRelationship(options, record)
    record.set("trade_group_id", relation.trade_group_id || "")
    record.set("entry_order_unique_id", relation.entry_order_unique_id || "")
    record.set("parent_order_unique_id", relation.parent_order_unique_id || "")
    record.set("sibling_order_unique_id", relation.sibling_order_unique_id || "")
    record.set("role", relation.role || "")
    record.set("relation_status", relation.relation_status || "")
    record.set("broker_order_id", relation.broker_order_id || "")
    record.set("position_side", relation.position_side || "")

    mergeOrderExtra(record, relation, false)
    if (saveAfterApply) {
        $app.save(record)
    }
    return relation
}

function getOrderStatusText(status) {
    return ORDER_STATUS_TEXT_MAP[status] || status || "未知"
}

function getOrderStatusTransitionText(previousStatus, currentStatus) {
    const currentText = getOrderStatusText(currentStatus)
    if (!previousStatus || previousStatus === currentStatus) {
        return currentText
    }
    return getOrderStatusText(previousStatus) + " -> " + currentText
}

function resolveOrderEventTimes(options, fallback) {
    const opts = options || {}
    const fallbackData = fallback || {}
    const rawBarTimeMs = firstNonEmpty(
        opts.bar_time_ms,
        opts.barTimeMs,
        fallbackData.bar_time_ms,
        fallbackData.barTimeMs
    )
    const normalized = formatTimestampMs(rawBarTimeMs || Date.now())
    return {
        us_time: firstNonEmpty(opts.us_time, opts.usTime, fallbackData.us_time, fallbackData.usTime, normalized.usTime),
        cn_time: firstNonEmpty(opts.cn_time, opts.cnTime, fallbackData.cn_time, fallbackData.cnTime, normalized.cnTime),
        bar_time_ms: normalized.barTimeMs,
    }
}

function applyOrderEventTimes(record, options, saveAfterApply) {
    const timePatch = resolveOrderEventTimes(options)
    const mergedExtra = {
        ...getOrderExtra(record),
        us_time: timePatch.us_time,
        cn_time: timePatch.cn_time,
        bar_time_ms: timePatch.bar_time_ms,
    }
    record.set("us_time", timePatch.us_time)
    record.set("cn_time", timePatch.cn_time)
    record.set("bar_time_ms", timePatch.bar_time_ms)
    record.set("extra", mergedExtra)
    if (saveAfterApply) {
        $app.save(record)
    }
    return timePatch
}

function resolveOrderStatusEventTimes(record, options) {
    const opts = options || {}
    const extra = record ? getOrderExtra(record) : {}
    const candidate = resolveOrderEventTimes(opts, extra)
    const previousStatus = firstNonEmpty(opts.previous_status, record ? record.get("status") : "", extra.current_status)
    const currentStatus = firstNonEmpty(opts.status, previousStatus)
    const hasExplicitTimeInput = opts.us_time !== undefined || opts.usTime !== undefined ||
        opts.cn_time !== undefined || opts.cnTime !== undefined ||
        opts.bar_time_ms !== undefined || opts.barTimeMs !== undefined

    if (!record) {
        return candidate
    }

    const currentUsTime = firstNonEmpty(record.get("us_time"), extra.us_time)
    const currentCnTime = firstNonEmpty(record.get("cn_time"), extra.cn_time)
    const currentBarTimeMs = parseInt(firstNonEmpty(record.get("bar_time_ms"), extra.bar_time_ms, 0), 10) || 0
    const unchanged = candidate.us_time === currentUsTime &&
        candidate.cn_time === currentCnTime &&
        candidate.bar_time_ms === currentBarTimeMs

    if (previousStatus && currentStatus && previousStatus !== currentStatus && (!hasExplicitTimeInput || unchanged)) {
        return formatNowStrings()
    }

    return candidate
}

function applyOrderStatusMeta(record, options, saveAfterApply) {
    const opts = options || {}
    const extra = getOrderExtra(record)
    const currentStatus = opts.status || record.get("status") || ""
    const previousStatus = opts.previous_status != null
        ? opts.previous_status
        : (extra.current_status || extra.previous_status || "")
    const eventTimes = resolveOrderEventTimes(opts, extra)
    const orderTime = firstNonEmpty(
        opts.order_time,
        record.get("order_time"),
        extra.order_time,
        eventTimes.us_time
    )
    const fillTime = currentStatus === "Filled"
        ? firstNonEmpty(opts.fill_time, record.get("fill_time"), extra.fill_time, eventTimes.us_time)
        : firstNonEmpty(record.get("fill_time"), extra.fill_time)

    const patch = {
        order_time: orderTime,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        previous_status: previousStatus || "",
        current_status: currentStatus,
        status_transition_text: getOrderStatusTransitionText(previousStatus, currentStatus),
        status_updated_us_time: eventTimes.us_time,
        status_updated_cn_time: eventTimes.cn_time,
        status_updated_bar_time_ms: eventTimes.bar_time_ms,
        last_status_source: opts.source || extra.last_status_source || "",
        last_status_reason: opts.reason || extra.last_status_reason || "",
    }

    if (!extra.created_us_time && (opts.created_us_time || !previousStatus)) {
        patch.created_us_time = firstNonEmpty(opts.created_us_time, orderTime, eventTimes.us_time)
    }
    if (!extra.created_cn_time && (opts.created_cn_time || !previousStatus)) {
        patch.created_cn_time = firstNonEmpty(opts.created_cn_time, eventTimes.cn_time)
    }
    if (!extra.created_bar_time_ms && (opts.created_bar_time_ms || !previousStatus)) {
        patch.created_bar_time_ms = firstNonEmpty(opts.created_bar_time_ms, eventTimes.bar_time_ms)
    }

    if (fillTime) {
        patch.fill_time = fillTime
    }
    if (currentStatus === "Filled") {
        patch.filled_us_time = firstNonEmpty(opts.fill_us_time, fillTime, eventTimes.us_time)
        patch.filled_cn_time = firstNonEmpty(opts.fill_cn_time, eventTimes.cn_time)
        patch.filled_bar_time_ms = firstNonEmpty(opts.fill_bar_time_ms, eventTimes.bar_time_ms)
    } else if (extra.filled_us_time) {
        patch.filled_us_time = extra.filled_us_time
        patch.filled_cn_time = extra.filled_cn_time || ""
        patch.filled_bar_time_ms = extra.filled_bar_time_ms || 0
    }

    const merged = mergeOrderExtra(record, patch, false)
    record.set("us_time", patch.us_time)
    record.set("cn_time", patch.cn_time)
    record.set("bar_time_ms", patch.bar_time_ms)
    if (patch.order_time) {
        record.set("order_time", patch.order_time)
    }
    if (patch.fill_time) {
        record.set("fill_time", patch.fill_time)
    }
    if (saveAfterApply) {
        $app.save(record)
    }
    return {
        extra: merged,
        eventTimes: eventTimes,
    }
}

function appendOrderDetail(record, options) {
    const opts = options || {}
    const status = opts.status || record.get("status") || ""
    const source = opts.source || "unknown"
    const reason = opts.reason || ""
    const uniqueId = record.get("unique_id") || record.id
    const symbol = record.get("symbol") || ""
    const environment = opts.environment || record.get("environment") || getOrderExtra(record).environment || "live"
    const sequence = nextSequence(uniqueId)
    const nowStrings = formatNowStrings()

    const detailsCollection = $app.findCollectionByNameOrId("order_details")
    const detailRecord = new Record(detailsCollection, {})
    detailRecord.set("order_id", uniqueId)
    detailRecord.set("symbol", symbol)
    detailRecord.set("environment", environment)
    detailRecord.set("direction", record.get("direction") || "")
    detailRecord.set("order_type", record.get("order_type") || "")
    detailRecord.set("status", status)
    detailRecord.set("reason", reason)
    detailRecord.set("signal_id", record.get("signal_id") || "")
    detailRecord.set("us_time", opts.us_time || record.get("us_time") || nowStrings.usTime)
    detailRecord.set("cn_time", opts.cn_time || record.get("cn_time") || nowStrings.cnTime)
    detailRecord.set("bar_time_ms", opts.bar_time_ms || record.get("bar_time_ms") || nowStrings.barTimeMs)

    const relation = resolveOrderRelationship(opts, record)
    detailRecord.set("broker_order_id", relation.broker_order_id || "")
    detailRecord.set("trade_group_id", relation.trade_group_id || "")
    detailRecord.set("entry_order_unique_id", relation.entry_order_unique_id || "")
    detailRecord.set("parent_order_unique_id", relation.parent_order_unique_id || "")
    detailRecord.set("sibling_order_unique_id", relation.sibling_order_unique_id || "")
    detailRecord.set("role", relation.role || "")
    detailRecord.set("relation_status", relation.relation_status || "")
    detailRecord.set("position_side", relation.position_side || "")

    const existingExtra = stripInternalExtra(getOrderExtra(record))
    detailRecord.set("extra", {
        sequence: sequence,
        environment: environment,
        source: source,
        status: status,
        original_order_id: record.get("order_id") || "",
        order_id: record.get("order_id") || "",
        broker_order_id: relation.broker_order_id || "",
        trade_group_id: relation.trade_group_id || "",
        entry_order_unique_id: relation.entry_order_unique_id || "",
        parent_order_unique_id: relation.parent_order_unique_id || "",
        sibling_order_unique_id: relation.sibling_order_unique_id || "",
        role: relation.role || "",
        relation_status: relation.relation_status || "",
        position_side: relation.position_side || "",
        quantity: record.get("quantity"),
        limit_price: record.get("limit_price"),
        fill_price: record.get("fill_price"),
        filled_qty: record.get("filled_qty"),
        tp_price: record.get("tp_price"),
        sl_price: record.get("sl_price"),
        pnl: record.get("pnl"),
        commission: record.get("commission"),
        rr_ratio: record.get("rr_ratio"),
        order_time: opts.order_time || record.get("order_time") || "",
        ...existingExtra,
        ...(opts.extra || {}),
    })

    $app.save(detailRecord)
    console.log(`[OrderEvents] order_details 已保存: order_id=${uniqueId}, status=${status}, source=${source}, sequence=${sequence}`)
    return detailRecord
}

module.exports = {
    appendOrderDetail,
    getOrderExtra,
    mergeOrderExtra,
    getOrderStatusText,
    getOrderStatusTransitionText,
    resolveOrderRelationship,
    applyOrderRelationship,
    resolveOrderEventTimes,
    resolveOrderStatusEventTimes,
    applyOrderEventTimes,
    applyOrderStatusMeta,
    formatNowStrings,
}
