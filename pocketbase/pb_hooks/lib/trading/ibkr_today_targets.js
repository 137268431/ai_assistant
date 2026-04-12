const { LIVE_ENVIRONMENT, normalizeRuntimeEnvironment } = require(`${__hooks}/lib/environment.js`)
const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)

const TODAY_TARGET_STATUSES = {
    active: true,
    candidate: true,
}

const WATCHLIST_ROLE_TRADE = "trade"
const DEFAULT_TECHNICAL_STATE = "watch"

function toNumber(value, fallback) {
    const number = Number(value)
    return Number.isFinite(number) ? number : (fallback || 0)
}

function toInt(value, fallback) {
    return Math.trunc(toNumber(value, fallback || 0))
}

function asObject(value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
        return value
    }
    if (typeof value === "string") {
        try {
            const parsed = JSON.parse(value)
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                return parsed
            }
        } catch (_) {}
    }
    return {}
}

function getIndicatorSnapshot(record) {
    const extra = asObject(record ? record.get("extra") : {})
    if (!record) return extra
    const fields = [
        "atr_pct",
        "ema_bullish",
        "ema_bearish",
        "crsi_bull_div",
        "crsi_bear_div",
        "obv_bull_div",
        "obv_bear_div",
        "fractal_bull",
        "fractal_bear",
        "ema_bull_touch",
        "ema_bear_touch",
        "trend_dir",
        "vwap_bullish",
    ]
    const snapshot = { ...extra }
    for (let i = 0; i < fields.length; i++) {
        const key = fields[i]
        const value = record.get(key)
        if (value !== undefined && value !== null && value !== "") {
            snapshot[key] = value
        }
    }
    return snapshot
}

function escapeFilter(value) {
    return String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')
}

function normalizeSymbols(values) {
    const source = Array.isArray(values) ? values : String(values || "").split(",")
    const items = []
    const seen = {}
    for (let i = 0; i < source.length; i++) {
        const symbol = String(source[i] || "").trim().toUpperCase()
        if (!symbol || seen[symbol]) continue
        seen[symbol] = true
        items.push(symbol)
    }
    return items
}

function pad2(value) {
    return String(value || 0).padStart(2, "0")
}

function shiftDate(ms, offsetHours) {
    return new Date(Number(ms) + offsetHours * 60 * 60 * 1000)
}

function formatOffsetDate(ms, offsetHours) {
    const date = shiftDate(ms, offsetHours)
    return `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`
}

function formatOffsetDateTime(ms, offsetHours) {
    const date = shiftDate(ms, offsetHours)
    return (
        `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())} ` +
        `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`
    )
}

function formatEtDate(ms) {
    return formatOffsetDate(ms, -4)
}

function formatEtDateTime(ms) {
    return formatOffsetDateTime(ms, -4)
}

function formatCnDateTime(ms) {
    return formatOffsetDateTime(ms, 8)
}

function classifySession(barTimeMs) {
    const date = shiftDate(barTimeMs, -4)
    const minutes = date.getUTCHours() * 60 + date.getUTCMinutes()
    if (minutes < 570) return "premarket"
    if (minutes >= 960) return "afterhours"
    return "regular"
}

function buildDailyChangeFields(history, currentClose) {
    const prevClose = history.length >= 1 ? toNumber(history[history.length - 1].close) : 0
    const prevPrevClose = history.length >= 2 ? toNumber(history[history.length - 2].close) : 0
    const close5 = history.length >= 5 ? toNumber(history[history.length - 5].close) : 0
    const dayChangePct = prevClose > 0 ? ((currentClose - prevClose) / prevClose) * 100 : 0
    const prevCloseChangePct = prevPrevClose > 0 ? ((prevClose - prevPrevClose) / prevPrevClose) * 100 : 0
    const change7d = close5 > 0 ? ((currentClose - close5) / close5) * 100 : 0
    return {
        day_change_pct: Math.round(dayChangePct * 100) / 100,
        prev_close_change_pct: Math.round(prevCloseChangePct * 100) / 100,
        change_7d: Math.round(change7d * 100) / 100,
    }
}

function buildTradabilityAssessment(row) {
    let score = 0
    const notes = []
    const price = Math.abs(toNumber(row.price))
    const avg10dVolume = Math.abs(toNumber(row.avg_10d_volume))
    const premarketVolume = Math.abs(toNumber(row.premarket_volume))
    const todayVolume = Math.abs(toNumber(row.today_volume))
    const atrPct = Math.abs(toNumber(row.atr_pct))
    const dayChangePct = Math.abs(toNumber(row.day_change_pct))
    const freshnessMin = Number.isFinite(Number(row.freshness_min)) ? Number(row.freshness_min) : null
    const targetScore = Math.abs(toNumber(row.score || row.target_score))

    if (price >= 2 && price <= 80) {
        score += 12
        notes.push("价位适中")
    } else if (price >= 1 && price <= 150) {
        score += 6
    }
    if (avg10dVolume >= 5000000) {
        score += 18
        notes.push("10日均量>500万")
    } else if (avg10dVolume >= 1000000) {
        score += 12
        notes.push("10日均量>100万")
    } else if (avg10dVolume >= 500000) {
        score += 6
    }
    if (premarketVolume >= 500000) {
        score += 18
        notes.push("盘前量能>50万")
    } else if (premarketVolume >= 100000) {
        score += 12
        notes.push("盘前量能>10万")
    } else if (todayVolume >= 300000) {
        score += 8
        notes.push("当日成交活跃")
    }
    if (atrPct >= 2 && atrPct <= 12) {
        score += 16
        notes.push("ATR波动充足")
    } else if (atrPct >= 1 && atrPct <= 20) {
        score += 8
    }
    if (dayChangePct >= 2) {
        score += 12
        notes.push("日内波动>2%")
    } else if (dayChangePct >= 0.8) {
        score += 6
    }
    if (freshnessMin !== null) {
        if (freshnessMin <= 20) {
            score += 14
            notes.push("bars新鲜")
        } else if (freshnessMin <= 60) {
            score += 8
        } else if (freshnessMin <= 180) {
            score += 3
        }
    }
    if (targetScore >= 10) {
        score += 10
        notes.push("已入目标池")
    } else if (targetScore >= 5) {
        score += 6
    }
    if (["long", "short"].indexOf(String(row.direction_bias || "").trim().toLowerCase()) !== -1) {
        score += 4
    }

    return { score: Math.min(100, score), notes }
}

function buildSymbolFilter(symbols) {
    const items = normalizeSymbols(symbols)
    if (!items.length) return ""
    return "(" + items.map((symbol) => `symbol = "${escapeFilter(symbol)}"`).join(" || ") + ")"
}

function buildBarEnvironmentFilter(runtimeEnvironment) {
    const environment = normalizeRuntimeEnvironment(runtimeEnvironment, LIVE_ENVIRONMENT)
    const clauses = [`environment = "${escapeFilter(environment)}"`]
    if (environment === LIVE_ENVIRONMENT) {
        clauses.push('environment = ""')
    }
    return clauses.length > 1 ? `(${clauses.join(" || ")})` : clauses[0]
}

function normalizeWatchlistRole(value) {
    return String(value || "").trim().toLowerCase() === "market_monitor"
        ? "market_monitor"
        : WATCHLIST_ROLE_TRADE
}

function getCurrentMarketDate() {
    return getTimeStrings().date
}

function getPriorityEnvironmentRank(environment, runtimeEnvironment) {
    const normalized = String(environment || "").trim().toLowerCase()
    if (normalized === runtimeEnvironment) return 2
    if (normalized === "global") return 1
    if (!normalized) return 0
    return -1
}

function loadWatchMeta(environment, symbols) {
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment, LIVE_ENVIRONMENT)
    const watchRecords = $app.findRecordsByFilter(
        "watchlist",
        'environment = {:env} || environment = "global" || environment = ""',
        "-updated",
        500,
        0,
        { env: runtimeEnvironment }
    ) || []
    const allowedSymbols = normalizeSymbols(symbols)
    const allowedMap = {}
    for (let i = 0; i < allowedSymbols.length; i++) {
        allowedMap[allowedSymbols[i]] = true
    }

    const watchMeta = {}
    const watchRank = {}
    for (let i = 0; i < watchRecords.length; i++) {
        const record = watchRecords[i]
        const symbol = String(record.get("symbol") || "").trim().toUpperCase()
        if (!symbol || !allowedMap[symbol]) continue
        if (normalizeWatchlistRole(record.get("symbol_role")) !== WATCHLIST_ROLE_TRADE) continue
        const rank = getPriorityEnvironmentRank(record.get("environment"), runtimeEnvironment)
        if (rank < 0) continue
        if (watchRank[symbol] != null && watchRank[symbol] > rank) continue
        watchRank[symbol] = rank
        watchMeta[symbol] = {
            exchange: String(record.get("exchange") || "").trim().toUpperCase(),
            industry: String(record.get("industry") || "").trim(),
            note: String(record.get("note") || "").trim(),
        }
    }

    return watchMeta
}

function rankStatus(status) {
    const normalized = String(status || "").trim().toLowerCase()
    if (normalized === "active") return 0
    if (normalized === "candidate") return 1
    return 9
}

function pickReasonList(currentReasons, fallbackReasons) {
    const seen = {}
    const merged = []
    const sources = []
    if (Array.isArray(currentReasons)) {
        sources.push(currentReasons)
    }
    if (Array.isArray(fallbackReasons)) {
        sources.push(fallbackReasons)
    }
    for (let i = 0; i < sources.length; i++) {
        const items = sources[i]
        for (let j = 0; j < items.length; j++) {
            const text = String(items[j] || "").trim()
            if (!text || seen[text]) continue
            seen[text] = true
            merged.push(text)
        }
    }
    return merged
}

function pushUniqueText(list, value) {
    const text = String(value || "").trim()
    if (!text) return
    if (list.indexOf(text) !== -1) return
    list.push(text)
}

function normalizeSignalStatus(value) {
    return String(value || "").trim().toLowerCase()
}

function normalizeSignalRecord(record) {
    const extra = asObject(record ? record.get("extra") : {})
    const barTimeMs = toInt(record ? record.get("bar_time_ms") : 0, toInt(extra.bar_time_ms, 0))
    const created = String(record ? record.get("created") : "").trim()
    const updated = String(record ? record.get("updated") : "").trim() || created
    const createdMs = Date.parse(created || "") || 0
    const updatedMs = Date.parse(updated || "") || createdMs
    return {
        symbol: String(record ? record.get("symbol") : "").trim().toUpperCase(),
        signal_id: String(record ? record.get("signal_id") : "").trim() || String(extra.signal_id || "").trim(),
        direction: String(record ? record.get("direction") : "").trim().toLowerCase() || String(extra.direction || "").trim().toLowerCase(),
        signal: String(record ? record.get("signal") : "").trim() || String(extra.signal || "").trim(),
        status: normalizeSignalStatus(record ? record.get("status") : extra.status),
        bar_time_ms: barTimeMs,
        created: created,
        updated: updated,
        created_ms: createdMs,
        updated_ms: updatedMs,
        sort_ms: barTimeMs || updatedMs || createdMs || 0,
        us_time: String(record ? record.get("us_time") : "").trim() || (barTimeMs > 0 ? formatEtDateTime(barTimeMs) : updated || created || ""),
        note: String(record ? record.get("note") : "").trim() || String(extra.note || extra.status_reason || "").trim(),
    }
}

function pickLatestSignal(currentSignal, nextSignal) {
    if (!nextSignal) return currentSignal || null
    if (!currentSignal) return nextSignal
    if (toInt(nextSignal.sort_ms, 0) !== toInt(currentSignal.sort_ms, 0)) {
        return toInt(nextSignal.sort_ms, 0) > toInt(currentSignal.sort_ms, 0) ? nextSignal : currentSignal
    }
    if (toInt(nextSignal.updated_ms, 0) !== toInt(currentSignal.updated_ms, 0)) {
        return toInt(nextSignal.updated_ms, 0) > toInt(currentSignal.updated_ms, 0) ? nextSignal : currentSignal
    }
    return nextSignal
}

function buildTechnicalFlags(indicatorExtra) {
    const flags = []
    if (!indicatorExtra || typeof indicatorExtra !== "object") return flags

    if (indicatorExtra.ema_bullish) pushUniqueText(flags, "EMA多头")
    if (indicatorExtra.ema_bearish) pushUniqueText(flags, "EMA空头")
    if (indicatorExtra.crsi_bull_div || indicatorExtra.obv_bull_div) pushUniqueText(flags, "多头背离")
    if (indicatorExtra.crsi_bear_div || indicatorExtra.obv_bear_div) pushUniqueText(flags, "空头背离")
    if (indicatorExtra.fractal_bull) pushUniqueText(flags, "分形↑")
    if (indicatorExtra.fractal_bear) pushUniqueText(flags, "分形↓")
    if (indicatorExtra.ema_bull_touch) pushUniqueText(flags, "EMA支撑")
    if (indicatorExtra.ema_bear_touch) pushUniqueText(flags, "EMA压力")
    if (toInt(indicatorExtra.trend_dir, 0) === 1) pushUniqueText(flags, "趋势多头")
    if (toInt(indicatorExtra.trend_dir, 0) === -1) pushUniqueText(flags, "趋势空头")
    if (indicatorExtra.vwap_bullish === true) pushUniqueText(flags, "VWAP多头")
    if (indicatorExtra.vwap_bullish === false) pushUniqueText(flags, "VWAP空头")

    return flags
}

function buildAlignedTechnicalFlags(indicatorExtra, directionBias) {
    const direction = String(directionBias || "").trim().toLowerCase()
    const flags = []
    if (!indicatorExtra || typeof indicatorExtra !== "object") return flags

    if (direction === "long") {
        if (indicatorExtra.ema_bullish) pushUniqueText(flags, "EMA多头")
        if (indicatorExtra.crsi_bull_div || indicatorExtra.obv_bull_div) pushUniqueText(flags, "多头背离")
        if (indicatorExtra.fractal_bull) pushUniqueText(flags, "分形↑")
        if (indicatorExtra.ema_bull_touch) pushUniqueText(flags, "EMA支撑")
        if (toInt(indicatorExtra.trend_dir, 0) === 1) pushUniqueText(flags, "趋势多头")
        if (indicatorExtra.vwap_bullish === true) pushUniqueText(flags, "VWAP多头")
        return flags
    }

    if (direction === "short") {
        if (indicatorExtra.ema_bearish) pushUniqueText(flags, "EMA空头")
        if (indicatorExtra.crsi_bear_div || indicatorExtra.obv_bear_div) pushUniqueText(flags, "空头背离")
        if (indicatorExtra.fractal_bear) pushUniqueText(flags, "分形↓")
        if (indicatorExtra.ema_bear_touch) pushUniqueText(flags, "EMA压力")
        if (toInt(indicatorExtra.trend_dir, 0) === -1) pushUniqueText(flags, "趋势空头")
        if (indicatorExtra.vwap_bullish === false) pushUniqueText(flags, "VWAP空头")
        return flags
    }

    return flags
}

function resolveTechnicalState(row, alignedTechnicalFlags) {
    const freshnessMin = Number.isFinite(Number(row && row.freshness_min)) ? Number(row.freshness_min) : null
    if (!row || !row.has_live_bar || freshnessMin == null || freshnessMin > 90) {
        return "stale"
    }
    if (row.is_operable && (alignedTechnicalFlags || []).length >= 2) {
        return "ready"
    }
    return DEFAULT_TECHNICAL_STATE
}

function resolveAttentionState(row) {
    const signalStatus = normalizeSignalStatus(row && row.latest_signal_status)
    if (signalStatus === "awaiting_confirm") {
        return { state: "awaiting_confirm", rank: 10 }
    }
    if (signalStatus === "pending") {
        return { state: "pending", rank: 11 }
    }
    if (!(row && row.has_signal_today) && row && row.technical_state === "ready") {
        return { state: "ready_no_signal", rank: 20 }
    }
    if (signalStatus === "executed") {
        return { state: "executed", rank: 30 }
    }
    if (signalStatus === "closed") {
        return { state: "closed", rank: 31 }
    }
    if (signalStatus === "expired") {
        return { state: "expired", rank: 32 }
    }
    if (signalStatus === "rejected") {
        return { state: "rejected", rank: 33 }
    }
    if (row && row.technical_state === "stale") {
        return { state: "stale", rank: 40 }
    }
    return { state: DEFAULT_TECHNICAL_STATE, rank: 25 }
}

function buildTodayTargetPayload(options) {
    const runtimeEnvironment = normalizeRuntimeEnvironment(options && options.environment, LIVE_ENVIRONMENT)
    const requestedMarketDate = String(options && (options.marketDate || options.date) || "").trim() || getCurrentMarketDate()
    const marketStartCandidateMs = Date.parse(`${requestedMarketDate}T04:00:00.000Z`)
    const marketDate = Number.isFinite(marketStartCandidateMs) ? requestedMarketDate : getCurrentMarketDate()
    const targetRecords = $app.findRecordsByFilter(
        "ibkr_targets",
        'environment = {:env} && date = {:date} && (status = "candidate" || status = "active")',
        "-updated",
        500,
        0,
        { env: runtimeEnvironment, date: marketDate }
    ) || []

    const targetBySymbol = {}
    const orderedSymbols = []
    for (let i = 0; i < targetRecords.length; i++) {
        const record = targetRecords[i]
        const symbol = String(record.get("symbol") || "").trim().toUpperCase()
        if (!symbol || targetBySymbol[symbol]) continue
        targetBySymbol[symbol] = record
        orderedSymbols.push(symbol)
    }

    const computedAtMs = Date.now()
    if (!orderedSymbols.length) {
        return {
            ok: true,
            environment: runtimeEnvironment,
            market_date: marketDate,
            computed_at_ms: computedAtMs,
            computed_at_us: formatEtDateTime(computedAtMs),
            computed_at_cn: formatCnDateTime(computedAtMs),
            summary: {
                total: 0,
                active_count: 0,
                candidate_count: 0,
                operable_count: 0,
                technical_ready_count: 0,
                signaled_count: 0,
                awaiting_confirm_count: 0,
                pending_count: 0,
                executed_count: 0,
                stale_count: 0,
            },
            items: [],
        }
    }

    const watchMeta = loadWatchMeta(runtimeEnvironment, orderedSymbols)
    const symbolFilter = buildSymbolFilter(orderedSymbols)
    const barEnvironmentFilter = buildBarEnvironmentFilter(runtimeEnvironment)
    const marketStartMs = Date.parse(`${marketDate}T04:00:00.000Z`)
    const marketEndMs = marketStartMs + 24 * 60 * 60 * 1000
    const lookbackDailyMs = marketStartMs - 20 * 24 * 60 * 60 * 1000
    const indicatorLookbackMs = marketStartMs - 5 * 24 * 60 * 60 * 1000
    const dailyFilter = [
        'interval = "1d"',
        barEnvironmentFilter,
        `bar_time_ms >= ${lookbackDailyMs}`,
        `bar_time_ms < ${marketEndMs}`,
        symbolFilter,
    ].join(" && ")
    const intradayFilter = [
        'interval = "5m"',
        barEnvironmentFilter,
        `bar_time_ms >= ${marketStartMs}`,
        `bar_time_ms < ${marketEndMs}`,
        symbolFilter,
    ].join(" && ")
    const indicatorFilter = [
        'interval = "5"',
        `environment = "${escapeFilter(runtimeEnvironment)}"`,
        `bar_time_ms >= ${indicatorLookbackMs}`,
        symbolFilter,
    ].join(" && ")
    const signalFilter = [
        `environment = "${escapeFilter(runtimeEnvironment)}"`,
        `bar_time_ms >= ${marketStartMs}`,
        `bar_time_ms < ${marketEndMs}`,
        symbolFilter,
    ].join(" && ")

    const dailyRecords = $app.findRecordsByFilter("ibkr_bars", dailyFilter, "bar_time_ms", 20000, 0) || []
    const intradayRecords = $app.findRecordsByFilter("ibkr_bars", intradayFilter, "bar_time_ms", 50000, 0) || []
    const indicatorRecords = $app.findRecordsByFilter("ibkr_indicators", indicatorFilter, "-bar_time_ms", 10000, 0) || []
    const signalRecords = $app.findRecordsByFilter("ibkr_signals", signalFilter, "-bar_time_ms,-updated", 10000, 0) || []

    const dailyHistoryBySymbol = {}
    const fallbackDailyBySymbol = {}
    for (let i = 0; i < dailyRecords.length; i++) {
        const record = dailyRecords[i]
        const symbol = String(record.get("symbol") || "").trim().toUpperCase()
        if (!symbol) continue
        const barTimeMs = toInt(record.get("bar_time_ms"), 0)
        const close = toNumber(record.get("close"), 0)
        if (barTimeMs <= 0 || close <= 0) continue
        const row = {
            bar_time_ms: barTimeMs,
            close: close,
            volume: toNumber(record.get("volume"), 0),
            us_time: String(record.get("us_time") || "").trim(),
            date: formatEtDate(barTimeMs),
        }
        if (!dailyHistoryBySymbol[symbol]) dailyHistoryBySymbol[symbol] = []
        dailyHistoryBySymbol[symbol].push(row)
        if (row.date < marketDate) {
            fallbackDailyBySymbol[symbol] = row
        }
    }

    const latestIntradayBySymbol = {}
    const volumeStatsBySymbol = {}
    for (let i = 0; i < intradayRecords.length; i++) {
        const record = intradayRecords[i]
        const symbol = String(record.get("symbol") || "").trim().toUpperCase()
        if (!symbol) continue
        const barTimeMs = toInt(record.get("bar_time_ms"), 0)
        if (barTimeMs <= 0) continue
        const row = {
            bar_time_ms: barTimeMs,
            close: toNumber(record.get("close"), 0),
            exchange: String(record.get("exchange") || "").trim().toUpperCase(),
            session_type: String(record.get("session_type") || "").trim().toLowerCase() || classifySession(barTimeMs),
            us_time: String(record.get("us_time") || "").trim(),
            volume: toNumber(record.get("volume"), 0),
        }
        latestIntradayBySymbol[symbol] = row
        if (!volumeStatsBySymbol[symbol]) {
            volumeStatsBySymbol[symbol] = { premarket: 0, today: 0 }
        }
        volumeStatsBySymbol[symbol].today += row.volume
        if (row.session_type === "premarket") {
            volumeStatsBySymbol[symbol].premarket += row.volume
        }
    }

    const latestIndicatorBySymbol = {}
    for (let i = 0; i < indicatorRecords.length; i++) {
        const record = indicatorRecords[i]
        const symbol = String(record.get("symbol") || "").trim().toUpperCase()
        if (symbol && !latestIndicatorBySymbol[symbol]) {
            latestIndicatorBySymbol[symbol] = record
        }
    }

    const signalAggBySymbol = {}
    for (let i = 0; i < signalRecords.length; i++) {
        const normalized = normalizeSignalRecord(signalRecords[i])
        if (!normalized.symbol) continue
        if (!signalAggBySymbol[normalized.symbol]) {
            signalAggBySymbol[normalized.symbol] = {
                count: 0,
                latest: null,
            }
        }
        signalAggBySymbol[normalized.symbol].count += 1
        signalAggBySymbol[normalized.symbol].latest = pickLatestSignal(signalAggBySymbol[normalized.symbol].latest, normalized)
    }

    const items = []
    let activeCount = 0
    let candidateCount = 0
    let operableCount = 0
    let technicalReadyCount = 0
    let signaledCount = 0
    let awaitingConfirmCount = 0
    let pendingCount = 0
    let executedCount = 0
    let staleCount = 0
    for (let i = 0; i < orderedSymbols.length; i++) {
        const symbol = orderedSymbols[i]
        const target = targetBySymbol[symbol]
        if (!target) continue
        const targetExtra = asObject(target.get("extra"))
        const screenerSnapshot = asObject(targetExtra.screener_snapshot)
        const meta = watchMeta[symbol] || {}
        const intraday = latestIntradayBySymbol[symbol]
        const fallbackDaily = fallbackDailyBySymbol[symbol]
        const indicatorRecord = latestIndicatorBySymbol[symbol]
        const indicatorExtra = getIndicatorSnapshot(indicatorRecord)
        const signalAgg = signalAggBySymbol[symbol] || { count: 0, latest: null }
        const latestSignal = signalAgg.latest || null
        const history = (dailyHistoryBySymbol[symbol] || []).filter((row) => row.date < marketDate)
        const last10 = history.slice(-10)
        const avg10dVolume = last10.length
            ? last10.reduce((sum, row) => sum + toNumber(row.volume), 0) / last10.length
            : 0
        let price = intraday ? toNumber(intraday.close, 0) : 0
        let priceSource = "5m"
        if (price <= 0) {
            price = fallbackDaily ? toNumber(fallbackDaily.close, 0) : 0
            priceSource = price > 0 ? "1d_close" : ""
        }
        const compareHistory = price > 0
            ? buildDailyChangeFields(history, price)
            : { day_change_pct: 0, prev_close_change_pct: 0, change_7d: 0 }
        const targetStatus = String(target.get("status") || "").trim().toLowerCase()
        const directionBias = String(target.get("direction_bias") || "neutral").trim().toLowerCase() || "neutral"
        const score = Math.round(toNumber(target.get("score"), 0) * 100) / 100
        const scanReason = String(target.get("scan_reason") || "").trim()
        const intradayBarTimeMs = intraday ? toInt(intraday.bar_time_ms, 0) : 0
        const latestBarTimeMs = intradayBarTimeMs || (fallbackDaily ? toInt(fallbackDaily.bar_time_ms, 0) : 0)
        const freshnessMin = intradayBarTimeMs > 0 ? Math.max(0, Math.floor((Date.now() - intradayBarTimeMs) / 60000)) : null
        const volumeStats = volumeStatsBySymbol[symbol] || { premarket: 0, today: 0 }
        const row = {
            symbol: symbol,
            record_id: typeof target.getId === "function" ? String(target.getId() || "") : "",
            status: targetStatus,
            target_status: targetStatus,
            direction_bias: directionBias,
            score: score,
            target_score: score,
            scan_reason: scanReason,
            exchange: String(meta.exchange || (intraday ? intraday.exchange : "") || target.get("exchange") || "").trim().toUpperCase(),
            industry: String(meta.industry || "").trim(),
            note: String(meta.note || "").trim(),
            price: price > 0 ? Math.round(price * 10000) / 10000 : 0,
            price_source: priceSource,
            atr_pct: Math.round(toNumber(indicatorExtra.atr_pct, targetExtra.atr_pct) * 100) / 100,
            avg_10d_volume: Math.round(avg10dVolume * 100) / 100,
            premarket_volume: Math.round(toNumber(volumeStats.premarket, screenerSnapshot.premarket_volume) * 100) / 100,
            today_volume: Math.round(toNumber(volumeStats.today, screenerSnapshot.today_volume) * 100) / 100,
            latest_bar_time_ms: latestBarTimeMs,
            latest_intraday_bar_time_ms: intradayBarTimeMs,
            latest_us_time: intraday ? intraday.us_time : (String(target.get("us_time") || "").trim() || (fallbackDaily ? fallbackDaily.us_time : "")),
            freshness_min: freshnessMin,
            has_live_bar: intradayBarTimeMs > 0,
            day_change_pct: compareHistory.day_change_pct,
            prev_close_change_pct: compareHistory.prev_close_change_pct,
            change_7d: compareHistory.change_7d,
            extra: targetExtra,
            updated: String(target.get("updated") || "").trim(),
        }
        const assessment = buildTradabilityAssessment(row)
        row.tradability_score = assessment.score
        row.operable_reasons = pickReasonList(assessment.notes, screenerSnapshot.operable_reasons)
        row.is_operable = Boolean(
            row.has_live_bar &&
            row.price > 0 &&
            row.avg_10d_volume >= 500000 &&
            row.tradability_score >= 60 &&
            row.freshness_min !== null &&
            row.freshness_min <= 90
        )
        row.technical_flags = buildTechnicalFlags(indicatorExtra)
        row.technical_aligned_flags = buildAlignedTechnicalFlags(indicatorExtra, directionBias)
        row.technical_state = resolveTechnicalState(row, row.technical_aligned_flags)
        row.has_signal_today = signalAgg.count > 0
        row.signal_count_today = signalAgg.count || 0
        row.latest_signal_id = latestSignal ? latestSignal.signal_id || "" : ""
        row.latest_signal_status = latestSignal ? latestSignal.status || "" : ""
        row.latest_signal_direction = latestSignal ? latestSignal.direction || "" : ""
        row.latest_signal_time = latestSignal ? latestSignal.us_time || "" : ""
        row.latest_signal_time_ms = latestSignal ? latestSignal.sort_ms || 0 : 0
        row.latest_signal_note = latestSignal ? latestSignal.note || "" : ""
        const attentionState = resolveAttentionState(row)
        row.attention_state = attentionState.state
        row.attention_rank = attentionState.rank
        if (targetStatus === "active") activeCount += 1
        if (targetStatus === "candidate") candidateCount += 1
        if (row.is_operable) operableCount += 1
        if (row.technical_state === "ready") technicalReadyCount += 1
        if (row.technical_state === "stale") staleCount += 1
        if (row.has_signal_today) signaledCount += 1
        if (row.latest_signal_status === "awaiting_confirm") awaitingConfirmCount += 1
        if (row.latest_signal_status === "pending") pendingCount += 1
        if (row.latest_signal_status === "executed") executedCount += 1
        items.push(row)
    }

    items.sort((left, right) => {
        const attentionDiff = toNumber(left.attention_rank, 99) - toNumber(right.attention_rank, 99)
        if (attentionDiff !== 0) return attentionDiff
        const latestSignalDiff = toInt(right.latest_signal_time_ms, 0) - toInt(left.latest_signal_time_ms, 0)
        if (latestSignalDiff !== 0) return latestSignalDiff
        const tradabilityDiff = toNumber(right.tradability_score, 0) - toNumber(left.tradability_score, 0)
        if (tradabilityDiff !== 0) return tradabilityDiff
        const scoreDiff = toNumber(right.score, 0) - toNumber(left.score, 0)
        if (scoreDiff !== 0) return scoreDiff
        const statusDiff = rankStatus(left.status) - rankStatus(right.status)
        if (statusDiff !== 0) return statusDiff
        return String(left.symbol || "").localeCompare(String(right.symbol || ""))
    })

    return {
        ok: true,
        environment: runtimeEnvironment,
        market_date: marketDate,
        computed_at_ms: computedAtMs,
        computed_at_us: formatEtDateTime(computedAtMs),
        computed_at_cn: formatCnDateTime(computedAtMs),
        summary: {
            total: items.length,
            active_count: activeCount,
            candidate_count: candidateCount,
            operable_count: operableCount,
            technical_ready_count: technicalReadyCount,
            signaled_count: signaledCount,
            awaiting_confirm_count: awaitingConfirmCount,
            pending_count: pendingCount,
            executed_count: executedCount,
            stale_count: staleCount,
        },
        items: items,
    }
}

module.exports = {
    TODAY_TARGET_STATUSES,
    buildTodayTargetPayload,
}
