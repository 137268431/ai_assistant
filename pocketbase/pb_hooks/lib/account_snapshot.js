function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback != null ? fallback : 0)
}

function toText(value) {
    return String(value == null ? "" : value).trim()
}

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

function pickFirstNonEmpty(values) {
    for (let i = 0; i < values.length; i++) {
        const text = toText(values[i])
        if (text) return text
    }
    return ""
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

function signalStatusWeight(status) {
    const key = toText(status).toLowerCase()
    if (key === "executed") return 90
    if (key === "confirmed") return 80
    if (key === "awaiting_confirm") return 70
    if (key === "pending") return 60
    if (key === "rejected" || key === "expired") return 20
    return 30
}

function normalizeOrderRecord(record) {
    const extra = parseJsonObject(record.get("extra"))
    const quantity = toNumber(record.get("quantity"), 0)
    const filledQtyRaw = toNumber(record.get("filled_qty"), 0)
    const status = pickFirstNonEmpty([record.get("status"), extra.status])
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
    const signalId = pickFirstNonEmpty([
        record.get("signal_id"),
        extra.signal_id,
    ])
    const updated = pickFirstNonEmpty([
        record.get("updated"),
        record.get("us_time"),
        record.get("created"),
    ])
    const relationStatus = pickFirstNonEmpty([
        record.get("relation_status"),
        extra.relation_status,
    ])
    const filledQty = filledQtyRaw > 0
        ? filledQtyRaw
        : ((String(status).toUpperCase() === "FILLED" || String(status).toUpperCase() === "EXECUTED") ? quantity : 0)

    return {
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

function normalizeSignalRecord(record) {
    const extra = parseJsonObject(record.get("extra"))
    const updated = pickFirstNonEmpty([
        record.get("updated"),
        record.get("us_time"),
        record.get("created"),
    ])
    const status = pickFirstNonEmpty([record.get("status"), extra.status])
    return {
        signal_id: pickFirstNonEmpty([record.get("signal_id"), extra.signal_id]),
        symbol: pickFirstNonEmpty([record.get("symbol"), extra.symbol]).toUpperCase(),
        status: status,
        note: pickFirstNonEmpty([record.get("note"), extra.status_reason, extra.note]),
        updated: updated,
        updated_ms: Date.parse(updated || "") || 0,
        status_weight: signalStatusWeight(status),
    }
}

function buildRelationContext(environment, symbols) {
    const normalizedSymbols = []
    const seenSymbols = {}
    for (let i = 0; i < (symbols || []).length; i++) {
        const symbol = toText(symbols[i]).toUpperCase()
        if (!symbol || seenSymbols[symbol]) continue
        seenSymbols[symbol] = true
        normalizedSymbols.push(symbol)
    }
    if (!normalizedSymbols.length) {
        return {
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

    let orderRecords = []
    try {
        orderRecords = $app.findRecordsByFilter(
            "orders",
            `environment = {:env} && (${orderClauses.join(" || ")})`,
            "-updated",
            400,
            0,
            orderParams
        ) || []
    } catch (_) {
        orderRecords = []
    }

    const groupsBySymbol = {}
    const signalIds = {}
    for (let i = 0; i < orderRecords.length; i++) {
        const normalized = normalizeOrderRecord(orderRecords[i])
        if (!normalized.symbol) continue
        const groupKey = normalized.trade_group_id || normalized.entry_order_unique_id || normalized.unique_id || normalized.broker_order_id
        if (!groupKey) continue
        if (!groupsBySymbol[normalized.symbol]) groupsBySymbol[normalized.symbol] = {}
        if (!groupsBySymbol[normalized.symbol][groupKey]) {
            groupsBySymbol[normalized.symbol][groupKey] = {
                trade_group_id: normalized.trade_group_id || groupKey,
                entry_order_unique_id: normalized.entry_order_unique_id || normalized.unique_id || groupKey,
                signal_id: normalized.signal_id,
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
        if (normalized.status_weight >= 70 && toText(normalized.status).toUpperCase() !== "FILLED") group.has_active_order = true
    }

    const activeGroupBySymbol = {}
    const signalMap = {}
    const signalIdList = []

    Object.keys(groupsBySymbol).forEach((symbol) => {
        const groups = Object.keys(groupsBySymbol[symbol]).map((key) => {
            const group = groupsBySymbol[symbol][key]
            group.has_open_exposure = group.entry_filled_qty > group.exit_filled_qty
            if (group.signal_id && !signalIds[group.signal_id]) {
                signalIds[group.signal_id] = true
                signalIdList.push(group.signal_id)
            }
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
                const normalized = normalizeSignalRecord(signalRecords[i])
                if (!normalized.signal_id) continue
                const existing = signalMap[normalized.signal_id]
                if (!existing || normalized.status_weight >= existing.status_weight || normalized.updated_ms >= existing.updated_ms) {
                    signalMap[normalized.signal_id] = normalized
                }
            }
        } catch (_) {}
    }

    return {
        activeGroupBySymbol: activeGroupBySymbol,
        signalMap: signalMap,
    }
}

function enrichAccountSnapshot(payload, environment) {
    if (!payload || typeof payload !== "object") return payload

    const positions = Array.isArray(payload.positions) ? payload.positions : []
    const symbols = positions.map((item) => toText(item && item.symbol).toUpperCase()).filter(Boolean)
    const context = buildRelationContext(environment, symbols)

    let systemManagedCount = 0
    let externalCount = 0
    let flatCount = 0

    payload.positions = positions.map((position) => {
        const normalizedSymbol = toText(position && position.symbol).toUpperCase()
        const quantity = toNumber(position && position.quantity, 0)
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

module.exports = {
    enrichAccountSnapshot,
}
