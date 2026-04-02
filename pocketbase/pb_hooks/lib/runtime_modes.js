function getRuntimeEnvironments() {
    const { LIVE_ENVIRONMENT, PAPER_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return [
        LIVE_ENVIRONMENT,
        PAPER_ENVIRONMENT,
        BACKTEST_ENVIRONMENT,
    ]
}

function isEnabledConfigValue(value) {
    const text = String(value || "").trim().toLowerCase()
    return !(text === "false" || text === "0" || text === "off" || text === "no")
}

function normalizeConfigKeys(configKeys) {
    return Array.isArray(configKeys) ? configKeys.filter(Boolean) : []
}

function isBacktestOptedIn(configKeys) {
    const keys = normalizeConfigKeys(configKeys)
    if (!keys.length) {
        return true
    }

    const { hasConfigOverride, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return keys.some((key) => hasConfigOverride(key, BACKTEST_ENVIRONMENT))
}

function getActiveRuntimeEnvironments(backtestOptInKeys) {
    const { BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    return getRuntimeEnvironments().filter((environment) => {
        if (environment === BACKTEST_ENVIRONMENT) {
            return isBacktestOptedIn(backtestOptInKeys)
        }
        return true
    })
}

function isConfigEnabled(key, defaultValue, environment, backtestOptInKeys) {
    const { normalizeRuntimeEnvironment, getConfigValue, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment || "", LIVE_ENVIRONMENT)
    if (runtimeEnvironment === BACKTEST_ENVIRONMENT && !isBacktestOptedIn(backtestOptInKeys)) {
        return false
    }
    return isEnabledConfigValue(getConfigValue(key, defaultValue, runtimeEnvironment))
}

function getEnabledRuntimeEnvironments(key, defaultValue, backtestOptInKeys) {
    return getActiveRuntimeEnvironments(backtestOptInKeys).filter((environment) => {
        return isConfigEnabled(key, defaultValue, environment, backtestOptInKeys)
    })
}

function getComputeEnabledForEnvironment(environment, backtestOptInKeys) {
    const { normalizeRuntimeEnvironment, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment || "", LIVE_ENVIRONMENT)
    const defaultValue = runtimeEnvironment === BACKTEST_ENVIRONMENT ? "false" : "true"
    return isConfigEnabled("qc_compute_enabled", defaultValue, runtimeEnvironment, backtestOptInKeys)
}

function getTradingEnabledForEnvironment(environment, backtestOptInKeys) {
    const { normalizeRuntimeEnvironment, LIVE_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment || "", LIVE_ENVIRONMENT)
    return isConfigEnabled("trading_enabled", "true", runtimeEnvironment, backtestOptInKeys)
}

function getEffectiveWriteMode(environment, backtestOptInKeys) {
    const { normalizeRuntimeEnvironment, getConfigValue, LIVE_ENVIRONMENT, BACKTEST_ENVIRONMENT } = require(`${__hooks}/lib/environment.js`)
    const runtimeEnvironment = normalizeRuntimeEnvironment(environment || "", LIVE_ENVIRONMENT)
    if (runtimeEnvironment === BACKTEST_ENVIRONMENT && !isBacktestOptedIn(backtestOptInKeys)) {
        return "disabled"
    }
    return getConfigValue("qc_write_mode", "shadow", runtimeEnvironment)
}

module.exports = {
    getRuntimeEnvironments,
    isEnabledConfigValue,
    isBacktestOptedIn,
    getActiveRuntimeEnvironments,
    isConfigEnabled,
    getEnabledRuntimeEnvironments,
    getComputeEnabledForEnvironment,
    getTradingEnabledForEnvironment,
    getEffectiveWriteMode,
}
