/// <reference path="./pb_data/types.d.ts" />

/**
 * webhook_tv.pb.js
 * TradingView Webhook 兼容入口；真实处理已迁到 ibkr-api。
 */

routerAdd("POST", "/webhook/tv", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const body = reqInfo.body || reqInfo.data || {}
    return proxyIbkrApiJson(c, "/webhook/tv", {
        method: "POST",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        body: body,
        timeout: 15,
    })
})
