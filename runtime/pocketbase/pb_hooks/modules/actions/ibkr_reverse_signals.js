/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_reverse_signals.pb.js
 * 逆向信号兼容入口；
 * 真实处理已迁到 ibkr-api，这里只保留 PocketBase 注册壳。
 */

var getReverseSignalsUtils = function() {
  return require(`${__hooks}/lib/reverse_utils.js`)
}

var getReverseSignalsNotifier = function() {
  return require(`${__hooks}/lib/feishu_reverse.js`)
}

var normalizeReverseEnvironment = function(value) {
  const { normalizeRuntimeEnvironment, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
  return normalizeRuntimeEnvironment(value || "", LIVE_ENVIRONMENT)
}

var getReverseRequestEnvironment = function(c) {
  const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
  return getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
}

var getReverseDataEnvironment = function(data) {
  const { getRuntimeEnvironmentFromData, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
  return getRuntimeEnvironmentFromData(data, LIVE_ENVIRONMENT)
}

function parseTriggeredSignals(value) {
  if (Array.isArray(value)) return value
  if (typeof value === "string" && value) {
    try {
      const parsed = JSON.parse(value)
      if (Array.isArray(parsed)) return parsed
    } catch (_) {}
    return value.split(",").map((item) => String(item || "").trim()).filter(Boolean)
  }
  return []
}

function getThreshold(environment) {
  let threshold = 6
  try {
    const { getConfigValue } = require(`${__hooks}/lib/environment.js`)
    threshold = parseInt(getConfigValue("reverse_signal_threshold", "6", environment)) || 6
  } catch (e) {
    console.log(`[ReverseSignal] reverse_signal_threshold 配置读取失败，使用默认值 6: ${e}`)
  }
  return threshold
}

function mapStrength(score) {
  if (score >= 6) return "strong"
  if (score >= 3) return "medium"
  return "weak"
}

function resolveIndicatorAction(score, targetState, forcedActionType) {
  if (forcedActionType) return forcedActionType
  if (targetState === "pending_entry") return "cancel"
  if (score >= 6) return "close"
  if (score >= 3) return "adjust_sl"
  return "cancel"
}

function loadIndicators(symbol, environment) {
  const runtimeEnvironment = normalizeReverseEnvironment(environment)
  const records = $app.findRecordsByFilter(
    "ibkr_indicators",
    "(environment = {:env} || environment = '') && symbol = {:symbol}",
    "-bar_time_ms",
    1,
    0,
    { env: runtimeEnvironment, symbol: symbol }
  )
  return records && records.length > 0 ? records[0] : null
}

function buildIndicatorAnalysis(symbol, direction, indicatorRecord) {
  const reverseUtils = getReverseSignalsUtils()
  const extra = reverseUtils.parseJsonObject(indicatorRecord.get("extra"))
  const crsi = Number(extra.crsi)
  const obvRsi = Number(extra.obv_rsi)
  const vwapDist = Number(extra.vwap_dist)
  const close = Number(extra.close)

  let score = 0
  const triggeredSignals = []

  if (direction === "long" && crsi > 70) {
    score += 2
    triggeredSignals.push("cRSI超买")
  } else if (direction === "short" && crsi < 30) {
    score += 2
    triggeredSignals.push("cRSI超卖")
  }

  const isBearSignal = direction === "long"
  const crsiReverseDiv = isBearSignal ? extra.crsi_bear_div : extra.crsi_bull_div
  const obvReverseDiv = isBearSignal ? extra.obv_bear_div : extra.obv_bull_div
  const fractalReverse = isBearSignal ? extra.fractal_bear : extra.fractal_bull
  const sdChannelReverse = isBearSignal ? extra.sd_upper : extra.sd_lower
  const emaTouchReverse = isBearSignal ? extra.ema_bear_touch : extra.ema_bull_touch

  if (crsiReverseDiv || obvReverseDiv) {
    score += 3
    triggeredSignals.push("背离")
  }

  if (fractalReverse || sdChannelReverse) {
    score += 2
    triggeredSignals.push("分形/SD通道")
  }

  if ((vwapDist || vwapDist === 0) && Math.abs(vwapDist) > 2 || emaTouchReverse) {
    score += 1
    triggeredSignals.push("VWAP偏离/EMA触碰")
  }

  return {
    score: score,
    triggered_signals: triggeredSignals,
    indicator_extra: extra,
    crsi: crsi,
    obv_rsi: obvRsi,
    vwap_dist: vwapDist,
    close: close,
  }
}

function buildReverseResponse(record, created, duplicate) {
  const reverseUtils = getReverseSignalsUtils()
  return {
    success: true,
    created: !!created,
    duplicate: !!duplicate,
    signal: reverseUtils.normalizeReverseRecord(record),
  }
}

function listReverseRecords(dateText, maxItems, environment) {
  const reverseUtils = getReverseSignalsUtils()
  const { COLLECTIONS } = require(`${__hooks}/lib/collections.js`)
  const limit = Math.max(1, Math.min(Number(maxItems) || 200, 500))
  const runtimeEnvironment = normalizeReverseEnvironment(environment)
  if (dateText) {
    const range = reverseUtils.buildDateRange(dateText)
    if (range) {
      return $app.findRecordsByFilter(
        COLLECTIONS.REVERSE_SIGNALS,
        "environment = {:env} && bar_time_ms >= {:start} && bar_time_ms <= {:end}",
        "-created",
        limit,
        0,
        { env: runtimeEnvironment, start: range.start_ms, end: range.end_ms }
      ) || []
    }
  }
  return $app.findRecordsByFilter(COLLECTIONS.REVERSE_SIGNALS, "environment = {:env}", "-created", limit, 0, { env: runtimeEnvironment }) || []
}

// GET /api/custom/ibkr/reverse/list - 获取某日反转信号列表
routerAdd("GET", "/api/custom/ibkr/reverse/list", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
  const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
  return proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/list", {
    method: "GET",
    environment: environment,
    query: {
      date: c.request.url.query().get("date") || "",
      symbol: c.request.url.query().get("symbol") || "",
      status: c.request.url.query().get("status") || "",
      limit: c.request.url.query().get("limit") || "",
    },
    timeout: 20,
  })
})

// POST /api/custom/ibkr/reverse/calculate - 从 ibkr_indicators 计算逆向信号并写入
routerAdd("POST", "/api/custom/ibkr/reverse/calculate", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const reqInfo = c.requestInfo()
  const body = reqInfo.body || reqInfo.data || {}
  return proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/calculate", {
    method: "POST",
    environment: getReverseDataEnvironment(body),
    body: body,
    timeout: 30,
  })
})

// GET /api/custom/ibkr/reverse/pending - 获取未处理的逆向信号
routerAdd("GET", "/api/custom/ibkr/reverse/pending", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const environment = getReverseRequestEnvironment(c)
  return proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/pending", {
    method: "GET",
    environment: environment,
    query: {
      limit: c.request.url.query().get("limit") || "",
    },
    timeout: 20,
  })
})

// POST /api/custom/ibkr/reverse/dispatch - 页面触发执行/取消
routerAdd("POST", "/api/custom/ibkr/reverse/dispatch", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const reqInfo = c.requestInfo()
  const body = reqInfo.body || reqInfo.data || {}
  return proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/dispatch", {
    method: "POST",
    environment: getReverseDataEnvironment(body),
    body: body,
    timeout: 30,
  })
})

// POST /api/custom/ibkr/reverse/ack - IBKR 回写处理结果
routerAdd("POST", "/api/custom/ibkr/reverse/ack", (c) => {
  const { proxyIbkrApiJson } = require(`${__hooks}/lib/system/api_proxy.js`)
  const reqInfo = c.requestInfo()
  const body = reqInfo.body || reqInfo.data || {}
  return proxyIbkrApiJson(c, "/api/custom/ibkr/reverse/ack", {
    method: "POST",
    environment: getReverseDataEnvironment(body),
    body: body,
    timeout: 30,
  })
})
