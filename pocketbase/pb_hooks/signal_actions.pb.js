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
    let dateStr = c.request.url.query().get("date") || "";
    if (!dateStr) {
      const loc = time.LoadLocation("America/New_York");
      const et = time.Now().In(loc);
      dateStr = et.Year() + "-" + et.Month() + "-" + et.Day();
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

  if (!signalId) {
    return c.json(400, { error: "Missing signal_id" });
  }

  try {
    const record = $app.findFirstRecordByFilter(
      "signals",
      `signal_id = {:sid}`,
      { sid: signalId }
    );

    record.set("status", status);
    record.set("note", note);
    $app.save(record);

    return c.json(200, { success: true, signal_id: signalId, status: status });
  } catch (err) {
    console.error("Error acknowledging signal:", err);
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
