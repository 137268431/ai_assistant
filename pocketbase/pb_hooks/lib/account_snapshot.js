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

function isClosedOrderStatus(status) {
    const key = toText(status).toUpperCase()
    return ["FILLED", "EXECUTED", "CANCELED", "CANCELLED", "CLOSED", "REJECTED", "INACTIVE", "EXPIRED"].indexOf(key) !== -1
}

function isOpenLikeOrder(order) {
    if (!order || isClosedOrderStatus(order.status)) return false
    const relationStatus = toText(order.relation_status).toLowerCase()
    if (relationStatus === "active" || relationStatus === "planned") return true
    return Number(order.status_weight || 0) >= 40
}

function serializeManagedOrder(order) {
    return {
        symbol: order.symbol || "",
        unique_id: order.unique_id || "",
        broker_order_id: order.broker_order_id || "",
        signal_id: order.signal_id || "",
        trade_group_id: order.trade_group_id || "",
        entry_order_unique_id: order.entry_order_unique_id || "",
        role: order.role || "",
        relation_status: order.relation_status || "",
        status: order.status || "",
        quantity: order.quantity || 0,
        filled_qty: order.filled_qty || 0,
        updated: order.updated || "",
    }
}

function buildManagedOrderContext(environment, brokerOrders) {
    const brokerList = Array.isArray(brokerOrders) ? brokerOrders : []
    const brokerOrderIds = {}
    for (let i = 0; i < brokerList.length; i++) {
        const brokerOrderId = toText(brokerList[i] && (brokerList[i].order_id || brokerList[i].broker_order_id))
        if (brokerOrderId) brokerOrderIds[brokerOrderId] = true
    }

    let orderRecords = []
    try {
        orderRecords = $app.findRecordsByFilter(
            "orders",
            "environment = {:env}",
            "-updated",
            800,
            0,
            { env: environment }
        ) || []
    } catch (_) {
        orderRecords = []
    }

    const groupsByKey = {}
    const pbActiveOrderIds = {}
    let activeOrderCount = 0

    for (let i = 0; i < orderRecords.length; i++) {
        const normalized = normalizeOrderRecord(orderRecords[i])
        if (!normalized.symbol) continue
        const groupKey = normalized.trade_group_id || normalized.entry_order_unique_id || normalized.unique_id || normalized.broker_order_id
        if (!groupKey) continue

        if (!groupsByKey[groupKey]) {
            groupsByKey[groupKey] = {
                symbol: normalized.symbol,
                signal_id: normalized.signal_id || "",
                trade_group_id: normalized.trade_group_id || groupKey,
                entry_order_unique_id: normalized.entry_order_unique_id || normalized.unique_id || groupKey,
                latest_updated_ms: 0,
                latest_updated: "",
                latest_order_status: "",
                best_status_weight: -1,
                entry_filled_qty: 0,
                exit_filled_qty: 0,
                has_active_order: false,
                matched_broker_orders: 0,
                orders: [],
            }
        }

        const group = groupsByKey[groupKey]
        group.orders.push(normalized)
        if (!group.signal_id && normalized.signal_id) group.signal_id = normalized.signal_id
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

        if (isOpenLikeOrder(normalized)) {
            group.has_active_order = true
            activeOrderCount += 1
            if (normalized.broker_order_id) {
                pbActiveOrderIds[normalized.broker_order_id] = true
                if (brokerOrderIds[normalized.broker_order_id]) {
                    group.matched_broker_orders += 1
                }
            }
        }
    }

    const activeGroups = Object.keys(groupsByKey).map((key) => {
        const group = groupsByKey[key]
        const hasOpenExposure = group.entry_filled_qty > group.exit_filled_qty
        return {
            symbol: group.symbol,
            signal_id: group.signal_id,
            trade_group_id: group.trade_group_id,
            entry_order_unique_id: group.entry_order_unique_id,
            latest_updated: group.latest_updated,
            latest_updated_ms: group.latest_updated_ms,
            latest_order_status: group.latest_order_status,
            has_active_order: group.has_active_order,
            has_open_exposure: hasOpenExposure,
            broker_matched: group.matched_broker_orders > 0,
            matched_broker_orders: group.matched_broker_orders,
            order_count: group.orders.length,
            orders: group.orders.map(serializeManagedOrder),
        }
    }).filter((group) => group.has_active_order || group.has_open_exposure)

    activeGroups.sort((left, right) => {
        const leftScore = (left.has_open_exposure ? 1000 : 0) + (left.has_active_order ? 100 : 0) + (left.broker_matched ? 10 : 0)
        const rightScore = (right.has_open_exposure ? 1000 : 0) + (right.has_active_order ? 100 : 0) + (right.broker_matched ? 10 : 0)
        if (leftScore !== rightScore) return rightScore - leftScore
        return (right.latest_updated_ms || 0) - (left.latest_updated_ms || 0)
    })

    const pbOnlyActiveGroups = activeGroups.filter((group) => group.has_active_order && !group.broker_matched)
    const brokerOnlyOrders = brokerList.filter((order) => {
        const brokerOrderId = toText(order && (order.order_id || order.broker_order_id))
        return brokerOrderId && !pbActiveOrderIds[brokerOrderId]
    }).map((order) => ({
        symbol: toText(order && order.symbol).toUpperCase(),
        order_id: toText(order && (order.order_id || order.broker_order_id)),
        status: toText(order && order.status),
    }))

    return {
        active_groups: activeGroups,
        pb_only_active_groups: pbOnlyActiveGroups,
        active_order_count: activeOrderCount,
        broker_only_orders: brokerOnlyOrders,
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
    const brokerOrders = Array.isArray(payload.orders) ? payload.orders : []
    const managedOrderContext = buildManagedOrderContext(environment, brokerOrders)
    const symbols = positions
        .map((item) => toText(item && item.symbol).toUpperCase())
        .concat(managedOrderContext.active_groups.map((group) => toText(group && group.symbol).toUpperCase()))
        .filter(Boolean)
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
        pb_active_order_groups: managedOrderContext.active_groups.length,
        pb_active_orders: managedOrderContext.active_order_count,
        pb_only_active_order_groups: managedOrderContext.pb_only_active_groups.length,
        broker_only_open_orders: managedOrderContext.broker_only_orders.length,
    }

    payload.managed_order_groups = managedOrderContext.active_groups
    payload.pb_only_order_groups = managedOrderContext.pb_only_active_groups
    payload.order_reconciliation = {
        broker_total_orders: brokerOrders.length,
        broker_open_orders: Number((payload.counts || {}).open_orders || 0) || 0,
        pb_active_order_groups: managedOrderContext.active_groups.length,
        pb_active_orders: managedOrderContext.active_order_count,
        pb_only_active_order_groups: managedOrderContext.pb_only_active_groups.length,
        broker_only_open_orders: managedOrderContext.broker_only_orders.length,
        broker_only_orders: managedOrderContext.broker_only_orders,
    }

    return payload
}

module.exports = {
    enrichAccountSnapshot,
}
