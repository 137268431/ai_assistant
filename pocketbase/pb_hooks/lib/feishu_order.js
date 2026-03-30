/**
 * feishu_order.js
 * 飞书订单卡片构建、通知与回调处理
 */

var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var orderEvents = require(`${__hooks}/lib/order_events.js`)

var PB_HOST = "https://pb.lzw-glory.top"

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

    var value = (typeof recordOrData.get === "function") ? recordOrData.get(fieldName) : recordOrData[fieldName]
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

function buildOrderDisplayData(orderOrRecord) {
    var get = (typeof orderOrRecord.get === "function") ? orderOrRecord.get.bind(orderOrRecord) : function(k) { return orderOrRecord[k] }
    var extra = getJsonField(orderOrRecord, "extra")
    var status = get("status") || "Init"
    var direction = get("direction") || "long"
    var unique_id = get("unique_id") || get("order_id") || ""
    var us_time = get("us_time") || ""
    var symbol = get("symbol") || ""
    var directionText = direction === "long" ? "做多 📈" : "做空 📉"
    var color = direction === "long" ? "green" : "red"
    var previousStatus = extra.previous_status || ""
    var currentStatus = extra.current_status || status
    var statusTransitionText = extra.status_transition_text || orderEvents.getOrderStatusTransitionText(previousStatus, status)

    return {
        symbol: symbol,
        direction: direction,
        position_side: get("position_side") || extra.position_side || direction,
        directionText: directionText,
        color: color,
        status: status,
        order_type: get("order_type") || extra.order_type || "Entry",
        role: get("role") || extra.role || "",
        relation_status: get("relation_status") || extra.relation_status || "",
        trade_group_id: get("trade_group_id") || extra.trade_group_id || unique_id,
        entry_order_unique_id: get("entry_order_unique_id") || extra.entry_order_unique_id || unique_id,
        parent_order_unique_id: get("parent_order_unique_id") || extra.parent_order_unique_id || "",
        sibling_order_unique_id: get("sibling_order_unique_id") || extra.sibling_order_unique_id || "",
        quantity: Number(get("quantity") != null ? get("quantity") : extra.quantity || 0),
        limit_price: Number(get("limit_price") != null ? get("limit_price") : extra.limit_price || 0),
        fill_price: Number(get("fill_price") != null ? get("fill_price") : extra.fill_price || 0),
        filled_qty: Number(get("filled_qty") != null ? get("filled_qty") : extra.filled_qty || 0),
        tp_price: Number(get("tp_price") != null ? get("tp_price") : extra.tp_price || 0),
        sl_price: Number(get("sl_price") != null ? get("sl_price") : extra.sl_price || 0),
        signal_id: get("signal_id") || "",
        order_id: get("order_id") || "",
        broker_order_id: get("broker_order_id") || extra.broker_order_id || get("order_id") || "",
        unique_id: unique_id,
        order_time: get("order_time") || extra.order_time || "",
        fill_time: get("fill_time") || extra.fill_time || "",
        us_time: us_time,
        cn_time: get("cn_time") || "",
        created_us_time: extra.created_us_time || get("order_time") || extra.order_time || "",
        created_cn_time: extra.created_cn_time || "",
        created_bar_time_ms: Number(extra.created_bar_time_ms || 0),
        updated_us_time: extra.status_updated_us_time || us_time || "",
        updated_cn_time: extra.status_updated_cn_time || get("cn_time") || "",
        updated_bar_time_ms: Number(extra.status_updated_bar_time_ms || get("bar_time_ms") || 0),
        filled_us_time: extra.filled_us_time || get("fill_time") || extra.fill_time || "",
        filled_cn_time: extra.filled_cn_time || "",
        filled_bar_time_ms: Number(extra.filled_bar_time_ms || 0),
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

function buildOrderActionElements(d) {
    var info = getOrderStatusInfo(d.status)
    if (!info.action) return []
    var actionTargetId = d.entry_order_unique_id || d.unique_id

    if (d.role && d.role !== "entry") {
        return []
    }

    if (info.action === "cancel") {
        return [{
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [{
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "❌ 取消挂单" },
                    type: "danger",
                    width: "fill",
                    action_type: "request",
                    url: PB_HOST + "/webhook/feishu/callback",
                    value: { action: "cancel", order_id: actionTargetId }
                }]
            }]
        }]
    }

    if (info.action === "close") {
        return [{
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [{
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "🔒 平仓" },
                    type: "primary",
                    width: "fill",
                    action_type: "request",
                    url: PB_HOST + "/webhook/feishu/callback",
                    value: { action: "close", order_id: actionTargetId }
                }]
            }]
        }]
    }

    return []
}

function buildOrderCard(orderOrRecord, options) {
    var opts = options || {}
    var d = buildOrderDisplayData(orderOrRecord)
    var info = getOrderStatusInfo(d.status)
    var headerTime = d.us_time || d.order_time || ""
    var title = info.emoji + " " + info.text + " · " + d.symbol + (headerTime ? " · " + headerTime : "")
    var statusMessage = opts.message || info.message
    var createTimeText = (d.created_us_time || d.order_time ? (d.created_us_time || d.order_time) + " ET" : "N/A") + (d.created_cn_time ? " / " + d.created_cn_time + " CN" : "")
    var fillTimeText = d.filled_us_time
        ? (d.filled_us_time + " ET" + (d.filled_cn_time ? " / " + d.filled_cn_time + " CN" : ""))
        : (d.fill_time ? d.fill_time + " ET" : "未成交")
    var updateTimeText = d.updated_us_time
        ? (d.updated_us_time + " ET" + (d.updated_cn_time ? " / " + d.updated_cn_time + " CN" : ""))
        : "N/A"

    var leftColumn = []
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + d.symbol } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + d.directionText } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**类型:** " + d.order_type } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**角色:** " + (d.role || "N/A") } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**状态:** " + info.text } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**状态流转:** " + d.status_transition_text } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**关系状态:** " + (d.relation_status || "N/A") } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**限价:** " + (d.limit_price ? "$" + d.limit_price.toFixed(2) : "N/A") } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** " + (d.tp_price ? "$" + d.tp_price.toFixed(2) : "N/A") } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** " + (d.sl_price ? "$" + d.sl_price.toFixed(2) : "N/A") } })

    var rightColumn = []
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**数量:** " + (d.quantity || "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**已成交:** " + (d.filled_qty || 0) } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**成交价:** " + (d.fill_price ? "$" + d.fill_price.toFixed(2) : "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + (d.signal_id || "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**订单ID:** " + d.unique_id } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**原始OrderID:** " + (d.broker_order_id || "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**交易组:** " + (d.trade_group_id || "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**主单ID:** " + (d.entry_order_unique_id || "N/A") } })
    if (d.parent_order_unique_id) {
        rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**父单:** " + d.parent_order_unique_id } })
    }
    if (d.sibling_order_unique_id) {
        rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**兄弟单:** " + d.sibling_order_unique_id } })
    }

    var elements = [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: leftColumn },
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: rightColumn }
        ]
    }]

    elements.push({ tag: "hr" })
    elements.push({
        tag: "column_set",
        columns: [{
            tag: "column",
            width: "weighted",
            weight: 1,
            vertical_spacing: "2px",
            elements: [
                { tag: "div", text: { tag: "lark_md", content: "**创建时间:** " + createTimeText } },
                { tag: "div", text: { tag: "lark_md", content: "**成交时间:** " + fillTimeText } },
                { tag: "div", text: { tag: "lark_md", content: "**更新时间:** " + updateTimeText } },
                { tag: "div", text: { tag: "lark_md", content: "**状态说明:** " + statusMessage } },
                d.last_status_reason ? { tag: "div", text: { tag: "lark_md", content: "**变更原因:** " + d.last_status_reason } } : null
            ].filter(Boolean)
        }]
    })

    var actionElements = buildOrderActionElements(d)
    if (actionElements.length > 0) {
        elements.push({ tag: "hr" })
        actionElements.forEach(function(el) { elements.push(el) })
    }

    return {
        schema: "2.0",
        config: { update_multi: true },
        header: {
            title: { tag: "plain_text", content: title },
            template: d.color
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function notifyOrder(action, order, options) {
    var opts = options || {}
    var status = (typeof order.get === "function") ? order.get("status") : order.status
    var card = buildOrderCard(order, {
        message: opts.message || getOrderStatusInfo(status || "").message
    })
    var messageId = opts.messageId || ""
    var result = messageId ? feishuApp.updateMessageCard(messageId, card) : feishuApp.sendCardToChatByTypeDetailed(card, "order")
    console.log("[FeishuOrder] 订单卡片同步:", result.success ? "成功" : "失败", "action:", action, "message_id:", result.message_id || messageId || "-")
    return result
}

function notifyNewOrder(order, options) {
    var opts = options || {}
    var card = buildOrderCard(order, { message: opts.message })
    var result = feishuApp.sendCardToChatByTypeDetailed(card, "order")
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
    var updateToken = opts.updateToken || null

    var records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: orderId })
    if (!records || records.length === 0) {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "订单不存在" },
            card: { type: "raw", data: null }
        }, updateToken)
    }

    var record = records[0]
    var currentStatus = record.get("status")
    var currentRole = record.get("role") || ""
    var orderMsg
    var orderCard

    console.log("[FeishuOrderCallback] 处理订单回调:", "order_id=", orderId, "action=", action, "current_status=", currentStatus)

    if (currentStatus === "Canceled" || currentStatus === "Closed") {
        orderMsg = currentStatus === "Canceled" ? "订单已取消，无法操作" : "订单已平仓，无法操作"
        orderCard = buildOrderCard(record, { message: orderMsg })
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "warning", content: orderMsg },
            card: { type: "raw", data: orderCard }
        }, updateToken)
    }

    if (action === "cancel") {
        if (currentRole && currentRole !== "entry") {
            orderMsg = "只能取消主入场挂单"
            orderCard = buildOrderCard(record, { message: orderMsg })
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "warning", content: orderMsg },
                card: { type: "raw", data: orderCard }
            }, updateToken)
        }
        if (currentStatus !== "Init" && currentStatus !== "Submitted") {
            orderMsg = currentStatus === "Filled" ? "订单已成交，无法取消" : "无法取消"
            orderCard = buildOrderCard(record, { message: orderMsg })
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "error", content: orderMsg },
                card: { type: "raw", data: orderCard }
            }, updateToken)
        }

        console.log("[FeishuOrderCallback] 执行取消:", "order_id=", orderId, "previous_status=", currentStatus, "update_token_present=", !!updateToken)
        record.set("status", "Canceled")
        orderEvents.applyOrderRelationship(record, {
            relation_status: "closed",
            status: "Canceled",
        }, false)
        var cancelMeta = orderEvents.applyOrderStatusMeta(record, {
            status: "Canceled",
            previous_status: currentStatus,
            source: "feishu_order_callback",
            reason: "飞书卡片取消挂单",
        }, false)
        var cancelEventTimes = cancelMeta.eventTimes
        $app.save(record)
        try {
            orderEvents.appendOrderDetail(record, {
                status: "Canceled",
                source: "feishu_order_callback",
                reason: "飞书卡片取消挂单",
                us_time: cancelEventTimes.us_time,
                cn_time: cancelEventTimes.cn_time,
                bar_time_ms: cancelEventTimes.bar_time_ms,
                extra: {
                    previous_status: currentStatus,
                    action: "cancel",
                    update_token_present: !!updateToken
                }
            })
        } catch (detailErr) {
            console.error("[FeishuOrderCallback] 写入取消 order_details 失败:", detailErr)
        }
        orderCard = buildOrderCard(record, { message: "订单已取消" })
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "取消指令已发送" },
            card: { type: "raw", data: orderCard }
        }, updateToken)
    }

    if (action === "close") {
        var tradeGroupId = record.get("trade_group_id") || record.get("entry_order_unique_id") || record.get("unique_id")
        var entryOrderUniqueId = record.get("entry_order_unique_id") || record.get("unique_id")
        if (currentStatus !== "Filled") {
            orderMsg = "只有成交的订单才能平仓"
            orderCard = buildOrderCard(record, { message: orderMsg })
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "error", content: orderMsg },
                card: { type: "raw", data: orderCard }
            }, updateToken)
        }

        var relatedRecords = $app.findRecordsByFilter(
            "orders",
            "trade_group_id = {:gid}",
            "-created",
            100,
            0,
            { gid: tradeGroupId }
        ) || []
        relatedRecords.forEach(function(groupRecord) {
            var groupStatus = groupRecord.get("status")
            if (groupStatus === "Canceled" || groupStatus === "Closed") {
                return
            }
            var nextStatus = groupRecord.get("unique_id") === entryOrderUniqueId ? "Closed" : "Canceled"
            groupRecord.set("status", nextStatus)
            orderEvents.applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: nextStatus,
            }, false)
            var closeMeta = orderEvents.applyOrderStatusMeta(groupRecord, {
                status: nextStatus,
                previous_status: groupStatus,
                source: "feishu_order_callback",
                reason: "飞书卡片平仓交易组",
            }, false)
            var closeEventTimes = closeMeta.eventTimes
            $app.save(groupRecord)
            try {
                orderEvents.appendOrderDetail(groupRecord, {
                    status: nextStatus,
                    source: "feishu_order_callback",
                    reason: "飞书卡片平仓交易组",
                    us_time: closeEventTimes.us_time,
                    cn_time: closeEventTimes.cn_time,
                    bar_time_ms: closeEventTimes.bar_time_ms,
                    extra: {
                        previous_status: groupStatus,
                        action: "close_group",
                        trade_group_id: tradeGroupId,
                        update_token_present: !!updateToken
                    }
                })
            } catch (detailErr) {
                console.error("[FeishuOrderCallback] 写入平仓 order_details 失败:", detailErr)
            }
        })
        orderCard = buildOrderCard(record, { message: "交易组已平仓" })
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "交易组平仓指令已发送" },
            card: { type: "raw", data: orderCard }
        }, updateToken)
    }

    return feishuApp.sendFeishuCallbackResponse(c, {
        toast: { type: "error", content: "未知操作: " + action }
    }, updateToken)
}

module.exports = {
    buildOrderDisplayData: buildOrderDisplayData,
    getOrderStatusInfo: getOrderStatusInfo,
    buildOrderCard: buildOrderCard,
    buildOrderCardV2: buildOrderCardV2,
    notifyOrder: notifyOrder,
    notifyNewOrder: notifyNewOrder,
    handleOrderCardCallback: handleOrderCardCallback
}
