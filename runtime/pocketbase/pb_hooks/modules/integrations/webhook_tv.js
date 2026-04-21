/// <reference path="./pb_data/types.d.ts" />

/**
 * webhook_tv.pb.js
 * TradingView Webhook 接收端点
 * 支持信号表和技术指标表的写入
 */

routerAdd("POST", "/webhook/tv", (c) => {
    const envUtils = require(`${__hooks}/lib/environment.js`)

    const reqInfo = c.requestInfo()
    const d = reqInfo.body || reqInfo.data || {}
    const dataType = d.type || "signal"

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
    console.log(
        `[Webhook TV] 接收信号数据: signal_id=${String(d.signal_id || "").trim()}, symbol=${String(d.symbol || "").trim()}, direction=${String(d.direction || "").trim()}, us_time=${String(d.us_time || "").trim()}`
    )

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

    const writableEnvironments = [
        envUtils.getRuntimeEnvironmentFromData(d, envUtils.LIVE_ENVIRONMENT)
    ]

    const createdEnvironments = []
    const skippedEnvironments = []
    const collectionName = "tv_signals"
    const col = $app.findCollectionByNameOrId(collectionName)

    for (let i = 0; i < writableEnvironments.length; i++) {
        const environment = writableEnvironments[i]
        const extra = { ...baseExtra, environment }
        if (!extra.source) {
            extra.source = "tradingview"
        }

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
        record.set("status", String(d.status || "pending"))

        $app.save(record)
        createdEnvironments.push(environment)
    }

    return c.json(200, {
        ok: true,
        type: "signal",
        created_environments: createdEnvironments,
        skipped_environments: skippedEnvironments,
    })
}, /* middlewares */)
