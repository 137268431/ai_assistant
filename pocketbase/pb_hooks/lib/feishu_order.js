/**
 * feishu_order.js
 * 飞书交易组订单卡片构建、通知与回调处理
 */

var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var orderEvents = require(`${__hooks}/lib/order_events.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)

var PB_HOST = "https://pb.lzw-glory.top"

function formatDateToken(dateToken) {
    if (!dateToken || !/^\d{8}$/.test(String(dateToken))) return ""
    var text = String(dateToken)
    return text.slice(0, 4) + "-" + text.slice(4, 6) + "-" + text.slice(6, 8)
}

function extractDateFromIdentifier(value) {
    var match = String(value || "").match(/(?:^|_)(20\d{6})(?:_|$)/)
    return match ? formatDateToken(match[1]) : ""
}

function extractDateFromBarTimeMs(barTimeMs) {
    var numeric = Number(barTimeMs)
    if (!numeric) return ""
    try {
        return new Date(numeric).toISOString().slice(0, 10)
    } catch (e) {
        return ""
    }
}

function resolvePageDate() {
    for (var i = 0; i < arguments.length; i++) {
        var value = arguments[i]
        var fromMs = extractDateFromBarTimeMs(value)
        if (fromMs) return fromMs
        var fromId = extractDateFromIdentifier(value)
        if (fromId) return fromId
    }
    return ""
}

function buildOpenLinkButton(label, url) {
    return {
        tag: "button",
        text: { tag: "plain_text", content: label },
        type: "default",
        width: "fill",
        multi_url: {
            url: url,
            pc_url: url,
            ios_url: url,
            android_url: url
        }
    }
}

function getJsonField(recordOrData, fieldName) {
    if (!recordOrData) return {}

    if (typeof recordOrData.getString === "function") {
        var raw = recordOrData.getString(fieldName) || ""
        if (raw) {
            try {
                var parsed = JSON.parse(raw)
                return parsed && typeof parsed === "object" ? parsed : {}
            } catch (e) {
                console.error("[FeishuOrder] JSON 解析失败:", fieldName, e)
            }
        }
    }

    var value = typeof recordOrData.get === "function" ? recordOrData.get(fieldName) : recordOrData[fieldName]
    if (value && typeof value === "object") return value
    if (typeof value === "string" && value) {
        try {
            var parsedValue = JSON.parse(value)
            return parsedValue && typeof parsedValue === "object" ? parsedValue : {}
        } catch (e) {
            console.error("[FeishuOrder] JSON 字符串解析失败:", fieldName, e)
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

function toNumber(value) {
    var num = Number(value)
    return isNaN(num) ? 0 : num
}

function formatMoney(value) {
    var amount = toNumber(value)
    return amount ? "$" + amount.toFixed(2) : "N/A"
}

function formatSignedMoney(value, sign) {
    var amount = toNumber(value)
    if (!amount) return "N/A"
    return (sign || "") + "$" + amount.toFixed(2)
}

function formatQty(value) {
    var qty = toNumber(value)
    if (!qty) return "0"
    return String(qty % 1 === 0 ? qty.toFixed(0) : qty)
}

function formatTimePair(usTime, cnTime, fallbackText) {
    if (!usTime && !cnTime) return fallbackText || "N/A"
    if (usTime && cnTime) return usTime + " ET / " + cnTime + " CN"
    if (usTime) return usTime + " ET"
    return cnTime + " CN"
}

function buildOrderDisplayData(orderOrRecord) {
    var get = typeof orderOrRecord.get === "function"
        ? orderOrRecord.get.bind(orderOrRecord)
        : function(k) { return orderOrRecord[k] }
    var extra = getJsonField(orderOrRecord, "extra")
    var status = get("status") || "Init"
    var direction = get("direction") || extra.direction || "long"
    var uniqueId = get("unique_id") || get("order_id") || extra.unique_id || ""
    var usTime = get("us_time") || extra.us_time || ""
    var cnTime = get("cn_time") || extra.cn_time || ""
    var previousStatus = extra.previous_status || ""
    var currentStatus = extra.current_status || status
    var statusTransitionText = extra.status_transition_text || orderEvents.getOrderStatusTransitionText(previousStatus, status)
    var directionText = direction === "short" ? "做空 📉" : "做多 📈"
    var color = direction === "short" ? "red" : "green"

    return {
        symbol: get("symbol") || extra.symbol || "",
        environment: get("environment") || extra.environment || envUtils.LIVE_ENVIRONMENT,
        direction: direction,
        directionText: directionText,
        color: color,
        status: status,
        order_type: get("order_type") || extra.order_type || "Entry",
        role: get("role") || extra.role || "",
        relation_status: get("relation_status") || extra.relation_status || "",
        trade_group_id: get("trade_group_id") || extra.trade_group_id || uniqueId,
        entry_order_unique_id: get("entry_order_unique_id") || extra.entry_order_unique_id || uniqueId,
        parent_order_unique_id: get("parent_order_unique_id") || extra.parent_order_unique_id || "",
        sibling_order_unique_id: get("sibling_order_unique_id") || extra.sibling_order_unique_id || "",
        quantity: toNumber(firstNonEmpty(get("quantity"), extra.quantity, 0)),
        filled_qty: toNumber(firstNonEmpty(get("filled_qty"), extra.filled_qty, 0)),
        limit_price: toNumber(firstNonEmpty(get("limit_price"), extra.limit_price, 0)),
        fill_price: toNumber(firstNonEmpty(get("fill_price"), extra.fill_price, 0)),
        tp_price: toNumber(firstNonEmpty(get("tp_price"), extra.tp_price, 0)),
        sl_price: toNumber(firstNonEmpty(get("sl_price"), extra.sl_price, 0)),
        signal_id: get("signal_id") || extra.signal_id || "",
        order_id: get("order_id") || "",
        broker_order_id: get("broker_order_id") || extra.broker_order_id || get("order_id") || "",
        unique_id: uniqueId,
        order_time: get("order_time") || extra.order_time || usTime || "",
        fill_time: get("fill_time") || extra.fill_time || "",
        us_time: usTime,
        cn_time: cnTime,
        created_us_time: extra.created_us_time || get("order_time") || extra.order_time || usTime || "",
        created_cn_time: extra.created_cn_time || cnTime || "",
        created_bar_time_ms: toNumber(extra.created_bar_time_ms || 0),
        updated_us_time: extra.status_updated_us_time || usTime || extra.created_us_time || "",
        updated_cn_time: extra.status_updated_cn_time || cnTime || extra.created_cn_time || "",
        updated_bar_time_ms: toNumber(extra.status_updated_bar_time_ms || get("bar_time_ms") || extra.bar_time_ms || 0),
        filled_us_time: extra.filled_us_time || get("fill_time") || extra.fill_time || "",
        filled_cn_time: extra.filled_cn_time || "",
        filled_bar_time_ms: toNumber(extra.filled_bar_time_ms || 0),
        previous_status: previousStatus,
        current_status: currentStatus,
        status_transition_text: statusTransitionText,
        last_status_reason: extra.last_status_reason || "",
        last_status_source: extra.last_status_source || "",
        extra: extra
    }
}

function getOrderStatusInfo(status) {
    var map = {
        Init: { emoji: "🆕", text: "初始化", message: "订单已初始化，等待提交", action: "cancel" },
        Submitted: { emoji: "⏳", text: "待成交", message: "订单已提交，等待成交", action: "cancel" },
        Filled: { emoji: "✅", text: "已成交", message: "主单已成交，可对整组仓位平仓", action: "close" },
        Canceled: { emoji: "❌", text: "已取消", message: "订单已取消", action: "" },
        Closed: { emoji: "🔒", text: "已平仓", message: "订单已平仓", action: "" }
    }
    return map[status] || { emoji: "❓", text: status || "未知", message: "订单状态未知", action: "" }
}

function roleRank(role) {
    return {
        entry: 0,
        take_profit: 1,
        repair_tp: 2,
        stop_loss: 3,
        repair_sl: 4
    }[role] || 9
}

function getRoleText(role, orderType) {
    return {
        entry: "主单",
        take_profit: "止盈单",
        repair_tp: "修复止盈单",
        stop_loss: "止损单",
        repair_sl: "修复止损单"
    }[role] || orderType || "-"
}

function getTradeGroupId(orderOrRecord) {
    if (!orderOrRecord) return ""
    var get = typeof orderOrRecord.get === "function"
        ? orderOrRecord.get.bind(orderOrRecord)
        : function(k) { return orderOrRecord[k] }
    var extra = getJsonField(orderOrRecord, "extra")
    return firstNonEmpty(
        get("trade_group_id"),
        extra.trade_group_id,
        get("entry_order_unique_id"),
        extra.entry_order_unique_id,
        get("unique_id"),
        get("order_id"),
        extra.unique_id
    )
}

function fetchTradeGroupRecords(orderOrRecord) {
    var tradeGroupId = getTradeGroupId(orderOrRecord)
    var environment = envUtils.getRecordEnvironment(orderOrRecord, envUtils.LIVE_ENVIRONMENT)
    if (!tradeGroupId) return []
    try {
        return $app.findRecordsByFilter(
            "orders",
            "trade_group_id = {:gid} && environment = {:env}",
            "-created",
            100,
            0,
            { gid: tradeGroupId, env: environment }
        ) || []
    } catch (err) {
        console.error("[FeishuOrder] 查询交易组失败:", tradeGroupId, err)
        return []
    }
}

function sortGroupOrders(left, right) {
    var roleDiff = roleRank(left.role) - roleRank(right.role)
    if (roleDiff !== 0) return roleDiff
    var timeDiff = toNumber(right.updated_bar_time_ms || right.created_bar_time_ms || 0) - toNumber(left.updated_bar_time_ms || left.created_bar_time_ms || 0)
    if (timeDiff !== 0) return timeDiff
    return String(left.unique_id || "").localeCompare(String(right.unique_id || ""))
}

function pickPrimaryOrder(orders) {
    for (var i = 0; i < orders.length; i++) {
        if (orders[i].role === "entry") return orders[i]
    }
    return orders[0] || null
}

function resolveTradeGroup(orderOrRecord) {
    var records = fetchTradeGroupRecords(orderOrRecord)
    if (records.length === 0 && orderOrRecord) {
        records = [orderOrRecord]
    }

    var orders = records.map(buildOrderDisplayData).sort(sortGroupOrders)
    var primary = pickPrimaryOrder(orders)
    var latest = orders.slice().sort(function(left, right) {
        return toNumber(right.updated_bar_time_ms || right.created_bar_time_ms || 0) - toNumber(left.updated_bar_time_ms || left.created_bar_time_ms || 0)
    })[0] || primary
    var exitOrder = null
    for (var i = 0; i < orders.length; i++) {
        var order = orders[i]
        if (order.role !== "entry" && order.status === "Filled") {
            exitOrder = order
            break
        }
    }
    var activeChildren = orders.filter(function(order) {
        return order.role !== "entry" && order.relation_status === "active"
    })
    var plannedChildren = orders.filter(function(order) {
        return order.role !== "entry" && order.relation_status === "planned"
    })
    var orphanedChildren = orders.filter(function(order) {
        return order.role !== "entry" && order.relation_status === "orphaned"
    })

    return {
        trade_group_id: getTradeGroupId(primary || orderOrRecord),
        orders: orders,
        primary: primary,
        latest: latest,
        exitOrder: exitOrder,
        activeChildren: activeChildren,
        plannedChildren: plannedChildren,
        orphanedChildren: orphanedChildren
    }
}

function getProtectionOrder(group, roles) {
    var list = Array.isArray(roles) ? roles : [roles]
    for (var i = 0; i < group.orders.length; i++) {
        if (list.indexOf(group.orders[i].role) !== -1) return group.orders[i]
    }
    return null
}

function getEntryReferencePrice(order) {
    return toNumber(firstNonEmpty(order.fill_price, order.limit_price, 0))
}

function getEntryReferenceQty(order) {
    var filledQty = toNumber(order.filled_qty || 0)
    return filledQty > 0 ? filledQty : toNumber(order.quantity || 0)
}

function calculateTargetAmount(entryPrice, targetPrice, qty) {
    var basePrice = toNumber(entryPrice)
    var target = toNumber(targetPrice)
    var quantity = toNumber(qty)
    if (!basePrice || !target || !quantity) return 0
    return Math.abs(target - basePrice) * quantity
}

function buildRiskRewardSummary(group) {
    var primary = group.primary || {}
    var tpOrder = getProtectionOrder(group, ["take_profit", "repair_tp"]) || {}
    var slOrder = getProtectionOrder(group, ["stop_loss", "repair_sl"]) || {}
    var entryPrice = getEntryReferencePrice(primary)
    var qty = getEntryReferenceQty(primary)
    var tpPrice = toNumber(firstNonEmpty(tpOrder.limit_price, primary.tp_price, 0))
    var slPrice = toNumber(firstNonEmpty(slOrder.limit_price, primary.sl_price, 0))
    var tpAmount = calculateTargetAmount(entryPrice, tpPrice, qty)
    var slAmount = calculateTargetAmount(entryPrice, slPrice, qty)

    return {
        entryPrice: entryPrice,
        qty: qty,
        tpPrice: tpPrice,
        slPrice: slPrice,
        tpAmount: tpAmount,
        slAmount: slAmount
    }
}

function getTradeGroupPageDate(group) {
    var primary = group.primary || {}
    var latest = group.latest || primary
    return resolvePageDate(
        primary.created_bar_time_ms,
        primary.updated_bar_time_ms,
        latest.updated_bar_time_ms,
        primary.signal_id,
        group.trade_group_id,
        primary.us_time,
        latest.us_time
    )
}

function buildSignalPageUrl(signalId, pageDate, environment) {
    if (!signalId) return ""
    var url = PB_HOST + "/ibkr_signals.html?signal_id=" + encodeURIComponent(signalId)
    if (environment) {
        url += "&environment=" + encodeURIComponent(environment)
    }
    if (pageDate) {
        url += "&date=" + encodeURIComponent(pageDate)
    }
    return url
}

function buildOrderPageUrl(group, pageDate) {
    if (!group) return ""
    var signalId = group.primary && group.primary.signal_id ? group.primary.signal_id : ""
    if (group.trade_group_id) {
        var detailUrl = PB_HOST + "/ibkr_order_details.html?trade_group_id=" + encodeURIComponent(group.trade_group_id)
        if (signalId) {
            detailUrl += "&signal_id=" + encodeURIComponent(signalId)
        }
        if (group.primary && group.primary.environment) {
            detailUrl += "&environment=" + encodeURIComponent(group.primary.environment)
        }
        if (pageDate) {
            detailUrl += "&date=" + encodeURIComponent(pageDate)
        }
        return detailUrl
    }
    if (signalId) {
        var listUrl = PB_HOST + "/ibkr_orders.html?signal_id=" + encodeURIComponent(signalId)
        if (group.primary && group.primary.environment) {
            listUrl += "&environment=" + encodeURIComponent(group.primary.environment)
        }
        if (pageDate) {
            listUrl += "&date=" + encodeURIComponent(pageDate)
        }
        return listUrl
    }
    return ""
}

function buildOrderViewElements(group) {
    var primary = group.primary || {}
    var pageDate = getTradeGroupPageDate(group)
    var signalUrl = buildSignalPageUrl(primary.signal_id || "", pageDate, primary.environment || "")
    var orderUrl = buildOrderPageUrl(group, pageDate)
    var columns = []

    if (signalUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("📡 查看信号", signalUrl)]
        })
    }
    if (orderUrl) {
        columns.push({
            tag: "column",
            width: "weighted",
            weight: 1,
            elements: [buildOpenLinkButton("📋 查看订单", orderUrl)]
        })
    }

    if (columns.length === 0) return []
    return [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: columns
    }]
}

function getTradeGroupStatusInfo(group) {
    var primary = group.primary || {}
    var primaryFilledQty = toNumber(primary.filled_qty || 0)
    if (primary.status === "Canceled") {
        return { emoji: "❌", text: "交易组已取消", message: "主单已取消，保护单已收尾", action: "" }
    }
    if (primary.status === "Closed") {
        return { emoji: "🔒", text: "交易组已平仓", message: "交易组已平仓", action: "" }
    }
    if (group.exitOrder) {
        var exitText = group.exitOrder.role === "take_profit" || group.exitOrder.role === "repair_tp" ? "止盈成交" : "止损成交"
        return { emoji: group.exitOrder.role.indexOf("tp") !== -1 ? "🎯" : "🛑", text: exitText, message: "保护单已成交，交易组已退出", action: "" }
    }
    if (primary.status === "Filled") {
        return { emoji: "📦", text: "持仓中", message: "主单已成交，保护单已激活", action: "close" }
    }
    if (primary.status === "Submitted" && (primaryFilledQty > 0 || group.activeChildren.length > 0)) {
        return { emoji: "🧩", text: "部分成交", message: "主单已部分成交，保护单已激活", action: "" }
    }
    if (primary.status === "Submitted") {
        return { emoji: "⏳", text: "主单待成交", message: "主单已提交，保护单已预创建", action: "cancel" }
    }
    return { emoji: "🆕", text: "交易组初始化", message: "主单与保护单已初始化", action: "cancel" }
}

function getTradeGroupCardMessageId(orderOrRecord) {
    var records = fetchTradeGroupRecords(orderOrRecord)
    if (!records || records.length === 0) return ""

    records.sort(function(left, right) {
        return roleRank(left.get("role") || "") - roleRank(right.get("role") || "")
    })

    for (var i = 0; i < records.length; i++) {
        var extra = getJsonField(records[i], "extra")
        if (extra.feishu_order_message_id) return extra.feishu_order_message_id
    }
    return ""
}

function persistTradeGroupCardMessageId(orderOrRecord, messageId) {
    if (!messageId) return
    var records = fetchTradeGroupRecords(orderOrRecord)
    if (!records || records.length === 0) return

    records.forEach(function(record) {
        orderEvents.mergeOrderExtra(record, {
            feishu_order_message_id: messageId,
            feishu_order_card_version: 2
        }, true)
    })
}

function buildGroupSummaryElements(group) {
    var primary = group.primary || {}
    var latest = group.latest || primary
    var info = getTradeGroupStatusInfo(group)
    var rr = buildRiskRewardSummary(group)
    var protectionText = group.activeChildren.length > 0
        ? "active=" + group.activeChildren.length
        : group.plannedChildren.length > 0
            ? "planned=" + group.plannedChildren.length
            : "none"

    return [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                vertical_spacing: "2px",
                elements: [
                    { tag: "div", text: { tag: "lark_md", content: "**标的:** " + (primary.symbol || latest.symbol || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**方向:** " + (primary.directionText || latest.directionText || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**止盈 / 预估盈利:** " + formatMoney(rr.tpPrice) + " / " + formatSignedMoney(rr.tpAmount, "+") } },
                    { tag: "div", text: { tag: "lark_md", content: "**止损 / 预估亏损:** " + formatMoney(rr.slPrice) + " / " + formatSignedMoney(rr.slAmount, "-") } },
                    { tag: "div", text: { tag: "lark_md", content: "**组状态:** " + info.text } },
                    { tag: "div", text: { tag: "lark_md", content: "**保护单:** " + protectionText } },
                    { tag: "div", text: { tag: "lark_md", content: "**SignalID:** " + (primary.signal_id || latest.signal_id || "N/A") } }
                ]
            },
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                vertical_spacing: "2px",
                elements: [
                    { tag: "div", text: { tag: "lark_md", content: "**交易组:** " + (group.trade_group_id || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**主单ID:** " + (primary.entry_order_unique_id || primary.unique_id || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**主单状态流转:** " + (primary.status_transition_text || "N/A") } },
                    { tag: "div", text: { tag: "lark_md", content: "**最近更新时间:** " + formatTimePair(latest.updated_us_time, latest.updated_cn_time, "N/A") } },
                    group.orphanedChildren.length > 0
                        ? { tag: "div", text: { tag: "lark_md", content: "**告警:** 存在 orphaned 子单" } }
                        : null
                ].filter(Boolean)
            }
        ]
    }]
}

function buildOrderSection(order) {
    var info = getOrderStatusInfo(order.status)
    var leftColumn = [
        { tag: "div", text: { tag: "lark_md", content: "**类型:** " + (order.order_type || "N/A") } },
        { tag: "div", text: { tag: "lark_md", content: "**角色:** " + getRoleText(order.role, order.order_type) } },
        { tag: "div", text: { tag: "lark_md", content: "**状态:** " + info.text } },
        { tag: "div", text: { tag: "lark_md", content: "**关系状态:** " + (order.relation_status || "N/A") } },
        { tag: "div", text: { tag: "lark_md", content: "**数量 / 已成交:** " + formatQty(order.quantity) + " / " + formatQty(order.filled_qty) } },
        { tag: "div", text: { tag: "lark_md", content: "**限价 / 成交价:** " + formatMoney(order.limit_price) + " / " + formatMoney(order.fill_price) } }
    ]

    var rightColumn = [
        { tag: "div", text: { tag: "lark_md", content: "**订单ID:** " + (order.unique_id || "N/A") } },
        { tag: "div", text: { tag: "lark_md", content: "**原始OrderID:** " + (order.broker_order_id || "N/A") } },
        { tag: "div", text: { tag: "lark_md", content: "**父单:** " + (order.parent_order_unique_id || "-") } },
        { tag: "div", text: { tag: "lark_md", content: "**兄弟单:** " + (order.sibling_order_unique_id || "-") } },
        { tag: "div", text: { tag: "lark_md", content: "**创建时间:** " + formatTimePair(order.created_us_time, order.created_cn_time, "N/A") } },
        { tag: "div", text: { tag: "lark_md", content: "**成交时间:** " + formatTimePair(order.filled_us_time, order.filled_cn_time, "未成交") } },
        { tag: "div", text: { tag: "lark_md", content: "**更新时间:** " + formatTimePair(order.updated_us_time, order.updated_cn_time, "N/A") } }
    ]

    return [
        { tag: "div", text: { tag: "lark_md", content: "### " + getRoleText(order.role, order.order_type) } },
        {
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [
                { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: leftColumn },
                { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: rightColumn }
            ]
        }
    ]
}

function buildOrderActionElements(group) {
    var primary = group.primary || {}
    var info = getTradeGroupStatusInfo(group)
    var actionTargetId = primary.entry_order_unique_id || primary.unique_id
    var primaryFilledQty = toNumber(primary.filled_qty || 0)
    var hasActivatedChild = group.activeChildren.length > 0 || !!group.exitOrder

    if (!actionTargetId) return []
    if (info.action === "cancel" && primaryFilledQty === 0 && !hasActivatedChild) {
        return [{
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [{
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "❌ 取消主单" },
                    type: "danger",
                    width: "fill",
                    action_type: "request",
                    url: PB_HOST + "/webhook/feishu/callback",
                    value: { action: "cancel", order_id: actionTargetId, environment: primary.environment || envUtils.LIVE_ENVIRONMENT }
                }]
            }]
        }]
    }

    if (info.action === "close" && !group.exitOrder) {
        return [{
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [{
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "🔒 平仓整组" },
                    type: "primary",
                    width: "fill",
                    action_type: "request",
                    url: PB_HOST + "/webhook/feishu/callback",
                    value: { action: "close", order_id: actionTargetId, environment: primary.environment || envUtils.LIVE_ENVIRONMENT }
                }]
            }]
        }]
    }

    return []
}

function buildOrderCard(orderOrRecord, options) {
    var opts = options || {}
    var group = resolveTradeGroup(orderOrRecord)
    var primary = group.primary || buildOrderDisplayData(orderOrRecord || {})
    var latest = group.latest || primary
    var info = getTradeGroupStatusInfo(group)
    var titleTime = latest.updated_us_time || latest.us_time || latest.order_time || ""
    var title = envUtils.labelTitleWithEnvironment(
        info.emoji + " " + info.text + " · " + (primary.symbol || latest.symbol || "-") + (titleTime ? " · " + titleTime : ""),
        primary.environment || latest.environment || envUtils.LIVE_ENVIRONMENT
    )
    var statusMessage = opts.message || info.message
    var elements = []

    buildGroupSummaryElements(group).forEach(function(element) { elements.push(element) })
    elements.push({ tag: "hr" })

    group.orders.forEach(function(order, index) {
        if (index > 0) {
            elements.push({ tag: "hr" })
        }
        buildOrderSection(order).forEach(function(element) { elements.push(element) })
    })

    elements.push({ tag: "hr" })
    elements.push({
        tag: "column_set",
        columns: [{
            tag: "column",
            width: "weighted",
            weight: 1,
            vertical_spacing: "2px",
            elements: [
                { tag: "div", text: { tag: "lark_md", content: "**状态说明:** " + statusMessage } },
                latest.last_status_reason ? { tag: "div", text: { tag: "lark_md", content: "**变更原因:** " + latest.last_status_reason } } : null,
                latest.last_status_source ? { tag: "div", text: { tag: "lark_md", content: "**变更来源:** " + latest.last_status_source } } : null
            ].filter(Boolean)
        }]
    })

    var viewElements = buildOrderViewElements(group)
    if (viewElements.length > 0) {
        elements.push({ tag: "hr" })
        viewElements.forEach(function(element) { elements.push(element) })
    }

    var actionElements = buildOrderActionElements(group)
    if (actionElements.length > 0) {
        elements.push({ tag: "hr" })
        actionElements.forEach(function(element) { elements.push(element) })
    }

    return {
        schema: "2.0",
        config: { update_multi: true },
        header: {
            title: { tag: "plain_text", content: title },
            template: primary.color || "green"
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function notifyOrder(action, order, options) {
    var opts = options || {}
    var status = typeof order.get === "function" ? order.get("status") : order.status
    var messageId = opts.messageId || getTradeGroupCardMessageId(order)
    var card = buildOrderCard(order, {
        message: opts.message || getOrderStatusInfo(status || "").message
    })
    var orderData = buildOrderDisplayData(order)
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(orderData.environment || "", envUtils.LIVE_ENVIRONMENT)
    var result = messageId ? feishuApp.updateMessageCard(messageId, card, runtimeEnvironment) : feishuApp.sendCardToChatByTypeDetailed(card, "order", runtimeEnvironment)
    var effectiveMessageId = result.message_id || messageId || ""
    if (result.success && effectiveMessageId) {
        persistTradeGroupCardMessageId(order, effectiveMessageId)
    }
    console.log("[FeishuOrder] 订单卡片同步:", result.success ? "成功" : "失败", "action:", action, "message_id:", effectiveMessageId || "-")
    return result
}

function notifyNewOrder(order, options) {
    var opts = options || {}
    var existingMessageId = opts.messageId || getTradeGroupCardMessageId(order)
    if (existingMessageId) {
        return notifyOrder("create", order, {
            messageId: existingMessageId,
            message: opts.message
        })
    }

    var card = buildOrderCard(order, { message: opts.message })
    var orderData = buildOrderDisplayData(order)
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(orderData.environment || "", envUtils.LIVE_ENVIRONMENT)
    var result = feishuApp.sendCardToChatByTypeDetailed(card, "order", runtimeEnvironment)
    if (result.success && result.message_id) {
        persistTradeGroupCardMessageId(order, result.message_id)
    }
    console.log("[FeishuOrder] 新订单卡片发送:", result.success ? "成功" : "失败", "message_id:", result.message_id || "-")
    return result
}

function buildOrderCardV2(symbol, directionText, statusEmoji, statusText, status, color, message) {
    return buildOrderCard({
        symbol: symbol,
        direction: directionText.indexOf("做空") !== -1 ? "short" : "long",
        status: status,
        unique_id: symbol
    }, { message: message || statusText || statusEmoji })
}

function handleOrderCardCallback(c, options) {
    var opts = options || {}
    var action = opts.action || ""
    var orderId = opts.orderId || ""
    var environment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var updateToken = opts.updateToken || null

    var records = $app.findRecordsByFilter("orders", "unique_id = {:id} && environment = {:env}", "", 1, 0, { id: orderId, env: environment })
    if (!records || records.length === 0) {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "订单不存在" },
            card: { type: "raw", data: null }
        }, updateToken)
    }

    var record = records[0]
    var currentStatus = record.get("status")
    var currentRole = record.get("role") || ""
    var tradeGroupId = record.get("trade_group_id") || record.get("entry_order_unique_id") || record.get("unique_id")
    var relatedRecords = fetchTradeGroupRecords(record)
    var primaryRecord = relatedRecords.filter(function(item) {
        return (item.get("role") || "") === "entry"
    })[0] || record
    var primaryFilledQty = toNumber(primaryRecord.get("filled_qty") || 0)
    var hasActiveChild = relatedRecords.some(function(item) {
        return (item.get("role") || "") !== "entry" && (item.get("relation_status") || "") === "active"
    })

    if (currentStatus === "Canceled" || currentStatus === "Closed") {
        var lockedMsg = currentStatus === "Canceled" ? "订单已取消，无法操作" : "订单已平仓，无法操作"
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "warning", content: lockedMsg },
            card: { type: "raw", data: buildOrderCard(primaryRecord, { message: lockedMsg }) }
        }, updateToken)
    }

    if (action === "cancel") {
        if (currentRole && currentRole !== "entry") {
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "warning", content: "只能取消主入场挂单" },
                card: { type: "raw", data: buildOrderCard(primaryRecord, { message: "只能取消主入场挂单" }) }
            }, updateToken)
        }
        if (primaryFilledQty > 0 || hasActiveChild) {
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "warning", content: "主单已部分成交或保护单已激活，请改用平仓" },
                card: { type: "raw", data: buildOrderCard(primaryRecord, { message: "主单已部分成交或保护单已激活，请改用平仓" }) }
            }, updateToken)
        }
        if (currentStatus !== "Init" && currentStatus !== "Submitted") {
            var cancelBlocked = currentStatus === "Filled" ? "订单已成交，无法取消" : "无法取消"
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "error", content: cancelBlocked },
                card: { type: "raw", data: buildOrderCard(primaryRecord, { message: cancelBlocked }) }
            }, updateToken)
        }

        relatedRecords.forEach(function(groupRecord) {
            var status = groupRecord.get("status")
            if (status === "Canceled" || status === "Closed" || status === "Filled") {
                return
            }
            groupRecord.set("status", "Canceled")
            orderEvents.applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: "Canceled"
            }, false)
            var cancelMeta = orderEvents.applyOrderStatusMeta(groupRecord, {
                status: "Canceled",
                previous_status: status,
                source: "feishu_order_callback",
                reason: "飞书卡片取消主单"
            }, false)
            $app.save(groupRecord)
            orderEvents.appendOrderDetail(groupRecord, {
                environment: environment,
                status: "Canceled",
                source: "feishu_order_callback",
                reason: "飞书卡片取消主单",
                us_time: cancelMeta.eventTimes.us_time,
                cn_time: cancelMeta.eventTimes.cn_time,
                bar_time_ms: cancelMeta.eventTimes.bar_time_ms,
                extra: {
                    previous_status: status,
                    action: "cancel",
                    trade_group_id: tradeGroupId
                }
            })
        })

        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "取消指令已发送" },
            card: { type: "raw", data: buildOrderCard(primaryRecord, { message: "主单已取消，保护单已收尾" }) }
        }, updateToken)
    }

    if (action === "close") {
        if ((primaryRecord.get("status") || "") !== "Filled") {
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "error", content: "只有成交的主单才能平仓" },
                card: { type: "raw", data: buildOrderCard(primaryRecord, { message: "只有成交的主单才能平仓" }) }
            }, updateToken)
        }

        relatedRecords.forEach(function(groupRecord) {
            var groupStatus = groupRecord.get("status")
            if (groupStatus === "Canceled" || groupStatus === "Closed") return
            var nextStatus = groupRecord.get("unique_id") === (primaryRecord.get("entry_order_unique_id") || primaryRecord.get("unique_id"))
                ? "Closed"
                : "Canceled"
            groupRecord.set("status", nextStatus)
            orderEvents.applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: nextStatus
            }, false)
            var closeMeta = orderEvents.applyOrderStatusMeta(groupRecord, {
                status: nextStatus,
                previous_status: groupStatus,
                source: "feishu_order_callback",
                reason: "飞书卡片平仓交易组"
            }, false)
            $app.save(groupRecord)
            orderEvents.appendOrderDetail(groupRecord, {
                environment: environment,
                status: nextStatus,
                source: "feishu_order_callback",
                reason: "飞书卡片平仓交易组",
                us_time: closeMeta.eventTimes.us_time,
                cn_time: closeMeta.eventTimes.cn_time,
                bar_time_ms: closeMeta.eventTimes.bar_time_ms,
                extra: {
                    previous_status: groupStatus,
                    action: "close_group",
                    trade_group_id: tradeGroupId
                }
            })
        })

        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "交易组平仓指令已发送" },
            card: { type: "raw", data: buildOrderCard(primaryRecord, { message: "交易组已平仓" }) }
        }, updateToken)
    }

    return feishuApp.sendFeishuCallbackResponse(c, {
        toast: { type: "error", content: "未知操作: " + action }
    }, updateToken)
}

module.exports = {
    buildOrderDisplayData: buildOrderDisplayData,
    getOrderStatusInfo: getOrderStatusInfo,
    getTradeGroupCardMessageId: getTradeGroupCardMessageId,
    persistTradeGroupCardMessageId: persistTradeGroupCardMessageId,
    buildOrderCard: buildOrderCard,
    buildOrderCardV2: buildOrderCardV2,
    notifyOrder: notifyOrder,
    notifyNewOrder: notifyNewOrder,
    handleOrderCardCallback: handleOrderCardCallback
}
