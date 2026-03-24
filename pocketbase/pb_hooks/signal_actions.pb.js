/// <reference path="./pb_data/types.d.ts" />

/**
 * signal_actions.pb.js
 * 信号确认/取消接口（供飞书按钮直接调用）
 * 通过 config 表中 signal_action_token 做简单校验
 */

function _page(emoji, title, detail, color) {
    color = color || "#333"
    return `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}
.card{background:#fff;border-radius:12px;padding:40px;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.1);max-width:360px;width:90%}
.emoji{font-size:48px;margin-bottom:16px}.title{font-size:22px;font-weight:600;color:${color};margin-bottom:8px}
.detail{color:#888;font-size:14px;margin-top:8px}</style></head>
<body><div class="card"><div class="emoji">${emoji}</div><div class="title">${title}</div><div class="detail">${detail}</div></div></body></html>`
}

function _verifyToken(c) {
    const token = c.request.url.query().get("token") || ""
    try {
        const cfg = $app.findFirstRecordByFilter("config", "key = 'signal_action_token'")
        const expected = cfg.get("value") || ""
        if (!expected) return true   // 未配置 token，跳过校验
        return token === expected
    } catch (_) {
        return true  // config 不存在，跳过校验
    }
}

routerAdd("GET", "/webhook/signal/confirm", (c) => {
    if (!_verifyToken(c)) {
        return c.html(403, _page("🔒", "无权限", "链接无效或已过期，请从飞书消息中重新点击", "#e53e3e"))
    }
    const signalId = c.request.url.query().get("id") || ""
    if (!signalId) {
        return c.html(400, _page("❌", "参数错误", "缺少信号ID", "#e53e3e"))
    }
    try {
        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || signalId
        const statusHints = {
            expired:   ["⏰", "信号已过期",   "该信号超时自动失效，无法操作", "#e53e3e"],
            canceled:  ["❌", "信号已取消",   "该信号已被取消，无法重复操作", "#e53e3e"],
            executed:  ["✅", "信号已执行",   "该信号已执行，无需重复确认",   "#38a169"],
            pending:   ["✅", "信号已确认",   "该信号已确认，无需重复操作",   "#38a169"],
        }
        if (currentStatus !== "pending" && currentStatus !== "awaiting_confirm") {
            const h = statusHints[currentStatus] || ["⚠️", "无法操作", "状态: " + currentStatus, "#888"]
            return c.html(200, _page(h[0], h[1], h[2] + "<br><small>" + symbol + "</small>", h[3]))
        }
        record.set("status", "pending")
        $app.save(record)
        return c.html(200, _page("✅", "信号已确认", symbol, "#38a169"))
    } catch (err) {
        console.error("[SignalAction] 确认失败:", err)
        return c.html(404, _page("🔍", "信号不存在", "找不到信号: " + signalId, "#e53e3e"))
    }
})

routerAdd("GET", "/webhook/signal/cancel", (c) => {
    if (!_verifyToken(c)) {
        return c.html(403, _page("🔒", "无权限", "链接无效或已过期，请从飞书消息中重新点击", "#e53e3e"))
    }
    const signalId = c.request.url.query().get("id") || ""
    if (!signalId) {
        return c.html(400, _page("❌", "参数错误", "缺少信号ID", "#e53e3e"))
    }
    try {
        const record = $app.findFirstRecordByFilter("signals", "id = {:id} || signal_id = {:id}", { id: signalId })
        const currentStatus = record.get("status")
        const symbol = record.get("symbol") || signalId
        const statusHints = {
            expired:  ["⏰", "信号已过期",   "该信号超时自动失效，无法操作", "#e53e3e"],
            canceled: ["❌", "信号已取消",   "该信号已被取消，无需重复操作", "#e53e3e"],
            executed: ["✅", "信号已执行",   "信号已执行，无法取消",         "#e53e3e"],
            pending:  ["⚠️", "信号已确认",   "该信号已确认，无法取消",       "#dd6b20"],
        }
        if (currentStatus !== "pending" && currentStatus !== "awaiting_confirm") {
            const h = statusHints[currentStatus] || ["⚠️", "无法操作", "状态: " + currentStatus, "#888"]
            return c.html(200, _page(h[0], h[1], h[2] + "<br><small>" + symbol + "</small>", h[3]))
        }
        record.set("status", "canceled")
        $app.save(record)
        return c.html(200, _page("✅", "信号已取消", symbol, "#38a169"))
    } catch (err) {
        console.error("[SignalAction] 取消失败:", err)
        return c.html(404, _page("🔍", "信号不存在", "找不到信号: " + signalId, "#e53e3e"))
    }
})

// ── QC 信号拉取 & 确认 ──

// GET /api/custom/signals/pending?date=YYYY-MM-DD - QC 拉取待执行信号（默认当天美东）
routerAdd("GET", "/api/custom/signals/pending", (c) => {
  try {
    // 支持 ?date= 查询指定日期，默认 UTC 转美东当天
    let dateStr = c.request.url.query().get("date") || "";
    if (!dateStr) {
      const now = new Date();
      const etOptions = { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" };
      const etParts = new Intl.DateTimeFormat("en-CA", etOptions).formatToParts(now);
      dateStr = etParts.find(p => p.type === "year").value
        + "-" + etParts.find(p => p.type === "month").value
        + "-" + etParts.find(p => p.type === "day").value;
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
}, $apis.requireSuperuserAuth());

// POST /api/custom/signals/ack - QC 确认信号已处理
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
}, $apis.requireSuperuserAuth());

// ── 订单操作 ──

routerAdd("GET", "/webhook/order/cancel", (c) => {
    if (!_verifyToken(c)) {
        return c.html(403, _page("🔒", "无权限", "链接无效或已过期，请从飞书消息中重新点击", "#e53e3e"))
    }
    const uniqueId = c.request.url.query().get("id") || ""
    if (!uniqueId) {
        return c.html(400, _page("❌", "参数错误", "缺少订单ID", "#e53e3e"))
    }
    try {
        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: uniqueId })
        if (!records || records.length === 0) {
            return c.html(404, _page("🔍", "订单不存在", "找不到订单: " + uniqueId, "#e53e3e"))
        }
        const record = records[0]
        const status = record.get("status")
        const symbol = record.get("symbol") || uniqueId
        if (status === "Filled" || status === "Canceled" || status === "Closed") {
            const hints = { Filled: "订单已成交，无法取消", Canceled: "订单已取消，无需重复操作", Closed: "订单已平仓，无法取消" }
            return c.html(200, _page("⚠️", hints[status] || "无法操作", symbol, "#e53e3e"))
        }
        record.set("action", "cancel")
        $app.save(record)
        console.log("[OrderAction] 取消挂单:", uniqueId)
        return c.html(200, _page("✅", "取消指令已发送", symbol + "<br><small>等待交易系统执行</small>", "#38a169"))
    } catch (err) {
        console.error("[OrderAction] 取消失败:", err)
        return c.html(500, _page("❌", "操作失败", String(err), "#e53e3e"))
    }
})

routerAdd("GET", "/webhook/order/close", (c) => {
    if (!_verifyToken(c)) {
        return c.html(403, _page("🔒", "无权限", "链接无效或已过期，请从飞书消息中重新点击", "#e53e3e"))
    }
    const uniqueId = c.request.url.query().get("id") || ""
    if (!uniqueId) {
        return c.html(400, _page("❌", "参数错误", "缺少订单ID", "#e53e3e"))
    }
    try {
        const records = $app.findRecordsByFilter("orders", "unique_id = {:id}", "", 1, 0, { id: uniqueId })
        if (!records || records.length === 0) {
            return c.html(404, _page("🔍", "订单不存在", "找不到订单: " + uniqueId, "#e53e3e"))
        }
        const record = records[0]
        const status = record.get("status")
        const symbol = record.get("symbol") || uniqueId
        if (status !== "Filled") {
            const hints = { Submitted: "订单尚未成交，请先取消挂单", Canceled: "订单已取消", Closed: "订单已平仓，无需重复操作" }
            return c.html(200, _page("⚠️", hints[status] || "无法平仓", symbol + "<br><small>当前状态: " + status + "</small>", "#e53e3e"))
        }
        record.set("action", "close")
        $app.save(record)
        console.log("[OrderAction] 平仓:", uniqueId)
        return c.html(200, _page("✅", "平仓指令已发送", symbol + "<br><small>等待交易系统执行</small>", "#38a169"))
    } catch (err) {
        console.error("[OrderAction] 平仓失败:", err)
        return c.html(500, _page("❌", "操作失败", String(err), "#e53e3e"))
    }
})
