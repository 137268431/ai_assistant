/// <reference path="./pb_data/types.d.ts" />

/**
 * feishu.pb.js
 * 飞书卡片回调兼容入口；真实处理已迁到 ibkr-api。
 */

console.log("[FeishuPB] Hook 文件开始加载...")

routerAdd("POST", "/webhook/feishu/callback", (c) => {
    const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const reqInfo = c.requestInfo()
    const body = reqInfo.body || reqInfo.data || {}
    return proxyIbkrApiJson(c, "/webhook/feishu/callback", {
        method: "POST",
        environment: getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT),
        body: body,
        timeout: 15,
    })
})

console.log("[FeishuPB] Hook 文件加载完成")
