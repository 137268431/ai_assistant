function toText(value) {
    return String(value == null ? "" : value).trim()
}

function parseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback
    const text = toText(value).toLowerCase()
    if (["1", "true", "yes", "y", "on"].includes(text)) return true
    if (["0", "false", "no", "n", "off"].includes(text)) return false
    return fallback
}

function parseInteger(value, fallback, minValue, maxValue) {
    const parsed = parseInt(value, 10)
    if (!Number.isFinite(parsed)) return fallback
    let next = parsed
    if (Number.isFinite(minValue)) next = Math.max(minValue, next)
    if (Number.isFinite(maxValue)) next = Math.min(maxValue, next)
    return next
}

function buildOrderQuery(options) {
    const opts = options || {}
    const params = {
        env: toText(opts.environment || "live").toLowerCase() || "live",
    }
    const filters = ["environment = {:env}"]

    const signalId = toText(opts.signalId)
    if (signalId) {
        params.signalId = signalId
        filters.push("signal_id = {:signalId}")
    }

    const uniqueId = toText(opts.uniqueId)
    if (uniqueId) {
        params.uniqueId = uniqueId
        filters.push("unique_id = {:uniqueId}")
    }

    const tradeGroupId = toText(opts.tradeGroupId)
    if (tradeGroupId) {
        params.tradeGroupId = tradeGroupId
        filters.push("(trade_group_id = {:tradeGroupId} || entry_order_unique_id = {:tradeGroupId})")
    }

    const updatedAfter = toText(opts.updatedAfter)
    if (updatedAfter) {
        params.updatedAfter = updatedAfter
        filters.push("updated >= {:updatedAfter}")
    }

    const statuses = Array.isArray(opts.statuses)
        ? opts.statuses.map((value) => toText(value)).filter(Boolean)
        : []
    if (statuses.length > 0) {
        const statusFilters = []
        for (let i = 0; i < statuses.length; i++) {
            const key = `status${i}`
            params[key] = statuses[i]
            statusFilters.push(`status = {:${key}}`)
        }
        filters.push(`(${statusFilters.join(" || ")})`)
    }

    return {
        filter: filters.join(" && "),
        params: params,
    }
}

function loadOrderRecords(options) {
    const opts = options || {}
    if (Array.isArray(opts.orderRecords)) {
        return opts.orderRecords
    }

    const app = opts.app || globalThis.$app
    if (!app || typeof app.findRecordsByFilter !== "function") {
        throw new Error("order detail reconcile requires app.findRecordsByFilter")
    }

    const query = buildOrderQuery(opts)
    const sort = toText(opts.sort || "-updated,-created") || "-updated,-created"
    const limit = parseInteger(opts.limit, 20, 1, 500)
    const offset = parseInteger(opts.offset, 0, 0)
    const scanAll = parseBoolean(opts.scanAll, false)
    if (!scanAll) {
        return app.findRecordsByFilter("orders", query.filter, sort, limit, offset, query.params) || []
    }

    const pageSize = parseInteger(opts.pageSize, limit, 1, 200)
    const maxScan = parseInteger(opts.maxScan, pageSize, pageSize, 2000)
    const records = []
    let currentOffset = offset

    while (records.length < maxScan) {
        const batchSize = Math.min(pageSize, maxScan - records.length)
        const batch = app.findRecordsByFilter("orders", query.filter, sort, batchSize, currentOffset, query.params) || []
        if (batch.length === 0) break
        for (let i = 0; i < batch.length; i++) {
            records.push(batch[i])
        }
        if (batch.length < batchSize) break
        currentOffset += batch.length
    }

    return records
}

function loadDetailRecords(options, orderRecord) {
    const opts = options || {}
    if (typeof opts.getDetailRecords === "function") {
        return opts.getDetailRecords(orderRecord, opts) || []
    }

    const app = opts.app || globalThis.$app
    if (!app || typeof app.findRecordsByFilter !== "function") {
        throw new Error("order detail reconcile requires app.findRecordsByFilter")
    }

    const { COLLECTIONS } = require(`${__hooks}/lib/collections.js`)
    const environment = toText(opts.environment || "live").toLowerCase() || "live"
    const uniqueId = toText(orderRecord && orderRecord.get && orderRecord.get("unique_id"))
    const detailLimit = parseInteger(opts.detailLimit, 50, 1, 200)

    return app.findRecordsByFilter(
        COLLECTIONS.ORDER_DETAILS,
        "order_id = {:orderId} && environment = {:env}",
        "-created,-bar_time_ms",
        detailLimit,
        0,
        { orderId: uniqueId, env: environment }
    ) || []
}

function buildRepairContext(orderRecord, detailRecords, environment, suppressNotification, options) {
    const opts = options || {}
    const latestDetail = Array.isArray(detailRecords) && detailRecords.length > 0 ? detailRecords[0] : null
    const status = toText(orderRecord && orderRecord.get && orderRecord.get("status")) || "Submitted"

    return {
        environment: environment,
        signal_id: toText(orderRecord && orderRecord.get && orderRecord.get("signal_id")),
        unique_id: toText(orderRecord && orderRecord.get && orderRecord.get("unique_id")),
        symbol: toText(orderRecord && orderRecord.get && orderRecord.get("symbol")),
        status: status,
        order_type: toText(orderRecord && orderRecord.get && orderRecord.get("order_type")),
        order_id: toText(orderRecord && orderRecord.get && orderRecord.get("order_id")),
        broker_order_id: toText(orderRecord && orderRecord.get && orderRecord.get("broker_order_id")),
        us_time: toText(orderRecord && orderRecord.get && orderRecord.get("us_time")),
        cn_time: toText(orderRecord && orderRecord.get && orderRecord.get("cn_time")),
        bar_time_ms: Number(orderRecord && orderRecord.get && orderRecord.get("bar_time_ms")) || 0,
        suppress_notification: suppressNotification,
        latest_detail: latestDetail,
        latest_detail_status: toText(latestDetail && latestDetail.get && latestDetail.get("status")),
        latest_detail_id: latestDetail ? toText(latestDetail.id || (latestDetail.get && latestDetail.get("id"))) : "",
        repair_reason: opts.onlyMissing
            ? (detailRecords.length > 0
                ? "reconciled_missing_current_status_detail"
                : "reconciled_missing_ibkr_order_details")
            : "reconciled_order_snapshot",
        repair_mode: opts.onlyMissing
            ? (detailRecords.length > 0 ? "missing_current_status_detail" : "missing_ibkr_order_details")
            : "snapshot_replay",
    }
}

function appendReconciledDetail(orderRecord, context, options) {
    const opts = options || {}
    const appendOrderDetail = typeof opts.appendOrderDetail === "function"
        ? opts.appendOrderDetail
        : require(`${__hooks}/lib/order_events.js`).appendOrderDetail
    const source = toText(opts.source || "orders/reconcile") || "orders/reconcile"
    const repairSource = toText(opts.repairSource || source) || source
    const extra = {
        repair_source: repairSource,
        repair_mode: context.repair_mode,
        previous_detail_status: context.latest_detail_status,
        suppress_notification: context.suppress_notification,
        ...(opts.extra || {}),
    }

    return appendOrderDetail(orderRecord, {
        environment: context.environment,
        status: context.status,
        source: source,
        reason: context.repair_reason,
        us_time: context.us_time,
        cn_time: context.cn_time,
        bar_time_ms: context.bar_time_ms,
        extra: extra,
    })
}

function reconcileOrderDetails(options) {
    const opts = options || {}
    const environment = toText(opts.environment || "live").toLowerCase() || "live"
    const onlyMissing = parseBoolean(opts.onlyMissing, true)
    const dryRun = parseBoolean(opts.dryRun, false)
    const suppressNotification = parseBoolean(opts.suppressNotification, true)
    const results = []
    let repaired = 0
    let skipped = 0
    let failed = 0

    const orderRecords = loadOrderRecords({
        ...opts,
        environment: environment,
    })

    for (let i = 0; i < orderRecords.length; i++) {
        const orderRecord = orderRecords[i]
        const context = buildRepairContext(orderRecord, [], environment, suppressNotification, { onlyMissing: onlyMissing })

        if (!context.unique_id || !context.symbol) {
            skipped += 1
            results.push({
                signal_id: context.signal_id,
                unique_id: context.unique_id,
                symbol: context.symbol,
                status: "skipped_invalid_source",
            })
            continue
        }

        const detailRecords = loadDetailRecords({
            ...opts,
            environment: environment,
        }, orderRecord)
        const latestContext = buildRepairContext(orderRecord, detailRecords, environment, suppressNotification, { onlyMissing: onlyMissing })
        const latestDetailMatches = latestContext.latest_detail_status === latestContext.status

        if (onlyMissing && latestDetailMatches) {
            skipped += 1
            results.push({
                signal_id: latestContext.signal_id,
                unique_id: latestContext.unique_id,
                symbol: latestContext.symbol,
                status: "skipped_current_status_detail_exists",
                detail_record_id: latestContext.latest_detail_id,
                latest_detail_status: latestContext.latest_detail_status,
            })
            continue
        }

        if (dryRun) {
            repaired += 1
            results.push({
                signal_id: latestContext.signal_id,
                unique_id: latestContext.unique_id,
                symbol: latestContext.symbol,
                status: "dry_run_ready",
                payload: latestContext,
            })
            continue
        }

        try {
            const detailRecord = appendReconciledDetail(orderRecord, latestContext, opts)
            repaired += 1
            results.push({
                signal_id: latestContext.signal_id,
                unique_id: latestContext.unique_id,
                symbol: latestContext.symbol,
                status: "repaired_detail",
                detail_record_id: detailRecord && detailRecord.id ? detailRecord.id : "",
                latest_detail_status: latestContext.latest_detail_status,
            })
        } catch (err) {
            failed += 1
            results.push({
                signal_id: latestContext.signal_id,
                unique_id: latestContext.unique_id,
                symbol: latestContext.symbol,
                status: "failed_exception",
                latest_detail_status: latestContext.latest_detail_status,
                error: err && err.message ? err.message : String(err),
            })
        }
    }

    return {
        success: true,
        environment: environment,
        dry_run: dryRun,
        only_missing: onlyMissing,
        suppress_notification: suppressNotification,
        summary: {
            scanned: orderRecords.length,
            repaired: repaired,
            skipped: skipped,
            failed: failed,
        },
        results: results,
    }
}

module.exports = {
    buildOrderQuery,
    parseBoolean,
    parseInteger,
    reconcileOrderDetails,
    toText,
}
