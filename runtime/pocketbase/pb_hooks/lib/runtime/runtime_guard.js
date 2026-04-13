var envUtils = require(`${__hooks}/lib/environment.js`)

function parseJson(rawValue) {
    var raw = typeof rawValue === "string" ? rawValue : String(rawValue || "")
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { ok: false, raw: raw }
    }
}

function inspectRequestedRuntimeEnvironment(environment) {
    var requestedEnvironment = String(environment || "live").trim().toLowerCase() || "live"
    var computeBase = envUtils.getIbkrComputeInternalUrl(requestedEnvironment, "http://127.0.0.1:5100")
    var runtimeUpstream = `${computeBase}/ibkr/status`
    var resp = $http.send({
        url: runtimeUpstream,
        method: "GET",
        timeout: 8,
    })
    var runtimePayload = parseJson(resp.raw)
    var actualRuntimeEnvironment = String(runtimePayload.environment || requestedEnvironment).trim().toLowerCase() || requestedEnvironment
    return {
        requested_environment: requestedEnvironment,
        actual_runtime_environment: actualRuntimeEnvironment,
        runtime_environment_mismatch: actualRuntimeEnvironment !== requestedEnvironment,
        runtime_payload: runtimePayload,
        proxy_upstream_runtime: runtimeUpstream,
    }
}

function buildRuntimeEnvironmentMismatchPayload(environmentInfo, route) {
    var requestedEnvironment = String(environmentInfo && environmentInfo.requested_environment || "live").trim().toLowerCase() || "live"
    var actualRuntimeEnvironment = String(environmentInfo && environmentInfo.actual_runtime_environment || requestedEnvironment).trim().toLowerCase() || requestedEnvironment
    return {
        ok: false,
        error: `当前 ${requestedEnvironment.toUpperCase()} 页面没有独立 runtime；实际运行中的是 ${actualRuntimeEnvironment.toUpperCase()}，请切到对应环境页面执行此动作。`,
        requested_environment: requestedEnvironment,
        actual_runtime_environment: actualRuntimeEnvironment,
        runtime_environment_mismatch: true,
        proxy_source: "pocketbase_ibkr_hook",
        proxy_hook: "ibkr_actions.pb.js",
        proxy_route: route,
        proxy_upstream_runtime: environmentInfo && environmentInfo.proxy_upstream_runtime ? environmentInfo.proxy_upstream_runtime : "",
    }
}

module.exports = {
    inspectRequestedRuntimeEnvironment: inspectRequestedRuntimeEnvironment,
    buildRuntimeEnvironmentMismatchPayload: buildRuntimeEnvironmentMismatchPayload,
}
