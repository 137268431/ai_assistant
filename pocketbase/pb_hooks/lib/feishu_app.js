/**
 * feishu_app.js
 * 飞书企业自建应用通用发送/更新能力
 * 领域逻辑拆分到 feishu_signal.js / feishu_order.js
 */

var envUtils = require(`${__hooks}/lib/environment.js`)

var FEISHU_APP_ID = "cli_a936b8d2cc79dccb"
var FEISHU_APP_SECRET = "ZZySOkZPaKBVkNhhk4upvfROPnXcSsry"

var FEISHU_CHAT_ID_SIGNAL = "oc_edb26dcc52938b7833ac9f32ae6b1620"
var FEISHU_CHAT_ID_ORDER = "oc_5ca4585e1fd108c2c662dfc358684945"
var FEISHU_CHAT_ID_ERROR = "oc_b7b52fc28816d90e27ce50ca7922a9ac"
var FEISHU_CHAT_ID_REVERSE = "oc_2931e2b8501df3a9d869d7aebceb8fe2"

var _cachedToken = null
var _tokenExpireTime = 0

function normalizeFeishuEnvironment(environment) {
    return envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
}

function isFeishuSuppressed(environment) {
    return normalizeFeishuEnvironment(environment) === envUtils.PAPER_ENVIRONMENT
}

function buildSuppressedResult(environment, action) {
    var runtimeEnvironment = normalizeFeishuEnvironment(environment)
    console.log("[FeishuApp] 发送跳过:", action || "message", "environment:", runtimeEnvironment)
    return {
        success: true,
        skipped: true,
        suppressed: true,
        environment: runtimeEnvironment,
        message_id: "",
        reason: "paper_environment_disabled"
    }
}

function getConfiguredChatId(configKey, fallbackChatId, environment) {
    var runtimeEnvironment = normalizeFeishuEnvironment(environment)
    var configured = envUtils.getConfigValue(configKey, fallbackChatId, runtimeEnvironment)
    return String(configured || fallbackChatId).trim() || fallbackChatId
}

function getSignalChatId(environment) {
    return getConfiguredChatId("signal_chat_id", FEISHU_CHAT_ID_SIGNAL, environment)
}

function getOrderChatId(environment) {
    return getConfiguredChatId("order_chat_id", FEISHU_CHAT_ID_ORDER, environment)
}

function getReverseChatId(environment) {
    return getConfiguredChatId("reverse_chat_id", FEISHU_CHAT_ID_REVERSE, environment)
}

function getErrorChatId(environment) {
    return getConfiguredChatId("system_alert_chat_id", FEISHU_CHAT_ID_ERROR, environment)
}

function getAppAccessToken() {
    var now = Date.now()

    if (_cachedToken && now < _tokenExpireTime - 300000) {
        return _cachedToken
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
        })

        if (res.statusCode !== 200) {
            console.error("[FeishuApp] 获取 Token 失败:", res.statusCode, res.raw)
            return null
        }

        var data = typeof res.json === "function" ? res.json() : JSON.parse(res.raw || "{}")
        if (data.code !== 0 || !data.app_access_token) {
            console.error("[FeishuApp] Token 响应异常:", data)
            return null
        }

        _cachedToken = data.app_access_token
        _tokenExpireTime = now + (data.expire * 1000)
        console.log("[FeishuApp] Token 获取成功")
        return _cachedToken
    } catch (err) {
        console.error("[FeishuApp] 获取 Token 异常:", err)
        return null
    }
}

function sendMessageDetailed(msgType, content, receiveId, receiveIdType, environment) {
    if (isFeishuSuppressed(environment)) {
        return buildSuppressedResult(environment, msgType)
    }

    var token = getAppAccessToken()
    if (!token) {
        console.error("[FeishuApp] 缺少 Token，无法发送消息")
        return { success: false, message_id: "", error: "missing_token" }
    }

    try {
        var bodyContent = JSON.stringify(content)
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
        })

        if (res.statusCode !== 200) {
            console.error("[FeishuApp] 发送失败:", res.statusCode, res.raw)
            return { success: false, message_id: "", error: "http_" + res.statusCode, raw: res.raw }
        }

        var data = typeof res.json === "function" ? res.json() : JSON.parse(res.raw || "{}")
        if (data.code !== 0) {
            console.error("[FeishuApp] 发送响应异常:", data)
            return { success: false, message_id: "", error: "api_" + data.code, data: data }
        }

        var messageId = data.data && data.data.message_id ? data.data.message_id : ""
        console.log("[FeishuApp] 消息发送成功:", messageId)
        return { success: true, message_id: messageId, data: data.data || {} }
    } catch (err) {
        console.error("[FeishuApp] 发送异常:", err)
        return { success: false, message_id: "", error: String(err) }
    }
}

function sendMessage(msgType, content, receiveId, receiveIdType, environment) {
    var result = sendMessageDetailed(msgType, content, receiveId, receiveIdType, environment)
    return !!(result && result.success && !result.suppressed)
}

function updateMessageCard(messageId, card, environment) {
    if (isFeishuSuppressed(environment)) {
        return buildSuppressedResult(environment, "update_card")
    }

    var token = getAppAccessToken()
    if (!token) {
        console.error("[FeishuApp] 缺少 Token，无法更新卡片")
        return { success: false, message_id: messageId || "", error: "missing_token" }
    }

    if (!messageId) {
        return { success: false, message_id: "", error: "missing_message_id" }
    }

    try {
        var res = $http.send({
            url: "https://open.feishu.cn/open-apis/im/v1/messages/" + messageId,
            method: "PATCH",
            headers: {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token
            },
            body: JSON.stringify({
                msg_type: "interactive",
                content: JSON.stringify(card)
            }),
            timeout: 15
        })

        if (res.statusCode !== 200) {
            console.error("[FeishuApp] 更新卡片失败:", res.statusCode, res.raw)
            return { success: false, message_id: messageId, error: "http_" + res.statusCode, raw: res.raw }
        }

        var data = typeof res.json === "function" ? res.json() : JSON.parse(res.raw || "{}")
        if (data.code !== 0) {
            console.error("[FeishuApp] 更新卡片响应异常:", data)
            return { success: false, message_id: messageId, error: "api_" + data.code, data: data }
        }

        console.log("[FeishuApp] 卡片更新成功:", messageId)
        return { success: true, message_id: messageId, data: data.data || {} }
    } catch (err) {
        console.error("[FeishuApp] 更新卡片异常:", err)
        return { success: false, message_id: messageId, error: String(err) }
    }
}

function getChatIdByType(type, environment) {
    if (type === "order") return getOrderChatId(environment)
    if (type === "reverse") return getReverseChatId(environment)
    if (type === "error") return getErrorChatId(environment)
    return getSignalChatId(environment)
}

function sendTextToChat(text, environment) {
    return sendMessage("text", { text: text }, getSignalChatId(environment), "chat_id", environment)
}

function sendPostToChat(title, content, type, environment) {
    var postContent = {
        post: {
            zh_cn: {
                title: title,
                content: content
            }
        }
    }
    return sendMessage("post", postContent, getChatIdByType(type, environment), "chat_id", environment)
}

function sendCardToChat(card, environment) {
    return sendMessage("interactive", card, getSignalChatId(environment), "chat_id", environment)
}

function sendCardToChatDetailed(card, environment) {
    return sendMessageDetailed("interactive", card, getSignalChatId(environment), "chat_id", environment)
}

function sendCardToChatByType(card, type, environment) {
    return sendMessage("interactive", card, getChatIdByType(type, environment), "chat_id", environment)
}

function sendCardToChatByTypeDetailed(card, type, environment) {
    return sendMessageDetailed("interactive", card, getChatIdByType(type, environment), "chat_id", environment)
}

function notifyError(title, content, environment) {
    var postContent = [[{ tag: "text", text: title }]]
    if (content) {
        postContent.push([{ tag: "text", text: content }])
    }
    var success = sendMessage("post", {
        post: { zh_cn: { title: "", content: postContent } }
    }, getErrorChatId(environment), "chat_id", environment)
    console.log("[FeishuApp] 异常通知发送:", success ? "成功" : "失败")
    return success
}

function notifySimple(message, environment) {
    return sendTextToChat(message, environment)
}

function sendFeishuPost(title, content, type, environment) {
    return sendPostToChat(title, content, type, environment)
}

function sendFeishuCallbackResponse(c, data, updateToken) {
    var jsonStr = JSON.stringify(data)
    var res = c.response
    res.header["Content-Type"] = ["application/json"]
    if (updateToken) {
        res.header["update_card_token"] = [updateToken]
    }
    res.write(jsonStr)
    return
}

module.exports = {
    sendTextToChat: sendTextToChat,
    sendPostToChat: sendPostToChat,
    sendCardToChat: sendCardToChat,
    sendCardToChatDetailed: sendCardToChatDetailed,
    sendCardToChatByType: sendCardToChatByType,
    sendCardToChatByTypeDetailed: sendCardToChatByTypeDetailed,
    sendMessage: sendMessage,
    sendMessageDetailed: sendMessageDetailed,
    updateMessageCard: updateMessageCard,
    getAppAccessToken: getAppAccessToken,
    notifySimple: notifySimple,
    notifyError: notifyError,
    sendFeishuPost: sendFeishuPost,
    sendFeishuCallbackResponse: sendFeishuCallbackResponse
}
