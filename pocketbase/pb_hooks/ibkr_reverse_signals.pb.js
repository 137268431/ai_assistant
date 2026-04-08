/// <reference path="./pb_data/types.d.ts" />

/**
 * ibkr_reverse_signals.pb.js
 * 逆向信号计算、查询、调度、回写 API
 */

var getReverseSignalsUtils = function() {
  return require(`${__hooks}/lib/reverse_utils.js`)
}

var getReverseSignalsNotifier = function() {
  return require(`${__hooks}/lib/feishu_reverse.js`)
}

var getCollectionRegistry = function() {
  return require(`${__hooks}/lib/collections.js`)
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
  const { COLLECTIONS } = getCollectionRegistry()
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
  try {
    const reverseUtils = require(`${__hooks}/lib/reverse_utils.js`)
    const { COLLECTIONS } = getCollectionRegistry()
    const { getRuntimeEnvironmentFromRequest, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const dateText = c.request.url.query().get("date") || ""
    const environment = getRuntimeEnvironmentFromRequest(c, LIVE_ENVIRONMENT)
    const symbol = String(c.request.url.query().get("symbol") || "").toUpperCase()
    const statusFilter = String(c.request.url.query().get("status") || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean)
    const limit = Math.max(1, Math.min(Number(c.request.url.query().get("limit")) || 200, 500))
    const runtimeEnvironment = environment
    let records = []
    if (dateText) {
      const range = reverseUtils.buildDateRange(dateText)
      if (range) {
        records = $app.findRecordsByFilter(
          COLLECTIONS.REVERSE_SIGNALS,
          "environment = {:env} && bar_time_ms >= {:start} && bar_time_ms <= {:end}",
          "-created",
          limit,
          0,
          { env: runtimeEnvironment, start: range.start_ms, end: range.end_ms }
        ) || []
      }
    }
    if (!records.length) {
      records = $app.findRecordsByFilter(COLLECTIONS.REVERSE_SIGNALS, "environment = {:env}", "-created", limit, 0, { env: runtimeEnvironment }) || []
    }
    const ibkr_signals = records
      .map((record) => reverseUtils.normalizeReverseRecord(record))
      .filter((signal) => !symbol || signal.symbol === symbol)
      .filter((signal) => statusFilter.length === 0 || statusFilter.includes(signal.status))

    return c.json(200, { ibkr_signals: ibkr_signals })
  } catch (err) {
    console.error("Error listing reverse ibkr_signals:", err)
    return c.json(500, { error: err.message })
  }
})

// POST /api/custom/ibkr/reverse/calculate - 从 ibkr_indicators 计算逆向信号并写入
routerAdd("POST", "/api/custom/ibkr/reverse/calculate", (c) => {
  const reverseUtils = getReverseSignalsUtils()
  const data = c.requestInfo().body || c.requestInfo().data || {}
  const environment = getReverseDataEnvironment(data)
  const symbol = String(data.symbol || "").trim().toUpperCase()
  const direction = String(data.direction || "").trim().toLowerCase()
  const forcedActionType = String(data.force_action_type || data.action_type || "").trim()
  const allowedActionTypes = ["cancel", "close", "adjust_sl", "adjust_tp"]

  if (!symbol || !direction) {
    return c.json(400, { error: "Missing symbol or direction" })
  }

  if (!["long", "short"].includes(direction)) {
    return c.json(400, { error: "Invalid direction" })
  }

  if (forcedActionType && !allowedActionTypes.includes(forcedActionType)) {
    return c.json(400, { error: `Invalid force_action_type: ${forcedActionType}` })
  }

  try {
    const activeOrder = reverseUtils.findLatestActiveEntryOrder(symbol, direction, environment)
    const orderContext = reverseUtils.buildOrderContext(activeOrder)

    if (!orderContext || (orderContext.direction && orderContext.direction !== direction)) {
      return c.json(200, {
        success: true,
        created: false,
        reason: "no_conflict_target",
        signal: null,
        analysis: {
          symbol: symbol,
          direction: direction,
          source: "indicator",
          target_state: "",
          strength: "weak",
          score: 0,
          action_type: forcedActionType || "cancel",
          triggered_signals: [],
        }
      })
    }

    if ((forcedActionType === "adjust_sl" || forcedActionType === "adjust_tp" || forcedActionType === "close") && orderContext.target_state !== "filled_position") {
      return c.json(400, { error: `Action ${forcedActionType} requires filled_position` })
    }
    if (forcedActionType === "cancel" && orderContext.target_state !== "pending_entry") {
      return c.json(400, { error: "Action cancel requires pending_entry" })
    }

    const indicatorRecord = loadIndicators(symbol, environment)
    if (!indicatorRecord) {
      return c.json(404, { error: "No ibkr_indicators found for symbol" })
    }

    const analysis = buildIndicatorAnalysis(symbol, direction, indicatorRecord)
    const scoreOverride = data.score_override != null ? Number(data.score_override) : null
    const score = scoreOverride != null && !isNaN(scoreOverride) ? scoreOverride : analysis.score
    const triggeredSignals = parseTriggeredSignals(data.triggered_signals)
    const effectiveTriggeredSignals = triggeredSignals.length > 0
      ? triggeredSignals
      : (analysis.triggered_signals.length > 0 ? analysis.triggered_signals : (forcedActionType ? [forcedActionType] : []))
    const strength = mapStrength(score)
    const actionType = resolveIndicatorAction(score, orderContext.target_state, forcedActionType)

    if ((!forcedActionType && score <= 0) || effectiveTriggeredSignals.length === 0) {
      return c.json(200, {
        success: true,
        created: false,
        reason: "no_reverse_conditions",
        signal: null,
        analysis: {
          symbol: symbol,
          direction: direction,
          source: "indicator",
          target_state: orderContext.target_state,
          strength: strength,
          score: score,
          action_type: actionType,
          triggered_signals: effectiveTriggeredSignals,
        }
      })
    }

    const extraData = {
      ...analysis.indicator_extra,
      current_direction: direction,
      reverse_kind: "indicator_conflict",
      target_state: orderContext.target_state,
      order_status: orderContext.order_status,
      relation_status: orderContext.relation_status,
      position_side: orderContext.position_side,
      origin_signal_id: String(data.origin_signal_id || data.signal_id || "").trim(),
      signal_id: orderContext.signal_id || "",
      order_unique_id: orderContext.order_unique_id || "",
      broker_order_id: orderContext.broker_order_id || "",
      order_id: orderContext.broker_order_id || "",
      trade_group_id: orderContext.trade_group_id || "",
      entry_order_unique_id: orderContext.entry_order_unique_id || "",
      entry_price: orderContext.entry_price || analysis.close || 0,
      quantity: orderContext.quantity || 0,
      take_profit: orderContext.take_profit || 0,
      stop_loss: orderContext.stop_loss || 0,
      crsi: analysis.crsi,
      obv_rsi: analysis.obv_rsi,
      vwap_dist: analysis.vwap_dist,
      close: analysis.close,
      manual_override: !!forcedActionType,
    }

    const upsertResult = reverseUtils.upsertReverseRecord({
      environment: environment,
      symbol: symbol,
      direction: direction,
      source: "indicator",
      priority: data.priority != null ? Number(data.priority) : 5,
      strength: strength,
      score: score,
      triggered_signals: effectiveTriggeredSignals,
      action_type: actionType,
      status: "pending",
      extra: extraData,
      bar_time_ms: indicatorRecord.get("bar_time_ms"),
      us_time: indicatorRecord.get("us_time") || "",
      cn_time: indicatorRecord.get("cn_time") || "",
    })

    const threshold = getThreshold(environment)
    if (score >= threshold) {
      try {
        const { notifyReverseSignal } = getReverseSignalsNotifier()
        notifyReverseSignal(upsertResult.record, {
          message: upsertResult.created ? "检测到指标反转信号，等待 IBKR 执行" : "检测到重复指标反转信号，已刷新现有记录"
        })
      } catch (notifyErr) {
        console.error("[ReverseSignal] 飞书通知失败:", notifyErr)
      }
    }

    return c.json(200, buildReverseResponse(upsertResult.record, upsertResult.created, !upsertResult.created))
  } catch (err) {
    console.error("Error calculating reverse signal:", err)
    return c.json(500, { error: err.message })
  }
})

// GET /api/custom/ibkr/reverse/pending - 获取未处理的逆向信号
routerAdd("GET", "/api/custom/ibkr/reverse/pending", (c) => {
  try {
    const reverseUtils = getReverseSignalsUtils()
    const { COLLECTIONS } = getCollectionRegistry()
    const environment = getReverseRequestEnvironment(c)
    const records = $app.findRecordsByFilter(
      COLLECTIONS.REVERSE_SIGNALS,
      "status = 'pending' && environment = {:env}",
      "-priority,-bar_time_ms",
      200,
      0,
      { env: environment }
    ) || []

    return c.json(200, {
      ibkr_signals: records.map((record) => reverseUtils.normalizeReverseRecord(record))
    })
  } catch (err) {
    console.error("Error fetching pending reverse ibkr_signals:", err)
    return c.json(500, { error: err.message })
  }
})

// POST /api/custom/ibkr/reverse/dispatch - 页面触发执行/取消
routerAdd("POST", "/api/custom/ibkr/reverse/dispatch", (c) => {
  const reverseUtils = getReverseSignalsUtils()
  const { COLLECTIONS } = getCollectionRegistry()
  const data = c.requestInfo().body || c.requestInfo().data || {}
  const reverseId = String(data.reverse_id || data.signal_id || "").trim()
  const action = String(data.action || "").trim()
  const reason = String(data.reason || "").trim()

  if (!reverseId) {
    return c.json(400, { error: "Missing reverse_id" })
  }
  if (!["execute", "cancel"].includes(action)) {
    return c.json(400, { error: "Invalid action" })
  }

  try {
    const record = $app.findRecordById(COLLECTIONS.REVERSE_SIGNALS, reverseId)
    if (!record) {
      return c.json(404, { error: "Reverse signal not found" })
    }

    const currentStatus = String(record.get("status") || "")
    if (currentStatus !== "pending") {
      return c.json(200, { success: true, signal: reverseUtils.normalizeReverseRecord(record) })
    }

    const extra = reverseUtils.getReverseExtra(record)
    const now = new Date().toISOString()

    if (action === "cancel") {
      record.set("status", "cancelled")
      record.set("reason", reason || "页面取消反转信号")
      record.set("processed_time", now)
      record.set("extra", {
        ...extra,
        dispatch_action: "cancel",
        dispatch_source: "page",
        result_status: "cancelled_by_page",
      })
      $app.save(record)
      try {
        const { notifyReverseStatus } = getReverseSignalsNotifier()
        notifyReverseStatus("cancel", record, {
          message: reason || "页面已取消该反转信号"
        })
      } catch (notifyErr) {
        console.error("[ReverseDispatch] 取消卡片同步失败:", notifyErr)
      }
      return c.json(200, { success: true, signal: reverseUtils.normalizeReverseRecord(record) })
    }

    record.set("priority", Math.min(Number(record.get("priority") || 5), 1))
    record.set("extra", {
      ...extra,
      manual_requested: true,
      manual_requested_at: now,
      manual_requested_source: "page",
      manual_requested_reason: reason || "",
      dispatch_action: "execute",
      dispatch_source: "page",
    })
    if (reason) {
      record.set("reason", reason)
    }
    $app.save(record)

    try {
      const { notifyReverseStatus } = getReverseSignalsNotifier()
      notifyReverseStatus("execute_request", record, {
        message: reason || "已请求 IBKR 优先执行该反转动作"
      })
    } catch (notifyErr) {
      console.error("[ReverseDispatch] 执行请求卡片同步失败:", notifyErr)
    }

    return c.json(200, { success: true, signal: reverseUtils.normalizeReverseRecord(record) })
  } catch (err) {
    console.error("Error dispatching reverse signal:", err)
    return c.json(500, { error: err.message })
  }
})

// POST /api/custom/ibkr/reverse/ack - IBKR 回写处理结果
routerAdd("POST", "/api/custom/ibkr/reverse/ack", (c) => {
  const reverseUtils = getReverseSignalsUtils()
  const { COLLECTIONS } = getCollectionRegistry()
  const data = c.requestInfo().body || c.requestInfo().data || {}
  const reverseId = String(data.signal_id || data.reverse_id || "").trim()
  const status = String(data.status || "confirmed").trim()
  const reason = String(data.reason || "").trim()

  if (!reverseId) {
    return c.json(400, { error: "Missing signal_id" })
  }

  try {
    const record = $app.findRecordById(COLLECTIONS.REVERSE_SIGNALS, reverseId)
    const extra = reverseUtils.getReverseExtra(record)
    const mergedExtra = {
      ...extra,
      broker_order_id: data.broker_order_id || data.order_id || extra.broker_order_id || extra.order_id || "",
      order_id: data.order_id || data.broker_order_id || extra.order_id || extra.broker_order_id || "",
      order_unique_id: data.order_unique_id || extra.order_unique_id || "",
      trade_group_id: data.trade_group_id || extra.trade_group_id || "",
      entry_order_unique_id: data.entry_order_unique_id || extra.entry_order_unique_id || "",
      signal_id: data.signal_id_orig || data.origin_signal_id || extra.signal_id || "",
      origin_signal_id: data.origin_signal_id || extra.origin_signal_id || "",
      current_direction: data.current_direction || extra.current_direction || "",
      new_direction: data.new_direction || extra.new_direction || "",
      executed_action: data.executed_action || data.action_type || extra.executed_action || "",
      result_status: data.result_status || extra.result_status || "",
      old_sl: data.old_sl != null ? Number(data.old_sl) : extra.old_sl,
      new_sl: data.new_sl != null ? Number(data.new_sl) : extra.new_sl,
      old_tp: data.old_tp != null ? Number(data.old_tp) : extra.old_tp,
      new_tp: data.new_tp != null ? Number(data.new_tp) : extra.new_tp,
      entry_price: data.entry_price != null ? Number(data.entry_price) : extra.entry_price,
      quantity: data.quantity != null ? Number(data.quantity) : extra.quantity,
      take_profit: data.take_profit != null ? Number(data.take_profit) : extra.take_profit,
      stop_loss: data.stop_loss != null ? Number(data.stop_loss) : extra.stop_loss,
      target_state: data.target_state || extra.target_state || "",
      target_order_status: data.target_order_status || data.order_status || extra.target_order_status || extra.order_status || "",
      relation_status: data.relation_status || extra.relation_status || "",
      position_side: data.position_side || extra.position_side || "",
      manual_requested: false,
      manual_requested_at: extra.manual_requested_at || "",
    }

    record.set("status", status)
    record.set("reason", reason)
    record.set("processed_time", new Date().toISOString())
    record.set("extra", mergedExtra)

    $app.save(record)

    try {
      const { notifyReverseStatus } = getReverseSignalsNotifier()
      notifyReverseStatus("ack", record, {
        message: reason || `IBKR 已回写 ${status}`
      })
    } catch (notifyErr) {
      console.error("[ReverseAck] 卡片同步失败:", notifyErr)
    }

    return c.json(200, {
      success: true,
      signal: reverseUtils.normalizeReverseRecord(record),
    })
  } catch (err) {
    console.error("Error acknowledging reverse signal:", err)
    return c.json(500, { error: err.message })
  }
})
