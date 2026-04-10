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

function cloneStringList(values) {
    if (!Array.isArray(values)) return []
    return values
        .map((item) => toText(item))
        .filter(Boolean)
}

function canonicalOrderStatus(status) {
    const key = toText(status).toUpperCase()
    if (["PENDING", "PRESUBMITTED", "SUBMITTED", "PENDINGSUBMIT", "INPROGRESS", "INIT"].indexOf(key) !== -1) return "SUBMITTED"
    if (["FILLED", "EXECUTED"].indexOf(key) !== -1) return "FILLED"
    if (["CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "EXPIRED", "API_CANCELLED"].indexOf(key) !== -1) return "CANCELED"
    return key || "UNKNOWN"
}

function orderStatusWeight(status) {
    const key = canonicalOrderStatus(status)
    if (key === "FILLED") return 90
    if (key === "SUBMITTED") return 70
    if (key === "CANCELED") return 10
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
    const brokerOrderId = pickFirstNonEmpty([
        record.get("broker_order_id"),
        extra.broker_order_id,
        record.get("order_id"),
    ])
    const orderId = pickFirstNonEmpty([
        record.get("order_id"),
        extra.order_id,
        brokerOrderId,
    ])
    const uniqueId = pickFirstNonEmpty([record.get("unique_id"), extra.unique_id])
    const groupKey = pickFirstNonEmpty([
        tradeGroupId,
        entryOrderUniqueId,
        uniqueId,
        brokerOrderId,
        orderId,
    ])
    const filledQty = filledQtyRaw > 0
        ? filledQtyRaw
        : ((canonicalOrderStatus(status) === "FILLED") ? quantity : 0)

    return {
        record_id: toText(record.get("id")),
        symbol: pickFirstNonEmpty([record.get("symbol"), extra.symbol]).toUpperCase(),
        status: status,
        status_key: canonicalOrderStatus(status),
        signal_id: signalId,
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        parent_order_unique_id: pickFirstNonEmpty([record.get("parent_order_unique_id"), extra.parent_order_unique_id]),
        sibling_order_unique_id: pickFirstNonEmpty([record.get("sibling_order_unique_id"), extra.sibling_order_unique_id]),
        unique_id: uniqueId,
        broker_order_id: brokerOrderId,
        order_id: orderId,
        role: role,
        relation_status: relationStatus,
        direction: pickFirstNonEmpty([record.get("direction"), extra.direction]),
        position_side: pickFirstNonEmpty([record.get("position_side"), extra.position_side]),
        quantity: quantity,
        filled_qty: filledQty,
        limit_price: toNumber(firstNonEmptyFromRaw([record.get("limit_price"), extra.limit_price]), 0),
        fill_price: toNumber(firstNonEmptyFromRaw([record.get("fill_price"), extra.fill_price]), 0),
        updated: updated,
        updated_ms: Date.parse(updated || "") || 0,
        status_weight: orderStatusWeight(status),
        group_key: groupKey,
        extra: extra,
    }
}

function firstNonEmptyFromRaw(values) {
    for (let i = 0; i < values.length; i++) {
        const value = values[i]
        if (value !== undefined && value !== null && value !== "") return value
    }
    return ""
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
    return ["FILLED", "CANCELED", "CLOSED", "REJECTED", "INACTIVE", "EXPIRED"].indexOf(canonicalOrderStatus(status)) !== -1
}

function isOpenLikeOrder(order) {
    if (!order || isClosedOrderStatus(order.status)) return false
    const relationStatus = toText(order.relation_status).toLowerCase()
    if (relationStatus === "active" || relationStatus === "planned") return true
    return Number(order.status_weight || 0) >= 40
}

function loadOrderRecords(environment) {
    try {
        return $app.findRecordsByFilter(
            "orders",
            "environment = {:env}",
            "-updated",
            800,
            0,
            { env: environment }
        ) || []
    } catch (_) {
        return []
    }
}

function serializeManagedOrder(order) {
    return {
        record_id: order.record_id || "",
        symbol: order.symbol || "",
        unique_id: order.unique_id || "",
        order_id: order.order_id || "",
        broker_order_id: order.broker_order_id || "",
        signal_id: order.signal_id || "",
        trade_group_id: order.trade_group_id || "",
        entry_order_unique_id: order.entry_order_unique_id || "",
        parent_order_unique_id: order.parent_order_unique_id || "",
        sibling_order_unique_id: order.sibling_order_unique_id || "",
        role: order.role || "",
        relation_status: order.relation_status || "",
        direction: order.direction || "",
        position_side: order.position_side || "",
        status: order.status || "",
        quantity: order.quantity || 0,
        filled_qty: order.filled_qty || 0,
        limit_price: order.limit_price || 0,
        fill_price: order.fill_price || 0,
        updated: order.updated || "",
    }
}

function buildPbOrderLookup(normalizedOrders) {
    const lookup = {
        by_broker_order_id: {},
        by_order_id: {},
        by_unique_id: {},
    }
    for (let i = 0; i < normalizedOrders.length; i++) {
        const order = normalizedOrders[i]
        if (order.broker_order_id && !lookup.by_broker_order_id[order.broker_order_id]) {
            lookup.by_broker_order_id[order.broker_order_id] = order
        }
        if (order.order_id && !lookup.by_order_id[order.order_id]) {
            lookup.by_order_id[order.order_id] = order
        }
        if (order.unique_id && !lookup.by_unique_id[order.unique_id]) {
            lookup.by_unique_id[order.unique_id] = order
        }
    }
    return lookup
}

function normalizeLiveOrder(order) {
    const normalized = order && typeof order === "object" ? order : {}
    const submittedTime = toText(normalized.submitted_time)
    const lastExecutionTime = toText(normalized.last_execution_time)
    const goodTillDate = toText(normalized.good_till_date)
    const clientOrderId = pickFirstNonEmpty([
        normalized.client_order_id,
        normalized.cOID,
        normalized.coid,
        normalized.order_ref,
        normalized.orderRef,
    ])
    const parentId = pickFirstNonEmpty([normalized.parent_id, normalized.parentId])
    const status = pickFirstNonEmpty([normalized.status, normalized.status_key])
    const statusKey = canonicalOrderStatus(normalized.status_key || status)
    const totalQuantity = toNumber(firstNonEmptyFromRaw([normalized.total_quantity, normalized.totalSize, normalized.quantity]), 0)
    const filledQuantity = toNumber(firstNonEmptyFromRaw([normalized.filled_quantity, normalized.filledQuantity, normalized.cum_fill]), 0)
    const remainingQuantity = firstNonEmptyFromRaw([normalized.remaining_quantity, normalized.remainingQuantity, normalized.remainingSize])
    const remaining = remainingQuantity !== ""
        ? toNumber(remainingQuantity, Math.max(totalQuantity - filledQuantity, 0))
        : Math.max(totalQuantity - filledQuantity, 0)
    const isOpen = normalized.is_open !== undefined
        ? Boolean(normalized.is_open)
        : !isClosedOrderStatus(statusKey)
    const seedSources = cloneStringList(normalized.seed_sources)
    const diagnosticTags = cloneStringList(normalized.diagnostic_tags)
    const updatedMs = Math.max(
        toNumber(normalized.last_execution_time_ms, 0),
        toNumber(normalized.submitted_time_ms, 0),
        Date.parse(lastExecutionTime || submittedTime || "") || 0
    )

    return {
        order_id: pickFirstNonEmpty([normalized.order_id, normalized.orderId, normalized.id]),
        parent_id: parentId,
        client_order_id: clientOrderId,
        symbol: toText(normalized.symbol).toUpperCase(),
        conid: toNumber(normalized.conid, 0),
        side: toText(normalized.side).toUpperCase(),
        status: status,
        status_key: statusKey,
        role: pickFirstNonEmpty([normalized.role]),
        order_type: toText(normalized.order_type || normalized.orderType).toUpperCase(),
        order_description: toText(normalized.order_description),
        price: toNumber(normalized.price, 0),
        trigger_price: toNumber(normalized.trigger_price, 0),
        avg_price: toNumber(normalized.avg_price, 0),
        total_quantity: totalQuantity,
        filled_quantity: filledQuantity,
        remaining_quantity: remaining,
        time_in_force: toText(normalized.time_in_force).toUpperCase(),
        account: toText(normalized.account),
        currency: toText(normalized.currency || "USD").toUpperCase(),
        asset_class: toText(normalized.asset_class).toUpperCase(),
        listing_exchange: toText(normalized.listing_exchange),
        submitted_time: submittedTime,
        submitted_time_ms: toNumber(normalized.submitted_time_ms, Date.parse(submittedTime || "") || 0),
        last_execution_time: lastExecutionTime,
        last_execution_time_ms: toNumber(normalized.last_execution_time_ms, Date.parse(lastExecutionTime || "") || 0),
        good_till_date: goodTillDate,
        good_till_date_ms: toNumber(normalized.good_till_date_ms, Date.parse(goodTillDate || "") || 0),
        outside_rth: Boolean(normalized.outside_rth),
        can_cancel: Boolean(normalized.can_cancel),
        can_modify: Boolean(normalized.can_modify),
        is_open: isOpen,
        is_child: normalized.is_child !== undefined ? Boolean(normalized.is_child) : Boolean(parentId),
        recovery_source: pickFirstNonEmpty([normalized.recovery_source, normalized._recovery_source]) || "bulk",
        seed_sources: seedSources,
        diagnostic_tags: diagnosticTags,
        diagnostic_note: toText(normalized.diagnostic_note),
        updated_ms: updatedMs,
        raw: normalized.raw && typeof normalized.raw === "object" ? normalized.raw : normalized,
    }
}

function resolvePbMatch(liveOrder, lookup) {
    if (!liveOrder) return null
    const orderId = toText(liveOrder.order_id)
    const clientOrderId = toText(liveOrder.client_order_id)
    if (orderId && lookup.by_broker_order_id[orderId]) return lookup.by_broker_order_id[orderId]
    if (orderId && lookup.by_order_id[orderId]) return lookup.by_order_id[orderId]
    if (clientOrderId && lookup.by_unique_id[clientOrderId]) return lookup.by_unique_id[clientOrderId]
    return null
}

function buildLiveGroupKey(liveOrder, pbMatch) {
    return pickFirstNonEmpty([
        pbMatch && pbMatch.trade_group_id,
        pbMatch && pbMatch.entry_order_unique_id,
        liveOrder && liveOrder.parent_id,
        liveOrder && liveOrder.order_id,
        liveOrder && liveOrder.client_order_id,
        `${toText(liveOrder && liveOrder.symbol).toUpperCase()}:${toText(liveOrder && liveOrder.side).toUpperCase()}:${toText(liveOrder && liveOrder.order_type).toUpperCase()}`,
    ])
}

function buildDiagnosticNote(liveOrder, pbMatch, tags) {
    const notes = []
    if (!pbMatch) {
        notes.push("broker 实时挂单未匹配到 PB 订单记录")
    }
    if (tags.indexOf("status_recovered") !== -1) {
        notes.push("该订单由单笔状态接口补回")
    }
    const mismatchTags = tags.filter((tag) => ["status_mismatch", "quantity_mismatch", "filled_qty_mismatch"].indexOf(tag) !== -1)
    if (pbMatch && mismatchTags.length) {
        notes.push(`PB 对账差异: ${mismatchTags.join(", ")}`)
    }
    if (tags.indexOf("missing_client_order_id") !== -1) {
        notes.push("client_order_id 缺失")
    }
    return notes.join("；")
}

function buildPbContext(liveOrder, pbMatch) {
    const tags = cloneStringList(liveOrder && liveOrder.diagnostic_tags)
    if (liveOrder && liveOrder.recovery_source === "status_recovered" && tags.indexOf("status_recovered") === -1) {
        tags.push("status_recovered")
    }
    if (liveOrder && !toText(liveOrder.client_order_id) && tags.indexOf("missing_client_order_id") === -1) {
        tags.push("missing_client_order_id")
    }
    if (!pbMatch) {
        return {
            match_state: "broker_only",
            record_id: "",
            signal_id: "",
            trade_group_id: "",
            entry_order_unique_id: "",
            parent_order_unique_id: "",
            sibling_order_unique_id: "",
            role: "",
            relation_status: "",
            direction: "",
            position_side: "",
            pb_status: "",
            pb_quantity: 0,
            pb_filled_qty: 0,
            pb_limit_price: 0,
            pb_fill_price: 0,
            diagnostic_tags: tags,
            diagnostic_note: buildDiagnosticNote(liveOrder, null, tags),
        }
    }

    if (canonicalOrderStatus(liveOrder.status_key || liveOrder.status) !== pbMatch.status_key && tags.indexOf("status_mismatch") === -1) {
        tags.push("status_mismatch")
    }
    if (Math.abs(toNumber(liveOrder.total_quantity, 0) - toNumber(pbMatch.quantity, 0)) > 1e-9 && tags.indexOf("quantity_mismatch") === -1) {
        tags.push("quantity_mismatch")
    }
    if (Math.abs(toNumber(liveOrder.filled_quantity, 0) - toNumber(pbMatch.filled_qty, 0)) > 1e-9 && tags.indexOf("filled_qty_mismatch") === -1) {
        tags.push("filled_qty_mismatch")
    }

    return {
        match_state: "matched",
        record_id: pbMatch.record_id || "",
        signal_id: pbMatch.signal_id || "",
        trade_group_id: pbMatch.trade_group_id || pbMatch.group_key || "",
        entry_order_unique_id: pbMatch.entry_order_unique_id || pbMatch.unique_id || "",
        parent_order_unique_id: pbMatch.parent_order_unique_id || "",
        sibling_order_unique_id: pbMatch.sibling_order_unique_id || "",
        role: pbMatch.role || "",
        relation_status: pbMatch.relation_status || "",
        direction: pbMatch.direction || "",
        position_side: pbMatch.position_side || "",
        pb_status: pbMatch.status || "",
        pb_quantity: pbMatch.quantity || 0,
        pb_filled_qty: pbMatch.filled_qty || 0,
        pb_limit_price: pbMatch.limit_price || 0,
        pb_fill_price: pbMatch.fill_price || 0,
        diagnostic_tags: tags,
        diagnostic_note: buildDiagnosticNote(liveOrder, pbMatch, tags),
    }
}

function finalizeGroup(group) {
    return {
        group_key: group.group_key,
        symbol: group.symbol,
        signal_id: group.signal_id,
        trade_group_id: group.trade_group_id,
        entry_order_unique_id: group.entry_order_unique_id,
        latest_updated: group.latest_updated,
        latest_updated_ms: group.latest_updated_ms,
        latest_order_status: group.latest_order_status,
        match_state: group.matched_live_orders > 0 ? "matched" : "broker_only",
        live_order_count: group.live_order_count,
        matched_live_orders: group.matched_live_orders,
        broker_only_live_orders: group.broker_only_live_orders,
        cancelable_orders: group.cancelable_orders,
        editable_orders: group.editable_orders,
        outside_rth_orders: group.outside_rth_orders,
        total_quantity: group.total_quantity,
        filled_quantity: group.filled_quantity,
        remaining_quantity: group.remaining_quantity,
        recovery_sources: Object.keys(group.recovery_sources).sort().map((key) => `${key}:${group.recovery_sources[key]}`),
        orders: group.orders,
    }
}

function sortGroups(groups) {
    return (groups || []).slice().sort((left, right) => {
        const leftScore = (left.matched_live_orders > 0 ? 100 : 0) + toNumber(left.cancelable_orders, 0)
        const rightScore = (right.matched_live_orders > 0 ? 100 : 0) + toNumber(right.cancelable_orders, 0)
        if (leftScore !== rightScore) return rightScore - leftScore
        return toNumber(right.latest_updated_ms, 0) - toNumber(left.latest_updated_ms, 0)
    })
}

function buildManagedOrderContext(environment, liveOrders) {
    const orderRecords = loadOrderRecords(environment)
    const normalizedOrderRecords = []
    for (let i = 0; i < orderRecords.length; i++) {
        const normalized = normalizeOrderRecord(orderRecords[i])
        if (normalized.symbol) normalizedOrderRecords.push(normalized)
    }

    const pbLookup = buildPbOrderLookup(normalizedOrderRecords)
    const activePbGroupsByKey = {}
    let activeOrderCount = 0

    for (let i = 0; i < normalizedOrderRecords.length; i++) {
        const normalized = normalizedOrderRecords[i]
        const groupKey = normalized.group_key
        if (!groupKey) continue

        if (!activePbGroupsByKey[groupKey]) {
            activePbGroupsByKey[groupKey] = {
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
                matched_live_orders: 0,
                orders: [],
            }
        }

        const group = activePbGroupsByKey[groupKey]
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
        }
    }

    const normalizedLiveOrders = []
    const liveGroupsByKey = {}
    const liveOrderIds = {}
    const matchedPbGroupCounts = {}
    const brokerOnlyOrders = []
    let matchedLiveOrderCount = 0
    let brokerOnlyLiveOrderCount = 0
    let editableOrderCount = 0
    let cancelableOrderCount = 0
    let outsideRthOrderCount = 0
    let missingClientOrderIdCount = 0
    let statusMismatchCount = 0
    let quantityMismatchCount = 0
    let filledQtyMismatchCount = 0

    for (let i = 0; i < (liveOrders || []).length; i++) {
        const liveOrder = normalizeLiveOrder(liveOrders[i])
        if (!liveOrder.order_id || !liveOrder.is_open) continue
        liveOrderIds[liveOrder.order_id] = true

        const pbMatch = resolvePbMatch(liveOrder, pbLookup)
        const pbContext = buildPbContext(liveOrder, pbMatch)
        liveOrder.pb_context = pbContext
        liveOrder.diagnostic_tags = cloneStringList(pbContext.diagnostic_tags)
        liveOrder.diagnostic_note = toText(pbContext.diagnostic_note)
        liveOrder.signal_id = pbContext.signal_id
        liveOrder.trade_group_id = pbContext.trade_group_id
        liveOrder.entry_order_unique_id = pbContext.entry_order_unique_id
        liveOrder.relation_status = pbContext.relation_status
        liveOrder.match_state = pbContext.match_state
        liveOrder.direction = pbContext.direction
        liveOrder.position_side = pbContext.position_side
        normalizedLiveOrders.push(liveOrder)

        if (pbContext.match_state === "matched") {
            matchedLiveOrderCount += 1
            const pbGroupKey = pickFirstNonEmpty([pbMatch && pbMatch.group_key, pbContext.trade_group_id, pbContext.entry_order_unique_id])
            if (pbGroupKey) matchedPbGroupCounts[pbGroupKey] = (matchedPbGroupCounts[pbGroupKey] || 0) + 1
        } else {
            brokerOnlyLiveOrderCount += 1
            brokerOnlyOrders.push({
                symbol: liveOrder.symbol,
                order_id: liveOrder.order_id,
                client_order_id: liveOrder.client_order_id,
                parent_id: liveOrder.parent_id,
                status: liveOrder.status,
            })
        }

        if (liveOrder.can_modify) editableOrderCount += 1
        if (liveOrder.can_cancel) cancelableOrderCount += 1
        if (liveOrder.outside_rth) outsideRthOrderCount += 1
        if (!liveOrder.client_order_id) missingClientOrderIdCount += 1
        if (liveOrder.diagnostic_tags.indexOf("status_mismatch") !== -1) statusMismatchCount += 1
        if (liveOrder.diagnostic_tags.indexOf("quantity_mismatch") !== -1) quantityMismatchCount += 1
        if (liveOrder.diagnostic_tags.indexOf("filled_qty_mismatch") !== -1) filledQtyMismatchCount += 1

        const liveGroupKey = buildLiveGroupKey(liveOrder, pbMatch)
        if (!liveGroupsByKey[liveGroupKey]) {
            liveGroupsByKey[liveGroupKey] = {
                group_key: liveGroupKey,
                symbol: liveOrder.symbol,
                signal_id: pbContext.signal_id || "",
                trade_group_id: pbContext.trade_group_id || liveGroupKey,
                entry_order_unique_id: pbContext.entry_order_unique_id || liveOrder.client_order_id || liveOrder.order_id,
                latest_updated: "",
                latest_updated_ms: 0,
                latest_order_status: "",
                live_order_count: 0,
                matched_live_orders: 0,
                broker_only_live_orders: 0,
                cancelable_orders: 0,
                editable_orders: 0,
                outside_rth_orders: 0,
                total_quantity: 0,
                filled_quantity: 0,
                remaining_quantity: 0,
                recovery_sources: {},
                orders: [],
            }
        }

        const liveGroup = liveGroupsByKey[liveGroupKey]
        liveGroup.live_order_count += 1
        if (!liveGroup.signal_id && pbContext.signal_id) liveGroup.signal_id = pbContext.signal_id
        if (!liveGroup.trade_group_id && pbContext.trade_group_id) liveGroup.trade_group_id = pbContext.trade_group_id
        if (!liveGroup.entry_order_unique_id && pbContext.entry_order_unique_id) liveGroup.entry_order_unique_id = pbContext.entry_order_unique_id
        if (pbContext.match_state === "matched") liveGroup.matched_live_orders += 1
        else liveGroup.broker_only_live_orders += 1
        if (liveOrder.can_cancel) liveGroup.cancelable_orders += 1
        if (liveOrder.can_modify) liveGroup.editable_orders += 1
        if (liveOrder.outside_rth) liveGroup.outside_rth_orders += 1
        liveGroup.total_quantity += Math.abs(toNumber(liveOrder.total_quantity, 0))
        liveGroup.filled_quantity += Math.abs(toNumber(liveOrder.filled_quantity, 0))
        liveGroup.remaining_quantity += Math.abs(toNumber(liveOrder.remaining_quantity, 0))
        liveGroup.orders.push(liveOrder)
        const updatedMs = Math.max(toNumber(liveOrder.last_execution_time_ms, 0), toNumber(liveOrder.submitted_time_ms, 0), toNumber(liveOrder.updated_ms, 0))
        if (updatedMs >= liveGroup.latest_updated_ms) {
            liveGroup.latest_updated_ms = updatedMs
            liveGroup.latest_updated = liveOrder.last_execution_time || liveOrder.submitted_time || ""
            liveGroup.latest_order_status = liveOrder.status
        }
        const recoverySource = liveOrder.recovery_source || "bulk"
        liveGroup.recovery_sources[recoverySource] = (liveGroup.recovery_sources[recoverySource] || 0) + 1
    }

    const activeGroups = Object.keys(activePbGroupsByKey).map((key) => {
        const group = activePbGroupsByKey[key]
        const hasOpenExposure = group.entry_filled_qty > group.exit_filled_qty
        const matchedLiveOrders = matchedPbGroupCounts[key] || 0
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
            broker_matched: matchedLiveOrders > 0,
            matched_broker_orders: matchedLiveOrders,
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
    const liveOrderGroups = sortGroups(Object.keys(liveGroupsByKey).map((key) => finalizeGroup(liveGroupsByKey[key])))
    const matchedOrderGroups = liveOrderGroups.filter((group) => group.matched_live_orders > 0)
    const brokerOnlyOrderGroups = liveOrderGroups.filter((group) => group.matched_live_orders === 0)

    normalizedLiveOrders.sort((left, right) => {
        if (left.can_cancel !== right.can_cancel) return left.can_cancel ? -1 : 1
        if (left.updated_ms !== right.updated_ms) return right.updated_ms - left.updated_ms
        return left.order_id.localeCompare(right.order_id)
    })

    return {
        order_records: normalizedOrderRecords,
        active_groups: activeGroups,
        pb_only_active_groups: pbOnlyActiveGroups,
        active_order_count: activeOrderCount,
        broker_only_orders: brokerOnlyOrders,
        live_orders: normalizedLiveOrders,
        live_order_groups: liveOrderGroups,
        matched_order_groups: matchedOrderGroups,
        broker_only_order_groups: brokerOnlyOrderGroups,
        matched_live_order_count: matchedLiveOrderCount,
        broker_only_live_order_count: brokerOnlyLiveOrderCount,
        editable_order_count: editableOrderCount,
        cancelable_order_count: cancelableOrderCount,
        outside_rth_order_count: outsideRthOrderCount,
        missing_client_order_id_count: missingClientOrderIdCount,
        status_mismatch_count: statusMismatchCount,
        quantity_mismatch_count: quantityMismatchCount,
        filled_qty_mismatch_count: filledQtyMismatchCount,
    }
}

function buildRelationContext(environment, symbols, normalizedOrderRecords) {
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

    let orders = Array.isArray(normalizedOrderRecords) ? normalizedOrderRecords.slice() : []
    if (!orders.length) {
        const records = loadOrderRecords(environment)
        orders = []
        for (let i = 0; i < records.length; i++) {
            const normalized = normalizeOrderRecord(records[i])
            if (normalized.symbol) orders.push(normalized)
        }
    }

    const symbolSet = {}
    for (let i = 0; i < normalizedSymbols.length; i++) symbolSet[normalizedSymbols[i]] = true
    const filteredOrders = orders.filter((item) => symbolSet[item.symbol])

    const groupsBySymbol = {}
    const signalIds = {}
    const signalIdList = []

    for (let i = 0; i < filteredOrders.length; i++) {
        const normalized = filteredOrders[i]
        const groupKey = normalized.group_key
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
        if (normalized.status_weight >= 70 && canonicalOrderStatus(normalized.status) !== "FILLED") group.has_active_order = true
    }

    const activeGroupBySymbol = {}
    const signalMap = {}

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
    const liveOpenOrders = Array.isArray(payload.live_open_orders) && payload.live_open_orders.length
        ? payload.live_open_orders
        : brokerOrders.filter((item) => !isClosedOrderStatus(item && (item.status_key || item.status)))
    const managedOrderContext = buildManagedOrderContext(environment, liveOpenOrders)
    const symbols = positions
        .map((item) => toText(item && item.symbol).toUpperCase())
        .concat(managedOrderContext.active_groups.map((group) => toText(group && group.symbol).toUpperCase()))
        .concat(managedOrderContext.live_order_groups.map((group) => toText(group && group.symbol).toUpperCase()))
        .filter(Boolean)
    const context = buildRelationContext(environment, symbols, managedOrderContext.order_records)

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

    const coverage = payload.live_order_coverage && typeof payload.live_order_coverage === "object"
        ? payload.live_order_coverage
        : {}
    const recoveredOpenOrders = toNumber(payload.counts && payload.counts.recovered_open_orders, 0)
        || managedOrderContext.live_orders.filter((item) => item.recovery_source === "status_recovered").length

    payload.live_open_orders = managedOrderContext.live_orders
    payload.live_order_groups = managedOrderContext.live_order_groups
    payload.matched_order_groups = managedOrderContext.matched_order_groups
    payload.broker_only_order_groups = managedOrderContext.broker_only_order_groups
    payload.managed_order_groups = managedOrderContext.active_groups
    payload.pb_only_order_groups = managedOrderContext.pb_only_active_groups
    payload.counts = {
        ...(payload.counts || {}),
        open_orders: managedOrderContext.live_orders.length,
        cancelable_orders: managedOrderContext.cancelable_order_count,
        editable_orders: managedOrderContext.editable_order_count,
        outside_rth_orders: managedOrderContext.outside_rth_order_count,
        recovered_open_orders: recoveredOpenOrders,
        broker_matched_orders: managedOrderContext.matched_live_order_count,
        broker_only_open_orders: managedOrderContext.broker_only_live_order_count,
        system_managed_positions: systemManagedCount,
        external_positions: externalCount,
        flat_positions: flatCount,
        pb_active_order_groups: managedOrderContext.active_groups.length,
        pb_active_orders: managedOrderContext.active_order_count,
        pb_only_active_order_groups: managedOrderContext.pb_only_active_groups.length,
        pb_shadow_groups: managedOrderContext.pb_only_active_groups.length,
        missing_client_order_id_orders: managedOrderContext.missing_client_order_id_count,
        status_mismatch_orders: managedOrderContext.status_mismatch_count,
        quantity_mismatch_orders: managedOrderContext.quantity_mismatch_count,
        filled_qty_mismatch_orders: managedOrderContext.filled_qty_mismatch_count,
    }

    payload.order_reconciliation = {
        broker_total_orders: brokerOrders.length,
        broker_open_orders: managedOrderContext.live_orders.length,
        broker_matched_orders: managedOrderContext.matched_live_order_count,
        broker_only_open_orders: managedOrderContext.broker_only_live_order_count,
        broker_matched_groups: managedOrderContext.matched_order_groups.length,
        broker_only_groups: managedOrderContext.broker_only_order_groups.length,
        pb_active_order_groups: managedOrderContext.active_groups.length,
        pb_active_orders: managedOrderContext.active_order_count,
        pb_only_active_order_groups: managedOrderContext.pb_only_active_groups.length,
        pb_shadow_groups: managedOrderContext.pb_only_active_groups.length,
        broker_only_orders: managedOrderContext.broker_only_orders,
        coverage_state: toText(coverage.coverage_state) || "complete",
        bulk_open_count: toNumber(coverage.bulk_open_count, 0),
        recovered_open_orders: recoveredOpenOrders,
        unresolved_seed_count: toNumber(coverage.unresolved_seed_count, 0),
        unresolved_order_ids: cloneStringList(coverage.unresolved_order_ids),
        cancelable_orders: managedOrderContext.cancelable_order_count,
        editable_orders: managedOrderContext.editable_order_count,
        outside_rth_orders: managedOrderContext.outside_rth_order_count,
        missing_client_order_id_orders: managedOrderContext.missing_client_order_id_count,
        status_mismatch_orders: managedOrderContext.status_mismatch_count,
        quantity_mismatch_orders: managedOrderContext.quantity_mismatch_count,
        filled_qty_mismatch_orders: managedOrderContext.filled_qty_mismatch_count,
    }

    return payload
}

module.exports = {
    enrichAccountSnapshot,
}
