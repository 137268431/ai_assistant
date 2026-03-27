/// <reference path="./pb_data/types.d.ts" />

/**
 * webhook_tv.pb.js
 * TradingView Webhook 接收端点
 * 支持信号表和技术指标表的写入
 */

routerAdd("POST", "/webhook/tv", (c) => {
    const { notifyNewSignal, sendFeishuPost } = require(`${__hooks}/feishu_app.js`)

    // ── 解析请求信息（含 body）──
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}

    // ── 路由：根据 type 字段区分写入信号表还是技术指标表 ──
    const dataType = d.type || "signal"

    // ══════════════════════════════════════
    // 技术指标表（type = "indicator"）
    // ══════════════════════════════════════
    if (dataType === "indicator") {
        // 以 bar_time_ms + symbol + interval 作为去重 key
        const dedupKey = String(d.bar_time_ms) + "_" + d.symbol + "_" + d.interval
        try {
            $app.findFirstRecordByFilter("indicators",
                "bar_time_ms = {:ms} && symbol = {:sym} && interval = {:tf}",
                { ms: d.bar_time_ms, sym: d.symbol, tf: d.interval }
            )
            return c.json(200, { ok: true, msg: "duplicate indicator, skipped", key: dedupKey })
        } catch(_) {}

        const col = $app.findCollectionByNameOrId("indicators")
        const rec = new Record(col, {})

        // 固定字段（用于查询、去重、筛选）
        rec.set("symbol",      d.symbol)
        rec.set("exchange",    d.exchange   || "")
        rec.set("interval",    d.interval   || "")
        rec.set("script_tag",  d.script_tag || "")
        rec.set("us_time",     d.us_time    || "")
        rec.set("cn_time",     d.cn_time    || "")
        rec.set("bar_time_ms", d.bar_time_ms)
        rec.set("bar_index",   d.bar_index)

        // 所有指标值统一放入 extra JSON 字段
        const extra = d.extra || {}
        rec.set("extra", extra)

        $app.save(rec)
        return c.json(200, { ok: true, type: "indicator" })
    }

    // ══════════════════════════════════════
    // 信号表（type = "signal"）
    // ══════════════════════════════════════
    // ── 打印接收到的信号数据日志 ──
    console.log("[Webhook TV] 接收信号数据:", JSON.stringify(d, null, 2))

    try {
        $app.findFirstRecordByData("signals", "signal_id", d.signal_id)
        return c.json(200, { ok: true, msg: "duplicate, skipped" })
    } catch(_) {}

    // 必需字段验证
    const requiredFields = ['symbol', 'direction', 'entry', 'stop_loss', 'take_profit', 'signal_id'];
    for (const field of requiredFields) {
        if (!d[field]) {
            return c.json(400, {
                ok: false,
                error: `Missing required field: ${field}`,
                signal_id: d.signal_id || 'unknown'
            });
        }
    }

    // 数据类型验证
    const numericFields = ['entry', 'stop_loss', 'take_profit'];
    for (const field of numericFields) {
        if (typeof d[field] !== 'number' || d[field] <= 0) {
            return c.json(400, {
                ok: false,
                error: `Invalid ${field}: must be positive number`,
                signal_id: d.signal_id
            });
        }
    }

    // 验证方向
    if (!['long', 'short'].includes(d.direction)) {
        return c.json(400, {
            ok: false,
            error: `Invalid direction: must be 'long' or 'short'`,
            signal_id: d.signal_id
        });
    }

    // extra 直接使用请求中的 extra
    const extra = d.extra || {}

    const col = $app.findCollectionByNameOrId("signals")
    const record = new Record(col, {})

    // date 字段提取
    let dateStr = ""
    const usTimeStr = d.us_time || ""
    if (usTimeStr) {
        const match = usTimeStr.match(/(\d{4}-\d{2}-\d{2})/);
        if (match) {
            dateStr = match[1];
        }
    }
    if (!dateStr) {
        dateStr = new Date().toISOString().substring(0, 10);
    }

    record.set("symbol",      d.symbol)
    record.set("direction",   d.direction)
    record.set("signal",      d.signal)
    record.set("limit_price", d.limit_price)
    record.set("entry",       d.entry)
    record.set("stop_loss",   d.stop_loss)
    record.set("take_profit", d.take_profit)
    record.set("rr",          d.rr)
    record.set("shares",      d.shares)
    record.set("signal_id",   d.signal_id)
    record.set("exchange",    d.exchange)
    record.set("interval",    d.interval)
    record.set("reason",      extra.reason)
    record.set("us_time",     d.us_time || "")
    record.set("cn_time",     d.cn_time || "")
    record.set("date",        dateStr)
    record.set("bar_time_ms", extra.bar_time_ms)
    record.set("bar_index",   extra.bar_index)
    record.set("script_tag",  extra.script_tag)
    record.set("chart_tf",    extra.chart_tf)
    record.set("extra",       extra)

    // 根据配置决定初始状态
    let initialStatus = "pending";  // 默认 pending（自动确认）
    try {
        const configRecord = $app.findFirstRecordByFilter("config", "key = 'signal_auto_confirm'");
        const autoConfirm = configRecord ? configRecord.get("value") : null;
        console.log(`[Webhook] signal_auto_confirm 配置值: ${autoConfirm}`);
        if (autoConfirm && autoConfirm.toLowerCase() === "false") {
            initialStatus = "awaiting_confirm";  // 需要手动确认
            console.log(`[Webhook] 信号 ${d.signal_id} 设置为 awaiting_confirm（需手动确认）`);
        } else {
            console.log(`[Webhook] 信号 ${d.signal_id} 设置为 pending（自动确认）`);
        }
    } catch (err) {
        console.log(`[Webhook] signal_auto_confirm 配置读取失败，使用默认值 pending: ${err}`);
        // 配置不存在，使用默认值 pending
    }
    record.set("status", initialStatus)

    $app.save(record)

    // 发送飞书通知
    try {
        notifyNewSignal({
            symbol: d.symbol,
            direction: d.direction,
            entry: d.entry,
            take_profit: d.take_profit,
            stop_loss: d.stop_loss,
            rr: d.rr,
            shares: d.shares,
            signal_id: d.signal_id,
            us_time: d.us_time || "",
            reason: extra.reason,
            extra: extra,
            status: initialStatus
        });
    } catch (err) {
        console.error("[Feishu] 发送信号通知失败:", err);
    }

    // ── 逆向信号自动检测：查询该 symbol 是否有活跃订单与新信号方向冲突 ──
    try {
        const activeOrders = $app.findRecordsByFilter(
            "orders",
            `symbol = {:symbol} && order_type = 'Entry' && (status = 'Submitted' || status = 'Filled')`,
            "-created", 10, 0,
            { symbol: d.symbol }
        );

        const reverseCol = $app.findCollectionByNameOrId("reverse_signals");
        let reverseCount = 0;

        for (const order of activeOrders) {
            const orderDirection = order.get("direction");
            // 方向冲突：新信号 long vs 持仓 short，或反之
            if (orderDirection && orderDirection !== d.direction) {
                const orderStatus = order.get("status");
                const actionType = orderStatus === "Filled" ? "close" : "cancel";
                const actionLabel = actionType === "close" ? "平仓" : "取消挂单";

                const revRecord = new Record(reverseCol, {});
                revRecord.set("symbol", d.symbol);
                revRecord.set("direction", orderDirection);  // 当前持仓/挂单方向
                revRecord.set("source", "signal");
                revRecord.set("priority", 1);
                revRecord.set("strength", "strong");
                revRecord.set("action_type", actionType);
                revRecord.set("triggered_signals", ["信号反转"]);
                revRecord.set("score", 10);
                revRecord.set("status", "pending");
                revRecord.set("bar_time_ms", extra.bar_time_ms || 0);
                revRecord.set("us_time", d.us_time || "");
                revRecord.set("cn_time", d.cn_time || "");
                // 原始信号ID存为 origin_signal_id，与QC回写的 signal_id 区分
                revRecord.set("extra", {
                    origin_signal_id: d.signal_id,
                    signal_id: ""  // QC 回写后填充
                });

                $app.save(revRecord);
                reverseCount++;

                // 发送飞书逆向信号通知（score >= 配置的阈值时，默认6）
                let threshold = 6;
                try {
                    const cfg = $app.findFirstRecordByFilter("config", "key = 'reverse_signal_threshold'");
                    threshold = cfg ? (parseInt(cfg.get("value")) || 6) : 6;
                    console.log(`[Webhook] reverse_signal_threshold 配置值: ${threshold}`);
                } catch (e) {
                    console.log(`[Webhook] reverse_signal_threshold 配置读取失败，使用默认值 6: ${e}`);
                }
                if (revRecord.get("score") >= threshold) {
                    const dirEmoji = d.direction === "long" ? "📈" : "📉";
                    const oldDirText = orderDirection === "long" ? "多" : "空";
                    const newDirText = d.direction === "long" ? "多" : "空";
                    sendFeishuPost(
                        `⚠️ 逆向信号 - ${d.symbol}`,
                        [
                            [{ tag: "text", text: `标的: ${d.symbol}` }],
                            [{ tag: "text", text: `当前方向: ${oldDirText} → 新信号: ${newDirText} ${dirEmoji}` }],
                            [{ tag: "text", text: `操作: ${actionLabel}` }],
                            [{ tag: "text", text: `订单状态: ${orderStatus}` }],
                            [{ tag: "text", text: `信号ID: ${d.signal_id}` }],
                            [{ tag: "text", text: `时间: ${d.us_time || ''}` }]
                        ],
                        "error"
                    );
                }
            }
        }

        if (reverseCount > 0) {
            console.log(`[Webhook] ${d.symbol}: 检测到 ${reverseCount} 个冲突订单，已写入 reverse_signals`);
        }
    } catch (err) {
        console.error("[Webhook] 逆向信号检测失败:", err);
    }

    return c.json(200, { ok: true, type: "signal" })
}, /* middlewares */)
