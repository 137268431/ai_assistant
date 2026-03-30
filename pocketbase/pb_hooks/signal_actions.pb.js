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
    console.log(`[SignalsPending] === 查询待执行信号 ===`);
    console.log(`[SignalsPending] date 参数: "${dateStr}"`);

    if (!dateStr) {
      return c.json(400, { error: "缺少 date 参数" });
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
      const extra = r.get("extra") || {};
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
        date: r.get("date"),
        us_time: r.get("us_time"),
        bar_time_ms: r.get("bar_time_ms"),
        extra: extra,
        created: r.get("created")
      };
    });

    console.log(`[SignalsPending] 返回 signals 数组长度: ${signals.length}`);
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
  const { notifyNewOrder } = require(`${__hooks}/lib/feishu_order.js`)
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

    // 2. 如果 QC 传递了订单数据，则创建订单
    if (orderData.unique_id && orderData.order_type) {
      console.log(`[SignalAck] === 创建订单记录 ===`);
      console.log(`[SignalAck] unique_id: ${orderData.unique_id}`);
      console.log(`[SignalAck] order_type: ${orderData.order_type}`);
      console.log(`[SignalAck] direction: ${orderData.direction || record.get("direction")}`);
      console.log(`[SignalAck] quantity: ${orderData.quantity || record.get("shares")}`);
      console.log(`[SignalAck] limit_price: ${orderData.limit_price || record.get("entry")}`);

      const ordersCol = $app.findCollectionByNameOrId("orders");

      // 查找是否已存在订单
      let orderRecord = null;
      let previousOrderStatus = ""
      try {
        const existing = $app.findRecordsByFilter(
          "orders",
          `unique_id = {:uniqueId}`,
          "",
          1,
          0,
          { uniqueId: orderData.unique_id }
        );
        if (existing.length > 0) {
          orderRecord = existing[0];
          previousOrderStatus = orderRecord.get("status") || ""
          console.log(`[SignalAck] 订单已存在，将更新: ${orderData.unique_id}`);
        }
      } catch (_) {}

      const isNewOrder = !orderRecord
      const orderExtraData = orderData.extra && typeof orderData.extra === "object" ? orderData.extra : {}
      const resolvedQuantity = orderData.quantity != null ? orderData.quantity : (record.get("shares") || orderExtraData.quantity || 0)
      const resolvedLimitPrice = orderData.limit_price != null ? orderData.limit_price : (record.get("entry") || orderExtraData.limit_price || 0)
      const resolvedStopLoss = orderData.stop_loss != null ? orderData.stop_loss : (record.get("stop_loss") || orderExtraData.sl_price || 0)
      const resolvedTakeProfit = orderData.take_profit != null ? orderData.take_profit : (record.get("take_profit") || orderExtraData.tp_price || 0)
      const tradeGroupId = orderData.trade_group_id || orderExtraData.trade_group_id || orderData.entry_order_unique_id || orderData.unique_id
      const entryOrderUniqueId = orderData.entry_order_unique_id || orderExtraData.entry_order_unique_id || orderData.unique_id
      const brokerOrderId = orderData.broker_order_id || orderData.order_id || ""
      const positionSide = orderData.position_side || orderData.direction || record.get("direction") || ""
      const eventTimes = resolveOrderEventTimes({
        us_time: orderData.us_time || orderExtraData.us_time,
        cn_time: orderData.cn_time || orderExtraData.cn_time,
        bar_time_ms: orderData.bar_time_ms || orderExtraData.bar_time_ms,
      }, {
        us_time: record.get("us_time") || "",
        cn_time: record.get("cn_time") || "",
        bar_time_ms: record.get("bar_time_ms") || 0,
      })
      const resolvedOrderTime = orderData.order_time || orderExtraData.order_time || eventTimes.us_time
      if (!orderRecord) {
        orderRecord = new Record(ordersCol, {});
        orderRecord.set("unique_id", orderData.unique_id);
        console.log(`[SignalAck] 创建新订单: ${orderData.unique_id}`);
      }

      orderRecord.set("order_type", orderData.order_type);
      orderRecord.set("symbol", symbol);
      orderRecord.set("direction", orderData.direction || record.get("direction"));
      orderRecord.set("quantity", resolvedQuantity);
      orderRecord.set("limit_price", resolvedLimitPrice);
      orderRecord.set("status", "Init");
      orderRecord.set("filled_qty", orderData.filled_qty || 0);
      orderRecord.set("fill_price", orderData.fill_price || 0);
      orderRecord.set("sl_price", resolvedStopLoss);
      orderRecord.set("tp_price", resolvedTakeProfit);
      orderRecord.set("signal_id", signalId);
      orderRecord.set("broker_order_id", brokerOrderId);
      orderRecord.set("order_time", resolvedOrderTime);
      orderRecord.set("us_time", eventTimes.us_time);
      orderRecord.set("cn_time", eventTimes.cn_time);
      orderRecord.set("bar_time_ms", eventTimes.bar_time_ms);

      $app.save(orderRecord);
      mergeOrderExtra(orderRecord, {
        ...orderExtraData,
        order_time: resolvedOrderTime,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        created_via: "signals/ack",
      }, true);
      applyOrderRelationship(orderRecord, {
        broker_order_id: brokerOrderId,
        trade_group_id: tradeGroupId,
        entry_order_unique_id: entryOrderUniqueId,
        parent_order_unique_id: orderData.parent_order_unique_id || orderExtraData.parent_order_unique_id || "",
        sibling_order_unique_id: orderData.sibling_order_unique_id || orderExtraData.sibling_order_unique_id || "",
        role: orderData.role || orderExtraData.role || "entry",
        relation_status: orderData.relation_status || orderExtraData.relation_status || "active",
        position_side: positionSide,
        order_type: orderData.order_type,
        unique_id: orderData.unique_id,
        status: "Init",
      }, true)
      applyOrderStatusMeta(orderRecord, {
        status: "Init",
        previous_status: previousOrderStatus,
        source: "signal_ack_init",
        reason: note,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        order_time: resolvedOrderTime,
        created_us_time: eventTimes.us_time,
        created_cn_time: eventTimes.cn_time,
        created_bar_time_ms: eventTimes.bar_time_ms,
      }, true)
      console.log(`[SignalAck] 订单已保存: id=${orderRecord.id}, status=Init`);

      // 3. 写入 order_details
      console.log(`[SignalAck] === 写入订单事件记录 ===`);
      appendOrderDetail(orderRecord, {
        status: "Init",
        source: "signal_ack_init",
        reason: note,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        order_time: resolvedOrderTime,
        extra: {
          order_type: orderData.order_type,
          direction: orderData.direction || record.get("direction") || "",
          quantity: resolvedQuantity,
          limit_price: resolvedLimitPrice,
          fill_price: orderData.fill_price || 0,
          filled_qty: orderData.filled_qty || 0,
          sl_price: resolvedStopLoss,
          tp_price: resolvedTakeProfit,
          created_via: "signals/ack",
          ...orderExtraData
        }
      });
      console.log(`[SignalAck] order_details 已保存: order_id=${orderData.unique_id}, order_type=${orderData.order_type}, status=Init`);
      if (isNewOrder && orderData.order_type === "Entry") {
        console.log(`[SignalAck] 发送 Init 新订单飞书通知: unique_id=${orderData.unique_id}`);
        const notifyResult = notifyNewOrder(orderRecord, { message: "订单已初始化，等待提交" });
        if (notifyResult.success && notifyResult.message_id) {
          mergeOrderExtra(orderRecord, {
            feishu_order_message_id: notifyResult.message_id,
            feishu_order_card_version: 1,
          }, true);
        }
      }
    } else {
      console.log(`[SignalAck] 未传递订单数据，跳过订单创建`);
    }

    console.log(`[SignalAck] === 信号确认处理完成 === success=true, signal_id=${signalId}, status=Init`);
    return c.json(200, { success: true, signal_id: signalId, status: "Init" });
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
        const role = record.get("role") || ((record.get("extra") || {}).role) || ""
        console.log(`[OrderAction] 收到取消请求: unique_id=${uniqueId}, symbol=${symbol}, current_status=${status}`)
        if (role && role !== "entry") {
            return c.html(200, warn("只能取消主单", "止盈/止损等子单不能直接取消，请操作主入场单", symbol))
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
        const oldStatus = status
        record.set("status", "Canceled")
        applyOrderRelationship(record, {
            relation_status: "closed",
            status: "Canceled",
        }, false)
        const metaResult = applyOrderStatusMeta(record, {
            status: "Canceled",
            previous_status: oldStatus,
            source: "webhook/order/cancel",
            reason: "页面取消挂单",
        }, false)
        const eventTimes = metaResult.eventTimes
        $app.save(record)
        try {
            appendOrderDetail(record, {
                status: "Canceled",
                source: "webhook/order/cancel",
                reason: "页面取消挂单",
                us_time: eventTimes.us_time,
                cn_time: eventTimes.cn_time,
                bar_time_ms: eventTimes.bar_time_ms,
                extra: {
                    previous_status: oldStatus,
                    action: "cancel",
                },
            })
        } catch (detailErr) {
            console.error("[OrderAction] 写入 order_details 失败:", detailErr)
        }
        const orderExtra = getOrderExtra(record)
        const syncResult = notifyOrder("canceled", record, {
            messageId: orderExtra.feishu_order_message_id || "",
            message: "订单已取消",
        })
        if (syncResult.success && syncResult.message_id && syncResult.message_id !== orderExtra.feishu_order_message_id) {
            mergeOrderExtra(record, {
                feishu_order_message_id: syncResult.message_id,
                feishu_order_card_version: 1,
            }, true)
        }
        console.log("[OrderAction] 订单已取消:", uniqueId)
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
                feishu_order_card_version: 1,
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
