/// <reference path="./pb_data/types.d.ts" />

/**
 * order_manage.pb.js
 * 订单管理 API（替代 GAS order_manager.gs）
 */

// POST /api/custom/ibkr/orders/upsert - 订单 upsert，自动写入 order_details
routerAdd("POST", "/api/custom/ibkr/orders/upsert", (c) => {
  const { notifyNewOrder, notifyOrder, getOrderStatusInfo } = require(`${__hooks}/lib/feishu_order.js`)
  const { appendOrderDetail, getOrderExtra, mergeOrderExtra, resolveOrderRelationship, resolveOrderStatusEventTimes, applyOrderStatusMeta, applyOrderRelationship } = require(`${__hooks}/lib/order_events.js`)
  const envUtils = require(`${__hooks}/lib/environment.js`)
  const data = c.requestInfo().body || c.requestInfo().data || {};

  function firstDefined() {
    for (let i = 0; i < arguments.length; i++) {
      const value = arguments[i]
      if (value !== undefined && value !== null && value !== "") {
        return value
      }
    }
    return undefined
  }

  function normalizeObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {}
  }

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

  // 字段映射：IBKR 发送的字段名 -> PB schema 字段名（已统一）
  const environment = envUtils.getRuntimeEnvironmentFromData(data, envUtils.LIVE_ENVIRONMENT);
  const uniqueId = data.unique_id;
  const orderType = data.order_type;
  const orderId = data.order_id;
  const symbol = data.symbol;
  const direction = data.direction;
  const quantity = data.quantity;
  const price = data.limit_price;
  const status = data.status || "Submitted";
  const avgFillPrice = data.fill_price;
  const extra = envUtils.attachEnvironment(data.extra || {}, environment);
  const reason = extra.reason || "";
  const suppressNotification = Boolean(data.suppress_notification || extra.suppress_notification);

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
        `unique_id = {:uniqueId} && environment = {:env}`,
        "",
        1,
        0,
        { uniqueId: uniqueId, env: environment }
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
    record.set("environment", environment);

    const previousStatus = record ? record.get("status") : "";
    const existingOrderExtra = record ? normalizeObject(getOrderExtra(record)) : {};
    const resolvedOrderId = firstDefined(orderId, record.get("order_id"), existingOrderExtra.order_id, "");
    const resolvedBrokerOrderId = firstDefined(
      data.broker_order_id,
      orderId,
      record.get("broker_order_id"),
      existingOrderExtra.broker_order_id,
      resolvedOrderId,
      ""
    );
    const resolvedDirection = firstDefined(direction, record.get("direction"), existingOrderExtra.direction, "");
    const resolvedQuantity = firstDefined(quantity, record.get("quantity"), existingOrderExtra.quantity, 0);
    const resolvedLimitPrice = firstDefined(price, record.get("limit_price"), existingOrderExtra.limit_price, 0);
    const resolvedFilledQty = firstDefined(
      data.filled_qty,
      status === "Filled" ? quantity : undefined,
      record.get("filled_qty"),
      existingOrderExtra.filled_qty,
      quantity,
      0
    );
    const resolvedFillPrice = firstDefined(avgFillPrice, record.get("fill_price"), existingOrderExtra.fill_price, 0);
    const resolvedSignalId = firstDefined(data.signal_id, record.get("signal_id"), existingOrderExtra.signal_id, "");
    const resolvedTpPrice = firstDefined(
      data.tp_price !== undefined ? parseFloat(data.tp_price) : undefined,
      record.get("tp_price"),
      existingOrderExtra.tp_price
    );
    const resolvedSlPrice = firstDefined(
      data.sl_price !== undefined ? parseFloat(data.sl_price) : undefined,
      record.get("sl_price"),
      existingOrderExtra.sl_price
    );
    const resolvedPnl = firstDefined(
      data.pnl !== undefined ? parseFloat(data.pnl) : undefined,
      record.get("pnl"),
      existingOrderExtra.pnl
    );
    const resolvedCommission = firstDefined(
      data.commission !== undefined ? parseFloat(data.commission) : undefined,
      record.get("commission"),
      existingOrderExtra.commission
    );
    const resolvedRrRatio = firstDefined(
      data.rr_ratio !== undefined ? parseFloat(data.rr_ratio) : undefined,
      record.get("rr_ratio"),
      existingOrderExtra.rr_ratio
    );
    const incomingRelation = resolveOrderRelationship({
      broker_order_id: resolvedBrokerOrderId,
      trade_group_id: data.trade_group_id || extra.trade_group_id || existingOrderExtra.trade_group_id || "",
      entry_order_unique_id: data.entry_order_unique_id || extra.entry_order_unique_id || existingOrderExtra.entry_order_unique_id || "",
      parent_order_unique_id: data.parent_order_unique_id || extra.parent_order_unique_id || existingOrderExtra.parent_order_unique_id || "",
      sibling_order_unique_id: data.sibling_order_unique_id || extra.sibling_order_unique_id || existingOrderExtra.sibling_order_unique_id || "",
      role: data.role || extra.role || existingOrderExtra.role || "",
      relation_status: data.relation_status || extra.relation_status || existingOrderExtra.relation_status || "",
      position_side: data.position_side || resolvedDirection || extra.position_side || existingOrderExtra.position_side || existingOrderExtra.direction || "",
      status: status,
      unique_id: uniqueId,
      order_type: orderType,
    }, record);
    const eventTimes = resolveOrderStatusEventTimes(record, {
      status: status,
      previous_status: previousStatus,
      us_time: data.us_time || extra.us_time || (status === "Filled" ? (data.fill_time || extra.fill_time || "") : ""),
      cn_time: data.cn_time || extra.cn_time,
      bar_time_ms: data.bar_time_ms != null ? data.bar_time_ms : extra.bar_time_ms,
    });
    const resolvedOrderTime = data.order_time || extra.order_time || record.get("order_time") || existingOrderExtra.order_time || eventTimes.us_time;
    const resolvedFillTime = firstDefined(data.fill_time, record.get("fill_time"), existingOrderExtra.fill_time, "");
    const hasExtraDelta = Object.keys(extra).some((key) => JSON.stringify(existingOrderExtra[key]) !== JSON.stringify(extra[key]));
    const isIdempotentUpsert = !!record.id &&
      previousStatus === status &&
      !hasExtraDelta &&
      String(record.get("order_id") || "") === String(resolvedOrderId || "") &&
      String(record.get("symbol") || "") === String(symbol || "") &&
      String(record.get("direction") || "") === String(resolvedDirection || "") &&
      Number(record.get("quantity") || 0) === Number(resolvedQuantity || 0) &&
      Number(record.get("limit_price") || 0) === Number(resolvedLimitPrice || 0) &&
      Number(record.get("filled_qty") || 0) === Number(resolvedFilledQty || 0) &&
      Number(record.get("fill_price") || 0) === Number(resolvedFillPrice || 0) &&
      Number(record.get("tp_price") || 0) === Number(resolvedTpPrice || 0) &&
      Number(record.get("sl_price") || 0) === Number(resolvedSlPrice || 0) &&
      Number(record.get("pnl") || 0) === Number(resolvedPnl || 0) &&
      Number(record.get("commission") || 0) === Number(resolvedCommission || 0) &&
      Number(record.get("rr_ratio") || 0) === Number(resolvedRrRatio || 0) &&
      String(record.get("signal_id") || "") === String(resolvedSignalId || "") &&
      String(record.get("broker_order_id") || "") === String(resolvedBrokerOrderId || "") &&
      String(record.get("order_time") || "") === String(resolvedOrderTime || "") &&
      String(record.get("fill_time") || "") === String(resolvedFillTime || "") &&
      String(record.get("trade_group_id") || "") === String(incomingRelation.trade_group_id || "") &&
      String(record.get("entry_order_unique_id") || "") === String(incomingRelation.entry_order_unique_id || "") &&
      String(record.get("parent_order_unique_id") || "") === String(incomingRelation.parent_order_unique_id || "") &&
      String(record.get("sibling_order_unique_id") || "") === String(incomingRelation.sibling_order_unique_id || "") &&
      String(record.get("role") || "") === String(incomingRelation.role || "") &&
      String(record.get("relation_status") || "") === String(incomingRelation.relation_status || "") &&
      String(record.get("position_side") || "") === String(incomingRelation.position_side || "") &&
      String(record.get("environment") || "") === String(environment) &&
      String(existingOrderExtra.last_status_reason || "") === String(reason || "");

    if (isIdempotentUpsert) {
      console.log(`[OrderUpsert] 幂等跳过重复事件: unique_id=${uniqueId}, status=${status}`);
    } else {
      const mergedOrderExtra = {
        ...existingOrderExtra,
        ...extra,
        environment: environment,
        order_id: resolvedOrderId || existingOrderExtra.order_id || "",
        broker_order_id: resolvedBrokerOrderId || existingOrderExtra.broker_order_id || "",
        order_time: resolvedOrderTime || existingOrderExtra.order_time || "",
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
      };
      record.set("order_type", orderType);
      record.set("order_id", resolvedOrderId || "");
      record.set("broker_order_id", resolvedBrokerOrderId || "");
      record.set("symbol", symbol);
      record.set("environment", environment);
      if (resolvedDirection) record.set("direction", resolvedDirection);
      record.set("quantity", resolvedQuantity);
      record.set("limit_price", resolvedLimitPrice);
      record.set("status", status);
      record.set("filled_qty", resolvedFilledQty);
      record.set("fill_price", resolvedFillPrice);
      record.set("extra", mergedOrderExtra);
      if (resolvedSignalId) record.set("signal_id", resolvedSignalId);
      if (resolvedTpPrice !== undefined) record.set("tp_price", resolvedTpPrice);
      if (resolvedSlPrice !== undefined) record.set("sl_price", resolvedSlPrice);
      if (resolvedPnl !== undefined) record.set("pnl", resolvedPnl);
      if (resolvedCommission !== undefined) record.set("commission", resolvedCommission);
      if (resolvedRrRatio !== undefined) record.set("rr_ratio", resolvedRrRatio);
      record.set("bar_time_ms", eventTimes.bar_time_ms);
      record.set("us_time", eventTimes.us_time);
      record.set("cn_time", eventTimes.cn_time);
      if (resolvedOrderTime) record.set("order_time", resolvedOrderTime);
      if (resolvedFillTime) record.set("fill_time", resolvedFillTime);
      applyOrderRelationship(record, incomingRelation, false)
      applyOrderStatusMeta(record, {
        status: status,
        previous_status: previousStatus || "",
        source: "orders/upsert",
        reason: reason,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        order_time: resolvedOrderTime,
        fill_time: resolvedFillTime || "",
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
        environment: environment,
        status: status,
        source: "orders/upsert",
        reason: reason,
        us_time: eventTimes.us_time,
        cn_time: eventTimes.cn_time,
        bar_time_ms: eventTimes.bar_time_ms,
        order_time: resolvedOrderTime,
        extra: {
          environment: environment,
          fill_time: resolvedFillTime,
          ...extra,
        },
      });
      console.log(`[OrderUpsert] order_details 已保存: order_id=${uniqueId}, order_type=${orderType}, status=${status}`);
    }

    // 同步飞书订单卡片
    if (suppressNotification) {
      console.log(`[OrderUpsert] 跳过飞书同步: unique_id=${uniqueId}, status=${status}, suppress_notification=true`);
    } else {
      try {
        const orderExtra = getOrderExtra(record);
        const statusInfo = getOrderStatusInfo(status);
        let notifyResult = null;

        if (!orderExtra.feishu_order_message_id && (status === "Submitted" || status === "Filled" || status === "Canceled" || status === "Closed")) {
          console.log(`[OrderUpsert] 首次发送订单卡片: unique_id=${uniqueId}, status=${status}`);
          notifyResult = notifyNewOrder(record, { message: statusInfo.message });
        } else if (!isIdempotentUpsert && (status === "Submitted" || status === "Filled" || status === "Canceled" || status === "Closed")) {
          console.log(`[OrderUpsert] 同步订单卡片: unique_id=${uniqueId}, previous_status=${previousStatus || "-"}, status=${status}`);
          notifyResult = notifyOrder(status.toLowerCase(), record, {
            messageId: orderExtra.feishu_order_message_id || "",
            message: statusInfo.message,
          });
        }

        if (notifyResult && notifyResult.success && notifyResult.message_id && notifyResult.message_id !== orderExtra.feishu_order_message_id) {
          mergeOrderExtra(record, {
            feishu_order_message_id: notifyResult.message_id,
            feishu_order_card_version: 2,
          }, true);
        }
      } catch (err) {
        console.error("[Feishu] 同步订单卡片失败:", err);
      }
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
        status: status,
        environment: environment
      }
    });
  } catch (err) {
    console.error(`[OrderUpsert] === 订单 Upsert 失败 === error:`, err.message);
    console.error(`[OrderUpsert] 堆栈:`, err.stack);
    return c.json(500, { error: err.message });
  }
});

// POST /api/custom/ibkr/orders/reconcile - 回补 orders 缺失的 order_details
routerAdd("POST", "/api/custom/ibkr/orders/reconcile", (c) => {
  const { appendOrderDetail } = require(`${__hooks}/lib/order_events.js`)
  const envUtils = require(`${__hooks}/lib/environment.js`)
  const request = c.requestInfo().body || c.requestInfo().data || {}

  function parseBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback
    const text = String(value).trim().toLowerCase()
    if (["1", "true", "yes", "y", "on"].includes(text)) return true
    if (["0", "false", "no", "n", "off"].includes(text)) return false
    return fallback
  }

  try {
    const environment = envUtils.getRuntimeEnvironmentFromData(request, envUtils.LIVE_ENVIRONMENT)
    const signalId = String(request.signal_id || "").trim()
    const limit = Math.max(1, Math.min(100, parseInt(request.limit, 10) || 20))
    const onlyMissing = parseBoolean(request.only_missing, true)
    const dryRun = parseBoolean(request.dry_run, false)
    const suppressNotification = parseBoolean(request.suppress_notification, true)

    const params = { env: environment, signalId: signalId }
    const filters = ["environment = {:env}"]
    if (signalId) {
      filters.push("signal_id = {:signalId}")
    }

    const orderRecords = $app.findRecordsByFilter(
      "orders",
      filters.join(" && "),
      "-updated,-created",
      limit,
      0,
      params
    ) || []

    const results = []
    let repaired = 0
    let skipped = 0
    let failed = 0

    for (let i = 0; i < orderRecords.length; i++) {
      const orderRecord = orderRecords[i]
      const currentSignalId = String(orderRecord.get("signal_id") || "").trim()
      const uniqueId = String(orderRecord.get("unique_id") || "").trim()
      const symbol = String(orderRecord.get("symbol") || "").trim()
      const status = String(orderRecord.get("status") || "").trim() || "Submitted"

      if (!uniqueId || !symbol) {
        skipped++
        results.push({
          signal_id: currentSignalId,
          unique_id: uniqueId,
          status: "skipped_invalid_source",
          symbol: symbol,
        })
        continue
      }

      const existingDetails = $app.findRecordsByFilter(
        "order_details",
        "order_id = {:orderId} && environment = {:env}",
        "-bar_time_ms",
        1,
        0,
        { orderId: uniqueId, env: environment }
      ) || []

      if (onlyMissing && existingDetails.length > 0) {
        skipped++
        results.push({
          signal_id: currentSignalId,
          unique_id: uniqueId,
          status: "skipped_existing_detail",
          symbol: symbol,
          detail_record_id: existingDetails[0].id,
        })
        continue
      }

      const payload = {
        environment: environment,
        unique_id: uniqueId,
        symbol: symbol,
        status: status,
        signal_id: currentSignalId,
        order_type: String(orderRecord.get("order_type") || "").trim(),
        order_id: String(orderRecord.get("order_id") || "").trim(),
        broker_order_id: String(orderRecord.get("broker_order_id") || "").trim(),
        us_time: String(orderRecord.get("us_time") || "").trim(),
        cn_time: String(orderRecord.get("cn_time") || "").trim(),
        bar_time_ms: Number(orderRecord.get("bar_time_ms") || 0) || 0,
        suppress_notification: suppressNotification,
      }

      if (dryRun) {
        repaired++
        results.push({
          signal_id: currentSignalId,
          unique_id: uniqueId,
          status: "dry_run_ready",
          symbol: symbol,
          payload: payload,
        })
        continue
      }

      try {
        const detailRecord = appendOrderDetail(orderRecord, {
          environment: environment,
          status: status,
          source: "orders/reconcile",
          reason: onlyMissing ? "reconciled_missing_order_details" : "reconciled_order_snapshot",
          us_time: payload.us_time,
          cn_time: payload.cn_time,
          bar_time_ms: payload.bar_time_ms,
          extra: {
            repair_source: "orders/reconcile",
            suppress_notification: suppressNotification,
          },
        })

        repaired++
        results.push({
          signal_id: currentSignalId,
          unique_id: uniqueId,
          status: "repaired_detail",
          symbol: symbol,
          detail_record_id: detailRecord.id,
        })
      } catch (err) {
        failed++
        results.push({
          signal_id: currentSignalId,
          unique_id: uniqueId,
          status: "failed_exception",
          symbol: symbol,
          error: err.message || String(err),
        })
      }
    }

    return c.json(200, {
      success: true,
      environment: environment,
      dry_run: dryRun,
      only_missing: onlyMissing,
      suppress_notification: suppressNotification,
      summary: {
        scanned: orderRecords.length,
        repaired: repaired,
        skipped: skipped,
        failed: failed,
      },
      results: results,
    })
  } catch (err) {
    console.error("[OrderReconcile] 失败:", err)
    return c.json(500, { success: false, error: err.message || String(err) })
  }
});
