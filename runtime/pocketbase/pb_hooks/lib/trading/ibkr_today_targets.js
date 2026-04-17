const { LIVE_ENVIRONMENT, normalizeRuntimeEnvironment } = require(`${__hooks}/lib/environment.js`)
const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)

const TODAY_TARGET_STATUSES = {
    active: true,
    candidate: true,
}

const WATCHLIST_ROLE_TRADE = "trade"
const DEFAULT_TECHNICAL_STATE = "watch"
const DAILY_SCAN_SUMMARY_TIME_ET = "09:20"
const MARKET_OPEN_CHECK_TIME_ET = "09:20"
const INTRADAY_REFRESH_RULE = "5m close-driven"
const TODAY_TARGET_PAYLOAD_CACHE_TTL_MS = 15 * 1000

const todayTargetPayloadCache = {}

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

function parseOptionalBoolean(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback
    if (typeof value === "boolean") return value
    const normalized = String(value || "").trim().toLowerCase()
    if (["true", "1", "yes", "y"].indexOf(normalized) !== -1) return true
    if (["false", "0", "no", "n"].indexOf(normalized) !== -1) return false
    return fallback
}

function matchesTodayTargetSignalState(row, signalState) {
    const value = String(signalState || "").trim().toLowerCase()
    if (!value) return true
    const latestStatus = normalizeSignalStatus(row && row.latest_signal_status)
    if (value === "needs_action") {
        return latestStatus === "awaiting_confirm" || latestStatus === "pending"
    }
    if (value === "signaled") return Boolean(row && row.has_signal_today)
    if (value === "no_signal") return !(row && row.has_signal_today)
    return latestStatus === value
}

function normalizeTodayTargetFilters(options) {
    return {
        search: String(options && options.search || "").trim().toUpperCase(),
        technical_state: String(options && (options.technical_state || options.technicalState) || "").trim().toLowerCase(),
        signal_state: String(options && (options.signal_state || options.signalState) || "").trim().toLowerCase(),
        target_status: String(options && (options.target_status || options.targetStatus) || "").trim().toLowerCase(),
        direction_bias: String(options && (options.direction_bias || options.directionBias) || "").trim().toLowerCase(),
        ready_only: parseOptionalBoolean(options && (options.ready_only != null ? options.ready_only : options.readyOnly), false),
        signaled_only: parseOptionalBoolean(options && (options.signaled_only != null ? options.signaled_only : options.signaledOnly), false),
        sort_by: String(options && (options.sort_by || options.sortBy) || "").trim().toLowerCase() || "attention_asc",
    }
}

function matchesTodayTargetFilters(row, filters) {
    const normalized = filters || normalizeTodayTargetFilters({})
    if (normalized.search) {
        ensureTodayTargetRowDetails(row)
        const haystack = [
            row && row.symbol,
            row && row.exchange,
            row && row.industry,
            row && row.scan_reason,
            row && row.note,
            row && row.latest_signal_id,
            row && row.latest_signal_status,
            row && row.workflow_label,
            row && row.workflow_summary,
            row && row.workflow_next_action,
        ]
            .concat(Array.isArray(row && row.technical_flags) ? row.technical_flags : [])
            .concat(Array.isArray(row && row.operable_reasons) ? row.operable_reasons : [])
            .concat(Array.isArray(row && row.workflow_blockers) ? row.workflow_blockers : [])
            .join(" ")
            .toUpperCase()
        if (haystack.indexOf(normalized.search) === -1) return false
    }
    if (normalized.technical_state && String(row && row.technical_state || "").trim().toLowerCase() !== normalized.technical_state) return false
    if (normalized.target_status && String(row && row.target_status || "").trim().toLowerCase() !== normalized.target_status) return false
    if (normalized.direction_bias && String(row && row.direction_bias || "").trim().toLowerCase() !== normalized.direction_bias) return false
    if (!matchesTodayTargetSignalState(row, normalized.signal_state)) return false
    if (normalized.ready_only && String(row && row.technical_state || "").trim().toLowerCase() !== "ready") return false
    if (normalized.signaled_only && !(row && row.has_signal_today)) return false
    return true
}

function sortTodayTargetRows(rows, sortBy) {
    const items = Array.isArray(rows) ? rows.slice() : []
    const normalizedSort = String(sortBy || "").trim().toLowerCase() || "attention_asc"
    items.sort((left, right) => {
        if (normalizedSort === "symbol_asc") {
            return String(left && left.symbol || "").localeCompare(String(right && right.symbol || ""))
        }
        if (normalizedSort === "signal_desc") {
            return toInt(right && right.latest_signal_time_ms, 0) - toInt(left && left.latest_signal_time_ms, 0)
                || toInt(left && left.attention_rank, 99) - toInt(right && right.attention_rank, 99)
        }
        if (normalizedSort === "tradability_desc") {
            return toNumber(right && right.tradability_score, 0) - toNumber(left && left.tradability_score, 0)
                || toNumber(right && right.target_score, 0) - toNumber(left && left.target_score, 0)
        }
        if (normalizedSort === "target_desc") {
            return toNumber(right && right.target_score, 0) - toNumber(left && left.target_score, 0)
                || toNumber(right && right.tradability_score, 0) - toNumber(left && left.tradability_score, 0)
        }
        return toInt(left && left.attention_rank, 99) - toInt(right && right.attention_rank, 99)
            || toInt(right && right.latest_signal_time_ms, 0) - toInt(left && left.latest_signal_time_ms, 0)
            || toNumber(right && right.tradability_score, 0) - toNumber(left && left.tradability_score, 0)
            || toNumber(right && right.target_score, 0) - toNumber(left && left.target_score, 0)
            || String(left && left.symbol || "").localeCompare(String(right && right.symbol || ""))
    })
    return items
}

function buildFilteredTodayTargetSummary(rows) {
    const items = Array.isArray(rows) ? rows : []
    let readyCount = 0
    let signaledCount = 0
    let needsActionCount = 0
    for (let i = 0; i < items.length; i++) {
        const row = items[i]
        if (String(row && row.technical_state || "").trim().toLowerCase() === "ready") readyCount += 1
        if (row && row.has_signal_today) signaledCount += 1
        const latestStatus = normalizeSignalStatus(row && row.latest_signal_status)
        if (latestStatus === "awaiting_confirm" || latestStatus === "pending") needsActionCount += 1
    }
    return {
        total: items.length,
        ready_count: readyCount,
        signaled_count: signaledCount,
        needs_action_count: needsActionCount,
    }
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

function buildPrimaryViewUrl(runtimeEnvironment, marketDate) {
    const params = [
        `tab=${encodeURIComponent("screener")}`,
        `view=${encodeURIComponent("current")}`,
        `date=${encodeURIComponent(String(marketDate || "").trim())}`,
        `market_date=${encodeURIComponent(String(marketDate || "").trim())}`,
        `environment=${encodeURIComponent(String(runtimeEnvironment || LIVE_ENVIRONMENT).trim())}`,
    ]
    return `/ibkr_screener.html?${params.join("&")}`
}

function buildWorkflowGuide(runtimeEnvironment, marketDate) {
    return {
        scan_summary_time_et: DAILY_SCAN_SUMMARY_TIME_ET,
        open_check_time_et: MARKET_OPEN_CHECK_TIME_ET,
        intraday_refresh_rule: INTRADAY_REFRESH_RULE,
        focus_order_rule: "先看 awaiting_confirm / pending，再看 ready 未出信号，最后看 executed / stale。",
        primary_view_url: buildPrimaryViewUrl(runtimeEnvironment, marketDate),
    }
}

function formatWorkflowDirection(row) {
    const direction = String(row && row.latest_signal_direction || row && row.direction_bias || "").trim().toUpperCase()
    return direction || "--"
}

function buildBaseWorkflowBlockers(row) {
    const blockers = []
    const freshnessMin = Number.isFinite(Number(row && row.freshness_min)) ? Number(row.freshness_min) : null
    const alignedFlags = Array.isArray(row && row.technical_aligned_flags) ? row.technical_aligned_flags : []

    if (!(row && row.has_live_bar)) {
        pushUniqueText(blockers, "缺少当日 5m bars")
    } else if (freshnessMin != null && freshnessMin > 90) {
        pushUniqueText(blockers, `bars 延迟 ${Math.round(freshnessMin)}m`)
    }

    if (row && row.price <= 0) {
        pushUniqueText(blockers, "价格未就绪")
    }
    if (row && row.avg_10d_volume < 500000) {
        pushUniqueText(blockers, "10日均量不足 50 万")
    }
    if (row && !row.is_operable && row.tradability_score < 60) {
        pushUniqueText(blockers, "可操作分不足")
    }
    if (alignedFlags.length < 2) {
        pushUniqueText(blockers, "方向一致技术条件未集齐")
    }
    return blockers
}

function buildWorkflowMeta(row) {
    const signalStatus = normalizeSignalStatus(row && row.latest_signal_status)
    const blockers = []
    let stage = "watch"
    let label = "观察中"
    let summary = "该标的仍在今日目标池内，但技术或信号条件还没进入优先执行阶段。"
    let nextAction = "先看技术 flags、量能与 freshness，满足后继续等 5m close 刷新。"

    if (signalStatus === "awaiting_confirm") {
        stage = "awaiting_confirm"
        label = "信号待确认"
        summary = row && row.latest_signal_time
            ? `${row.latest_signal_time} 已产生 ${formatWorkflowDirection(row)} 信号，当前等待确认完成。`
            : "今日已产生信号，当前等待确认完成。"
        pushUniqueText(blockers, "等待信号确认完成")
        if (row && row.latest_signal_note) {
            pushUniqueText(blockers, row.latest_signal_note)
        }
        nextAction = "优先查看 Signals 页，确认 signal 状态、有效期和后续订单动作。"
    } else if (signalStatus === "pending") {
        stage = "pending"
        label = "信号待执行"
        summary = row && row.latest_signal_time
            ? `${row.latest_signal_time} 信号已进入 pending，等待执行链路推进。`
            : "今日信号已进入 pending，等待执行链路推进。"
        pushUniqueText(blockers, "等待下单或成交反馈")
        if (row && row.latest_signal_note) {
            pushUniqueText(blockers, row.latest_signal_note)
        }
        nextAction = "优先查看 Signals / Orders，确认挂单、成交和风控状态。"
    } else if (!(row && row.has_signal_today) && row && row.technical_state === "ready") {
        stage = "ready_no_signal"
        label = "技术已就绪"
        summary = "技术条件和可操作性已基本满足，但今日还没有触发信号。"
        pushUniqueText(blockers, "等待下一次 5m close 触发信号")
        nextAction = "盘中按 5m close 继续观察，重点联动当前榜单和 Signals 页。"
    } else if (row && row.technical_state === "stale") {
        stage = "stale"
        label = "数据待刷新"
        summary = "该标的缺少足够新鲜的盘中 bars，当前技术判断不可靠。"
        const baseBlockers = buildBaseWorkflowBlockers(row)
        for (let i = 0; i < baseBlockers.length; i++) {
            pushUniqueText(blockers, baseBlockers[i])
        }
        nextAction = "先检查 bars / indicators 是否刷新，再决定是否继续跟踪。"
    } else if (signalStatus === "executed") {
        stage = "executed"
        label = "已执行"
        summary = "今日信号已执行，后续重点转向持仓、退出和保护单管理。"
        nextAction = "去 Signals / Orders 跟踪持仓、止盈止损和退出状态。"
    } else if (signalStatus === "closed") {
        stage = "closed"
        label = "已闭环"
        summary = "今日信号已结束闭环，当前不再是优先执行对象。"
        nextAction = "保留复盘结论；若再次入榜，再重新进入关注。"
    } else if (signalStatus === "expired") {
        stage = "expired"
        label = "信号过期"
        summary = "今日信号已过期，当前执行窗口已经结束。"
        nextAction = "等待新的 5m close 或次日重新筛选。"
    } else if (signalStatus === "rejected") {
        stage = "rejected"
        label = "信号已拒绝"
        summary = "今日信号已被拒绝或取消，不再继续推进执行。"
        if (row && row.latest_signal_note) {
            pushUniqueText(blockers, row.latest_signal_note)
        }
        nextAction = "查看信号备注与风控原因，确认是否继续观察。"
    } else {
        const baseBlockers = buildBaseWorkflowBlockers(row)
        for (let i = 0; i < baseBlockers.length; i++) {
            pushUniqueText(blockers, baseBlockers[i])
        }
    }

    if (!blockers.length && (stage === "watch" || stage === "ready_no_signal")) {
        pushUniqueText(blockers, "等待下一次 5m close 刷新")
    }

    return {
        stage: stage,
        label: label,
        summary: summary,
        blockers: blockers,
        next_action: nextAction,
    }
}

function pruneTodayTargetPayloadCache(nowMs) {
    const cacheNowMs = toInt(nowMs, Date.now())
    const keys = Object.keys(todayTargetPayloadCache)
    for (let i = 0; i < keys.length; i++) {
        const key = keys[i]
        const entry = todayTargetPayloadCache[key]
        if (!entry || toInt(entry.expires_at_ms, 0) <= cacheNowMs) {
            delete todayTargetPayloadCache[key]
        }
    }
}

function buildTodayTargetPayloadCacheKey(environment, marketDate, filters, paginationEnabled, page, perPage) {
    const normalizedFilters = filters || normalizeTodayTargetFilters({})
    return JSON.stringify({
        environment: String(environment || LIVE_ENVIRONMENT).trim().toLowerCase(),
        market_date: String(marketDate || "").trim(),
        search: normalizedFilters.search || "",
        technical_state: normalizedFilters.technical_state || "",
        signal_state: normalizedFilters.signal_state || "",
        target_status: normalizedFilters.target_status || "",
        direction_bias: normalizedFilters.direction_bias || "",
        ready_only: Boolean(normalizedFilters.ready_only),
        signaled_only: Boolean(normalizedFilters.signaled_only),
        sort_by: normalizedFilters.sort_by || "attention_asc",
        paginate: Boolean(paginationEnabled),
        page: Math.max(1, toInt(page, 1)),
        per_page: Math.max(1, toInt(perPage, 10)),
    })
}

function getCachedTodayTargetPayload(cacheKey, nowMs) {
    pruneTodayTargetPayloadCache(nowMs)
    const entry = todayTargetPayloadCache[String(cacheKey || "")]
    if (!entry || !entry.payload) return null
    return entry.payload
}

function setCachedTodayTargetPayload(cacheKey, payload, nowMs) {
    pruneTodayTargetPayloadCache(nowMs)
    todayTargetPayloadCache[String(cacheKey || "")] = {
        expires_at_ms: toInt(nowMs, Date.now()) + TODAY_TARGET_PAYLOAD_CACHE_TTL_MS,
        payload: payload,
    }
    return payload
}

function clearTodayTargetPayloadCache() {
    const keys = Object.keys(todayTargetPayloadCache)
    for (let i = 0; i < keys.length; i++) {
        delete todayTargetPayloadCache[keys[i]]
    }
}

function ensureTodayTargetRowDetails(row) {
    if (!row || row.workflow_summary) return row
    const workflowMeta = buildWorkflowMeta(row)
    row.workflow_stage = workflowMeta.stage
    row.workflow_label = workflowMeta.label
    row.workflow_summary = workflowMeta.summary
    row.workflow_blockers = workflowMeta.blockers
    row.workflow_next_action = workflowMeta.next_action
    return row
}

function buildTodayTargetPayload(options) {
    const runtimeEnvironment = normalizeRuntimeEnvironment(options && options.environment, LIVE_ENVIRONMENT)
    const currentMarketDate = getCurrentMarketDate()
    const requestedMarketDate = String(options && (options.marketDate || options.date) || "").trim() || currentMarketDate
    const marketStartCandidateMs = Date.parse(`${requestedMarketDate}T04:00:00.000Z`)
    const marketDate = Number.isFinite(marketStartCandidateMs) ? requestedMarketDate : currentMarketDate
    const filters = normalizeTodayTargetFilters(options)
    const workflowGuide = buildWorkflowGuide(runtimeEnvironment, marketDate)
    const paginationEnabled = Boolean(options && options.paginate)
    const requestedPerPage = Math.max(1, Math.min(200, toInt(options && (options.per_page || options.perPage), 10)))
    const requestedPage = paginationEnabled ? Math.max(1, toInt(options && options.page, 1)) : 1
    const cacheNowMs = Date.now()
    const cacheKey = buildTodayTargetPayloadCacheKey(
        runtimeEnvironment,
        marketDate,
        filters,
        paginationEnabled,
        requestedPage,
        requestedPerPage
    )
    const cachedPayload = getCachedTodayTargetPayload(cacheKey, cacheNowMs)
    if (cachedPayload) {
        return cachedPayload
    }
    const targetFilterClauses = [
        'environment = {:env}',
        'date = {:date}',
        '(status = "candidate" || status = "active")',
    ]
    const targetFilterParams = { env: runtimeEnvironment, date: marketDate }
    const targetRecords = $app.findRecordsByFilter(
        "ibkr_targets",
        targetFilterClauses.join(" && "),
        "-updated",
        500,
        0,
        targetFilterParams
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
            current_market_date: currentMarketDate,
            computed_at_ms: computedAtMs,
            computed_at_us: formatEtDateTime(computedAtMs),
            computed_at_cn: formatCnDateTime(computedAtMs),
            workflow: workflowGuide,
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
    const indicatorLookbackMs = marketStartMs
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
    const intradayRecords = $app.findRecordsByFilter("ibkr_bars", intradayFilter, "", 50000, 0) || []
    const indicatorRecords = $app.findRecordsByFilter("ibkr_indicators", indicatorFilter, "", 10000, 0) || []
    const signalRecords = $app.findRecordsByFilter("ibkr_signals", signalFilter, "", 10000, 0) || []

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
        const currentLatest = latestIntradayBySymbol[symbol]
        if (!currentLatest || row.bar_time_ms >= currentLatest.bar_time_ms) {
            latestIntradayBySymbol[symbol] = row
        }
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
        if (!symbol) continue
        const currentLatest = latestIndicatorBySymbol[symbol]
        if (!currentLatest || toInt(record.get("bar_time_ms"), 0) >= toInt(currentLatest.get("bar_time_ms"), 0)) {
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

    const filteredItems = sortTodayTargetRows(items, filters.sort_by).filter((row) => matchesTodayTargetFilters(row, filters))
    const filteredSummary = buildFilteredTodayTargetSummary(filteredItems)
    const totalPages = paginationEnabled
        ? Math.max(1, Math.ceil(filteredItems.length / requestedPerPage))
        : 1
    const page = paginationEnabled
        ? Math.min(requestedPage, totalPages)
        : 1
    const offset = paginationEnabled ? (page - 1) * requestedPerPage : 0
    const pagedItems = paginationEnabled
        ? filteredItems.slice(offset, offset + requestedPerPage)
        : filteredItems
    for (let i = 0; i < pagedItems.length; i++) {
        ensureTodayTargetRowDetails(pagedItems[i])
    }

    const payload = {
        ok: true,
        environment: runtimeEnvironment,
        market_date: marketDate,
        current_market_date: currentMarketDate,
        computed_at_ms: computedAtMs,
        computed_at_us: formatEtDateTime(computedAtMs),
        computed_at_cn: formatCnDateTime(computedAtMs),
        workflow: workflowGuide,
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
        filters: filters,
        filtered_summary: filteredSummary,
        filtered_total: filteredItems.length,
        pagination_enabled: paginationEnabled,
        page: page,
        per_page: paginationEnabled ? requestedPerPage : filteredItems.length,
        total_pages: totalPages,
        has_prev_page: paginationEnabled ? page > 1 : false,
        has_next_page: paginationEnabled ? page < totalPages : false,
        returned_count: pagedItems.length,
        items: pagedItems,
    }
    return setCachedTodayTargetPayload(cacheKey, payload, cacheNowMs)
}

module.exports = {
    TODAY_TARGET_STATUSES,
    clearTodayTargetPayloadCache,
    buildTodayTargetPayload,
}
