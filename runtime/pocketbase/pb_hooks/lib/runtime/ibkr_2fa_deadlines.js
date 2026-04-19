const US_OFFSET_MINUTES = -4 * 60
const CN_OFFSET_MINUTES = 8 * 60
const WEEKLY_REAUTH_REASON = "weekly_reauth"
const WEEKLY_PREAMARKET_HOUR = 9
const WEEKLY_PREAMARKET_MINUTE = 20
const DEFAULT_CONFIRM_TIMEOUT_SECONDS = 180
const DAY_MS = 24 * 60 * 60 * 1000

function toFiniteNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : fallback
}

function parseShiftedTimeMs(value, offsetMinutes) {
    const text = String(value || "").trim()
    if (!text) return 0
    const parsed = Date.parse(text.replace(" ", "T") + "Z")
    if (!Number.isFinite(parsed)) return 0
    return parsed - (Number(offsetMinutes || 0) * 60000)
}

function formatShiftedTime(ms, offsetMinutes) {
    const safeMs = toFiniteNumber(ms, 0)
    if (safeMs <= 0) return ""
    return new Date(safeMs + Number(offsetMinutes || 0) * 60000)
        .toISOString()
        .slice(0, 19)
        .replace("T", " ")
}

function getShiftedParts(ms, offsetMinutes) {
    const safeMs = toFiniteNumber(ms, 0)
    if (safeMs <= 0) {
        return {
            year: 0,
            month: 0,
            day: 0,
            weekday: 0,
            hour: 0,
            minute: 0,
            second: 0,
        }
    }
    const shifted = new Date(safeMs + Number(offsetMinutes || 0) * 60000)
    return {
        year: shifted.getUTCFullYear(),
        month: shifted.getUTCMonth() + 1,
        day: shifted.getUTCDate(),
        weekday: shifted.getUTCDay(),
        hour: shifted.getUTCHours(),
        minute: shifted.getUTCMinutes(),
        second: shifted.getUTCSeconds(),
    }
}

function buildShiftedTimestampMs(parts, offsetMinutes) {
    const year = toFiniteNumber(parts && parts.year, 0)
    const month = toFiniteNumber(parts && parts.month, 0)
    const day = toFiniteNumber(parts && parts.day, 0)
    const hour = toFiniteNumber(parts && parts.hour, 0)
    const minute = toFiniteNumber(parts && parts.minute, 0)
    const second = toFiniteNumber(parts && parts.second, 0)
    if (year <= 0 || month <= 0 || day <= 0) return 0
    return Date.UTC(year, month - 1, day, hour, minute, second) - (Number(offsetMinutes || 0) * 60000)
}

function getWeeklyReauthBusinessDeadline(baseMs, options) {
    const safeBaseMs = toFiniteNumber(baseMs, Date.now())
    const safeNowMs = toFiniteNumber(options && options.nowMs, safeBaseMs)
    const usParts = getShiftedParts(safeBaseMs, US_OFFSET_MINUTES)
    const currentUsStartMs = buildShiftedTimestampMs({
        year: usParts.year,
        month: usParts.month,
        day: usParts.day,
        hour: 0,
        minute: 0,
        second: 0,
    }, US_OFFSET_MINUTES)
    const mondayStartMs = usParts.weekday === 0
        ? (currentUsStartMs + DAY_MS)
        : (currentUsStartMs + ((1 - usParts.weekday) * DAY_MS))
    const mondayUsParts = getShiftedParts(mondayStartMs, US_OFFSET_MINUTES)
    const deadlineMs = buildShiftedTimestampMs({
        year: mondayUsParts.year,
        month: mondayUsParts.month,
        day: mondayUsParts.day,
        hour: WEEKLY_PREAMARKET_HOUR,
        minute: WEEKLY_PREAMARKET_MINUTE,
        second: 0,
    }, US_OFFSET_MINUTES)

    return {
        business_deadline_ms: deadlineMs,
        business_deadline_at: formatShiftedTime(deadlineMs, US_OFFSET_MINUTES),
        business_deadline_cn: formatShiftedTime(deadlineMs, CN_OFFSET_MINUTES),
        business_deadline_label: "周一盘前前完成验证",
        business_deadline_overdue: safeNowMs > deadlineMs,
    }
}

function getConfirmDeadline(triggeredAt, options) {
    const opts = options || {}
    const timeoutSeconds = Math.max(1, Math.round(toFiniteNumber(opts.confirmTimeoutSeconds, DEFAULT_CONFIRM_TIMEOUT_SECONDS)))
    const triggeredMs = typeof triggeredAt === "number"
        ? toFiniteNumber(triggeredAt, 0)
        : parseShiftedTimeMs(triggeredAt, US_OFFSET_MINUTES)
    if (triggeredMs <= 0) {
        return {
            confirm_window_seconds: timeoutSeconds,
            confirm_deadline_ms: 0,
            confirm_deadline_at: "",
            confirm_deadline_cn: "",
            confirm_deadline_overdue: false,
        }
    }
    const nowMs = toFiniteNumber(opts.nowMs, Date.now())
    const deadlineMs = triggeredMs + timeoutSeconds * 1000
    return {
        confirm_window_seconds: timeoutSeconds,
        confirm_deadline_ms: deadlineMs,
        confirm_deadline_at: formatShiftedTime(deadlineMs, US_OFFSET_MINUTES),
        confirm_deadline_cn: formatShiftedTime(deadlineMs, CN_OFFSET_MINUTES),
        confirm_deadline_overdue: nowMs > deadlineMs,
    }
}

function deriveTwoFactorDeadlines(stateData, options) {
    const state = stateData && typeof stateData === "object" ? stateData : {}
    const opts = options || {}
    const nowMs = toFiniteNumber(opts.nowMs, Date.now())
    const requestedMs = parseShiftedTimeMs(state.requested_at, US_OFFSET_MINUTES)
    const triggeredMs = parseShiftedTimeMs(state.triggered_at, US_OFFSET_MINUTES)
    const status = String(state.status || "").trim().toLowerCase()
    const reason = String(state.reason || state.recovery_reason || "").trim().toLowerCase()
    const deadlines = {
        business_deadline_ms: 0,
        business_deadline_at: "",
        business_deadline_cn: "",
        business_deadline_label: "",
        business_deadline_overdue: false,
        confirm_window_seconds: Math.max(1, Math.round(toFiniteNumber(opts.confirmTimeoutSeconds, DEFAULT_CONFIRM_TIMEOUT_SECONDS))),
        confirm_deadline_ms: 0,
        confirm_deadline_at: "",
        confirm_deadline_cn: "",
        confirm_deadline_overdue: false,
    }

    if (reason === WEEKLY_REAUTH_REASON) {
        Object.assign(deadlines, getWeeklyReauthBusinessDeadline(requestedMs || triggeredMs || nowMs, { nowMs: nowMs }))
    }

    if (["triggered", "waiting_confirm", "waiting_response", "timeout", "failed"].includes(status)) {
        Object.assign(deadlines, getConfirmDeadline(triggeredMs, {
            nowMs: nowMs,
            confirmTimeoutSeconds: deadlines.confirm_window_seconds,
        }))
    }

    return deadlines
}

module.exports = {
    US_OFFSET_MINUTES,
    CN_OFFSET_MINUTES,
    WEEKLY_REAUTH_REASON,
    WEEKLY_PREAMARKET_HOUR,
    WEEKLY_PREAMARKET_MINUTE,
    DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    parseShiftedTimeMs,
    formatShiftedTime,
    getWeeklyReauthBusinessDeadline,
    getConfirmDeadline,
    deriveTwoFactorDeadlines,
}
