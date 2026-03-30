/// <reference path="../pb_data/types.d.ts" />

/**
 * order_events.js
 * 共享订单事件日志与 order_details 写入工具
 */

const ORDER_INTERNAL_EXTRA_KEYS = {
    feishu_order_message_id: true,
    feishu_order_card_version: true,
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

function appendOrderDetail(record, options) {
    const opts = options || {}
    const status = opts.status || record.get("status") || ""
    const source = opts.source || "unknown"
    const reason = opts.reason || ""
    const uniqueId = record.get("unique_id") || record.id
    const symbol = record.get("symbol") || ""
    const sequence = nextSequence(uniqueId)
    const nowStrings = formatNowStrings()

    const detailsCollection = $app.findCollectionByNameOrId("order_details")
    const detailRecord = new Record(detailsCollection, {})
    detailRecord.set("order_id", uniqueId)
    detailRecord.set("symbol", symbol)
    detailRecord.set("direction", record.get("direction") || "")
    detailRecord.set("order_type", record.get("order_type") || "")
    detailRecord.set("status", status)
    detailRecord.set("reason", reason)
    detailRecord.set("signal_id", record.get("signal_id") || "")
    detailRecord.set("us_time", opts.us_time || record.get("us_time") || nowStrings.usTime)
    detailRecord.set("cn_time", opts.cn_time || record.get("cn_time") || nowStrings.cnTime)
    detailRecord.set("bar_time_ms", opts.bar_time_ms || record.get("bar_time_ms") || nowStrings.barTimeMs)

    const existingExtra = stripInternalExtra(getOrderExtra(record))
    detailRecord.set("extra", {
        sequence: sequence,
        source: source,
        status: status,
        original_order_id: record.get("order_id") || "",
        order_id: record.get("order_id") || "",
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
    resolveOrderEventTimes,
    applyOrderEventTimes,
    formatNowStrings,
}
