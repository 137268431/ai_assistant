const US_EASTERN_DST_OFFSET_MINUTES = -4 * 60
const US_EASTERN_STANDARD_OFFSET_MINUTES = -5 * 60
const CN_OFFSET_MINUTES = 8 * 60

function toFiniteNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : fallback
}

function pad2(value) {
    return String(value || 0).padStart(2, "0")
}

function nthWeekdayOfMonth(year, monthIndex, weekday, nth) {
    const firstDayWeekday = new Date(Date.UTC(year, monthIndex, 1)).getUTCDay()
    const delta = (weekday - firstDayWeekday + 7) % 7
    return 1 + delta + (nth - 1) * 7
}

function isUsEasternDstUtcMs(utcMs) {
    const safeUtcMs = toFiniteNumber(utcMs, 0)
    if (safeUtcMs <= 0) return false
    const utcDate = new Date(safeUtcMs)
    const year = utcDate.getUTCFullYear()
    const dstStartDay = nthWeekdayOfMonth(year, 2, 0, 2)
    const dstEndDay = nthWeekdayOfMonth(year, 10, 0, 1)
    const dstStartUtcMs = Date.UTC(year, 2, dstStartDay, 7, 0, 0)
    const dstEndUtcMs = Date.UTC(year, 10, dstEndDay, 6, 0, 0)
    return safeUtcMs >= dstStartUtcMs && safeUtcMs < dstEndUtcMs
}

function getUsEasternOffsetMinutesForUtcMs(utcMs) {
    return isUsEasternDstUtcMs(utcMs)
        ? US_EASTERN_DST_OFFSET_MINUTES
        : US_EASTERN_STANDARD_OFFSET_MINUTES
}

function formatTimeByOffset(utcMs, offsetMinutes) {
    const safeUtcMs = toFiniteNumber(utcMs, 0)
    if (safeUtcMs <= 0) return ""
    return new Date(safeUtcMs + Number(offsetMinutes || 0) * 60000)
        .toISOString()
        .slice(0, 19)
        .replace("T", " ")
}

function getPartsByOffset(utcMs, offsetMinutes) {
    const safeUtcMs = toFiniteNumber(utcMs, 0)
    if (safeUtcMs <= 0) {
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
    const shifted = new Date(safeUtcMs + Number(offsetMinutes || 0) * 60000)
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

function buildUtcMsFromOffsetParts(parts, offsetMinutes) {
    const year = toFiniteNumber(parts && parts.year, 0)
    const month = toFiniteNumber(parts && parts.month, 0)
    const day = toFiniteNumber(parts && parts.day, 0)
    const hour = toFiniteNumber(parts && parts.hour, 0)
    const minute = toFiniteNumber(parts && parts.minute, 0)
    const second = toFiniteNumber(parts && parts.second, 0)
    if (year <= 0 || month <= 0 || day <= 0) return 0
    return Date.UTC(year, month - 1, day, hour, minute, second) - (Number(offsetMinutes || 0) * 60000)
}

function matchesLocalParts(actualParts, expectedParts) {
    return (
        Number(actualParts.year || 0) === Number(expectedParts.year || 0)
        && Number(actualParts.month || 0) === Number(expectedParts.month || 0)
        && Number(actualParts.day || 0) === Number(expectedParts.day || 0)
        && Number(actualParts.hour || 0) === Number(expectedParts.hour || 0)
        && Number(actualParts.minute || 0) === Number(expectedParts.minute || 0)
        && Number(actualParts.second || 0) === Number(expectedParts.second || 0)
    )
}

function formatUsEasternTime(utcMs) {
    const safeUtcMs = toFiniteNumber(utcMs, 0)
    if (safeUtcMs <= 0) return ""
    return formatTimeByOffset(safeUtcMs, getUsEasternOffsetMinutesForUtcMs(safeUtcMs))
}

function formatCnTime(utcMs) {
    return formatTimeByOffset(utcMs, CN_OFFSET_MINUTES)
}

function getUsEasternParts(utcMs) {
    const safeUtcMs = toFiniteNumber(utcMs, 0)
    if (safeUtcMs <= 0) return getPartsByOffset(0, 0)
    return getPartsByOffset(safeUtcMs, getUsEasternOffsetMinutesForUtcMs(safeUtcMs))
}

function buildUsEasternTimestampMs(parts) {
    const safeParts = {
        year: toFiniteNumber(parts && parts.year, 0),
        month: toFiniteNumber(parts && parts.month, 0),
        day: toFiniteNumber(parts && parts.day, 0),
        hour: toFiniteNumber(parts && parts.hour, 0),
        minute: toFiniteNumber(parts && parts.minute, 0),
        second: toFiniteNumber(parts && parts.second, 0),
    }
    if (safeParts.year <= 0 || safeParts.month <= 0 || safeParts.day <= 0) return 0

    const candidateOffsets = [
        US_EASTERN_DST_OFFSET_MINUTES,
        US_EASTERN_STANDARD_OFFSET_MINUTES,
    ]
    for (let i = 0; i < candidateOffsets.length; i++) {
        const candidateUtcMs = buildUtcMsFromOffsetParts(safeParts, candidateOffsets[i])
        if (candidateUtcMs <= 0) continue
        if (matchesLocalParts(getUsEasternParts(candidateUtcMs), safeParts)) {
            return candidateUtcMs
        }
    }

    const fallbackReferenceUtcMs = Date.UTC(
        safeParts.year,
        safeParts.month - 1,
        safeParts.day,
        12,
        0,
        0
    )
    const fallbackOffsetMinutes = getUsEasternOffsetMinutesForUtcMs(fallbackReferenceUtcMs)
    return buildUtcMsFromOffsetParts(safeParts, fallbackOffsetMinutes)
}

function parseUsEasternTimeMs(value) {
    const text = String(value || "").trim()
    if (!text) return 0
    const match = text.match(
        /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/
    )
    if (!match) return 0
    return buildUsEasternTimestampMs({
        year: Number(match[1]),
        month: Number(match[2]),
        day: Number(match[3]),
        hour: Number(match[4]),
        minute: Number(match[5]),
        second: Number(match[6] || 0),
    })
}

module.exports = {
    US_EASTERN_DST_OFFSET_MINUTES,
    US_EASTERN_STANDARD_OFFSET_MINUTES,
    CN_OFFSET_MINUTES,
    formatTimeByOffset,
    formatUsEasternTime,
    formatCnTime,
    getUsEasternOffsetMinutesForUtcMs,
    getUsEasternParts,
    buildUsEasternTimestampMs,
    parseUsEasternTimeMs,
}
