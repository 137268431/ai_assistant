/// <reference path="./pb_data/types.d.ts" />

/**
 * order_manage.pb.js
 * 订单管理 API（替代 GAS order_manager.gs）
 */

// POST /api/custom/orders/upsert - 订单 upsert，自动写入 order_details
routerAdd("POST", "/api/custom/orders/upsert", (c) => {
  const { notifyNewOrder, notifyOrder } = require(`${__hooks}/feishu.js`)
  const data = c.requestInfo().body || c.requestInfo().data || {};

  // 字段映射：QC 发送的字段名 -> PB schema 字段名（已统一）
  const uniqueId = data.unique_id;
  const orderType = data.order_type;
  const orderId = data.order_id;
  const symbol = data.symbol;
  const direction = data.direction;
  const quantity = data.quantity;
  const price = data.limit_price;
  const status = data.status || "pending";
  const filledQty = data.filled_qty || data.quantity;
  const avgFillPrice = data.fill_price;
  const extra = data.extra || {};

  if (!uniqueId || !orderType || !symbol) {
    return c.json(400, { error: "Missing required fields" });
  }

  try {
    const collection = $app.findCollectionByNameOrId("orders");

    // 查找是否已存在
    let record = null;
    try {
      const existing = $app.findRecordsByFilter(
        "orders",
        `unique_id = {:uniqueId}`,
        "",
        1,
        0,
        { uniqueId: uniqueId }
      );
      if (existing.length > 0) {
        record = existing[0];
      }
    } catch (err) {
      // 不存在，创建新记录
    }

    if (!record) {
      record = new Record(collection, {});
      record.set("unique_id", uniqueId);
    }

    // 更新字段
    record.set("order_type", orderType);
    record.set("order_id", orderId || "");
    record.set("symbol", symbol);
    if (direction) record.set("direction", direction);
    record.set("quantity", quantity);
    record.set("limit_price", price);
    record.set("status", status);
    record.set("filled_qty", filledQty);
    record.set("fill_price", avgFillPrice);
    record.set("extra", extra);
    if (data.signal_id) record.set("signal_id", data.signal_id);
    // 添加盈亏和手续费字段
    if (data.pnl !== undefined) record.set("pnl", parseFloat(data.pnl));
    if (data.commission !== undefined) record.set("commission", parseFloat(data.commission));
    if (data.rr_ratio !== undefined) record.set("rr_ratio", parseFloat(data.rr_ratio));

    $app.save(record);

    // 写入 order_details（用于详细日志）
    // 问题 15 修复: 添加序列号确保每次状态变化都记录为独立事件
    const detailsCollection = $app.findCollectionByNameOrId("order_details");

    // 查询该订单已有的记录数，用作序列号
    let sequence = 1;
    try {
      const existingDetails = $app.findRecordsByFilter(
        "order_details",
        `order_id = {:orderId}`,
        "-bar_time_ms",
        1000,
        0,
        { orderId: uniqueId }
      );
      sequence = existingDetails.length + 1;
    } catch (err) {
      // 首次记录，sequence = 1
    }

    const detailRecord = new Record(detailsCollection, {});
    detailRecord.set("order_id", uniqueId);  // 使用 unique_id 作为主键
    detailRecord.set("symbol", symbol);
    detailRecord.set("direction", direction || "");
    // 将 status 转换为 event_type (submitted/filled/canceled/closed)
    const eventTypeMap = {
      "Submitted": "submitted",
      "Filled": "filled",
      "Canceled": "canceled",
      "Closed": "closed"
    };
    detailRecord.set("event_type", eventTypeMap[status] || status.toLowerCase());
    detailRecord.set("old_value", "");
    detailRecord.set("new_value", "");
    detailRecord.set("reason", extra.reason || "");
    detailRecord.set("signal_id", data.signal_id || "");
    detailRecord.set("event_time", new Date().toISOString());
    // 添加 us_time, cn_time, bar_time_ms
    const now = new Date();
    detailRecord.set("us_time", now.toISOString().replace('T', ' ').substring(0, 19));
    detailRecord.set("cn_time", new Date(now.getTime() + 8*60*60*1000).toISOString().replace('T', ' ').substring(0, 19));
    detailRecord.set("bar_time_ms", data.bar_time_ms || now.getTime());
    // 在 extra 中保存完整的订单信息和序列号
    const detailExtra = {
      sequence: sequence,  // 添加序列号
      order_type: orderType,
      original_order_id: orderId,
      quantity: quantity,
      limit_price: price,
      fill_price: avgFillPrice,
      filled_qty: filledQty,
      ...extra
    };
    detailRecord.set("extra", detailExtra);

    $app.save(detailRecord);

    // 发送飞书通知（仅关键状态变化）
    try {
      // 判断是否需要发送通知
      let shouldNotify = false;
      let action = "";

      if (status === "Submitted" && sequence === 1 && orderType === "Entry") {
        // 首次创建 Entry 订单 - 发送交互式通知（带取消/平仓按钮）
        shouldNotify = true;
        action = "created";
      } else if (status === "Filled") {
        // 订单成交
        shouldNotify = true;
        action = "filled";
      } else if (status === "Canceled") {
        // 订单取消
        shouldNotify = true;
        action = "canceled";
      } else if (status === "Closed") {
        // 订单平仓
        shouldNotify = true;
        action = "closed";
      }

      if (shouldNotify) {
        const orderData = {
          record_id: record.id,
          unique_id: uniqueId,
          symbol: symbol,
          direction: direction,
          order_type: orderType,
          status: status,
          quantity: quantity,
          limit_price: price,
          fill_price: avgFillPrice,
          signal_id: data.signal_id || ""
        };

        // Entry Submitted 发送交互式卡片，其他发送普通通知
        if (action === "created" && orderType === "Entry") {
          notifyNewOrder(orderData);
        } else {
          notifyOrder(action, orderData);
        }
      }
    } catch (err) {
      console.error("[Feishu] 构建订单通知失败:", err);
    }

    return c.json(200, {
      success: true,
      order: {
        id: record.id,
        unique_id: uniqueId,
        order_type: orderType,
        order_id: orderId,
        symbol: symbol,
        status: status
      }
    });
  } catch (err) {
    console.error("Error upserting order:", err);
    return c.json(500, { error: err.message });
  }
});

// GET /api/custom/orders/pending - 获取待执行操作
routerAdd("GET", "/api/custom/orders/pending", (c) => {
  try {
    // 问题 4 修复: 使用独立的 action 字段而不是 JSON 查询
    const records = $app.findRecordsByFilter(
      "orders",
      "action != ''",  // 使用独立字段，有索引支持
      "-updated",
      100,
      0
    );

    const actions = records.map((r) => {
      const extra = r.get("extra") || {};
      return {
        id: r.id,
        unique_id: r.get("unique_id"),
        order_id: r.get("order_id"),
        symbol: r.get("symbol"),
        direction: r.get("direction"),
        order_type: r.get("order_type"),
        action: r.get("action"),  // 从独立字段读取
        action_params: extra.action_params || {},
        status: r.get("status")
      };
    });

    return c.json(200, { status: "success", actions: actions });
  } catch (err) {
    console.error("Error fetching pending order actions:", err);
    return c.json(500, { error: err.message });
  }
});

// POST /api/custom/orders/ack - 确认操作完成
routerAdd("POST", "/api/custom/orders/ack", (c) => {
  const data = c.requestInfo().body || c.requestInfo().data || {};
  const uniqueId = data.unique_id;
  const result = data.result || "completed";

  if (!uniqueId) {
    return c.json(400, { error: "Missing unique_id" });
  }

  try {
    const records = $app.findRecordsByFilter(
      "orders",
      `unique_id = {:uniqueId}`,
      "",
      1,
      0,
      { uniqueId: uniqueId }
    );

    if (records.length === 0) {
      return c.json(404, { error: "Order not found" });
    }

    const record = records[0];
    const extra = record.get("extra") || {};

    // 问题 4 修复: 清除独立的 action 字段
    record.set("action", "");

    // 同时清除 extra 中的 action（向后兼容）
    delete extra.action;
    delete extra.action_params;
    extra.last_action_result = result;
    extra.last_action_time = new Date().toISOString();

    record.set("extra", extra);
    $app.save(record);

    return c.json(200, { success: true });
  } catch (err) {
    console.error("Error acknowledging order action:", err);
    return c.json(500, { error: err.message });
  }
});
