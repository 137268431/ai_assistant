/// <reference path="./pb_data/types.d.ts" />

/**
 * webhook_tv.pb.js
 * TradingView Webhook 接收端点
 * 支持信号表和技术指标表的写入
 */

routerAdd("POST", "/webhook/tv", (c) => {
    const { notifyNewSignal, sendFeishuPost } = require(`${__hooks}/feishu.js`)

    // ── 解析请求信息（含 body）──
    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}

    // ── 路由：根据 type 字段区分写入信号表还是技术指标表 ──
    const dataType = d.type || "signal"

    // ══════════════════════════════════════
    // 大盘指数表（type = "market_index"）
    // ══════════════════════════════════════
    if (dataType === "market_index") {
        // 以 symbol + barTimeMs 作为去重 key
        const dedupKey = d.symbol + "_" + d.barTimeMs

        // 去重检查
        try {
            $app.findFirstRecordByFilter("market_index",
                "symbol = {:sym} && bar_time_ms = {:ms}",
                { sym: d.symbol, ms: d.barTimeMs }
            )
            return c.json(200, { ok: true, msg: "duplicate market_index, skipped", key: dedupKey })
        } catch(_) {}

        const col = $app.findCollectionByNameOrId("market_index")
        const rec = new Record(col, {})

        rec.set("symbol",      d.symbol)
        rec.set("price",       d.price)
        rec.set("change_pct",  d.changePct  || 0)
        rec.set("prev_close_pct", d.prevClosePct || 0)
        rec.set("us_time",     d.usTime     || "")
        rec.set("cn_time",     d.cnTime     || "")
        rec.set("bar_time_ms", d.barTimeMs)
        rec.set("interval",    d.interval   || "")
        rec.set("extra",       d.extra      || {})

        $app.save(rec)
        return c.json(200, { ok: true, type: "market_index", symbol: d.symbol })
    }

    // ══════════════════════════════════════
    // 技术指标表（type = "indicator"）
    // ══════════════════════════════════════
    if (dataType === "indicator") {
        // 以 barTimeMs + symbol + interval 作为去重 key
        const dedupKey = String(d.barTimeMs) + "_" + d.symbol + "_" + d.interval
        try {
            $app.findFirstRecordByFilter("indicators",
                "bar_time_ms = {:ms} && symbol = {:sym} && interval = {:tf}",
                { ms: d.barTimeMs, sym: d.symbol, tf: d.interval }
            )
            return c.json(200, { ok: true, msg: "duplicate indicator, skipped", key: dedupKey })
        } catch(_) {}

        const col = $app.findCollectionByNameOrId("indicators")
        const rec = new Record(col, {})

        // 固定字段（用于查询、去重、筛选）
        rec.set("symbol",      d.symbol)
        rec.set("exchange",    d.exchange   || "")
        rec.set("interval",    d.interval   || "")
        rec.set("script_tag",  d.scriptTag  || "")
        rec.set("us_time",     d.usTime     || "")
        rec.set("cn_time",     d.cnTime     || "")
        rec.set("bar_time_ms", d.barTimeMs)
        rec.set("bar_index",   d.barIndex)

        // 所有指标值统一放入 extra JSON 字段，新增指标无需改表结构
        const extra = {
            // OHLCV
            close:        d.close,
            high:         d.high,
            low:          d.low,
            open:         d.open,
            volume:       d.volume,
            // 价格变动
            day_change_pct:      d.dayChangePct,
            prev_close_change_pct: d.prevCloseChangePct,
            change_7d:    d.change7d,
            // VWAP
            vwap:         d.vwap,
            vwap_upper1:  d.vwapUpper1,
            vwap_lower1:  d.vwapLower1,
            vwap_upper2:  d.vwapUpper2,
            vwap_lower2:  d.vwapLower2,
            vwap_dist:    d.vwapDist,
            vwap_bullish: d.vwapBullish,
            // EMA
            ema_fast:      d.emaFast,
            ema_slow:      d.emaSlow,
            ema_trend:     d.emaTrend,
            ema_longest:   d.emaLongest,
            slope_slow:    d.slopeSlow,
            slope_trend:   d.slopeTrend,
            slope_longest: d.slopeLongest,
            ema_bullish:   d.emaBullish,
            ema_bearish:   d.emaBearish,
            trend_dir:     d.trendDir,
            // SD Channel
            sd_reg:        d.sdReg,
            sd_std_dev:    d.sdStdDev,
            sd_zone:       d.sdZone,
            sd_trend:      d.sdTrend,
            // DTP
            dtp_avg:       d.dtpAvg,
            dtp_atr:       d.dtpAtr,
            dtp_dir:       d.dtpDir,
            dtp_phase:     d.dtpPhase    || "",
            dtp_phase_bars:d.dtpPhaseBars,
            // ATR / cRSI / OBV
            atr:           d.atr,
            atr_raw:       d.atrRaw,
            atr_pct:       d.atrPct,
            crsi:          d.crsi,
            crsi_ub:       d.crsiUb,
            crsi_db:       d.crsiDb,
            crsi_ob:       d.crsiOB,
            crsi_os:       d.crsiOS,
            obv_rsi:       d.obvRsi,
            // cRSI 背离
            crsi_bull_div: d.crsiBullDiv,
            crsi_bear_div: d.crsiBearDiv,
            crsi_hid_bull: d.crsiHidBull,
            crsi_hid_bear: d.crsiHidBear,
            // OBV 背离
            obv_bull_div:  d.obvBullDiv,
            obv_bear_div:  d.obvBearDiv,
            obv_hid_bull:  d.obvHidBull,
            obv_hid_bear:  d.obvHidBear,
            // 分形信号
            fractal_bull:  d.fractalBull,
            fractal_bear:  d.fractalBear,
            // SD 通道触及
            sd_lower:      d.sdLower,
            sd_upper:      d.sdUpper,
            // EMA 触及
            ema_bull_touch: d.emaBullTouch,
            ema_bear_touch: d.emaBearTouch
        }
        rec.set("extra", extra)

        $app.save(rec)
        return c.json(200, { ok: true, type: "indicator" })
    }

    // ══════════════════════════════════════
    // 信号过滤：检查是否为大盘指数
    // ══════════════════════════════════════
    if (dataType === "signal" || !dataType) {
        // 读取配置的大盘指数列表
        let marketIndexSymbols = []
        try {
            const configRecord = $app.findFirstRecordByFilter("config", "key = 'market_index_symbols'")
            const configValue = configRecord.get("value") || ""
            marketIndexSymbols = configValue.split(",").map(s => s.trim().toUpperCase()).filter(s => s)
        } catch(_) {}

        // 如果配置了大盘指数，检查信号标的是否在列表中
        if (marketIndexSymbols.length > 0) {
            const signalSymbol = (d.symbol || "").toUpperCase()
            if (marketIndexSymbols.includes(signalSymbol)) {
                console.log(`[Webhook] 过滤大盘指数信号: ${signalSymbol}`)
                return c.json(200, { ok: true, msg: "market_index filtered, skipped", symbol: signalSymbol })
            }
        }
    }

    // ══════════════════════════════════════
    // 信号表（type = "signal" 或无 type 字段，向后兼容）
    // ══════════════════════════════════════
    try {
        $app.findFirstRecordByData("signals", "signal_id", d.signal_id)
        return c.json(200, { ok: true, msg: "duplicate, skipped" })
    } catch(_) {}

    // 问题 3 修复: 添加必需字段验证
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

    // 问题 10 修复: 添加数据类型验证
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

    const extra = {
        sd_zone:      d.sdZone      || "",
        sd_trend:     d.sdTrend     || "",
        dtp_dir:      d.dtpDir      || "",
        dtp_phase:    d.dtpPhase    || "",
        crsi_state:   d.crsiState   || "",
        industry:     d.industry    || "",
        atr:          d.atr         || 0,
        atr_raw:      d.atr         || 0,  // Pine 传来的已是 atrRaw
        atr_pct:      d.atrPct      || 0,
        sl_dist_pct:  d.slDistPct   || 0,
        sl_atr_ratio: d.slAtrRatio  || 0,
        close:        d.close       || 0,
        // 大盘关联字段
        market_indexes: []  // 将在后续填充
    }

    // ══════════════════════════════════════
    // 获取大盘指数数据用于信号关联
    // ══════════════════════════════════════
    let marketIndexData = { SPY: null, QQQ: null, VIX: null }
    try {
        const configRecord = $app.findFirstRecordByFilter("config", "key = 'market_index_symbols'")
        const configValue = configRecord.get("value") || "SPY,QQQ,VIX"
        const symbols = configValue.split(",").map(s => s.trim().toUpperCase()).filter(s => s)

        for (const sym of symbols) {
            try {
                const latest = $app.findFirstRecordByFilter("market_index",
                    "symbol = {:sym}",
                    { sym: sym },
                    "-bar_time_ms"
                )
                if (latest) {
                    marketIndexData[sym] = {
                        symbol: sym,
                        price: latest.get("price"),
                        change_pct: latest.get("change_pct") || 0,
                        prev_close_pct: latest.get("prev_close_pct") || 0
                    }
                }
            } catch(_) {}
        }
    } catch(_) {}

    // 计算与大盘的关联性
    const signalDirection = d.direction
    let marketRelation = "neutral"  // neutral, with_trend, against_trend
    let marketRelationText = ""
    const marketInfos = []

    // 使用 SPY 和 QQQ 判断大盘方向
    const spyPct = marketIndexData.SPY?.change_pct || 0
    const qqqPct = marketIndexData.QQQ?.change_pct || 0
    const marketAvgPct = (spyPct + qqqPct) / 2
    const marketDirection = marketAvgPct > 0 ? "up" : (marketAvgPct < 0 ? "down" : "neutral")

    if (signalDirection === "long" && marketDirection === "up") {
        marketRelation = "with_trend"
        marketRelationText = "顺势"
    } else if (signalDirection === "long" && marketDirection === "down") {
        marketRelation = "against_trend"
        marketRelationText = "逆势"
    } else if (signalDirection === "short" && marketDirection === "down") {
        marketRelation = "with_trend"
        marketRelationText = "顺势"
    } else if (signalDirection === "short" && marketDirection === "up") {
        marketRelation = "against_trend"
        marketRelationText = "逆势"
    }

    // 收集大盘信息
    for (const [sym, data] of Object.entries(marketIndexData)) {
        if (data) {
            marketInfos.push({
                symbol: sym,
                change_pct: data.change_pct,
                direction: data.change_pct > 0 ? "up" : (data.change_pct < 0 ? "down" : "neutral")
            })
        }
    }

    // 更新 extra 中的大盘关联数据
    extra.market_indexes = marketInfos
    extra.market_relation = marketRelation
    extra.market_relation_text = marketRelationText
    extra.market_avg_pct = marketAvgPct

    const col = $app.findCollectionByNameOrId("signals")
    const record = new Record(col, {})

    // 问题 11 修复: 增强 date 字段提取
    let dateStr = ""
    if (d.usTime) {
        // 支持多种格式
        const match = d.usTime.match(/(\d{4}-\d{2}-\d{2})/);
        if (match) {
            dateStr = match[1];
        }
    }
    if (!dateStr) {
        // fallback: 使用服务器当前日期
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
    record.set("reason",      d.reason)
    record.set("us_time",     d.usTime)
    record.set("cn_time",     d.cnTime)
    record.set("date",        dateStr)
    record.set("bar_time_ms", d.barTimeMs)
    record.set("bar_index",   d.barIndex)
    record.set("script_tag",  d.scriptTag)
    record.set("chart_tf",    d.chartTf)
    record.set("extra",       extra)

    // 根据配置决定初始状态
    let initialStatus = "pending";  // 默认 pending（自动确认）
    try {
        const configRecord = $app.findFirstRecordByFilter("config", "key = 'signal_auto_confirm'");
        const autoConfirm = configRecord.get("value");
        if (autoConfirm === "false" || autoConfirm === false) {
            initialStatus = "awaiting_confirm";  // 需要手动确认
        }
    } catch (err) {
        // 配置不存在，使用默认值 pending
    }
    record.set("status", initialStatus)

    $app.save(record)

    // 发送飞书通知（使用 feishu_notify.pb.js 中的 notifyNewSignal）
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
            us_time: d.usTime,
            reason: d.reason,
            market_relation: marketRelationText,
            market_avg_pct: marketAvgPct,
            market_indexes: marketInfos,
            extra: extra
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
                revRecord.set("bar_time_ms", d.barTimeMs || 0);
                revRecord.set("us_time", d.usTime || "");
                revRecord.set("cn_time", d.cnTime || "");
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
                    threshold = parseInt(cfg.get("value")) || 6;
                } catch (e) {}
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
                            [{ tag: "text", text: `时间: ${d.usTime || ''}` }]
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
