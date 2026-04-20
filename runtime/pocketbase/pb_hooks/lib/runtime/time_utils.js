const usEasternTime = typeof __hooks !== "undefined"
    ? require(`${__hooks}/lib/runtime/us_eastern_time.js`)
    : require("./us_eastern_time.js")

function getTimeStrings(nowMs) {
    const safeNowMs = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now()
    const us = usEasternTime.formatUsEasternTime(safeNowMs)
    const cn = usEasternTime.formatCnTime(safeNowMs)
    const date = us.slice(0, 10)
    return {
        us: us,
        cn: cn,
        date: date,
        todayStart: `${date} 00:00:00`,
    }
}

module.exports = {
    getTimeStrings,
}
