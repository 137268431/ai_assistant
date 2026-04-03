/// <reference path="./pb_data/types.d.ts" />

/**
 * webhook_tv.pb.js
 * TradingView Webhook 接收端点
 * 支持信号表和技术指标表的写入
 */

routerAdd("POST", "/webhook/tv", (c) => {
    const { notifyNewSignal, mergeSignalExtra } = require(`${__hooks}/lib/feishu_signal.js`)
    const reverseUtils = require(`${__hooks}/lib/reverse_utils.js`)
    const { notifyReverseSignal } = require(`${__hooks}/lib/feishu_reverse.js`)
    const envUtils = require(`${__hooks}/lib/environment.js`)

    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const dataType = d.type || "signal"

    function getWriteMode(environment) {
        try {
            return String(envUtils.getConfigValue("ibkr_write_mode", "shadow", environment) || "shadow").trim().toLowerCase()
        } catch (_) {
            return "shadow"
        }
    }

    function toFiniteNumber(value) {
        const number = Number(value)
        return Number.isFinite(number) ? number : null
    }

    function normalizeRiskRewardValue(rawValue, entry, stopLoss, takeProfit) {
        if (typeof rawValue === "number" && Number.isFinite(rawValue)) {
            return rawValue.toFixed(2)
        }

        const text = String(rawValue || "").trim()
        if (text) {
            const normalizedText = text.replace(/：/g, ":")
            const ratioMatch = normalizedText.match(/^([+-]?\d+(?:\.\d+)?)\s*:\s*([+-]?\d+(?:\.\d+)?)$/)
            if (ratioMatch) {
                const numerator = toFiniteNumber(ratioMatch[1])
                const denominator = toFiniteNumber(ratioMatch[2])
                if (numerator != null && denominator != null && denominator !== 0) {
                    return (numerator / denominator).toFixed(2)
                }
            }

            const directValue = toFiniteNumber(normalizedText)
            if (directValue != null) {
                return directValue.toFixed(2)
            }
        }

        const entryPrice = toFiniteNumber(entry)
        const stopLossPrice = toFiniteNumber(stopLoss)
        const takeProfitPrice = toFiniteNumber(takeProfit)
        if (entryPrice != null && stopLossPrice != null && takeProfitPrice != null) {
            const risk = Math.abs(entryPrice - stopLossPrice)
            const reward = Math.abs(takeProfitPrice - entryPrice)
            if (risk > 0) {
                return (reward / risk).toFixed(2)
            }
        }

        return text
    }

    function toOptionalBoolean(value) {
        if (value == null || value === "") {
            return null
        }
        const text = String(value).trim().toLowerCase()
        if (["true", "1", "yes", "on"].includes(text)) return true
        if (["false", "0", "no", "off"].includes(text)) return false
        return null
    }

    // ══════════════════════════════════════
    // 技术指标表（type = "indicator"）
    // ══════════════════════════════════════
    if (dataType === "indicator") {
        function parseObject(value) {
            if (!value) return {}
            if (typeof value === "string") {
                try {
                    const parsed = JSON.parse(value)
                    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}
                } catch (_) {
                    return {}
                }
            }
            return (typeof value === "object" && !Array.isArray(value)) ? value : {}
        }

        function coerceScalar(value) {
            if (typeof value !== "string") return value
            const text = value.trim()
            if (text === "") return ""
            if (text === "true") return true
            if (text === "false") return false
            if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text)
            return value
        }

        const aliasMap = {
            dayChangePct: "day_change_pct",
            prevCloseChangePct: "prev_close_change_pct",
            change7d: "change_7d",
            obvRsi: "obv_rsi",
            emaBullTouch: "ema_bull_touch",
            emaBearTouch: "ema_bear_touch",
            emaBullish: "ema_bullish",
            emaBearish: "ema_bearish",
            vwapUpper1: "vwap_upper1",
            vwapLower1: "vwap_lower1",
            vwapUpper2: "vwap_upper2",
            vwapLower2: "vwap_lower2",
            vwapDist: "vwap_dist",
            sdStdDev: "sd_std_dev",
            dtpPhaseBars: "dtp_phase_bars",
        }

        const rawExtra = parseObject(d.extra)
        const extra = {}
        Object.keys(rawExtra).forEach((key) => {
            extra[key] = coerceScalar(rawExtra[key])
        })
        Object.keys(aliasMap).forEach((fromKey) => {
            const toKey = aliasMap[fromKey]
            if (extra[fromKey] != null && extra[toKey] == null) {
                extra[toKey] = extra[fromKey]
            }
        })

        const symbol = String(d.symbol || extra.symbol || "").trim().toUpperCase()
        const interval = String(d.interval || extra.interval || "").trim()
        const exchange = String(d.exchange || extra.exchange || "").trim().toUpperCase()
        const scriptTag = String(d.script_tag || extra.script_tag || "").trim()
        const usTime = String(d.us_time || extra.us_time || "").trim()
        const cnTime = String(d.cn_time || extra.cn_time || "").trim()
        const barTimeMs = Number(d.bar_time_ms != null ? d.bar_time_ms : extra.bar_time_ms)
        const rawBarIndex = d.bar_index != null ? d.bar_index : extra.bar_index
        const barIndex = rawBarIndex == null || rawBarIndex === "" ? null : Number(rawBarIndex)

        if (!symbol) {
            return c.json(400, { ok: false, error: "Missing required field: symbol", type: "indicator" })
        }
        if (!interval) {
            return c.json(400, { ok: false, error: "Missing required field: interval", type: "indicator", symbol })
        }
        if (!Number.isFinite(barTimeMs) || barTimeMs <= 0) {
            return c.json(400, { ok: false, error: "Invalid bar_time_ms", type: "indicator", symbol, interval })
        }

        extra.symbol = symbol
        extra.interval = interval
        extra.bar_time_ms = Math.trunc(barTimeMs)
        if (exchange) extra.exchange = exchange
        if (scriptTag) extra.script_tag = scriptTag
        if (usTime) extra.us_time = usTime
        if (cnTime) extra.cn_time = cnTime
        if (Number.isFinite(barIndex)) extra.bar_index = Math.trunc(barIndex)
        const environment = envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)
        const collectionName = "tv_indicators"
        extra.environment = environment
        if (!extra.source) {
            extra.source = "tradingview"
        }

        const dedupKey = `${Math.trunc(barTimeMs)}_${symbol}_${interval}`
        console.log(`[Webhook TV] 接收指标数据: symbol=${symbol}, interval=${interval}, bar_time_ms=${Math.trunc(barTimeMs)}, fields=${Object.keys(extra).sort().join(",")}`)

        try {
            $app.findFirstRecordByFilter(
                collectionName,
                "bar_time_ms = {:ms} && symbol = {:sym} && interval = {:tf} && environment = {:env}",
                { ms: Math.trunc(barTimeMs), sym: symbol, tf: interval, env: environment }
            )
            return c.json(200, { ok: true, msg: "duplicate indicator, skipped", key: dedupKey })
        } catch (_) {}

        const col = $app.findCollectionByNameOrId(collectionName)
        const rec = new Record(col, {})

        rec.set("symbol", symbol)
        rec.set("environment", environment)
        rec.set("exchange", exchange)
        rec.set("interval", interval)
        rec.set("script_tag", scriptTag)
        rec.set("us_time", usTime)
        rec.set("cn_time", cnTime)
        rec.set("bar_time_ms", Math.trunc(barTimeMs))
        rec.set("bar_index", Number.isFinite(barIndex) ? Math.trunc(barIndex) : null)
        rec.set("extra", extra)

        $app.save(rec)
        return c.json(200, { ok: true, type: "indicator", key: dedupKey, id: rec.id })
    }

    // ══════════════════════════════════════
    // 信号表（type = "signal"）
    // ══════════════════════════════════════
    // ── 打印接收到的信号数据日志 ──
    console.log("[Webhook TV] 接收信号数据:", JSON.stringify(d, null, 2))

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
    const baseExtra = d.extra && typeof d.extra === "object" ? d.extra : {}

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

    const requestedEnvironment = String(d.environment || "").trim().toLowerCase()
    const targetEnvironments = requestedEnvironment
        ? [envUtils.normalizeRuntimeEnvironment(requestedEnvironment, envUtils.LIVE_ENVIRONMENT)]
        : [envUtils.LIVE_ENVIRONMENT, envUtils.PAPER_ENVIRONMENT]
    const modeSkippedEnvironments = []
    const writableEnvironments = targetEnvironments

    function getThreshold(environment) {
        try {
            return parseInt(envUtils.getConfigValue("reverse_signal_threshold", "6", environment)) || 6
        } catch (e) {
            console.log(`[Webhook] reverse_signal_threshold 配置读取失败，使用默认值 6: ${e}`)
            return 6
        }
    }

    function getInitialStatus(environment) {
        try {
            const autoConfirm = envUtils.getConfigValue("signal_auto_confirm", "true", environment)
            console.log(`[Webhook] signal_auto_confirm(${environment}) 配置值: ${autoConfirm}`);
            if (autoConfirm && String(autoConfirm).toLowerCase() === "false") {
                return "awaiting_confirm"
            }
        } catch (err) {
            console.log(`[Webhook] signal_auto_confirm 配置读取失败，使用默认值 pending: ${err}`);
        }
        return "pending"
    }

    if (writableEnvironments.length === 0) {
        return c.json(200, {
            ok: true,
            type: "signal",
            skipped: true,
            reason: "all_target_environments_tv_disabled",
            skipped_mode_environments: modeSkippedEnvironments,
        })
    }

    const createdEnvironments = []
    const skippedEnvironments = []
    const workflowEnvironments = []
    const collectionName = "tv_signals"
    const col = $app.findCollectionByNameOrId(collectionName)

    for (let i = 0; i < writableEnvironments.length; i++) {
        const environment = writableEnvironments[i]
        const extra = { ...baseExtra, environment }
        if (!extra.source) {
            extra.source = "tradingview"
        }
        const writeMode = getWriteMode(environment)
        const workflowOverride = toOptionalBoolean(d.workflow_enabled != null ? d.workflow_enabled : extra.workflow_enabled)
        const workflowEnabled = workflowOverride != null ? workflowOverride : writeMode !== "shadow"
        extra.write_mode = writeMode
        extra.workflow_enabled = workflowEnabled

        try {
            $app.findFirstRecordByFilter(
                collectionName,
                "signal_id = {:sid} && environment = {:env}",
                { sid: d.signal_id, env: environment }
            )
            skippedEnvironments.push(environment)
            continue
        } catch (_) {}

        const record = new Record(col, {})
        record.set("symbol", d.symbol)
        record.set("environment", environment)
        record.set("direction", d.direction)
        record.set("signal", d.signal)
        record.set("limit_price", d.limit_price)
        record.set("entry", d.entry)
        record.set("stop_loss", d.stop_loss)
        record.set("take_profit", d.take_profit)
        record.set("rr", normalizeRiskRewardValue(d.rr, d.entry, d.stop_loss, d.take_profit))
        record.set("shares", d.shares)
        record.set("signal_id", d.signal_id)
        record.set("exchange", d.exchange)
        record.set("interval", d.interval)
        record.set("reason", extra.reason)
        record.set("us_time", d.us_time || "")
        record.set("cn_time", d.cn_time || "")
        record.set("date", dateStr)
        record.set("bar_time_ms", extra.bar_time_ms)
        record.set("bar_index", extra.bar_index)
        record.set("script_tag", extra.script_tag)
        record.set("chart_tf", extra.chart_tf)
        record.set("extra", extra)
        record.set("status", String(d.status || getInitialStatus(environment)))

        $app.save(record)
        createdEnvironments.push(environment)

        if (!workflowEnabled) {
            continue
        }

        workflowEnvironments.push(environment)

        try {
            const notifyResult = notifyNewSignal(record);
            if (notifyResult && notifyResult.success && notifyResult.message_id) {
                mergeSignalExtra(record, {
                    feishu_signal_message_id: notifyResult.message_id,
                    feishu_signal_card_version: 1,
                }, true);
            }
        } catch (err) {
            console.error("[Feishu] 发送信号通知失败:", err);
        }

        try {
            const activeOrders = $app.findRecordsByFilter(
                "orders",
                `symbol = {:symbol} && environment = {:env} && order_type = 'Entry' && (status = 'Submitted' || status = 'Filled')`,
                "-created", 10, 0,
                { symbol: d.symbol, env: environment }
            );

            let reverseCount = 0;
            const threshold = getThreshold(environment)

            for (const order of activeOrders) {
                const orderDirection = order.get("direction");
                if (orderDirection && orderDirection !== d.direction) {
                    const orderStatus = order.get("status");
                    const actionType = orderStatus === "Filled" ? "close" : "cancel";
                    const orderContext = reverseUtils.buildOrderContext(order);

                    const upsertResult = reverseUtils.upsertReverseRecord({
                        environment: environment,
                        symbol: d.symbol,
                        direction: orderDirection,
                        source: "signal",
                        priority: 1,
                        strength: "strong",
                        action_type: actionType,
                        triggered_signals: ["信号反转"],
                        score: 10,
                        status: "pending",
                        bar_time_ms: extra.bar_time_ms || 0,
                        us_time: d.us_time || "",
                        cn_time: d.cn_time || "",
                        extra: {
                            environment: environment,
                            reverse_kind: "signal_conflict",
                            target_state: orderContext ? orderContext.target_state : (orderStatus === "Filled" ? "filled_position" : "pending_entry"),
                            order_status: orderStatus,
                            relation_status: orderContext ? orderContext.relation_status : "",
                            position_side: orderContext ? orderContext.position_side : orderDirection,
                            current_direction: orderDirection,
                            new_direction: d.direction,
                            origin_signal_id: d.signal_id,
                            signal_id: orderContext ? orderContext.signal_id : "",
                            order_unique_id: orderContext ? orderContext.order_unique_id : "",
                            broker_order_id: orderContext ? orderContext.broker_order_id : "",
                            order_id: orderContext ? orderContext.broker_order_id : "",
                            trade_group_id: orderContext ? orderContext.trade_group_id : "",
                            entry_order_unique_id: orderContext ? orderContext.entry_order_unique_id : "",
                            entry_price: orderContext ? orderContext.entry_price : d.entry,
                            quantity: orderContext ? orderContext.quantity : d.shares,
                            take_profit: orderContext ? orderContext.take_profit : d.take_profit,
                            stop_loss: orderContext ? orderContext.stop_loss : d.stop_loss,
                        },
                    })

                    if (upsertResult.created) {
                        reverseCount++
                    }

                    if (upsertResult.created && 10 >= threshold) {
                        try {
                            notifyReverseSignal(upsertResult.record, {
                                message: orderStatus === "Filled"
                                    ? "检测到反向新信号，IBKR 将先平旧仓再评估新方向"
                                    : "检测到反向新信号，IBKR 将先撤销旧挂单再评估新方向"
                            })
                        } catch (notifyErr) {
                            console.error("[Webhook] 逆向飞书通知失败:", notifyErr)
                        }
                    }
                }
            }

            if (reverseCount > 0) {
                console.log(`[Webhook] ${d.symbol}/${environment}: 检测到 ${reverseCount} 个冲突订单，已写入 reverse_signals`);
            }
        } catch (err) {
            console.error("[Webhook] 逆向信号检测失败:", err);
        }
    }

    return c.json(200, {
        ok: true,
        type: "signal",
        created_environments: createdEnvironments,
        workflow_environments: workflowEnvironments,
        skipped_environments: skippedEnvironments,
        skipped_mode_environments: modeSkippedEnvironments,
    })
}, /* middlewares */)
