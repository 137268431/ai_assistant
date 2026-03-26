/// <reference path="./pb_data/types.d.ts" />

const { sendFeishuPost } = require(`${__hooks}/feishu.js`);

/**
 * reverse_signals.pb.js
 * 逆向信号计算、查询、确认 API
 */

// POST /api/custom/reverse/calculate - 从 indicators 计算逆向信号并写入
routerAdd("POST", "/api/custom/reverse/calculate", (c) => {
  const data = c.requestInfo().body || c.requestInfo().data || {};
  const symbol = data.symbol;
  const direction = data.direction; // 'long' or 'short'

  if (!symbol || !direction) {
    return c.json(400, { error: "Missing symbol or direction" });
  }

  try {
    // 获取最新的 indicators 数据
    const indicators = $app.findRecordsByFilter(
      "indicators",
      `symbol = {:symbol}`,
      "-bar_time_ms",
      1,
      0,
      { symbol: symbol }
    );

    if (!indicators || indicators.length === 0) {
      return c.json(404, { error: "No indicators found for symbol" });
    }

    const ind = indicators[0];
    // C1 修复: 所有指标数据存在 extra JSON 内，不是顶层字段
    const extra = ind.get("extra") || {};
    const crsi = extra.crsi;
    const obvRsi = extra.obv_rsi;
    const vwapDist = extra.vwap_dist;
    const close = extra.close;

    // 计算逆向信号分数
    let score = 0;
    const triggeredSignals = [];

    // cRSI 超买超卖 (+2分)
    if (direction === "long" && crsi > 70) {
      score += 2;
      triggeredSignals.push("cRSI超买");
    } else if (direction === "short" && crsi < 30) {
      score += 2;
      triggeredSignals.push("cRSI超卖");
    }

    // C2 修复: 字段名与 webhook 存储的实际 key 对齐，且需区分方向
    // long 持仓找反转 → 看看空信号（bear 背离/熊信号）
    // short 持仓找反转 → 看看多信号（bull 背离/牛信号）
    const isBearSignal = direction === "long";
    const crsiBearDiv = isBearSignal ? extra.crsi_bear_div : extra.crsi_bull_div;
    const obvBearDiv  = isBearSignal ? extra.obv_bear_div  : extra.obv_bull_div;
    const fractalRev  = isBearSignal ? extra.fractal_bear  : extra.fractal_bull;
    const sdChannelRev = isBearSignal ? extra.sd_upper     : extra.sd_lower;
    const emaTouchRev  = isBearSignal ? extra.ema_bear_touch : extra.ema_bull_touch;

    // cRSI/OBV 背离 (+3分)
    if (crsiBearDiv || obvBearDiv) {
      score += 3;
      triggeredSignals.push("背离");
    }

    // 分形/SD通道 (+2分)
    if (fractalRev || sdChannelRev) {
      score += 2;
      triggeredSignals.push("分形/SD通道");
    }

    // VWAP偏离/EMA触碰 (+1分)
    if ((vwapDist != null && Math.abs(vwapDist) > 2) || emaTouchRev) {
      score += 1;
      triggeredSignals.push("VWAP偏离/EMA触碰");
    }

    // 确定强度
    let strength = "weak";
    if (score >= 6) {
      strength = "strong";
    } else if (score >= 3) {
      strength = "medium";
    }

    // 确定操作类型
    let actionType = "close";
    if (strength === "strong") {
      actionType = "close";
    } else if (strength === "medium") {
      actionType = "adjust_sl";
    } else {
      actionType = "cancel";
    }

    // 写入 reverse_signals 表
    const collection = $app.findCollectionByNameOrId("reverse_signals");
    const record = new Record(collection, {});
    record.set("symbol", symbol);
    record.set("direction", direction);
    record.set("source", "indicator");  // 来源：技术指标
    record.set("priority", 5);  // 技术指标优先级为 5（低于 signal 的 1）
    record.set("strength", strength);
    record.set("score", score);
    record.set("triggered_signals", triggeredSignals);
    record.set("action_type", actionType);
    record.set("status", "pending");
    // 技术指标和 origin_signal_id 都放到 extra
    const extraData = {
      crsi: crsi,
      obv_rsi: obvRsi,
      vwap_dist: vwapDist,
      close: close,
      origin_signal_id: "",
      signal_id: "",  // QC 回写后填充
      ...extra  // 合并其他指标数据
    };
    record.set("extra", extraData);
    record.set("bar_time_ms", ind.get("bar_time_ms"));
    record.set("us_time", ind.get("us_time") || "");
    record.set("cn_time", ind.get("cn_time") || "");

    $app.save(record);

    // 发送飞书逆向信号通知（score >= 配置的阈值时，默认6）
    let threshold = 6;
    try {
        const cfg = $app.findFirstRecordByFilter("config", "key = 'reverse_signal_threshold'");
        threshold = cfg ? (parseInt(cfg.get("value")) || 6) : 6;
        console.log(`[ReverseSignal] reverse_signal_threshold 配置值: ${threshold}`);
    } catch (e) {
        console.log(`[ReverseSignal] reverse_signal_threshold 配置读取失败，使用默认值 6: ${e}`);
    }
    if (score >= threshold) {
        const dirEmoji = direction === "long" ? "📈" : "📉";
        const dirText = direction === "long" ? "多" : "空";
        sendFeishuPost(
            `⚠️ 指标逆向信号 - ${symbol}`,
            [
                [{ tag: "text", text: `标的: ${symbol}` }],
                [{ tag: "text", text: `方向: ${dirText} ${dirEmoji}` }],
                [{ tag: "text", text: `强度: ${strength} (score=${score})` }],
                [{ tag: "text", text: `触发: ${triggeredSignals.join(', ')}` }],
                [{ tag: "text", text: `时间: ${ind.get("us_time") || ''}` }]
            ],
            "error"
        );
    }

    return c.json(200, {
      success: true,
      signal: {
        id: record.id,
        symbol: symbol,
        direction: direction,
        strength: strength,
        score: score,
        action_type: actionType,
        triggered_signals: triggeredSignals
      }
    });
  } catch (err) {
    console.error("Error calculating reverse signal:", err);
    return c.json(500, { error: err.message });
  }
});

// GET /api/custom/reverse/pending - 获取未处理的逆向信号
routerAdd("GET", "/api/custom/reverse/pending", (c) => {
  try {
    const records = $app.findRecordsByFilter(
      "reverse_signals",
      "status = 'pending'",
      "-bar_time_ms",
      100,
      0
    );

    const signals = (records || []).map((r) => {
      const extra = r.get("extra") || {};
      return {
        id: r.id,
        symbol: r.get("symbol"),
        direction: r.get("direction"),
        source: r.get("source"),
        priority: r.get("priority"),
        signal_id: extra.signal_id,  // QC 回写的原始信号ID（权威）
        origin_signal_id: extra.origin_signal_id,  // webhook 触发时的原始信号ID
        order_id: extra.order_id,
        strength: r.get("strength"),
        score: r.get("score"),
        action_type: r.get("action_type"),
        status: r.get("status"),
        reason: r.get("reason"),
        triggered_signals: r.get("triggered_signals"),
        // 技术指标从 extra 中获取
        crsi: extra.crsi,
        obv_rsi: extra.obv_rsi,
        vwap_dist: extra.vwap_dist,
        close: extra.close,
        extra: extra,
        bar_time_ms: r.get("bar_time_ms"),
        created: r.get("created")
      };
    });

    return c.json(200, { signals: signals });
  } catch (err) {
    console.error("Error fetching pending reverse signals:", err);
    return c.json(500, { error: err.message });
  }
});

// POST /api/custom/reverse/ack - 标记反转信号状态
routerAdd("POST", "/api/custom/reverse/ack", (c) => {
  const data = c.requestInfo().body || c.requestInfo().data || {};
  const signalId = data.signal_id;
  const status = data.status || 'confirmed';  // pending/confirmed/cancelled/expired
  const reason = data.reason || '';

  if (!signalId) {
    return c.json(400, { error: "Missing signal_id" });
  }

  try {
    const record = $app.findRecordById("reverse_signals", signalId);
    record.set("status", status);
    record.set("reason", reason);
    record.set("processed_time", new Date().toISOString());

    // 保存 order_id 和 signal_id 到 extra
    const extra = record.get("extra") || {};
    if (data.order_id) {
      extra.order_id = data.order_id;
    }
    if (data.signal_id_orig) {
      extra.signal_id = data.signal_id_orig;
    }
    record.set("extra", extra);

    $app.save(record);

    return c.json(200, { success: true });
  } catch (err) {
    console.error("Error acknowledging reverse signal:", err);
    return c.json(500, { error: err.message });
  }
});
