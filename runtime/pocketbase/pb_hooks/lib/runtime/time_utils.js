function getTimeStrings() {
    const now = new Date()
    const usOffset = -4 * 60
    const cnOffset = 8 * 60
    const usTime = new Date(now.getTime() + usOffset * 60000)
    const cnTime = new Date(now.getTime() + cnOffset * 60000)
    const date = usTime.toISOString().slice(0, 10)

    return {
        us: usTime.toISOString().slice(0, 19).replace("T", " "),
        cn: cnTime.toISOString().slice(0, 19).replace("T", " "),
        date: date,
        todayStart: `${date} 00:00:00`,
    }
}

module.exports = {
    getTimeStrings,
}
