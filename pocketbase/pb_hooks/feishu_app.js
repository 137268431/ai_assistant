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
 * 发送新信号通知（带确认/拒绝按钮）
 * 样式与 feishu.js 完全一致
 */
function notifyNewSignal(signal) {
    var directionText = signal.direction === "long" ? "做多 📈" : "做空 📉";
    var color = signal.direction === "long" ? "green" : "red";
    var extra = signal.extra || {};

    // 大盘关联信息 - 直接显示 SPY 和 QQQ 涨跌幅
    var marketInfoText = "";
    if (signal.market_indexes && signal.market_indexes.length > 0) {
        var spyData = signal.market_indexes.find(function(m) { return m.symbol === "SPY"; });
        var qqqData = signal.market_indexes.find(function(m) { return m.symbol === "QQQ"; });
        var vixData = signal.market_indexes.find(function(m) { return m.symbol === "VIX"; });

        var parts = [];
        if (spyData) {
            var change = spyData.change_pct;
            parts.push("SPY: " + (change > 0 ? "+" : "") + change.toFixed(2) + "%");
        }
        if (qqqData) {
            var change = qqqData.change_pct;
            parts.push("QQQ: " + (change > 0 ? "+" : "") + change.toFixed(2) + "%");
        }
        if (parts.length > 0) {
            marketInfoText = "**大盘:** " + parts.join(" | ");
        }

        // VIX 恐慌指数单独一行
        if (vixData) {
            var vixLevel = vixData.change_pct >= 20 ? "🔴 恐慌" : vixData.change_pct >= 10 ? "🟡 紧张" : "🟢 平稳";
            marketInfoText += "\n**VIX恐慌:** " + vixLevel + " " + (vixData.change_pct > 0 ? "+" : "") + vixData.change_pct.toFixed(1) + "%";
        }
    }

    // 计算盈亏
    var isLong = signal.direction === "long";
    var shares = signal.shares || 0;
    var tpProfit = (isLong ? (signal.take_profit - signal.entry) : (signal.entry - signal.take_profit)) * shares;
    var slLoss = (isLong ? (signal.entry - signal.stop_loss) : (signal.stop_loss - signal.entry)) * shares;

    function formatAmount(amount) {
        if (!amount || amount === 0) return "0";
        if (Math.abs(amount) >= 10000) {
            return (amount / 10000).toFixed(2) + "w";
        }
        return amount.toFixed(2);
    }

    // 构建两列布局的字段 - 左侧
    var leftColumn = [];
    leftColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**标的:** " + signal.symbol } });
    leftColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**方向:** " + directionText } });

    // 涨幅（当日/前收/近7日）
    var changeDisplay = "N/A";
    if (extra.day_change_pct !== undefined) {
        var dayPct = Number(extra.day_change_pct || 0);
        var prevPct = Number(extra.prev_close_change_pct || 0);
        var d7Pct = Number(extra.change_7d || 0);
        changeDisplay = (dayPct > 0 ? "+" : "") + dayPct.toFixed(2) + "%/" + (prevPct > 0 ? "+" : "") + prevPct.toFixed(2) + "%/" + (d7Pct > 0 ? "+" : "") + d7Pct.toFixed(2) + "%";
    }
    leftColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**涨幅:** " + changeDisplay } });
    leftColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**入场:** $" + signal.entry.toFixed(2) } });
    leftColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**止盈:** $" + signal.take_profit.toFixed(2) } });
    leftColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**止损:** $" + signal.stop_loss.toFixed(2) } });

    // 构建两列布局的字段 - 右侧
    var rightColumn = [];
    rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**盈利:** +$" + formatAmount(tpProfit) } });
    rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**亏损:** -$" + formatAmount(slLoss) } });
    rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**风报比:** " + (signal.rr || "N/A") } });
    rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**股数:** " + (signal.shares || "N/A") } });

    // 波动率和 ATR 止损（股数下面）
    if (extra.atr_pct) {
        var atrLevel = extra.atr_pct >= 3 ? "高" : extra.atr_pct >= 1.5 ? "中" : "低";
        var atrEmoji = extra.atr_pct >= 3 ? "⚡" : extra.atr_pct >= 1.5 ? "〜" : "·";
        rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**波动率:** " + atrEmoji + " " + atrLevel + " " + extra.atr_pct.toFixed(2) + "%" } });
        if (extra.sl_atr_ratio) {
            rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**ATR止损:** " + extra.sl_atr_ratio.toFixed(1) + "倍" } });
        }
    } else if (extra.atr) {
        rightColumn.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**ATR:** " + extra.atr.toFixed(2) } });
    }

    // 添加原因（单独一行）
    var reasonColumn = null;
    if (extra.reason) {
        reasonColumn = { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**原因:** " + extra.reason } };
    }

    // 添加信号ID（单独一行）
    var signalIdColumn = { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**信号ID:** " + signal.signal_id } };

    // 添加大盘信息
    var marketColumn = null;
    if (marketInfoText) {
        marketColumn = { tag: "div", margin: "sm", text: { tag: "lark_md", content: marketInfoText.replace(/^\n/, "") } };
    }

    // 构建 elements 数组
    var elements = [
        {
            tag: "column_set",
            columns: [
                { tag: "column", width: "weighted", weight: 1, elements: leftColumn },
                { tag: "column", width: "weighted", weight: 1, elements: rightColumn }
            ]
        }
    ];

    // 添加原因行（如果存在）
    if (reasonColumn) {
        elements.push({
            tag: "column_set",
            columns: [
                { tag: "column", width: "weighted", weight: 1, elements: [reasonColumn] }
            ]
        });
    }

    // 添加信号ID行
    elements.push({
        tag: "column_set",
        columns: [
            { tag: "column", width: "weighted", weight: 1, elements: [signalIdColumn] }
        ]
    });

    // 添加大盘信息行（如果存在）
    if (marketColumn) {
        elements.push({
            tag: "column_set",
            columns: [
                { tag: "column", width: "weighted", weight: 1, elements: [marketColumn] }
            ]
        });
    }

    var card = {
        header: {
            title: { tag: "plain_text", content: "🔔 新交易信号 · " + signal.symbol + " · " + (signal.us_time || "") },
            template: color
        },
        elements: elements
    };

    // 添加操作按钮
    card.elements.push({ tag: "hr", margin: "sm" });
    card.elements.push({
        tag: "action",
        actions: [
            {
                tag: "button",
                text: { tag: "plain_text", content: "✅ 确认" },
                type: "primary",
                action_type: "request",
                url: PB_HOST + "/webhook/feishu/callback",
                value: { action: "confirm", signal_id: signal.signal_id }
            },
            {
                tag: "button",
                text: { tag: "plain_text", content: "❌ 拒绝" },
                type: "danger",
                action_type: "request",
                url: PB_HOST + "/webhook/feishu/callback",
                value: { action: "reject", signal_id: signal.signal_id }
            }
        ]
    });

    var success = sendCardToChat(card);
    console.log("[FeishuApp] 信号通知发送:", success ? "成功" : "失败");
    return success;
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
            { tag: "div", text: { tag: "lark_md", content: fields } },
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

    // 回调响应辅助函数
    buildSignalCardV2: function(symbol, directionText, statusEmoji, statusText, status, color, message, extraFields) {
        var elements = [
            { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**标的:** " + symbol } },
            { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**方向:** " + directionText } }
        ];

        // 如果有额外字段，显示完整的信号信息
        if (extraFields && extraFields.length > 0) {
            elements.push({ tag: "hr", margin: "sm" });
            elements = elements.concat(extraFields);
        }

        // 状态和消息区域
        elements.push({ tag: "hr", margin: "sm" });
        elements.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: "**状态:** " + statusText } });
        elements.push({ tag: "div", margin: "sm", text: { tag: "lark_md", content: message } });

        return {
            schema: "2.0",
            config: { update_multi: true },
            header: {
                title: { tag: "plain_text", content: statusEmoji + " 信号状态 · " + symbol },
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
                    { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**标的:** " + symbol } },
                    { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**方向:** " + directionText } },
                    { tag: "div", margin: "sm", text: { tag: "lark_md", content: "**状态:** " + statusText } },
                    { tag: "hr", margin: "sm" },
                    { tag: "div", margin: "sm", text: { tag: "lark_md", content: message } }
                ]
            }
        }
    },
    sendFeishuCallbackResponse: function(c, data) {
        var jsonStr = JSON.stringify(data);
        c.response.header["Content-Type"] = ["application/json"];
        c.response.write(jsonStr);
        return
    }
}
