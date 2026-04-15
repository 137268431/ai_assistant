const MONITOR_ALERT_COOLDOWN_MS = 15 * 60 * 1000
const MONITOR_ALERT_STATE_KEY = "system_monitor_alert"
const MONITOR_ALERT_FLAG_CODES = {
    monitor_endpoint_unavailable: true,
    gateway_offline: true,
    session_unauthenticated: true,
    websocket_not_ready: true,
    subscription_utilization_high: true,
    subscription_utilization_critical: true,
    market_data_silent: true,
    market_data_silent_critical: true,
    data_freshness_delayed: true,
    data_freshness_offline: true,
    host_memory_high: true,
    host_memory_critical: true,
    host_disk_high: true,
    host_disk_critical: true,
    host_load_high: true,
    host_load_critical: true,
    host_cpu_high: true,
    host_cpu_critical: true,
    pb_disk_high: true,
    pb_disk_critical: true,
}
const RUNTIME_KEYS = ["ibkr_compute_enabled", "ibkr_trading_enabled", "pb_scheduler_enabled"]

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function parseStateValue(raw) {
    if (!raw) return {}
    if (typeof raw === "object") return raw
    try {
        const parsed = JSON.parse(String(raw || ""))
        return parsed && typeof parsed === "object" ? parsed : {}
    } catch (_) {
        return {}
    }
}

function getStateRecord(stateKey, environment, dateToken) {
    try {
        return $app.findFirstRecordByFilter(
            "ibkr_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: stateKey, d: dateToken, env: environment }
        )
    } catch (_) {
        return null
    }
}

function getStateData(stateKey, environment, dateToken) {
    const record = getStateRecord(stateKey, environment, dateToken)
    if (!record) return { record: null, data: {} }
    const raw = typeof record.getString === "function"
        ? (record.getString("data") || "")
        : record.get("data")
    return {
        record: record,
        data: parseStateValue(raw),
    }
}

function saveStateData(stateKey, environment, dateToken, patch) {
    const current = getStateData(stateKey, environment, dateToken)
    const collection = $app.findCollectionByNameOrId("ibkr_state")
    const record = current.record || new Record(collection, {})
    const next = {
        ...(current.data || {}),
        ...(patch || {}),
    }
    record.set("state_key", stateKey)
    record.set("date", dateToken)
    record.set("environment", environment)
    record.set("data", JSON.stringify(next))
    $app.save(record)
    return next
}

function parseHttpJson(resp) {
    if (!resp) return {}
    const raw = typeof resp.raw === "string"
        ? resp.raw
        : (typeof resp === "string" ? resp : String(resp.raw || ""))
    return raw ? JSON.parse(raw) : {}
}

function fetchComputeJson(path, timeoutSeconds, environment) {
    const { fetchComputeJsonWithFallback } = require(`${__hooks}/lib/compute_http.js`)
    const result = fetchComputeJsonWithFallback(path, timeoutSeconds, environment)
    const payload = result && result.payload && typeof result.payload === "object"
        ? result.payload
        : {}
    if (result && result.upstream && !Array.isArray(payload) && !payload.proxy_upstream) {
        payload.proxy_upstream = result.upstream
    }
    return payload
}

function selectMonitorAlertFlags(flags) {
    const items = []
    for (let i = 0; i < (flags || []).length; i++) {
        const item = flags[i]
        const code = String(item && item.code || "").trim()
        const severity = String(item && item.severity || "").trim().toLowerCase()
        if (!MONITOR_ALERT_FLAG_CODES[code]) continue
        if (severity !== "warning" && severity !== "error") continue
        items.push(item)
    }
    return items
}

function buildSyntheticMonitorAlertFlags(monitorPayload) {
    const status = String(monitorPayload && monitorPayload.status || "").trim().toLowerCase()
    const code = toNumber(monitorPayload && monitorPayload.code, 0)
    const error = String(monitorPayload && monitorPayload.error || "").trim()
    if (!error && status !== "offline" && status !== "error" && code < 500) {
        return []
    }
    const detail = error || (code > 0
        ? `compute /ibkr/monitor 返回 HTTP ${code}`
        : "compute /ibkr/monitor 当前不可用")
    return [{
        severity: "error",
        code: "monitor_endpoint_unavailable",
        title: "Monitor endpoint unavailable",
        detail: detail,
    }]
}

function buildMonitorAlertFingerprint(monitorPayload, flags) {
    return JSON.stringify({
        status: String(monitorPayload && monitorPayload.status || "").trim().toLowerCase(),
        flag_codes: (flags || []).map((item) => String(item && item.code || "").trim()).filter(Boolean).sort(),
    })
}

function formatBytes(value) {
    const bytes = Number(value)
    if (!Number.isFinite(bytes) || bytes < 0) return "--"
    if (bytes === 0) return "0 B"
    const units = ["B", "KB", "MB", "GB", "TB"]
    let size = bytes
    let unitIndex = 0
    while (size >= 1024 && unitIndex < units.length - 1) {
        size /= 1024
        unitIndex += 1
    }
    const digits = size >= 100 ? 0 : (size >= 10 ? 1 : 2)
    return `${size.toFixed(digits)} ${units[unitIndex]}`
}

function runSystemMonitorAlertGuard(logPrefix) {
    const prefix = logPrefix || "[IBKRMonitorAlert]"
    const feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
    const pocketbaseDiskMonitor = require(`${__hooks}/lib/pocketbase_disk_monitor.js`)
    const { getActiveRuntimeEnvironments, getComputeEnabledForEnvironment } = require(`${__hooks}/lib/runtime_modes.js`)
    const { getTimeStrings } = require(`${__hooks}/lib/time_utils.js`)
    const { writeSystemEvent } = require(`${__hooks}/lib/system_events.js`)

    const times = getTimeStrings()
    const nowMs = Date.now()
    const environments = getActiveRuntimeEnvironments(RUNTIME_KEYS)

    for (let i = 0; i < environments.length; i++) {
        const environment = environments[i]
        if (!getComputeEnabledForEnvironment(environment, RUNTIME_KEYS)) {
            continue
        }

        const monitorPayload = pocketbaseDiskMonitor.enrichMonitorPayloadWithPocketBaseDisk(
            fetchComputeJson("/ibkr/monitor", 10, environment),
            false
        )
        if (!monitorPayload || typeof monitorPayload !== "object") {
            continue
        }

        const baseAlertFlags = selectMonitorAlertFlags(monitorPayload.flags || [])
        const syntheticAlertFlags = buildSyntheticMonitorAlertFlags(monitorPayload)
        const alertFlags = baseAlertFlags.concat(
            syntheticAlertFlags.filter((item) => {
                const syntheticCode = String(item && item.code || "").trim()
                return !baseAlertFlags.some((baseItem) => String(baseItem && baseItem.code || "").trim() === syntheticCode)
            })
        )
        if (!alertFlags.length) {
            saveStateData(MONITOR_ALERT_STATE_KEY, environment, times.date, {
                last_monitor_check_at: times.us,
                last_monitor_issue_at: "",
                last_monitor_alert_hash: "",
                last_monitor_alert_ms: 0,
            })
            continue
        }

        const fingerprint = buildMonitorAlertFingerprint(monitorPayload, alertFlags)
        const state = getStateData(MONITOR_ALERT_STATE_KEY, environment, times.date).data || {}
        const lastAlertHash = String(state.last_monitor_alert_hash || "")
        const lastAlertMs = toNumber(state.last_monitor_alert_ms, 0)
        const shouldNotify = (
            fingerprint !== lastAlertHash
            || lastAlertMs <= 0
            || (nowMs - lastAlertMs) >= MONITOR_ALERT_COOLDOWN_MS
        )

        if (!shouldNotify) {
            saveStateData(MONITOR_ALERT_STATE_KEY, environment, times.date, {
                last_monitor_check_at: times.us,
                last_monitor_issue_at: String(state.last_monitor_issue_at || times.us),
                last_monitor_alert_hash: lastAlertHash,
                last_monitor_alert_ms: lastAlertMs,
            })
            continue
        }

        const apiUtilization = monitorPayload.api_utilization || {}
        const host = monitorPayload.host || {}
        const cpu = host.cpu || {}
        const memory = host.memory || {}
        const disk = host.disk || {}
        const loadavg = host.loadavg || {}
        const pocketbaseDisk = ((monitorPayload.pocketbase || {}).disk) || {}
        const pocketbaseFilesystem = pocketbaseDisk.filesystem || {}
        const level = alertFlags.some((item) => String(item && item.severity || "").trim().toLowerCase() === "error")
            ? "error"
            : "warning"
        const title = level === "error"
            ? `IBKR Monitor 严重告警（${alertFlags.length}项）`
            : `IBKR Monitor 告警（${alertFlags.length}项）`
        const detail = {
            "检查时间": times.us,
            "监控状态": String(monitorPayload.status || "unknown").toUpperCase(),
            "触发项": alertFlags.slice(0, 4).map((item) => `${item.title || item.code}: ${item.detail || ""}`).join(" | "),
            "订阅占用": `${toNumber(apiUtilization.active_subscription_count, 0)}/${toNumber(apiUtilization.subscription_limit, 0)} (${toNumber(apiUtilization.utilization_pct, 0).toFixed(2)}%)`,
            "WebSocket": `msg_age=${apiUtilization.last_message_age_s != null ? `${apiUtilization.last_message_age_s}s` : "--"} · subs=${toNumber(apiUtilization.ws_subscribed_count, 0)} · pending=${toNumber(apiUtilization.pending_subscription_count, 0)}`,
            "主机CPU": cpu.used_pct != null ? `${cpu.used_pct}%` : "--",
            "主机Load/CPU": loadavg.per_cpu_1 != null ? String(loadavg.per_cpu_1) : "--",
            "主机内存": memory.used_pct != null ? `${memory.used_pct}%` : "--",
            "主机磁盘": disk.used_pct != null ? `${disk.used_pct}%` : "--",
            "PB磁盘": pocketbaseFilesystem.used_pct != null
                ? `${pocketbaseFilesystem.used_pct}% · data=${formatBytes(pocketbaseDisk.data_size_bytes)} · free=${formatBytes(pocketbaseFilesystem.available_bytes)}`
                : "--",
        }

        const notified = level === "error"
            ? feishuSystem.notifyAlert("ibkr_compute", title, detail, environment)
            : feishuSystem.notifyWarning("ibkr_compute", title, detail, environment)
        writeSystemEvent("alert", level, "ibkr_compute", title, detail, environment, notified)
        saveStateData(MONITOR_ALERT_STATE_KEY, environment, times.date, {
            last_monitor_check_at: times.us,
            last_monitor_issue_at: times.us,
            last_monitor_alert_hash: fingerprint,
            last_monitor_alert_ms: nowMs,
        })
        console.log(`${prefix} ${environment}: level=${level}, notified=${notified}, flags=${alertFlags.map((item) => item.code).join(",")}`)
    }
}

module.exports = {
    runSystemMonitorAlertGuard,
}
