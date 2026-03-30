/// <reference path="./pb_data/types.d.ts" />

/**
 * order_manage.pb.js
 * 订单管理 API（替代 GAS order_manager.gs）
 */

// POST /api/custom/orders/upsert - 订单 upsert，自动写入 order_details
routerAdd("POST", "/api/custom/orders/upsert", (c) => {
  const { notifyNewOrder, notifyOrder, getOrderStatusInfo } = require(`${__hooks}/lib/feishu_order.js`)
  const { appendOrderDetail, getOrderExtra, mergeOrderExtra, resolveOrderStatusEventTimes, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
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
  console.log(`[OrderUpsert] trade_group_id: ${data.trade_group_id}`);
  console.log(`[OrderUpsert] entry_order_unique_id: ${data.entry_order_unique_id}`);
  console.log(`[OrderUpsert] parent_order_unique_id: ${data.parent_order_unique_id}`);
  console.log(`[OrderUpsert] sibling_order_unique_id: ${data.sibling_order_unique_id}`);
  console.log(`[OrderUpsert] role: ${data.role}`);
  console.log(`[OrderUpsert] relation_status: ${data.relation_status}`);
  console.log(`[OrderUpsert] extra: ${JSON.stringify(data.extra || {})}`);

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
  const brokerOrderId = data.broker_order_id || orderId || "";

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
    const previousStatus = record ? record.get("status") : "";
    const eventTimes = resolveOrderStatusEventTimes(record, {
      status: status,
      previous_status: previousStatus,
      us_time: data.us_time || extra.us_time || (status === "Filled" ? (data.fill_time || extra.fill_time || "") : ""),
      cn_time: data.cn_time || extra.cn_time,
      bar_time_ms: data.bar_time_ms != null ? data.bar_time_ms : extra.bar_time_ms,
    });
    const existingOrderExtra = record ? getOrderExtra(record) : {};
    const resolvedOrderTime = data.order_time || extra.order_time || record.get("order_time") || existingOrderExtra.order_time || eventTimes.us_time;
    const mergedOrderExtra = {
      ...existingOrderExtra,
      ...extra,
      order_time: resolvedOrderTime || existingOrderExtra.order_time || "",
      us_time: eventTimes.us_time,
      cn_time: eventTimes.cn_time,
      bar_time_ms: eventTimes.bar_time_ms,
    };
    record.set("order_type", orderType);
    record.set("order_id", orderId || "");
    record.set("broker_order_id", brokerOrderId);
    record.set("symbol", symbol);
    if (direction) record.set("direction", direction);
    record.set("quantity", quantity);
    record.set("limit_price", price);
    record.set("status", status);
    record.set("filled_qty", filledQty);
    record.set("fill_price", avgFillPrice);
    record.set("extra", mergedOrderExtra);
    if (data.signal_id) record.set("signal_id", data.signal_id);
    if (data.tp_price !== undefined) record.set("tp_price", parseFloat(data.tp_price));
    if (data.sl_price !== undefined) record.set("sl_price", parseFloat(data.sl_price));
    if (data.pnl !== undefined) record.set("pnl", parseFloat(data.pnl));
    if (data.commission !== undefined) record.set("commission", parseFloat(data.commission));
    if (data.rr_ratio !== undefined) record.set("rr_ratio", parseFloat(data.rr_ratio));
    record.set("bar_time_ms", eventTimes.bar_time_ms);
    record.set("us_time", eventTimes.us_time);
    record.set("cn_time", eventTimes.cn_time);
    if (resolvedOrderTime) record.set("order_time", resolvedOrderTime);
    if (data.fill_time) record.set("fill_time", data.fill_time);
    applyOrderRelationship(record, {
      broker_order_id: brokerOrderId,
      trade_group_id: data.trade_group_id || extra.trade_group_id || existingOrderExtra.trade_group_id || "",
      entry_order_unique_id: data.entry_order_unique_id || extra.entry_order_unique_id || existingOrderExtra.entry_order_unique_id || "",
      parent_order_unique_id: data.parent_order_unique_id || extra.parent_order_unique_id || existingOrderExtra.parent_order_unique_id || "",
      sibling_order_unique_id: data.sibling_order_unique_id || extra.sibling_order_unique_id || existingOrderExtra.sibling_order_unique_id || "",
      role: data.role || extra.role || existingOrderExtra.role || "",
      relation_status: data.relation_status || extra.relation_status || existingOrderExtra.relation_status || "",
      position_side: data.position_side || data.direction || extra.position_side || existingOrderExtra.position_side || existingOrderExtra.direction || "",
      status: status,
      unique_id: uniqueId,
      order_type: orderType,
    }, false)
    applyOrderStatusMeta(record, {
      status: status,
      previous_status: previousStatus || "",
      source: "orders/upsert",
      reason: extra.reason || "",
      us_time: eventTimes.us_time,
      cn_time: eventTimes.cn_time,
      bar_time_ms: eventTimes.bar_time_ms,
      order_time: resolvedOrderTime,
      fill_time: data.fill_time || "",
      fill_us_time: status === "Filled" ? eventTimes.us_time : "",
      fill_cn_time: status === "Filled" ? eventTimes.cn_time : "",
      fill_bar_time_ms: status === "Filled" ? eventTimes.bar_time_ms : 0,
      created_us_time: !previousStatus ? eventTimes.us_time : "",
      created_cn_time: !previousStatus ? eventTimes.cn_time : "",
      created_bar_time_ms: !previousStatus ? eventTimes.bar_time_ms : 0,
    }, false)

    $app.save(record);
    console.log(`[OrderUpsert] 状态落库: unique_id=${uniqueId}, previous_status=${previousStatus || "-"}, new_status=${status}`);

    // 写入 order_details（通过 appendOrderDetail 写入完整 order 镜像）
    appendOrderDetail(record, {
      status: status,
      source: "orders/upsert",
      reason: extra.reason || "",
      us_time: eventTimes.us_time,
      cn_time: eventTimes.cn_time,
      bar_time_ms: eventTimes.bar_time_ms,
      order_time: resolvedOrderTime,
      extra: {
        fill_time: data.fill_time,
        ...extra,
      },
    });
    console.log(`[OrderUpsert] order_details 已保存: order_id=${uniqueId}, order_type=${orderType}, status=${status}`);

    // 同步飞书订单卡片
    try {
      const orderExtra = getOrderExtra(record);
      const statusInfo = getOrderStatusInfo(status);
      let notifyResult = null;

      if (status === "Submitted" && !orderExtra.feishu_order_message_id) {
        console.log(`[OrderUpsert] 首次发送订单卡片: unique_id=${uniqueId}, status=${status}`);
        notifyResult = notifyNewOrder(record, { message: statusInfo.message });
      } else if (status === "Submitted" || status === "Filled" || status === "Canceled" || status === "Closed") {
        console.log(`[OrderUpsert] 同步订单卡片: unique_id=${uniqueId}, previous_status=${previousStatus || "-"}, status=${status}`);
        notifyResult = notifyOrder(status.toLowerCase(), record, {
          messageId: orderExtra.feishu_order_message_id || "",
          message: statusInfo.message,
        });
      }

      if (notifyResult && notifyResult.success && notifyResult.message_id && notifyResult.message_id !== orderExtra.feishu_order_message_id) {
        mergeOrderExtra(record, {
          feishu_order_message_id: notifyResult.message_id,
          feishu_order_card_version: 1,
        }, true);
      }
    } catch (err) {
      console.error("[Feishu] 同步订单卡片失败:", err);
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
