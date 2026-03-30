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

    return {
        symbol: symbol,
        direction: direction,
        directionText: directionText,
        color: color,
        status: status,
        order_type: get("order_type") || extra.order_type || "Entry",
        quantity: Number(get("quantity") != null ? get("quantity") : extra.quantity || 0),
        limit_price: Number(get("limit_price") != null ? get("limit_price") : extra.limit_price || 0),
        fill_price: Number(get("fill_price") != null ? get("fill_price") : extra.fill_price || 0),
        filled_qty: Number(get("filled_qty") != null ? get("filled_qty") : extra.filled_qty || 0),
        tp_price: Number(get("tp_price") != null ? get("tp_price") : extra.tp_price || 0),
        sl_price: Number(get("sl_price") != null ? get("sl_price") : extra.sl_price || 0),
        signal_id: get("signal_id") || "",
        order_id: get("order_id") || "",
        unique_id: unique_id,
        order_time: get("order_time") || extra.order_time || "",
        fill_time: get("fill_time") || extra.fill_time || "",
        us_time: us_time,
        cn_time: get("cn_time") || "",
        extra: extra
    }
}

function getOrderStatusInfo(status) {
    var map = {
        Init: { emoji: "🆕", text: "初始化", message: "订单已初始化，等待提交", action: "cancel" },
        Submitted: { emoji: "⏳", text: "待成交", message: "订单已提交，等待成交", action: "cancel" },
        Filled: { emoji: "✅", text: "已成交", message: "订单已成交，可执行平仓", action: "close" },
        Canceled: { emoji: "❌", text: "已取消", message: "订单已取消", action: "" },
        Closed: { emoji: "🔒", text: "已平仓", message: "订单已平仓", action: "" }
    }
    return map[status] || { emoji: "❓", text: status || "未知", message: "订单状态未知", action: "" }
}

function buildOrderActionElements(d) {
    var info = getOrderStatusInfo(d.status)
    if (!info.action) return []

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
                    value: { action: "cancel", order_id: d.unique_id }
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
                    value: { action: "close", order_id: d.unique_id }
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

    var leftColumn = []
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + d.symbol } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + d.directionText } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**类型:** " + d.order_type } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**状态:** " + info.text } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**限价:** " + (d.limit_price ? "$" + d.limit_price.toFixed(2) : "N/A") } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** " + (d.tp_price ? "$" + d.tp_price.toFixed(2) : "N/A") } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** " + (d.sl_price ? "$" + d.sl_price.toFixed(2) : "N/A") } })

    var rightColumn = []
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**数量:** " + (d.quantity || "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**已成交:** " + (d.filled_qty || 0) } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**成交价:** " + (d.fill_price ? "$" + d.fill_price.toFixed(2) : "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + (d.signal_id || "N/A") } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**订单ID:** " + d.unique_id } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**下单时间:** " + (d.order_time || d.us_time || "N/A") } })

    var elements = [{
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: leftColumn },
            { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: rightColumn }
        ]
    }]

    elements.push({ tag: "hr" })
    elements.push({ tag: "div", text: { tag: "lark_md", content: "**状态:** " + info.text + " · " + statusMessage } })

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
        if (currentStatus !== "Init" && currentStatus !== "Submitted") {
            orderMsg = currentStatus === "Filled" ? "订单已成交，无法取消" : "无法取消"
            orderCard = buildOrderCard(record, { message: orderMsg })
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "error", content: orderMsg },
                card: { type: "raw", data: orderCard }
            }, updateToken)
        }

        console.log("[FeishuOrderCallback] 执行取消:", "order_id=", orderId, "previous_status=", currentStatus, "update_token_present=", !!updateToken)
        var cancelEventTimes = orderEvents.applyOrderEventTimes(record)
        record.set("status", "Canceled")
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
        if (currentStatus !== "Filled") {
            orderMsg = "只有成交的订单才能平仓"
            orderCard = buildOrderCard(record, { message: orderMsg })
            return feishuApp.sendFeishuCallbackResponse(c, {
                toast: { type: "error", content: orderMsg },
                card: { type: "raw", data: orderCard }
            }, updateToken)
        }

        var closeEventTimes = orderEvents.applyOrderEventTimes(record)
        record.set("status", "Closed")
        $app.save(record)
        try {
            orderEvents.appendOrderDetail(record, {
                status: "Closed",
                source: "feishu_order_callback",
                reason: "飞书卡片平仓",
                us_time: closeEventTimes.us_time,
                cn_time: closeEventTimes.cn_time,
                bar_time_ms: closeEventTimes.bar_time_ms,
                extra: {
                    previous_status: currentStatus,
                    action: "close",
                    update_token_present: !!updateToken
                }
            })
        } catch (detailErr) {
            console.error("[FeishuOrderCallback] 写入平仓 order_details 失败:", detailErr)
        }
        orderCard = buildOrderCard(record, { message: "订单已平仓" })
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "平仓指令已发送" },
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
