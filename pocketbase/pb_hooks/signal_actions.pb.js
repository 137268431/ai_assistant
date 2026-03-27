/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_actions.pb.js
 * 信号确认/取消接口（供飞书按钮直接调用）
 */

console.log("[SignalActions] Hook 文件开始加载...");

routerAdd("GET", "/webhook/signal/confirm", (c) => {
    const { ok, warn, fail, info } = require(`${__hooks}/_page.js`)
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
        return c.html(200, ok("信号已确认", "确认成功", symbol))
    } catch (err) {
        console.error("[SignalAction] 确认失败:", err)
        return c.html(404, fail("信号不存在", "找不到信号", signalId))
    }
})

routerAdd("GET", "/webhook/signal/cancel", (c) => {
    const { ok, warn, fail, info } = require(`${__hooks}/_page.js`)
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
    if (!dateStr) {
      return c.json(400, { error: "缺少 date 参数" });
    }

    const records = $app.findRecordsByFilter(
      "signals",
      `status = 'pending' && date = {:d}`,
      "-bar_time_ms",
      100,
      0,
      { d: dateStr }
    );

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

    return c.json(200, { signals: signals });
  } catch (err) {
    console.error("Error fetching pending signals:", err);
    return c.json(500, { error: err.message });
  }
});

routerAdd("POST", "/api/custom/signals/ack", (c) => {
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
    const prevStatus = record.get("status");
    console.log(`[SignalAck] 找到信号: symbol=${symbol}, 原状态=${prevStatus}`);

    record.set("status", status);
    record.set("note", note);
    $app.save(record);
    console.log(`[SignalAck] 信号状态已更新: ${prevStatus} → ${status}`);

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
          console.log(`[SignalAck] 订单已存在，将更新: ${orderData.unique_id}`);
        }
      } catch (_) {}

      if (!orderRecord) {
        orderRecord = new Record(ordersCol, {});
        orderRecord.set("unique_id", orderData.unique_id);
        console.log(`[SignalAck] 创建新订单: ${orderData.unique_id}`);
      }

      orderRecord.set("order_type", orderData.order_type || "Entry");
      orderRecord.set("symbol", symbol);
      orderRecord.set("direction", orderData.direction || record.get("direction"));
      orderRecord.set("quantity", orderData.quantity || 0);
      orderRecord.set("limit_price", orderData.limit_price || 0);
      orderRecord.set("status", orderData.status || "Submitted");
      orderRecord.set("filled_qty", orderData.filled_qty || 0);
      orderRecord.set("fill_price", orderData.fill_price || 0);
      orderRecord.set("stop_loss", orderData.stop_loss || 0);
      orderRecord.set("take_profit", orderData.take_profit || 0);
      orderRecord.set("signal_id", signalId);
      orderRecord.set("order_time", orderData.order_time || orderData.us_time || "");
      orderRecord.set("us_time", orderData.us_time || "");
      orderRecord.set("cn_time", orderData.cn_time || "");
      orderRecord.set("bar_time_ms", orderData.bar_time_ms || 0);

      $app.save(orderRecord);
      console.log(`[SignalAck] 订单已保存: id=${orderRecord.id}, status=Submitted`);

      // 3. 写入 order_details
      console.log(`[SignalAck] === 写入订单事件记录 ===`);
      const detailsCol = $app.findCollectionByNameOrId("order_details");
      const detailRecord = new Record(detailsCol, {});
      detailRecord.set("order_id", orderData.unique_id);
      detailRecord.set("symbol", symbol);
      detailRecord.set("direction", orderData.direction || record.get("direction") || "");
      detailRecord.set("event_type", "submitted");
      detailRecord.set("old_value", prevStatus);
      detailRecord.set("new_value", status);
      detailRecord.set("reason", note);
      detailRecord.set("signal_id", signalId);
      // 优先使用 QC 传来的交易时间，回测时这是真实交易时间
      const qcTime = orderData.us_time || "";
      const qcCnTime = orderData.cn_time || "";
      const qcBarTime = orderData.bar_time_ms || 0;

      // 兜底用 PB 服务器时间（仅在 QC 未传时间时使用）
      const pbNow = new Date();
      const pbNowISO = pbNow.toISOString();
      const pbNowStr = pbNowISO.replace('T', ' ').substring(0, 19);
      const pbNowCn = new Date(pbNow.getTime() + 8*60*60*1000).toISOString().replace('T', ' ').substring(0, 19);

      detailRecord.set("event_time", qcTime ? new Date(qcTime).toISOString() : pbNowISO);
      detailRecord.set("us_time", qcTime || pbNowStr);
      detailRecord.set("cn_time", qcCnTime || pbNowCn);
      detailRecord.set("bar_time_ms", qcBarTime || pbNow.getTime());

      // extra 存 QC 传入的扩展信息
      if (orderData.extra) detailRecord.set("extra", orderData.extra);

      $app.save(detailRecord);
      console.log(`[SignalAck] order_details 已保存: order_id=${orderData.unique_id}, event_type=submitted`);
    } else {
      console.log(`[SignalAck] 未传递订单数据，跳过订单创建`);
    }

    console.log(`[SignalAck] === 信号确认处理完成 === success=true, signal_id=${signalId}, status=${status}`);
    return c.json(200, { success: true, signal_id: signalId, status: status });
  } catch (err) {
    console.error(`[SignalAck] 错误:`, err.message);
    console.error(`[SignalAck] 堆栈:`, err.stack);
    return c.json(500, { error: err.message });
  }
});

// ── 订单操作 ──

routerAdd("GET", "/webhook/order/cancel", (c) => {
    const { ok, warn, fail } = require(`${__hooks}/_page.js`)
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
        const statusHints = {
            Filled:   { fn: warn, title: "订单已成交", msg: "订单已成交，无法取消" },
            Canceled: { fn: warn, title: "订单已取消", msg: "无需重复操作" },
            Closed:   { fn: warn, title: "订单已平仓", msg: "无法取消" },
        }
        if (statusHints[status]) {
            const h = statusHints[status]
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        record.set("action", "cancel")
        $app.save(record)
        console.log("[OrderAction] 取消挂单:", uniqueId)
        return c.html(200, ok("取消指令已发送", "等待交易系统执行", symbol))
    } catch (err) {
        console.error("[OrderAction] 取消失败:", err)
        return c.html(500, fail("操作失败", String(err)))
    }
})

routerAdd("GET", "/webhook/order/close", (c) => {
    const { ok, warn, fail } = require(`${__hooks}/_page.js`)
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
        const statusHints = {
            Submitted: { fn: warn, title: "订单未成交", msg: "请先取消挂单" },
            Canceled: { fn: warn, title: "订单已取消", msg: "无法平仓" },
            Closed:   { fn: warn, title: "订单已平仓", msg: "无需重复操作" },
        }
        if (statusHints[status]) {
            const h = statusHints[status]
            return c.html(200, h.fn(h.title, h.msg, symbol))
        }
        record.set("action", "close")
        $app.save(record)
        console.log("[OrderAction] 平仓:", uniqueId)
        return c.html(200, ok("平仓指令已发送", "等待交易系统执行", symbol))
    } catch (err) {
        console.error("[OrderAction] 平仓失败:", err)
        return c.html(500, fail("操作失败", String(err)))
    }
})

console.log('[SignalActions] Hook 文件加载完成');
