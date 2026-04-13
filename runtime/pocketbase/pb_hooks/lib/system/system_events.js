function writeSystemEvent(eventType, level, source, title, detail, environment, notified) {
    const { labelTitleWithEnvironment, addEnvironmentToDetail } = require(`${__hooks}/lib/environment.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)

    try {
        const collection = $app.findCollectionByNameOrId("system_events")
        const record = new Record(collection)
        const times = getTimeStrings()
        record.set("event_type", eventType)
        record.set("level", level)
        record.set("source", source)
        record.set("environment", environment)
        record.set("title", labelTitleWithEnvironment(title, environment))
        record.set("detail", addEnvironmentToDetail(detail, environment))
        record.set("us_time", times.us)
        record.set("cn_time", times.cn)
        record.set("notified", !!notified)
        $app.save(record)
        return record
    } catch (err) {
        console.error(`[SystemEvents] 写 system_events 失败: ${err.message}`)
        return null
    }
}

module.exports = {
    writeSystemEvent,
}
