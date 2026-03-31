/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_actions.pb.js
 * 信号确认/取消接口（供飞书按钮直接调用）
 */

console.log("[SignalActions] Hook 文件开始加载...");

routerAdd("GET", "/webhook/signal/confirm", (c) => {
    const { appendOrderDetail } = require(`${__hooks}/lib/order_events.js`)
    const { getSignalExtra, mergeSignalExtra, notifySignalStatus } = require(`${__hooks}/lib/feishu_signal.js`)
    const { ok, warn, fail, info } = require(`${__hooks}/lib/_page.js`)
    const signalId = c.request.url.query().get("id") || ""
    if (!signalId) {
        return c.html(400, fail("参数错误", "缺少信号ID"))
    }
    try {
        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || signalId
        const statusHints = {
            expired:   { fn: fail,   title: "信号已过期",   msg: "该信号超时自动失效，无法操作" },
            rejected:  { fn: fail,   title: "信号已拒绝",   msg: "该信号已被拒绝，无法重复操作" },
            executed:  { fn: ok,     title: "信号已执行",   msg: "该信号已执行，无需重复确认" },
            pending:   { fn: ok,     title: "信号已确认",   msg: "该信号已确认，无需重复操作" },
        }
        if (currentStatus !== "pending" && currentStatus !== "awaiting_confirm") {
            const h = statusHints[currentStatus] || { fn: warn, title: "无法操作", msg: "状态: " + currentStatus }
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        record.set("status", "pending")
        $app.save(record)
        try {
            const signalExtra = getSignalExtra(record)
            const syncResult = notifySignalStatus("pending", record, {
                messageId: signalExtra.feishu_signal_message_id || "",
                message: "信号已确认，等待执行",
            })
            if (syncResult.success && syncResult.message_id && syncResult.message_id !== signalExtra.feishu_signal_message_id) {
                mergeSignalExtra(record, {
                    feishu_signal_message_id: syncResult.message_id,
                    feishu_signal_card_version: 1,
                }, true)
            }
        } catch (syncErr) {
            console.error("[SignalAction] 同步确认卡片失败:", syncErr)
        }
        return c.html(200, ok("信号已确认", "确认成功", symbol))
    } catch (err) {
        console.error("[SignalAction] 确认失败:", err)
        return c.html(404, fail("信号不存在", "找不到信号", signalId))
    }
})

routerAdd("GET", "/webhook/signal/cancel", (c) => {
    const { getSignalExtra, mergeSignalExtra, notifySignalStatus } = require(`${__hooks}/lib/feishu_signal.js`)
    const { ok, warn, fail, info } = require(`${__hooks}/lib/_page.js`)
    const signalId = c.request.url.query().get("id") || ""
    if (!signalId) {
        return c.html(400, fail("参数错误", "缺少信号ID"))
    }
    try {
        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || signalId
        const statusHints = {
            expired:   { fn: fail,   title: "信号已过期",   msg: "该信号超时自动失效，无法操作" },
            rejected:  { fn: fail,   title: "信号已拒绝",   msg: "该信号已被拒绝，无法重复操作" },
            executed:  { fn: warn,   title: "信号已执行",   msg: "信号已执行，无法取消" },
            pending:   { fn: warn,   title: "信号已确认",   msg: "该信号已确认，无法取消" },
        }
        if (currentStatus !== "pending" && currentStatus !== "awaiting_confirm") {
            const h = statusHints[currentStatus] || { fn: warn, title: "无法操作", msg: "状态: " + currentStatus }
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        record.set("status", "rejected")
        $app.save(record)
        try {
            const signalExtra = getSignalExtra(record)
            const syncResult = notifySignalStatus("rejected", record, {
                messageId: signalExtra.feishu_signal_message_id || "",
                message: "信号已拒绝，暂不执行",
            })
            if (syncResult.success && syncResult.message_id && syncResult.message_id !== signalExtra.feishu_signal_message_id) {
                mergeSignalExtra(record, {
                    feishu_signal_message_id: syncResult.message_id,
                    feishu_signal_card_version: 1,
                }, true)
            }
        } catch (syncErr) {
            console.error("[SignalAction] 同步拒绝卡片失败:", syncErr)
        }
        return c.html(200, fail("信号已拒绝", "拒绝成功", symbol))
    } catch (err) {
        console.error("[SignalAction] 取消失败:", err)
        return c.html(404, fail("信号不存在", "找不到信号", signalId))
    }
})

// ── QC 信号拉取 & 确认 ──

routerAdd("GET", "/api/custom/signals/pending", (c) => {
  try {
    const dateStr = c.request.url.query().get("date") || "";
    const indicatorCache = {}
    console.log(`[SignalsPending] === 查询待执行信号 ===`);
    console.log(`[SignalsPending] date 参数: "${dateStr}"`);

    if (!dateStr) {
      return c.json(400, { error: "缺少 date 参数" });
    }

    function parseObject(value) {
      if (!value) return {}
      if (typeof value === "string") {
        try {
          const parsed = JSON.parse(value)
          return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}
        } catch (_) {
          return {}
        }
      }
      return (typeof value === "object" && !Array.isArray(value)) ? value : {}
    }

    function normalizeIndicatorRecord(record) {
      if (!record) return null
      const extra = parseObject(record.get("extra"))
      return {
        id: record.id,
        symbol: record.get("symbol"),
        exchange: record.get("exchange"),
        interval: record.get("interval"),
        script_tag: record.get("script_tag"),
        us_time: record.get("us_time"),
        cn_time: record.get("cn_time"),
        bar_time_ms: record.get("bar_time_ms"),
        bar_index: record.get("bar_index"),
        created: record.get("created"),
        updated: record.get("updated"),
        ...extra,
      }
    }

    function findLatestIndicator(signalRecord) {
      const symbol = String(signalRecord.get("symbol") || "").trim().toUpperCase()
      const preferredInterval = String(signalRecord.get("chart_tf") || signalRecord.get("interval") || "").trim()
      const signalBarTimeMs = Number(signalRecord.get("bar_time_ms") || 0)
      const cacheKey = `${symbol}|${preferredInterval}|${signalBarTimeMs || 0}`

      if (Object.prototype.hasOwnProperty.call(indicatorCache, cacheKey)) {
        return indicatorCache[cacheKey]
      }

      function query(filter, params) {
        const rows = $app.findRecordsByFilter("indicators", filter, "-bar_time_ms", 1, 0, params)
        return rows && rows.length ? rows[0] : null
      }

      let indicatorRecord = null
      if (symbol && preferredInterval && signalBarTimeMs > 0) {
        indicatorRecord = query(
          "symbol = {:sym} && interval = {:tf} && bar_time_ms <= {:ms}",
          { sym: symbol, tf: preferredInterval, ms: signalBarTimeMs }
        )
      }
      if (!indicatorRecord && symbol && preferredInterval) {
        indicatorRecord = query(
          "symbol = {:sym} && interval = {:tf}",
          { sym: symbol, tf: preferredInterval }
        )
      }
      if (!indicatorRecord && symbol && signalBarTimeMs > 0) {
        indicatorRecord = query(
          "symbol = {:sym} && bar_time_ms <= {:ms}",
          { sym: symbol, ms: signalBarTimeMs }
        )
      }
      if (!indicatorRecord && symbol) {
        indicatorRecord = query(
          "symbol = {:sym}",
          { sym: symbol }
        )
      }

      const normalized = normalizeIndicatorRecord(indicatorRecord)
      indicatorCache[cacheKey] = normalized
      return normalized
    }

    function enrichSignalExtra(extra, indicator) {
      const merged = { ...extra }
      if (!indicator) return merged

      const indicatorKeys = [
        "close",
        "day_change_pct",
        "prev_close_change_pct",
        "change_7d",
        "atr",
        "atr_pct",
        "sl_dist_pct",
        "sl_atr_ratio",
        "trend_dir",
        "ema_bullish",
        "ema_bearish",
        "ema_bull_touch",
        "ema_bear_touch",
        "fractal_bull",
        "fractal_bear",
        "crsi",
        "obv_rsi",
        "dtp_dir",
        "dtp_phase",
        "dtp_phase_bars",
        "sd_zone",
        "sd_trend",
        "vwap",
        "vwap_dist",
        "vwap_bullish",
      ]

      indicatorKeys.forEach((key) => {
        if ((merged[key] == null || merged[key] === "") && indicator[key] != null) {
          merged[key] = indicator[key]
        }
      })

      merged.latest_indicator_id = indicator.id
      return merged
    }

    // 先查所有 pending 状态的信号（不看 date），看数据库里有什么
    const allPending = $app.findRecordsByFilter(
      "signals",
      `status = 'pending'`,
      "-bar_time_ms",
      100,
      0
    );
    console.log(`[SignalsPending] 数据库中全部 pending 信号数: ${allPending.length}`);
    if (allPending.length > 0) {
      const sample = allPending[0];
      console.log(`[SignalsPending] 示例信号 date 字段: "${sample.get("date")}" (类型: ${typeof sample.get("date")})`);
      console.log(`[SignalsPending] 示例信号 status: "${sample.get("status")}"`);
      console.log(`[SignalsPending] 示例信号 signal_id: "${sample.get("signal_id")}"`);
    }

    // 执行带 date 过滤的查询
    const records = $app.findRecordsByFilter(
      "signals",
      `status = 'pending' && date = {:d}`,
      "-bar_time_ms",
      100,
      0,
      { d: dateStr }
    );
    console.log(`[SignalsPending] date="${dateStr}" 过滤后命中数: ${records.length}`);

    const signals = records.map((r) => {
      const signalExtra = parseObject(r.get("extra"))
      const latestIndicator = findLatestIndicator(r)
      const extra = enrichSignalExtra(signalExtra, latestIndicator)
      return {
        id: r.id,
        signal_id: r.get("signal_id"),
        symbol: r.get("symbol"),
        direction: r.get("direction"),
        signal: r.get("signal"),
        entry: r.get("entry"),
        stop_loss: r.get("stop_loss"),
        take_profit: r.get("take_profit"),
        limit_price: r.get("limit_price"),
        shares: r.get("shares"),
        rr: r.get("rr"),
        reason: r.get("reason"),
        exchange: r.get("exchange"),
        interval: r.get("interval"),
        chart_tf: r.get("chart_tf"),
        date: r.get("date"),
        us_time: r.get("us_time"),
        cn_time: r.get("cn_time"),
        bar_time_ms: r.get("bar_time_ms"),
        latest_indicator: latestIndicator,
        extra: extra,
        created: r.get("created")
      };
    });

    const matchedIndicators = signals.filter((s) => !!s.latest_indicator).length
    console.log(`[SignalsPending] 返回 signals 数组长度: ${signals.length}`);
    console.log(`[SignalsPending] 已关联指标快照: ${matchedIndicators}/${signals.length}`);
    console.log(`[SignalsPending] === 查询完成 ===`);
    return c.json(200, { signals: signals });
  } catch (err) {
    console.error("[SignalsPending] 查询异常:", err.message);
    return c.json(500, { error: err.message });
  }
});

routerAdd("POST", "/api/custom/signals/ack", (c) => {
  const { appendOrderDetail, mergeOrderExtra, resolveOrderEventTimes, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
  const { getSignalExtra, mergeSignalExtra, notifySignalStatus } = require(`${__hooks}/lib/feishu_signal.js`)
  const { notifyNewOrder, notifyOrder, getOrderStatusInfo, getTradeGroupCardMessageId } = require(`${__hooks}/lib/feishu_order.js`)
  const data = c.requestInfo().body || c.requestInfo().data || {};
  const signalId = data.signal_id;
  const status = data.status || "executed";
  const note = data.note || "";

  // 订单字段（QC 传递）
  const orderData = data.order || {};

  console.log(`[SignalAck] === 开始处理信号确认 ===`);
  console.log(`[SignalAck] signal_id: ${signalId}`);
  console.log(`[SignalAck] status: ${status}`);
  console.log(`[SignalAck] note: ${note}`);
  console.log(`[SignalAck] 订单数据:`, JSON.stringify(orderData, null, 2));

  if (!signalId) {
    console.log(`[SignalAck] 错误: 缺少 signal_id`);
    return c.json(400, { error: "Missing signal_id" });
  }

  try {
    // 1. 更新信号状态
    console.log(`[SignalAck] 查找信号记录: signal_id=${signalId}`);
    const record = $app.findFirstRecordByFilter(
      "signals",
      `signal_id = {:sid}`,
      { sid: signalId }
    );
    const symbol = record.get("symbol");
    console.log(`[SignalAck] 找到信号: symbol=${symbol}`);

    record.set("status", status);
    record.set("note", note);
    $app.save(record);
    console.log(`[SignalAck] 信号状态已更新: ${status}`);
    try {
      const signalExtra = getSignalExtra(record);
      const signalSyncResult = notifySignalStatus(status, record, {
        messageId: signalExtra.feishu_signal_message_id || "",
        message: status === "executed" ? "信号已执行，已创建订单" : "",
      });
      if (signalSyncResult.success && signalSyncResult.message_id && signalSyncResult.message_id !== signalExtra.feishu_signal_message_id) {
        mergeSignalExtra(record, {
          feishu_signal_message_id: signalSyncResult.message_id,
          feishu_signal_card_version: 1,
        }, true);
      }
    } catch (syncErr) {
      console.error("[SignalAck] 同步信号卡片失败:", syncErr);
    }

    function deriveProtectionUniqueIds(entryUniqueId) {
      const base = String(entryUniqueId || "").endsWith("_entry")
        ? String(entryUniqueId || "").slice(0, -6)
        : String(entryUniqueId || "")
      return {
        tp: base ? `${base}_take_profit` : "",
        sl: base ? `${base}_stop_loss` : "",
      }
    }

    function buildAckOrders() {
      if (!(orderData.unique_id && orderData.order_type)) return []

      const orderExtraData = orderData.extra && typeof orderData.extra === "object" ? orderData.extra : {}
      const resolvedQuantity = orderData.quantity != null ? orderData.quantity : (record.get("shares") || orderExtraData.quantity || 0)
      const resolvedLimitPrice = orderData.limit_price != null ? orderData.limit_price : (record.get("entry") || orderExtraData.limit_price || 0)
      const resolvedStopLoss = orderData.stop_loss != null ? orderData.stop_loss : (record.get("stop_loss") || orderExtraData.sl_price || 0)
      const resolvedTakeProfit = orderData.take_profit != null ? orderData.take_profit : (record.get("take_profit") || orderExtraData.tp_price || 0)
      const entryOrderUniqueId = orderData.entry_order_unique_id || orderExtraData.entry_order_unique_id || orderData.unique_id
      const tradeGroupId = orderData.trade_group_id || orderExtraData.trade_group_id || entryOrderUniqueId
      const protectionIds = deriveProtectionUniqueIds(entryOrderUniqueId)
      const childOrdersInput = Array.isArray(data.child_orders)
        ? data.child_orders
        : (Array.isArray(orderData.child_orders) ? orderData.child_orders : [])
      const sharedFields = {
        symbol: symbol,
        direction: orderData.direction || record.get("direction"),
        position_side: orderData.position_side || orderData.direction || record.get("direction") || "",
        quantity: resolvedQuantity,
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        signal_id: signalId,
        order_time: orderData.order_time || orderExtraData.order_time || orderData.us_time || record.get("us_time") || "",
        us_time: orderData.us_time || orderExtraData.us_time || record.get("us_time") || "",
        cn_time: orderData.cn_time || orderExtraData.cn_time || record.get("cn_time") || "",
        bar_time_ms: orderData.bar_time_ms || orderExtraData.bar_time_ms || record.get("bar_time_ms") || 0,
      }

      const entryOrder = {
        ...orderData,
        ...sharedFields,
        unique_id: orderData.unique_id,
        order_type: orderData.order_type || "Entry",
        role: orderData.role || orderExtraData.role || "entry",
        relation_status: orderData.relation_status || orderExtraData.relation_status || "active",
        limit_price: resolvedLimitPrice,
        status: orderData.status || "Init",
        filled_qty: orderData.filled_qty != null ? orderData.filled_qty : 0,
        fill_price: orderData.fill_price != null ? orderData.fill_price : 0,
        stop_loss: resolvedStopLoss,
        take_profit: resolvedTakeProfit,
      }

      function resolveChildInput(kind) {
        for (let i = 0; i < childOrdersInput.length; i++) {
          const child = childOrdersInput[i] || {}
          const role = child.role || ""
          const type = child.order_type || ""
          if (kind === "take_profit" && (role === "take_profit" || role === "repair_tp" || type === "TakeProfit")) return child
          if (kind === "stop_loss" && (role === "stop_loss" || role === "repair_sl" || type === "StopLoss")) return child
        }
        return {}
      }

      function buildChildOrder(kind, defaultPrice, defaultUniqueId, siblingUniqueId) {
        const childInput = resolveChildInput(kind)
        const childExtra = childInput.extra && typeof childInput.extra === "object" ? childInput.extra : {}
        const childPrice = childInput.limit_price != null ? childInput.limit_price : (childExtra.limit_price != null ? childExtra.limit_price : defaultPrice)
        if (!childPrice) return null
        const isTakeProfit = kind === "take_profit"
        return {
          ...childInput,
          ...sharedFields,
          unique_id: childInput.unique_id || childExtra.unique_id || defaultUniqueId,
          order_type: childInput.order_type || (isTakeProfit ? "TakeProfit" : "StopLoss"),
          role: childInput.role || childExtra.role || kind,
          relation_status: childInput.relation_status || childExtra.relation_status || "planned",
          parent_order_unique_id: childInput.parent_order_unique_id || childExtra.parent_order_unique_id || entryOrderUniqueId,
          sibling_order_unique_id: childInput.sibling_order_unique_id || childExtra.sibling_order_unique_id || siblingUniqueId,
          limit_price: childPrice,
          status: childInput.status || childExtra.status || "Init",
          filled_qty: childInput.filled_qty != null ? childInput.filled_qty : 0,
          fill_price: childInput.fill_price != null ? childInput.fill_price : 0,
          broker_order_id: childInput.broker_order_id || childInput.order_id || "",
        }
      }

      const orders = [entryOrder]
      const tpOrder = buildChildOrder("take_profit", resolvedTakeProfit, protectionIds.tp, protectionIds.sl)
      const slOrder = buildChildOrder("stop_loss", resolvedStopLoss, protectionIds.sl, protectionIds.tp)
      if (tpOrder) orders.push(tpOrder)
      if (slOrder) orders.push(slOrder)
      return orders
    }

    function upsertAckOrder(orderPayload) {
      console.log(`[SignalAck] === 创建订单记录 === ${orderPayload.unique_id} / ${orderPayload.order_type}`)
      const ordersCol = $app.findCollectionByNameOrId("orders")
      let orderRecord = null
      let previousOrderStatus = ""
      try {
        const existing = $app.findRecordsByFilter(
          "orders",
          `unique_id = {:uniqueId}`,
          "",
          1,
          0,
          { uniqueId: orderPayload.unique_id }
        )
        if (existing.length > 0) {
          orderRecord = existing[0]
          previousOrderStatus = orderRecord.get("status") || ""
        }
      } catch (_) {}

      const orderExtraData = orderPayload.extra && typeof orderPayload.extra === "object" ? orderPayload.extra : {}
      const resolvedStatus = orderPayload.status || "Init"
      const resolvedQuantity = orderPayload.quantity != null ? orderPayload.quantity : (record.get("shares") || orderExtraData.quantity || 0)
      const resolvedLimitPrice = orderPayload.limit_price != null ? orderPayload.limit_price : (record.get("entry") || orderExtraData.limit_price || 0)
      const resolvedStopLoss = orderPayload.stop_loss != null ? orderPayload.stop_loss : (record.get("stop_loss") || orderExtraData.sl_price || 0)
      const resolvedTakeProfit = orderPayload.take_profit != null ? orderPayload.take_profit : (record.get("take_profit") || orderExtraData.tp_price || 0)
      const brokerOrderId = orderPayload.broker_order_id || orderPayload.order_id || ""
      const eventTimes = resolveOrderEventTimes({
        us_time: orderPayload.us_time || orderExtraData.us_time,
        cn_time: orderPayload.cn_time || orderExtraData.cn_time,
        bar_time_ms: orderPayload.bar_time_ms || orderExtraData.bar_time_ms,
      }, {
        us_time: record.get("us_time") || "",
        cn_time: record.get("cn_time") || "",
        bar_time_ms: record.get("bar_time_ms") || 0,
      })
      const resolvedOrderTime = orderPayload.order_time || orderExtraData.order_time || eventTimes.us_time

      if (!orderRecord) {
        orderRecord = new Record(ordersCol, {})
        orderRecord.set("unique_id", orderPayload.unique_id)
      }

      orderRecord.set("order_type", orderPayload.order_type)
      orderRecord.set("order_id", orderPayload.order_id || "")
      orderRecord.set("symbol", symbol)
      orderRecord.set("direction", orderPayload.direction || record.get("direction"))
      orderRecord.set("quantity", resolvedQuantity)
      orderRecord.set("limit_price", resolvedLimitPrice)
      orderRecord.set("status", resolvedStatus)
      orderRecord.set("filled_qty", orderPayload.filled_qty != null ? orderPayload.filled_qty : 0)
      orderRecord.set("fill_price", orderPayload.fill_price != null ? orderPayload.fill_price : 0)
      orderRecord.set("sl_price", resolvedStopLoss)
      orderRecord.set("tp_price", resolvedTakeProfit)
      orderRecord.set("signal_id", signalId)
      orderRecord.set("broker_order_id", brokerOrderId)
      orderRecord.set("order_time", resolvedOrderTime)
      orderRecord.set("us_time", eventTimes.us_time)
      orderRecord.set("cn_time", eventTimes.cn_time)
      orderRecord.set("bar_time_ms", eventTimes.bar_time_ms)
      if (orderPayload.fill_time) {
        orderRecord.set("fill_time", orderPayload.fill_time)
      }

      $app.save(orderRecord)
      mergeOrderExtra(orderRecord, {
        ...orderExtraData,
        order_time: resolvedOrderTime,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        created_via: "signals/ack",
      }, true)
      applyOrderRelationship(orderRecord, {
        broker_order_id: brokerOrderId,
        trade_group_id: orderPayload.trade_group_id || orderExtraData.trade_group_id || orderPayload.entry_order_unique_id || orderPayload.unique_id,
        entry_order_unique_id: orderPayload.entry_order_unique_id || orderExtraData.entry_order_unique_id || orderPayload.unique_id,
        parent_order_unique_id: orderPayload.parent_order_unique_id || orderExtraData.parent_order_unique_id || "",
        sibling_order_unique_id: orderPayload.sibling_order_unique_id || orderExtraData.sibling_order_unique_id || "",
        role: orderPayload.role || orderExtraData.role || (orderPayload.order_type === "Entry" ? "entry" : ""),
        relation_status: orderPayload.relation_status || orderExtraData.relation_status || (orderPayload.order_type === "Entry" ? "active" : "planned"),
        position_side: orderPayload.position_side || orderPayload.direction || record.get("direction") || "",
        order_type: orderPayload.order_type,
        unique_id: orderPayload.unique_id,
        status: resolvedStatus,
      }, true)
      applyOrderStatusMeta(orderRecord, {
        status: resolvedStatus,
        previous_status: previousOrderStatus,
        source: "signal_ack",
        reason: orderExtraData.reason || note,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        order_time: resolvedOrderTime,
        fill_time: orderPayload.fill_time || "",
        fill_us_time: resolvedStatus === "Filled" ? eventTimes.us_time : "",
        fill_cn_time: resolvedStatus === "Filled" ? eventTimes.cn_time : "",
        fill_bar_time_ms: resolvedStatus === "Filled" ? eventTimes.bar_time_ms : 0,
        created_us_time: !previousOrderStatus ? eventTimes.us_time : "",
        created_cn_time: !previousOrderStatus ? eventTimes.cn_time : "",
        created_bar_time_ms: !previousOrderStatus ? eventTimes.bar_time_ms : 0,
      }, true)
      appendOrderDetail(orderRecord, {
        status: resolvedStatus,
        source: "signal_ack",
        reason: orderExtraData.reason || note,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        order_time: resolvedOrderTime,
        extra: {
          order_type: orderPayload.order_type,
          direction: orderPayload.direction || record.get("direction") || "",
          quantity: resolvedQuantity,
          limit_price: resolvedLimitPrice,
          fill_price: orderPayload.fill_price || 0,
          filled_qty: orderPayload.filled_qty || 0,
          sl_price: resolvedStopLoss,
          tp_price: resolvedTakeProfit,
          created_via: "signals/ack",
          ...orderExtraData
        }
      })

      return {
        record: orderRecord,
        previous_status: previousOrderStatus,
        status: resolvedStatus,
      }
    }

    const ackOrders = buildAckOrders()
    let primaryOrderRecord = null
    let primaryStatus = "Init"

    if (ackOrders.length > 0) {
      console.log(`[SignalAck] 本次将创建/更新 ${ackOrders.length} 条订单记录`)
      const results = ackOrders.map(upsertAckOrder)
      const primaryResult = results.find((item) => (item.record.get("role") || "") === "entry") || results[0]
      primaryOrderRecord = primaryResult ? primaryResult.record : null
      primaryStatus = primaryResult ? primaryResult.status : "Init"

      if (primaryOrderRecord) {
        const messageId = getTradeGroupCardMessageId(primaryOrderRecord)
        const statusInfo = getOrderStatusInfo(primaryStatus)
        const defaultMessage = primaryStatus === "Submitted"
          ? "主单已提交，止盈止损子单已预创建"
          : "主单与止盈止损子单已初始化"
        if (messageId) {
          notifyOrder("signal_ack", primaryOrderRecord, {
            messageId: messageId,
            message: statusInfo.message || defaultMessage,
          })
        } else {
          notifyNewOrder(primaryOrderRecord, {
            message: primaryStatus === "Submitted" ? defaultMessage : "订单组已初始化，等待提交",
          })
        }
      }
    } else {
      console.log(`[SignalAck] 未传递订单数据，跳过订单创建`)
    }

    console.log(`[SignalAck] === 信号确认处理完成 === success=true, signal_id=${signalId}, status=${primaryStatus}`);
    return c.json(200, { success: true, signal_id: signalId, status: primaryStatus });
  } catch (err) {
    console.error(`[SignalAck] 错误:`, err.message);
    console.error(`[SignalAck] 堆栈:`, err.stack);
    return c.json(500, { error: err.message });
  }
});

// ── 订单操作（飞书按钮直接更新状态） ──

routerAdd("GET", "/webhook/order/cancel", (c) => {
    const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
    const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
    const { ok, warn, fail } = require(`${__hooks}/lib/_page.js`)
    const uniqueId = c.request.url.query().get("id") || ""
    if (!uniqueId) {
        return c.html(400, fail("参数错误", "缺少订单ID"))
    }
    try {
        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: uniqueId })
        if (!records || records.length === 0) {
            return c.html(404, fail("订单不存在", "找不到订单", uniqueId))
        }
        const record = records[0]
        const status = record.get("status")
        const symbol = record.get("symbol") || uniqueId
        const filledQty = Number(record.get("filled_qty") || 0)
        const tradeGroupId = record.get("trade_group_id") || record.get("entry_order_unique_id") || record.get("unique_id")
        const role = record.get("role") || ((record.get("extra") || {}).role) || ""
        console.log(`[OrderAction] 收到取消请求: unique_id=${uniqueId}, symbol=${symbol}, current_status=${status}`)
        if (role && role !== "entry") {
            return c.html(200, warn("只能取消主单", "止盈/止损等子单不能直接取消，请操作主入场单", symbol))
        }
        if (filledQty > 0) {
            return c.html(200, warn("主单已部分成交", "主单已部分成交，不能直接取消，请改用平仓整组", symbol))
        }
        const statusHints = {
            Filled:   { fn: warn, title: "订单已成交", msg: "订单已成交，无法取消" },
            Canceled: { fn: warn, title: "订单已取消", msg: "无需重复操作" },
            Closed:   { fn: warn, title: "订单已平仓", msg: "无法取消" },
        }
        if (statusHints[status]) {
            const h = statusHints[status]
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        const relatedRecords = $app.findRecordsByFilter(
            "orders",
            "trade_group_id = {:gid}",
            "-created",
            100,
            0,
            { gid: tradeGroupId }
        ) || [record]
        relatedRecords.forEach((groupRecord) => {
            const currentStatus = groupRecord.get("status")
            if (currentStatus === "Canceled" || currentStatus === "Closed" || currentStatus === "Filled") {
                return
            }
            groupRecord.set("status", "Canceled")
            applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: "Canceled",
            }, false)
            const metaResult = applyOrderStatusMeta(groupRecord, {
                status: "Canceled",
                previous_status: currentStatus,
                source: "webhook/order/cancel",
                reason: "页面取消主单",
            }, false)
            const eventTimes = metaResult.eventTimes
            $app.save(groupRecord)
            try {
                appendOrderDetail(groupRecord, {
                    status: "Canceled",
                    source: "webhook/order/cancel",
                    reason: "页面取消主单",
                    us_time: eventTimes.us_time,
                    cn_time: eventTimes.cn_time,
                    bar_time_ms: eventTimes.bar_time_ms,
                    extra: {
                        previous_status: currentStatus,
                        action: "cancel",
                        trade_group_id: tradeGroupId,
                    },
                })
            } catch (detailErr) {
                console.error("[OrderAction] 写入 order_details 失败:", detailErr)
            }
        })
        const orderExtra = getOrderExtra(record)
        const syncResult = notifyOrder("canceled", record, {
            messageId: orderExtra.feishu_order_message_id || "",
            message: "主单已取消，保护单已收尾",
        })
        if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
            mergeOrderExtra(record, {
                feishu_order_message_id: syncResult.message_id,
                feishu_order_card_version: 2,
            }, true)
        }
        console.log("[OrderAction] 交易组已取消:", tradeGroupId)
        return c.html(200, ok("订单已取消", "状态已更新", symbol))
    } catch (err) {
        console.error("[OrderAction] 取消失败:", err)
        return c.html(500, fail("操作失败", String(err)))
    }
})

routerAdd("GET", "/webhook/order/close", (c) => {
    const { appendOrderDetail, getOrderExtra, mergeOrderExtra, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
    const { notifyOrder } = require(`${__hooks}/lib/feishu_order.js`)
    const { ok, warn, fail } = require(`${__hooks}/lib/_page.js`)
    const uniqueId = c.request.url.query().get("id") || ""
    if (!uniqueId) {
        return c.html(400, fail("参数错误", "缺少订单ID"))
    }
    try {
        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: uniqueId })
        if (!records || records.length === 0) {
            return c.html(404, fail("订单不存在", "找不到订单", uniqueId))
        }
        const record = records[0]
        const status = record.get("status")
        const symbol = record.get("symbol") || uniqueId
        const tradeGroupId = record.get("trade_group_id") || record.get("entry_order_unique_id") || record.get("unique_id")
        const entryOrderUniqueId = record.get("entry_order_unique_id") || record.get("unique_id")
        const statusHints = {
            Init:      { fn: warn, title: "订单未成交", msg: "只有成交的订单才能平仓" },
            Submitted: { fn: warn, title: "订单未成交", msg: "请先取消挂单" },
            Canceled: { fn: warn, title: "订单已取消", msg: "无法平仓" },
            Closed:   { fn: warn, title: "订单已平仓", msg: "无需重复操作" },
        }
        if (statusHints[status]) {
            const h = statusHints[status]
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        if (status !== "Filled") {
            return c.html(200, warn("订单未成交", "只有成交的订单才能平仓", symbol))
        }
        const relatedRecords = $app.findRecordsByFilter(
            "orders",
            "trade_group_id = {:gid}",
            "-created",
            100,
            0,
            { gid: tradeGroupId }
        ) || []
        relatedRecords.forEach((groupRecord) => {
            const currentGroupStatus = groupRecord.get("status")
            if (currentGroupStatus === "Canceled" || currentGroupStatus === "Closed") {
                return
            }
            const nextStatus = groupRecord.get("unique_id") === entryOrderUniqueId ? "Closed" : "Canceled"
            groupRecord.set("status", nextStatus)
            applyOrderRelationship(groupRecord, {
                relation_status: "closed",
                status: nextStatus,
            }, false)
            const metaResult = applyOrderStatusMeta(groupRecord, {
                status: nextStatus,
                previous_status: currentGroupStatus,
                source: "webhook/order/close",
                reason: `页面平仓交易组 ${tradeGroupId}`,
            }, false)
            const eventTimes = metaResult.eventTimes
            $app.save(groupRecord)
            try {
                appendOrderDetail(groupRecord, {
                    status: nextStatus,
                    source: "webhook/order/close",
                    reason: `页面平仓交易组 ${tradeGroupId}`,
                    us_time: eventTimes.us_time,
                    cn_time: eventTimes.cn_time,
                    bar_time_ms: eventTimes.bar_time_ms,
                    extra: {
                        action: "close_group",
                        previous_status: currentGroupStatus,
                        trade_group_id: tradeGroupId,
                    },
                })
            } catch (detailErr) {
                console.error("[OrderAction] 平仓写入 order_details 失败:", detailErr)
            }
        })
        const orderExtra = getOrderExtra(record)
        const syncResult = notifyOrder("closed", record, {
            messageId: orderExtra.feishu_order_message_id || "",
            message: `交易组已平仓 (${tradeGroupId})`,
        })
        if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
            mergeOrderExtra(record, {
                feishu_order_message_id: syncResult.message_id,
                feishu_order_card_version: 2,
            }, true)
        }
        console.log("[OrderAction] 交易组已平仓:", tradeGroupId)
        return c.html(200, ok("交易组已平仓", "状态已更新", symbol))
    } catch (err) {
        console.error("[OrderAction] 平仓失败:", err)
        return c.html(500, fail("操作失败", String(err)))
    }
})

console.log('[SignalActions] Hook 文件加载完成');
