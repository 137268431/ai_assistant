/**
 * feishu_app.js - 飞书企业自建应用通知模块
 * 通过 require(`${__hooks}/feishu_app.js`) 引入
 * 支持交互卡片（按钮可回调更新）
 */

var FEISHU_APP_ID = "cli_a936b8d2cc79dccb";
var FEISHU_APP_SECRET = "ZZySOkZPaKBVkNhhk4upvfROPnXcSsry";
var PB_HOST = "https://pb.lzw-glory.top";

// 群聊 chat_id
var FEISHU_CHAT_ID_SIGNAL = "oc_edb26dcc52938b7833ac9f32ae6b1620";  // 信号群
var FEISHU_CHAT_ID_ORDER = "oc_5ca4585e1fd108c2c662dfc358684945";  // 订单群
var FEISHU_CHAT_ID_ERROR = "oc_b7b52fc28816d90e27ce50ca7922a9ac";   // 异常群

// Token 缓存（避免频繁刷新）
var _cachedToken = null;
var _tokenExpireTime = 0;

/**
 * 获取 App Access Token
 */
function getAppAccessToken() {
    var now = Date.now();

    // 检查缓存是否有效（提前5分钟过期）
    if (_cachedToken && now < _tokenExpireTime - 300000) {
        return _cachedToken;
    }

    try {
        var res = $http.send({
            url: "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                app_id: FEISHU_APP_ID,
                app_secret: FEISHU_APP_SECRET
            }),
            timeout: 10
        });

        if (res.statusCode !== 200) {
            console.error("[FeishuApp] 获取 Token 失败:", res.statusCode, res.raw);
            return null;
        }

        var data = typeof res.json === "function" ? res.json() : JSON.parse(res.raw || "{}");
        if (data.code !== 0 || !data.app_access_token) {
            console.error("[FeishuApp] Token 响应异常:", data);
            return null;
        }

        _cachedToken = data.app_access_token;
        _tokenExpireTime = now + (data.expire * 1000);  // expire 单位是秒
        console.log("[FeishuApp] Token 获取成功");

        return _cachedToken;
    } catch (err) {
        console.error("[FeishuApp] 获取 Token 异常:", err);
        return null;
    }
}

/**
 * 发送消息（支持文本和卡片）
 * @param {string} msgType - "text" 或 "interactive"
 * @param {object} content - 消息内容
 * @param {string} receiveId - chat_id 或 open_id
 * @param {string} receiveIdType - "chat_id" 或 "open_id"
 */
function sendMessage(msgType, content, receiveId, receiveIdType) {
    var token = getAppAccessToken();
    if (!token) {
        console.error("[FeishuApp] 缺少 Token，无法发送消息");
        return false;
    }

    try {
        var bodyContent = msgType === "interactive" ? JSON.stringify(content) : JSON.stringify(content);

        var res = $http.send({
            url: "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=" + receiveIdType,
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token
            },
            body: JSON.stringify({
                receive_id: receiveId,
                msg_type: msgType,
                content: bodyContent
            }),
            timeout: 15
        });

        if (res.statusCode !== 200) {
            console.error("[FeishuApp] 发送失败:", res.statusCode, res.raw);
            return false;
        }

        var data = typeof res.json === "function" ? res.json() : JSON.parse(res.raw || "{}");
        if (data.code !== 0) {
            console.error("[FeishuApp] 发送响应异常:", data);
            return false;
        }

        console.log("[FeishuApp] 消息发送成功:", data.data && data.data.message_id);
        return true;
    } catch (err) {
        console.error("[FeishuApp] 发送异常:", err);
        return false;
    }
}

/**
 * 发送文本消息到信号群
 */
function sendTextToChat(text) {
    return sendMessage("text", { text: text }, FEISHU_CHAT_ID_SIGNAL, "chat_id");
}

/**
 * 发送富文本消息（post）到指定群
 * @param {string} title - 标题
 * @param {string} content - 内容
 * @param {string} type - 群类型: "signal", "order", "error"
 */
function sendPostToChat(title, content, type) {
    var chatId = FEISHU_CHAT_ID_SIGNAL;
    if (type === "order") {
        chatId = FEISHU_CHAT_ID_ORDER;
    } else if (type === "error") {
        chatId = FEISHU_CHAT_ID_ERROR;
    }
    var postContent = {
        post: {
            zh_cn: {
                title: title,
                content: content
            }
        }
    };
    return sendMessage("post", postContent, chatId, "chat_id");
}

/**
 * 发送交互卡片到信号群
 */
function sendCardToChat(card) {
    return sendMessage("interactive", card, FEISHU_CHAT_ID_SIGNAL, "chat_id");
}

/**
 * 发送交互卡片到指定群
 * @param {object} card - 卡片内容
 * @param {string} type - 群类型: "signal", "order", "error"
 */
function sendCardToChatByType(card, type) {
    var chatId = FEISHU_CHAT_ID_SIGNAL;
    if (type === "order") {
        chatId = FEISHU_CHAT_ID_ORDER;
    } else if (type === "error") {
        chatId = FEISHU_CHAT_ID_ERROR;
    }
    return sendMessage("interactive", card, chatId, "chat_id");
}

// ── 信号通知 ──

/**
 * 从 signals record 构建展示用数据（回调和通知共用）
 * 兼容 PB record（有 get() 方法）和 plain object
 * @param {object} recordOrData - PocketBase record 或普通对象
 * @returns {object} 包含所有展示字段的结构化对象
 */
function buildSignalDisplayData(recordOrData) {
    // 兼容 PB record 和 plain object
    var get = (typeof recordOrData.get === "function") ? recordOrData.get.bind(recordOrData) : function(k) { return recordOrData[k] };

    var symbol = get("symbol") || ""
    var direction = get("direction") || "long"
    var entry = Number(get("entry")) || 0
    var take_profit = Number(get("take_profit")) || 0
    var stop_loss = Number(get("stop_loss")) || 0
    var shares = Number(get("shares")) || 0
    var rr = get("rr") || "N/A"
    var signal_id = get("signal_id") || ""
    var us_time = get("us_time") || ""
    // PB JSON 字段返回 raw bytes，用 getString 直接获取字符串
    var isPBRecord = typeof recordOrData.getString === "function"
    var extraStr = isPBRecord ? recordOrData.getString("extra") : ""
    var extra = {}
    if (extraStr) {
        try { extra = JSON.parse(extraStr) } catch(e) {}
    } else {
        // plain object（webhook 直接传入的数据）
        var extraRaw = get("extra")
        if (extraRaw && typeof extraRaw === "object") {
            extra = extraRaw
        } else if (typeof extraRaw === "string") {
            try { extra = JSON.parse(extraRaw) } catch(e) {}
        }
    }
    var reason = extra.reason || get("reason") || ""

    // 涨幅（当日/前收/近7日）
    var changeDisplay = "N/A"
    var dayPct = Number(extra.day_change_pct || 0)
    var prevPct = Number(extra.prev_close_change_pct || 0)
    var d7Pct = Number(extra.change_7d || 0)
    if (extra.day_change_pct !== undefined) {
        changeDisplay = (dayPct > 0 ? "+" : "") + dayPct.toFixed(2) + "%/" + (prevPct > 0 ? "+" : "") + prevPct.toFixed(2) + "%/" + (d7Pct > 0 ? "+" : "") + d7Pct.toFixed(2) + "%"
    }

    // 盈亏计算
    var isLong = direction === "long"
    var tpProfit = (isLong ? (take_profit - entry) : (entry - take_profit)) * shares
    var slLoss = (isLong ? (entry - stop_loss) : (stop_loss - entry)) * shares
    var formatAmount = function(a) {
        if (!a || a === 0) return "0"
        if (Math.abs(a) >= 10000) return (a / 10000).toFixed(2) + "w"
        return a.toFixed(2)
    }

    // 波动率/ATR止损
    var atrText = null
    if (extra.atr_pct) {
        var atrLevel = extra.atr_pct >= 3 ? "高" : extra.atr_pct >= 1.5 ? "中" : "低"
        var atrEmoji = extra.atr_pct >= 3 ? "⚡" : extra.atr_pct >= 1.5 ? "〜" : "·"
        atrText = "**波动率:** " + atrEmoji + " " + atrLevel + " " + extra.atr_pct.toFixed(2) + "%"
        if (extra.sl_atr_ratio) {
            atrText += "\n**ATR止损:** " + extra.sl_atr_ratio.toFixed(1) + "倍"
        }
    } else if (extra.atr) {
        atrText = "**ATR:** " + extra.atr.toFixed(2)
    }

    // 大盘信息 - 先尝试从 extra.market_indexes 取，若为空则从 indicators 表查询最新数据
    var marketIndexes = extra.market_indexes || []
    if (marketIndexes.length === 0) {
        try {
            var fetched = []
            var marketSyms = ["SPY", "QQQ", "VIX"]
            for (var i = 0; i < marketSyms.length; i++) {
                var msym = marketSyms[i]
                try {
                    var recs = $app.findRecordsByFilter("indicators", "symbol = {:sym}", "-bar_time_ms", 1, 0, { sym: msym })
                    if (recs && recs.length > 0) {
                        // PB record 的 extra 字段需要用 getString 获取后再 parse
                        var indExtraStr = recs[0].getString("extra")
                        var indExtra = {}
                        if (indExtraStr) {
                            try { indExtra = JSON.parse(indExtraStr) } catch(e) {}
                        }
                        var indChange = indExtra.day_change_pct
                        if (indChange !== undefined) {
                            fetched.push({ symbol: msym, change_pct: Number(indChange) })
                        }
                    }
                } catch(e) {}
            }
            if (fetched.length > 0) {
                marketIndexes = fetched
                console.log("[FeishuApp] 从indicators表查询market_indexes:", JSON.stringify(fetched))
            }
        } catch(e) {
            console.log("[FeishuApp] 查询indicators表失败:", e)
        }
    }

    // 构建 marketInfoText
    var marketInfoText = null
    if (marketIndexes.length > 0) {
        var spyD = marketIndexes.find(function(m) { return m.symbol === "SPY" })
        var qqqD = marketIndexes.find(function(m) { return m.symbol === "QQQ" })
        var vixD = marketIndexes.find(function(m) { return m.symbol === "VIX" })
        var mParts = []
        if (spyD) mParts.push("SPY: " + (spyD.change_pct > 0 ? "+" : "") + Number(spyD.change_pct).toFixed(2) + "%")
        if (qqqD) mParts.push("QQQ: " + (qqqD.change_pct > 0 ? "+" : "") + Number(qqqD.change_pct).toFixed(2) + "%")
        if (mParts.length > 0) marketInfoText = "**大盘:** " + mParts.join(" | ")
        if (vixD) {
            var v = Number(vixD.change_pct)
            var vixLevel = v >= 20 ? "🔴 恐慌" : v >= 10 ? "🟡 紧张" : "🟢 平稳"
            marketInfoText += "\n**VIX恐慌:** " + vixLevel + " " + (v > 0 ? "+" : "") + v.toFixed(1) + "%"
        }
    }

    return {
        symbol: symbol,
        direction: direction,
        entry: entry,
        take_profit: take_profit,
        stop_loss: stop_loss,
        shares: shares,
        rr: rr,
        signal_id: signal_id,
        us_time: us_time,
        reason: reason,
        changeDisplay: changeDisplay,
        tpProfit: tpProfit,
        slLoss: slLoss,
        formatAmount: formatAmount,
        atrText: atrText,
        marketInfoText: marketInfoText,
        marketIndexes: marketIndexes,
        extra: extra
    }
}

/**
 * 发送新信号通知（带确认/拒绝按钮）
 * 样式与 feishu.js 完全一致
 */
function notifyNewSignal(signal) {
    var d = buildSignalDisplayData(signal)
    var directionText = d.direction === "long" ? "做多 📈" : "做空 📉"
    var color = d.direction === "long" ? "green" : "red"

    // 构建两列布局的字段 - 左侧
    var leftColumn = []
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + d.symbol } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**涨幅:** " + d.changeDisplay } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**入场:** $" + d.entry.toFixed(2) } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止盈:** $" + d.take_profit.toFixed(2) } })
    leftColumn.push({ tag: "div", text: { tag: "lark_md", content: "**止损:** $" + d.stop_loss.toFixed(2) } })

    // 右侧
    var rightColumn = []
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**盈利:** +$" + d.formatAmount(d.tpProfit) } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**亏损:** -$" + d.formatAmount(d.slLoss) } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**风报比:** " + d.rr } })
    rightColumn.push({ tag: "div", text: { tag: "lark_md", content: "**股数:** " + (d.shares || "N/A") } })
    if (d.atrText) {
        d.atrText.split("\n").forEach(function(line) {
            rightColumn.push({ tag: "div", text: { tag: "lark_md", content: line } })
        })
    }

    // 构建 elements 数组
    var elements = [
        {
            tag: "column_set",
            horizontal_spacing: "default",
            columns: [
                { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: leftColumn },
                { tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: rightColumn }
            ]
        }
    ]

    // 原因/信号ID/大盘 放入单列 column_set 控制间距
    var infoElements = []
    infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**信号ID:** " + d.signal_id } })
    if (d.reason) {
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: "**原因:** " + d.reason } })
    }
    if (d.marketInfoText) {
        infoElements.push({ tag: "div", text: { tag: "lark_md", content: d.marketInfoText } })
    }
    elements.push({ tag: "column_set", columns: [{ tag: "column", width: "weighted", weight: 1, vertical_spacing: "2px", elements: infoElements }] })

    // 操作按钮（schema 2.0 用 column_set 横排）
    elements.push({ tag: "hr" })
    elements.push({
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            {
                tag: "column", width: "weighted", weight: 1,
                elements: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "✅ 确认" },
                    type: "primary",
                    width: "fill",
                    behaviors: [{ type: "callback", value: { action: "confirm", signal_id: d.signal_id } }]
                }]
            },
            {
                tag: "column", width: "weighted", weight: 1,
                elements: [{
                    tag: "button",
                    text: { tag: "plain_text", content: "❌ 拒绝" },
                    type: "danger",
                    width: "fill",
                    behaviors: [{ type: "callback", value: { action: "reject", signal_id: d.signal_id } }]
                }]
            }
        ]
    })

    var card = {
        schema: "2.0",
        config: { update_multi: true },
        header: {
            title: { tag: "plain_text", content: "🔔 新交易信号 · " + d.symbol + " · " + d.us_time },
            template: color
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }

    var success = sendCardToChat(card)
    console.log("[FeishuApp] 信号通知发送:", success ? "成功" : "失败")
    return success
}

/**
 * 发送异常通知
 */
function notifyError(title, content) {
    var postContent = [[{ tag: "text", text: title }]];
    if (content) {
        postContent.push([{ tag: "text", text: content }]);
    }
    var success = sendMessage("post", { post: { zh_cn: { title: "", content: postContent } } }, FEISHU_CHAT_ID_ERROR, "chat_id");
    console.log("[FeishuApp] 异常通知发送:", success ? "成功" : "失败");
    return success;
}

/**
 * 订单状态变更时发送普通通知（发送到订单群）
 */
function notifyOrder(action, order) {
    var titles = {
        filled:   "✅ 订单已成交",
        canceled: "❌ 订单已取消",
        closed:   "🔒 订单已平仓",
        updated:  "🔄 订单状态变更"
    };
    var title = (titles[action] || "📊 订单状态变更") + " · " + order.symbol;
    var directionText = order.direction === "long" ? "做多 📈" : "做空 📉";
    var content = [
        [{ tag: "text", text: "标的: " + order.symbol }],
        [{ tag: "text", text: "方向: " + directionText }],
        [{ tag: "text", text: "类型: " + order.order_type }],
        [{ tag: "text", text: "状态: " + order.status }]
    ];
    if (order.quantity)    content.push([{ tag: "text", text: "数量: " + order.quantity }]);
    if (order.limit_price) content.push([{ tag: "text", text: "限价: $" + order.limit_price.toFixed(2) }]);
    if (order.fill_price)  content.push([{ tag: "text", text: "成交价: $" + order.fill_price.toFixed(2) }]);
    if (order.pnl !== undefined) content.push([{ tag: "text", text: "盈亏: $" + Number(order.pnl).toFixed(2) }]);
    if (order.signal_id)   content.push([{ tag: "text", text: "信号ID: " + order.signal_id }]);
    content.push([{ tag: "text", text: "订单ID: " + order.unique_id }]);
    sendFeishuPost(title, content, "order");
}

/**
 * 发送订单通知（带取消/平仓按钮）
 * 发送到订单群
 */
function notifyNewOrder(order) {
    var directionText = order.direction === "long" ? "做多 📈" : "做空 📉";
    var color = order.direction === "long" ? "green" : "red";

    var fields = "**标的:** " + order.symbol + "\n" +
        "**方向:** " + directionText + "\n" +
        "**类型:** " + order.order_type + "\n" +
        "**数量:** " + (order.quantity || "N/A") + "\n" +
        "**限价:** $" + (order.limit_price ? order.limit_price.toFixed(2) : "N/A");
    if (order.signal_id) fields += "\n**信号ID:** " + order.signal_id;
    fields += "\n**订单ID:** " + order.unique_id;

    var card = {
        header: {
            title: { tag: "plain_text", content: "📝 新订单 · " + order.symbol },
            template: color
        },
        elements: [
            { tag: "div", margin: "4px", text: { tag: "lark_md", content: fields } },
            {
                tag: "action",
                actions: [
                    {
                        tag: "button",
                        text: { tag: "plain_text", content: "❌ 取消挂单" },
                        type: "danger",
                        action_type: "request",
                        url: PB_HOST + "/webhook/feishu/order/callback",
                        value: { action: "cancel", order_id: order.unique_id }
                    },
                    {
                        tag: "button",
                        text: { tag: "plain_text", content: "🔒 平仓" },
                        type: "default",
                        action_type: "request",
                        url: PB_HOST + "/webhook/feishu/order/callback",
                        value: { action: "close", order_id: order.unique_id }
                    }
                ]
            }
        ]
    };

    var success = sendCardToChatByType(card, "order");
    console.log("[FeishuApp] 订单通知发送:", success ? "成功" : "失败");
    return success;
}

/**
 * 发送简单文本通知
 */
function notifySimple(message) {
    return sendTextToChat(message);
}

/**
 * 发送富文本通知
 */
function sendFeishuPost(title, content) {
    return sendPostToChat(title, content);
}

module.exports = {
    // 发送函数
    sendTextToChat: sendTextToChat,
    sendPostToChat: sendPostToChat,
    sendCardToChat: sendCardToChat,
    sendCardToChatByType: sendCardToChatByType,
    sendMessage: sendMessage,
    getAppAccessToken: getAppAccessToken,

    // 通知函数
    notifyNewSignal: notifyNewSignal,
    notifyNewOrder: notifyNewOrder,
    notifyOrder: notifyOrder,
    notifySimple: notifySimple,
    notifyError: notifyError,
    sendFeishuPost: sendFeishuPost,

    // 公共方法
    buildSignalDisplayData: buildSignalDisplayData,

    // 回调响应辅助函数
    buildSignalCardV2: function(symbol, directionText, statusEmoji, statusText, status, color, message, extraFields, us_time) {
        var elements = [];

        // 如果有完整字段（来自 buildSignalDisplayData），直接使用
        if (extraFields && extraFields.length > 0) {
            elements = extraFields.slice();
        } else {
            // 兜底：只展示标的和方向
            elements.push({ tag: "div", text: { tag: "lark_md", content: "**标的:** " + symbol } });
            elements.push({ tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } });
        }

        // 状态和消息区域（合并为一行）
        elements.push({ tag: "hr" });
        elements.push({ tag: "div", text: { tag: "lark_md", content: "**状态:** " + statusText + " · " + message } });

        return {
            schema: "2.0",
            config: { update_multi: true },
            header: {
                title: { tag: "plain_text", content: statusEmoji + " " + statusText + " · " + symbol + " · " + (us_time || "") },
                template: color
            },
            body: {
                direction: "vertical",
                elements: elements
            }
        }
    },
    buildOrderCardV2: function(symbol, directionText, statusEmoji, statusText, status, color, message) {
        return {
            schema: "2.0",
            config: { update_multi: true },
            header: {
                title: { tag: "plain_text", content: statusEmoji + " 订单状态 · " + symbol },
                template: color
            },
            body: {
                direction: "vertical",
                elements: [
                    { tag: "div", text: { tag: "lark_md", content: "**标的:** " + symbol } },
                    { tag: "div", text: { tag: "lark_md", content: "**方向:** " + directionText } },
                    { tag: "div", text: { tag: "lark_md", content: "**状态:** " + statusText } },
                    { tag: "hr" },
                    { tag: "div", text: { tag: "lark_md", content: message } }
                ]
            }
        }
    },
    sendFeishuCallbackResponse: function(c, data, updateToken) {
        var jsonStr = JSON.stringify(data);
        var res = c.response;
        res.header["Content-Type"] = ["application/json"];
        if (updateToken) {
            res.header["update_card_token"] = [updateToken];
        }
        res.write(jsonStr);
        return
    }
}
