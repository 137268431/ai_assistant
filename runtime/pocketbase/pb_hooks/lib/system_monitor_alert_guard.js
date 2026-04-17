const MONITOR_ALERT_COOLDOWN_MS = 15 * 60 * 1000
const MONITOR_ALERT_STATE_KEY = "system_monitor_alert"
const HOST_LOAD_CONSECUTIVE_CONFIG_KEY = "system_monitor_host_load_consecutive_count"
const DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT = 2
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
const LOAD_MONITOR_ALERT_CODES = {
    host_load_high: 1,
    host_load_critical: 2,
}

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function normalizePositiveInt(value, fallback) {
    const num = Math.floor(Number(value))
    if (Number.isFinite(num) && num >= 1) {
        return num
    }
    const safeFallback = Math.floor(Number(fallback))
    if (Number.isFinite(safeFallback) && safeFallback >= 1) {
        return safeFallback
    }
    return 1
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
    if (result && result.status_code && !Array.isArray(payload) && payload.proxy_status_code == null) {
        payload.proxy_status_code = Number(result.status_code || 0) || 0
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
    const code = toNumber(monitorPayload && (monitorPayload.proxy_status_code != null ? monitorPayload.proxy_status_code : monitorPayload.code), 0)
    const error = String(monitorPayload && monitorPayload.error || "").trim()
    const proxyUpstream = String(monitorPayload && monitorPayload.proxy_upstream || "").trim()
    if (proxyUpstream && code < 400) {
        return []
    }
    if (!error && status !== "offline" && code < 400) {
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

function getHostLoadConsecutiveThreshold(environment) {
    const { getConfigValue } = require(`${__hooks}/lib/environment.js`)
    return normalizePositiveInt(
        getConfigValue(
            HOST_LOAD_CONSECUTIVE_CONFIG_KEY,
            String(DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT),
            environment
        ),
        DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT
    )
}

function isLoadMonitorAlertFlag(flag) {
    const code = String(flag && flag.code || "").trim()
    return !!LOAD_MONITOR_ALERT_CODES[code]
}

function selectPrimaryLoadMonitorAlertFlag(flags) {
    let selected = null
    let selectedRank = 0
    for (let i = 0; i < (flags || []).length; i++) {
        const item = flags[i]
        const rank = LOAD_MONITOR_ALERT_CODES[String(item && item.code || "").trim()] || 0
        if (rank > selectedRank) {
            selected = item
            selectedRank = rank
        }
    }
    return selected
}

function evaluateMonitorAlertFlags(alertFlags, previousState, threshold, observedAt) {
    const immediateAlertFlags = []
    for (let i = 0; i < (alertFlags || []).length; i++) {
        const item = alertFlags[i]
        if (!isLoadMonitorAlertFlag(item)) {
            immediateAlertFlags.push(item)
        }
    }

    const loadAlertFlag = selectPrimaryLoadMonitorAlertFlag(alertFlags)
    if (!loadAlertFlag) {
        return {
            effective_alert_flags: immediateAlertFlags,
            pending_only: false,
            state_patch: {
                pending_host_load_hits: 0,
                pending_host_load_since: "",
                pending_host_load_active_code: "",
            },
            threshold_met: false,
        }
    }

    const prev = previousState || {}
    const prevHits = Math.max(0, Math.floor(toNumber(prev.pending_host_load_hits, 0)))
    const prevHadLoad = !!LOAD_MONITOR_ALERT_CODES[String(prev.pending_host_load_active_code || "").trim()]
    const nextHits = prevHadLoad ? (prevHits + 1) : 1
    const normalizedThreshold = normalizePositiveInt(threshold, DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT)
    const thresholdMet = nextHits >= normalizedThreshold

    return {
        effective_alert_flags: thresholdMet
            ? immediateAlertFlags.concat([loadAlertFlag])
            : immediateAlertFlags,
        pending_only: immediateAlertFlags.length === 0 && !thresholdMet,
        state_patch: {
            pending_host_load_hits: nextHits,
            pending_host_load_since: String(prev.pending_host_load_since || "").trim() || String(observedAt || ""),
            pending_host_load_active_code: String(loadAlertFlag.code || "").trim(),
        },
        threshold_met: thresholdMet,
    }
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

function formatMonitorSubscriptionUsage(apiUtilization) {
    const totalCount = toNumber(apiUtilization && apiUtilization.active_subscription_count, 0)
    const totalLimit = toNumber(apiUtilization && (apiUtilization.total_subscription_limit != null ? apiUtilization.total_subscription_limit : apiUtilization.subscription_limit), 0)
    const tradeCount = toNumber(apiUtilization && apiUtilization.active_trade_symbol_count, 0)
    const tradeLimit = toNumber(apiUtilization && (apiUtilization.trade_subscription_limit != null ? apiUtilization.trade_subscription_limit : apiUtilization.subscription_limit), 0)
    const tradePct = toNumber(apiUtilization && (apiUtilization.trade_utilization_pct != null ? apiUtilization.trade_utilization_pct : apiUtilization.utilization_pct), 0)
    const monitorCount = toNumber(
        apiUtilization && (apiUtilization.active_monitor_symbol_count != null
            ? apiUtilization.active_monitor_symbol_count
            : Math.max(0, totalCount - tradeCount)),
        0
    )
    const parts = [
        `trade ${tradeCount}/${tradeLimit || "--"} (${tradePct.toFixed(2)}%)`,
    ]
    if (totalCount > 0 || totalLimit > 0) {
        parts.push(`total ${totalCount}/${totalLimit || "--"}`)
    }
    if (monitorCount > 0) {
        parts.push(`monitor ${monitorCount}`)
    }
    return parts.join(" · ")
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

        const threshold = getHostLoadConsecutiveThreshold(environment)
        const baseAlertFlags = selectMonitorAlertFlags(monitorPayload.flags || [])
        const syntheticAlertFlags = buildSyntheticMonitorAlertFlags(monitorPayload)
        const alertFlags = baseAlertFlags.concat(
            syntheticAlertFlags.filter((item) => {
                const syntheticCode = String(item && item.code || "").trim()
                return !baseAlertFlags.some((baseItem) => String(baseItem && baseItem.code || "").trim() === syntheticCode)
            })
        )
        const state = getStateData(MONITOR_ALERT_STATE_KEY, environment, times.date).data || {}
        const evaluation = evaluateMonitorAlertFlags(alertFlags, state, threshold, times.us)
        const effectiveAlertFlags = evaluation.effective_alert_flags || []
        if (!alertFlags.length) {
            saveStateData(MONITOR_ALERT_STATE_KEY, environment, times.date, {
                last_monitor_check_at: times.us,
                last_monitor_issue_at: "",
                last_monitor_alert_hash: "",
                last_monitor_alert_ms: 0,
                ...evaluation.state_patch,
            })
            continue
        }

        if (!effectiveAlertFlags.length) {
            saveStateData(MONITOR_ALERT_STATE_KEY, environment, times.date, {
                last_monitor_check_at: times.us,
                ...evaluation.state_patch,
            })
            if (evaluation.pending_only) {
                console.log(
                    `${prefix} ${environment}: host load pending ${evaluation.state_patch.pending_host_load_hits}/${threshold}, waiting before alert`
                )
            }
            continue
        }

        const fingerprint = buildMonitorAlertFingerprint(monitorPayload, effectiveAlertFlags)
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
                ...evaluation.state_patch,
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
        const level = effectiveAlertFlags.some((item) => String(item && item.severity || "").trim().toLowerCase() === "error")
            ? "error"
            : "warning"
        const title = level === "error"
            ? `IBKR Monitor 严重告警（${effectiveAlertFlags.length}项）`
            : `IBKR Monitor 告警（${effectiveAlertFlags.length}项）`
        const detail = {
            "检查时间": times.us,
            "监控状态": String(monitorPayload.status || "unknown").toUpperCase(),
            "触发项": effectiveAlertFlags.slice(0, 4).map((item) => `${item.title || item.code}: ${item.detail || ""}`).join(" | "),
            "订阅占用": formatMonitorSubscriptionUsage(apiUtilization),
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
            ...evaluation.state_patch,
        })
        console.log(`${prefix} ${environment}: level=${level}, notified=${notified}, flags=${effectiveAlertFlags.map((item) => item.code).join(",")}`)
    }
}

module.exports = {
    DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT,
    HOST_LOAD_CONSECUTIVE_CONFIG_KEY,
    evaluateMonitorAlertFlags,
    normalizePositiveInt,
    runSystemMonitorAlertGuard,
}
