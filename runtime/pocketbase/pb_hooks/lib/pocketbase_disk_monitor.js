const POCKETBASE_DISK_CACHE_TTL_MS = 60 * 1000
const POCKETBASE_DISK_WARN_USED_PCT = 85
const POCKETBASE_DISK_CRITICAL_USED_PCT = 92

let pocketbaseDiskCache = {
    expires_ms: 0,
    snapshot: null,
}

function toNumber(value, fallback) {
    const num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function safeString(value) {
    return String(value || "").trim()
}

function cloneJson(value) {
    try {
        return JSON.parse(JSON.stringify(value))
    } catch (_) {
        return value
    }
}

function normalizePath(value) {
    return safeString(value).replace(/\\/g, "/")
}

function commandOutputText(raw) {
    if (typeof raw === "string") return raw
    if (Array.isArray(raw)) {
        let text = ""
        for (let i = 0; i < raw.length; i++) {
            text += String.fromCharCode(toNumber(raw[i], 0))
        }
        return text
    }
    if (typeof ArrayBuffer !== "undefined" && raw && ArrayBuffer.isView && ArrayBuffer.isView(raw)) {
        let text = ""
        for (let i = 0; i < raw.length; i++) {
            text += String.fromCharCode(toNumber(raw[i], 0))
        }
        return text
    }
    return String(raw || "")
}

function splitPathSegments(value) {
    return normalizePath(value).split("/").filter(Boolean)
}

function basename(value) {
    const segments = splitPathSegments(value)
    return segments.length ? segments[segments.length - 1] : ""
}

function getPocketBaseDataPath() {
    try {
        if (typeof $app !== "undefined" && $app && typeof $app.dataDir === "function") {
            const fromApp = normalizePath($app.dataDir())
            if (fromApp) return fromApp
        }
    } catch (_) {}

    const hooksPath = normalizePath(typeof __hooks !== "undefined" ? __hooks : "")
    if (!hooksPath) return ""

    try {
        return normalizePath($filepath.join($filepath.dir(hooksPath), "pb_data"))
    } catch (_) {}

    const segments = splitPathSegments(hooksPath)
    segments.pop()
    return `/${segments.join("/")}/pb_data`
}

function scanPocketBaseDataTree(rootPath) {
    const normalizedRoot = normalizePath(rootPath)
    const result = {
        exists: false,
        total_size_bytes: 0,
        file_count: 0,
        dir_count: 0,
        top_entries: [],
        scan_error: "",
    }
    if (!normalizedRoot) {
        result.scan_error = "missing_pb_data_path"
        return result
    }

    let rootInfo = null
    try {
        rootInfo = $os.stat(normalizedRoot)
    } catch (err) {
        result.scan_error = err && err.message ? err.message : String(err || "")
        return result
    }

    result.exists = true
    if (!rootInfo || !rootInfo.isDir()) {
        const fileSize = rootInfo ? toNumber(rootInfo.size(), 0) : 0
        result.total_size_bytes = fileSize
        result.file_count = fileSize > 0 ? 1 : 0
        result.top_entries = [{
            name: basename(normalizedRoot) || "pb_data",
            path: normalizedRoot,
            is_dir: false,
            size_bytes: fileSize,
        }]
        return result
    }

    const topEntrySizes = {}
    const topEntryKinds = {}
    const topEntryPaths = {}
    result.dir_count = 1

    try {
        $filepath.walk(normalizedRoot, function (currentPath, info, err) {
            if (err || !info) {
                return
            }

            const normalizedCurrent = normalizePath(currentPath)
            if (!normalizedCurrent || normalizedCurrent === normalizedRoot) {
                return
            }

            const relative = normalizedCurrent.slice(normalizedRoot.length + 1)
            const topName = relative.split("/")[0]
            if (!topName) {
                return
            }

            if (!(topName in topEntrySizes)) {
                topEntrySizes[topName] = 0
                topEntryKinds[topName] = info.isDir() ? "dir" : "file"
                topEntryPaths[topName] = normalizePath($filepath.join(normalizedRoot, topName))
            }

            if (info.isDir()) {
                result.dir_count += 1
                return
            }

            const sizeBytes = toNumber(info.size(), 0)
            result.file_count += 1
            result.total_size_bytes += sizeBytes
            topEntrySizes[topName] += sizeBytes
        })
    } catch (err) {
        result.scan_error = err && err.message ? err.message : String(err || "")
    }

    result.top_entries = Object.keys(topEntrySizes)
        .map((name) => ({
            name: name,
            path: topEntryPaths[name] || normalizePath($filepath.join(normalizedRoot, name)),
            is_dir: topEntryKinds[name] === "dir",
            size_bytes: toNumber(topEntrySizes[name], 0),
        }))
        .sort((left, right) => {
            const sizeDiff = toNumber(right && right.size_bytes, 0) - toNumber(left && left.size_bytes, 0)
            if (sizeDiff) return sizeDiff
            return safeString(left && left.name).localeCompare(safeString(right && right.name))
        })

    return result
}

function collectFilesystemUsage(targetPath) {
    const result = {
        path: normalizePath(targetPath),
        mount_path: "",
        device: "",
        total_bytes: 0,
        used_bytes: 0,
        available_bytes: 0,
        used_pct: null,
        source: "df -kP",
        available: false,
        error: "",
    }
    if (!result.path) {
        result.error = "missing_target_path"
        return result
    }

    try {
        const output = commandOutputText($os.exec("df", "-kP", result.path).output()).trim()
        const lines = output.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
        if (lines.length < 2) {
            result.error = "df_output_incomplete"
            return result
        }

        const columns = lines[1].split(/\s+/)
        if (columns.length < 6) {
            result.error = "df_output_parse_error"
            return result
        }

        const totalKb = toNumber(columns[1], 0)
        const usedKb = toNumber(columns[2], 0)
        const availableKb = toNumber(columns[3], 0)
        const usedPctText = safeString(columns[4]).replace("%", "")

        result.device = safeString(columns[0])
        result.total_bytes = totalKb * 1024
        result.used_bytes = usedKb * 1024
        result.available_bytes = availableKb * 1024
        result.used_pct = Number.isFinite(Number(usedPctText)) ? Number(usedPctText) : null
        result.mount_path = columns.slice(5).join(" ")
        result.available = true
        return result
    } catch (err) {
        result.error = err && err.message ? err.message : String(err || "")
        return result
    }
}

function buildPocketBaseDiskSnapshot() {
    const collectedAt = new Date().toISOString()
    const dataPath = getPocketBaseDataPath()
    const rootPath = dataPath ? normalizePath($filepath.dir(dataPath)) : ""
    const tree = scanPocketBaseDataTree(dataPath)
    const filesystem = collectFilesystemUsage(tree.exists ? dataPath : (rootPath || dataPath))
    const topEntries = Array.isArray(tree.top_entries) ? tree.top_entries : []
    const topEntrySizeMap = {}

    for (let i = 0; i < topEntries.length; i++) {
        const item = topEntries[i]
        topEntrySizeMap[safeString(item && item.name)] = toNumber(item && item.size_bytes, 0)
    }

    const dbBackupEntries = topEntries.filter((item) => /^data\.db\.backup\./.test(safeString(item && item.name)))
    const dbBackupSizeBytes = dbBackupEntries.reduce(
        (sum, item) => sum + toNumber(item && item.size_bytes, 0),
        0,
    )

    const snapshot = {
        source: "pocketbase_hook_disk_monitor",
        collected_at: collectedAt,
        cache_ttl_s: Math.round(POCKETBASE_DISK_CACHE_TTL_MS / 1000),
        status: "ok",
        root_path: rootPath,
        data_path: dataPath,
        data_exists: Boolean(tree.exists),
        data_size_bytes: toNumber(tree.total_size_bytes, 0),
        file_count: toNumber(tree.file_count, 0),
        dir_count: toNumber(tree.dir_count, 0),
        scan_error: safeString(tree.scan_error),
        db_path: dataPath ? normalizePath($filepath.join(dataPath, "data.db")) : "",
        db_size_bytes: toNumber(topEntrySizeMap["data.db"], 0),
        storage_path: dataPath ? normalizePath($filepath.join(dataPath, "storage")) : "",
        storage_size_bytes: toNumber(topEntrySizeMap.storage, 0),
        backups_path: dataPath ? normalizePath($filepath.join(dataPath, "backups")) : "",
        backups_size_bytes: toNumber(topEntrySizeMap.backups, 0),
        db_backup_glob: dataPath ? normalizePath($filepath.join(dataPath, "data.db.backup.*")) : "",
        db_backup_size_bytes: dbBackupSizeBytes,
        db_backup_file_count: dbBackupEntries.length,
        aux_path: dataPath ? normalizePath($filepath.join(dataPath, "aux")) : "",
        aux_size_bytes: toNumber(topEntrySizeMap.aux, 0),
        filesystem: filesystem,
        top_entries: topEntries.slice(0, 6),
    }

    const usedPct = Number(filesystem && filesystem.used_pct)
    if (!snapshot.data_exists && !filesystem.available) {
        snapshot.status = "unavailable"
    } else if (Number.isFinite(usedPct) && usedPct >= POCKETBASE_DISK_CRITICAL_USED_PCT) {
        snapshot.status = "error"
    } else if (Number.isFinite(usedPct) && usedPct >= POCKETBASE_DISK_WARN_USED_PCT) {
        snapshot.status = "warning"
    } else if (snapshot.scan_error) {
        snapshot.status = "partial"
    } else {
        snapshot.status = "ok"
    }

    return snapshot
}

function getPocketBaseDiskSnapshot(forceRefresh) {
    const nowMs = Date.now()
    if (!forceRefresh && pocketbaseDiskCache.snapshot && pocketbaseDiskCache.expires_ms > nowMs) {
        return cloneJson(pocketbaseDiskCache.snapshot)
    }

    const snapshot = buildPocketBaseDiskSnapshot()
    pocketbaseDiskCache = {
        expires_ms: nowMs + POCKETBASE_DISK_CACHE_TTL_MS,
        snapshot: cloneJson(snapshot),
    }
    return cloneJson(snapshot)
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

function buildPocketBaseDiskFlags(snapshot) {
    const disk = snapshot && typeof snapshot === "object" ? snapshot : {}
    const filesystem = disk.filesystem && typeof disk.filesystem === "object" ? disk.filesystem : {}
    const usedPct = Number(filesystem.used_pct)
    if (!Number.isFinite(usedPct)) {
        return []
    }

    const mountPath = safeString(filesystem.mount_path || filesystem.path || disk.data_path || "--")
    const detail = `${mountPath} 已使用 ${usedPct.toFixed(1)}%，剩余 ${formatBytes(filesystem.available_bytes)}，pb_data ${formatBytes(disk.data_size_bytes)}，db backups ${toNumber(disk.db_backup_file_count, 0)} 个 / ${formatBytes(disk.db_backup_size_bytes)}`
    if (usedPct >= POCKETBASE_DISK_CRITICAL_USED_PCT) {
        return [{
            severity: "error",
            code: "pb_disk_critical",
            title: "PocketBase disk critical",
            detail: detail,
        }]
    }
    if (usedPct >= POCKETBASE_DISK_WARN_USED_PCT) {
        return [{
            severity: "warning",
            code: "pb_disk_high",
            title: "PocketBase disk high",
            detail: detail,
        }]
    }
    return []
}

function mergeMonitorFlags(baseFlags, extraFlags) {
    const merged = []
    const seen = {}
    const groups = [baseFlags, extraFlags]

    for (let groupIndex = 0; groupIndex < groups.length; groupIndex++) {
        const list = Array.isArray(groups[groupIndex]) ? groups[groupIndex] : []
        for (let i = 0; i < list.length; i++) {
            const item = list[i]
            const code = safeString(item && item.code)
            const key = code || JSON.stringify(item || {})
            if (seen[key]) continue
            seen[key] = true
            merged.push(item)
        }
    }

    return merged
}

function mergeMonitorStatus(currentStatus, flags) {
    const normalized = safeString(currentStatus).toLowerCase() || "ok"
    if (normalized === "offline") {
        return "offline"
    }
    if (normalized === "error") {
        return "error"
    }

    const list = Array.isArray(flags) ? flags : []
    const hasError = list.some((item) => safeString(item && item.severity).toLowerCase() === "error")
    if (hasError) return "error"

    const hasWarning = list.some((item) => safeString(item && item.severity).toLowerCase() === "warning")
    if (hasWarning) return normalized === "ok" ? "warning" : normalized

    return normalized
}

function enrichMonitorPayloadWithPocketBaseDisk(payload, forceRefresh) {
    const response = payload && typeof payload === "object" && !Array.isArray(payload)
        ? payload
        : {}
    const diskSnapshot = getPocketBaseDiskSnapshot(forceRefresh)
    const extraFlags = buildPocketBaseDiskFlags(diskSnapshot)

    response.pocketbase = response.pocketbase && typeof response.pocketbase === "object" && !Array.isArray(response.pocketbase)
        ? response.pocketbase
        : {}
    response.pocketbase.disk = diskSnapshot
    response.flags = mergeMonitorFlags(response.flags, extraFlags)
    response.status = mergeMonitorStatus(response.status, response.flags)

    if (response.status === "error" || response.status === "offline") {
        response.ok = false
    } else if (response.ok == null) {
        response.ok = true
    }

    return response
}

module.exports = {
    buildPocketBaseDiskFlags,
    enrichMonitorPayloadWithPocketBaseDisk,
    getPocketBaseDiskSnapshot,
}
