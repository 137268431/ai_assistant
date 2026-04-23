/// <reference path="./pb_data/types.d.ts" />

/**
 * order_manage.pb.js
 * 订单管理兼容入口；
 * 真实处理已迁到 ibkr-api，这里只保留 PocketBase 注册壳。
 */

// POST /api/custom/ibkr/orders/upsert - 订单 upsert，自动写入 ibkr_order_details
routerAdd("POST", "/api/custom/ibkr/orders/upsert", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const envUtils = require(`${__hooks}/lib/environment.js`)
  const reqInfo = c.requestInfo()
  const body = reqInfo.body || reqInfo.data || {}
  return proxyIbkrApiJson(c, "/api/custom/ibkr/orders/upsert", {
    method: "POST",
    environment: envUtils.getRuntimeEnvironmentFromData(body, envUtils.LIVE_ENVIRONMENT),
    body: body,
    timeout: 30,
  })
})

// POST /api/custom/ibkr/orders/reconcile - 回补 orders 缺失的 ibkr_order_details
routerAdd("POST", "/api/custom/ibkr/orders/reconcile", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const envUtils = require(`${__hooks}/lib/environment.js`)
  const reqInfo = c.requestInfo()
  const body = reqInfo.body || reqInfo.data || {}
  return proxyIbkrApiJson(c, "/api/custom/ibkr/orders/reconcile", {
    method: "POST",
    environment: envUtils.getRuntimeEnvironmentFromData(body, envUtils.LIVE_ENVIRONMENT),
    body: body,
    timeout: 45,
  })
})
