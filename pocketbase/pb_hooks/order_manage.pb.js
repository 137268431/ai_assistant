/// <reference path="./pb_data/types.d.ts" />

/**
 * order_manage.pb.js
 * 订单管理 API（替代 GAS order_manager.gs）
 */

// POST /api/custom/orders/upsert - 订单 upsert，自动写入 order_details
routerAdd("POST", "/api/custom/orders/upsert", (c) => {
  const { notifyNewOrder, notifyOrder } = require(`${__hooks}/feishu_app.js`)
  const data = c.requestInfo().body || c.requestInfo().data || {};

  console.log(`[OrderUpsert] === 订单 Upsert 开始 ===`);
  console.log(`[OrderUpsert] unique_id: ${data.unique_id}`);
  console.log(`[OrderUpsert] order_type: ${data.order_type}`);
  console.log(`[OrderUpsert] symbol: ${data.symbol}`);
  console.log(`[OrderUpsert] direction: ${data.direction}`);
  console.log(`[OrderUpsert] quantity: ${data.quantity}`);
  console.log(`[OrderUpsert] limit_price: ${data.limit_price}`);
  console.log(`[OrderUpsert] status: ${data.status}`);
  console.log(`[OrderUpsert] signal_id: ${data.signal_id}`);
  console.log(`[OrderUpsert] fill_price: ${data.fill_price}`);
  console.log(`[OrderUpsert] filled_qty: ${data.filled_qty}`);

  // 字段映射：QC 发送的字段名 -> PB schema 字段名（已统一）
  const uniqueId = data.unique_id;
  const orderType = data.order_type;
  const orderId = data.order_id;
  const symbol = data.symbol;
  const direction = data.direction;
  const quantity = data.quantity;
  const price = data.limit_price;
  const status = data.status || "Submitted";
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
    if (data.tp_price !== undefined) record.set("tp_price", parseFloat(data.tp_price));
    if (data.sl_price !== undefined) record.set("sl_price", parseFloat(data.sl_price));
    if (data.pnl !== undefined) record.set("pnl", parseFloat(data.pnl));
    if (data.commission !== undefined) record.set("commission", parseFloat(data.commission));
    if (data.rr_ratio !== undefined) record.set("rr_ratio", parseFloat(data.rr_ratio));
    if (data.bar_time_ms != null) record.set("bar_time_ms", parseInt(data.bar_time_ms) || 0);
    if (data.us_time) record.set("us_time", data.us_time);
    if (data.cn_time) record.set("cn_time", data.cn_time);
    if (data.order_time) record.set("order_time", data.order_time);
    if (data.fill_time) record.set("fill_time", data.fill_time);

    $app.save(record);

    // 写入 order_details（用于详细日志）
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
    detailRecord.set("order_id", uniqueId);
    detailRecord.set("symbol", symbol);
    detailRecord.set("direction", direction || "");
    detailRecord.set("order_type", orderType);
    detailRecord.set("status", status);
    detailRecord.set("reason", extra.reason || "");
    detailRecord.set("signal_id", data.signal_id || "");
    // 使用 QC 传来的时间，回测时这是真实交易时间
    const qcUsTime = data.us_time || "";
    const qcCnTime = data.cn_time || "";
    const qcBarTime = data.bar_time_ms || 0;
    const now = new Date();
    detailRecord.set("us_time", qcUsTime || now.toISOString().replace('T', ' ').substring(0, 19));
    detailRecord.set("cn_time", qcCnTime || new Date(now.getTime() + 8*60*60*1000).toISOString().replace('T', ' ').substring(0, 19));
    detailRecord.set("bar_time_ms", qcBarTime || now.getTime());
    // 在 extra 中保存完整的订单信息和序列号
    const detailExtra = {
      sequence: sequence,
      status: status,
      original_order_id: orderId,
      quantity: quantity,
      limit_price: price,
      fill_price: avgFillPrice,
      filled_qty: filledQty,
      tp_price: data.tp_price,
      sl_price: data.sl_price,
      ...extra
    };
    detailRecord.set("extra", detailExtra);

    $app.save(detailRecord);
    console.log(`[OrderUpsert] order_details 已保存: order_id=${uniqueId}, order_type=${orderType}, status=${status}, sequence=${sequence}`);

    // 发送飞书通知（仅关键状态变化）
    try {
      let shouldNotify = false;
      let notifyAction = "";

      if (status === "Submitted" && sequence === 1 && orderType === "Entry") {
        shouldNotify = true;
        notifyAction = "created";
        console.log(`[OrderUpsert] 触发通知: 新的 Entry 订单已提交`);
      } else if (status === "Filled") {
        shouldNotify = true;
        notifyAction = "filled";
        console.log(`[OrderUpsert] 触发通知: 订单已成交`);
      } else if (status === "Canceled") {
        shouldNotify = true;
        notifyAction = "canceled";
        console.log(`[OrderUpsert] 触发通知: 订单已取消`);
      } else if (status === "Closed") {
        shouldNotify = true;
        notifyAction = "closed";
        console.log(`[OrderUpsert] 触发通知: 订单已平仓`);
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

        if (notifyAction === "created" && orderType === "Entry") {
          console.log(`[OrderUpsert] 发送飞书交互卡片通知`);
          notifyNewOrder(orderData);
        } else {
          notifyOrder(notifyAction, orderData);
        }
      }
    } catch (err) {
      console.error("[Feishu] 构建订单通知失败:", err);
    }

    console.log(`[OrderUpsert] === 订单 Upsert 完成 === success=true, order_id=${record.id}, unique_id=${uniqueId}, status=${status}`);
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
    console.error(`[OrderUpsert] === 订单 Upsert 失败 === error:`, err.message);
    console.error(`[OrderUpsert] 堆栈:`, err.stack);
    return c.json(500, { error: err.message });
  }
});
