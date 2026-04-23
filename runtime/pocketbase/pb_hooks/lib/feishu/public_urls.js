var envUtils = require(`${__hooks}/lib/environment.js`)

var DEFAULT_CONSOLE_PUBLIC_URL = "https://quant.lzw-glory.top"
var DEFAULT_PB_AUTH_PUBLIC_URL = "https://pb.lzw-glory.top"

function normalizeBaseUrl(value, fallback) {
    var text = String(value || fallback || "").trim().replace(/\/+$/, "")
    return text || String(fallback || "").trim().replace(/\/+$/, "")
}

function getConsolePublicUrl(environment) {
    var legacyUrl = envUtils.getConfigValue("pb_public_url", DEFAULT_CONSOLE_PUBLIC_URL, environment)
    var configured = envUtils.getConfigValue("ibkr_console_public_url", legacyUrl, environment)
    return normalizeBaseUrl(configured, DEFAULT_CONSOLE_PUBLIC_URL)
}

function getPocketBaseAuthPublicUrl(environment) {
    var configured = envUtils.getConfigValue("pb_auth_public_url", DEFAULT_PB_AUTH_PUBLIC_URL, environment)
    return normalizeBaseUrl(configured, DEFAULT_PB_AUTH_PUBLIC_URL)
}

function getFeishuCallbackBaseUrl(environment) {
    var fallback = getConsolePublicUrl(environment)
    return normalizeBaseUrl(envUtils.getIbkrApiPublicUrl(environment, fallback), fallback)
}

function getFeishuCallbackUrl(environment) {
    return getFeishuCallbackBaseUrl(environment) + "/webhook/feishu/callback"
}

module.exports = {
    DEFAULT_CONSOLE_PUBLIC_URL,
    DEFAULT_PB_AUTH_PUBLIC_URL,
    getConsolePublicUrl,
    getPocketBaseAuthPublicUrl,
    getFeishuCallbackBaseUrl,
    getFeishuCallbackUrl,
}
