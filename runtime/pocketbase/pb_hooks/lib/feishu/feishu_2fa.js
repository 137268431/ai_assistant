var feishuApp = require(`${__hooks}/lib/feishu_app.js`)
var feishuSystem = require(`${__hooks}/lib/feishu_system.js`)
var feishuStartup = require(`${__hooks}/lib/feishu/feishu_startup.js`)
var envUtils = require(`${__hooks}/lib/environment.js`)
var publicUrls = require(`${__hooks}/lib/feishu/public_urls.js`)
var timeUtils = require(`${__hooks}/lib/time_utils.js`)
var systemEvents = require(`${__hooks}/lib/system_events.js`)
var deadlineUtils = require(`${__hooks}/lib/runtime/ibkr_2fa_deadlines.js`)
var usEasternTime = require(`${__hooks}/lib/runtime/us_eastern_time.js`)

var IBKR_2FA_STATE_KEY = "ibkr_2fa"
var IBKR_2FA_STATE_DATE = "global"
var DEFAULT_TWO_FA_CHAT_ID = "oc_c48c10447685e80cfea0c003864aa51f"
var CARD_UPDATE_COOLDOWN_MS = 15000
var REQUEST_RENOTIFY_COOLDOWN_MS = 900000
var DELIVERY_LOCK_TTL_MS = 20000
var CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000
var ACTIVE_STATUSES = ["requested", "triggered", "waiting_confirm", "waiting_response"]
var TERMINAL_STATUSES = ["success", "timeout", "failed"]
var MANUAL_AUTH_REASON_LABELS = {
    auto_restore: "静默恢复",
    weekly_reauth: "每周重登提醒",
    manual_start: "启动验证",
    startup: "启动验证",
    manual_reauth: "手动重登验证",
    manual_gateway_restart: "网关重启验证",
    panic_reset_2fa: "重开验证",
}
var RECOVERY_FIELDS = [
    "cycle_id",
    "recovery_phase",
    "recovery_class",
    "recovery_reason",
    "interruption_kind",
    "manual_takeover_active",
    "manual_takeover_started_at",
    "manual_takeover_until",
    "probe_started_at",
    "probe_last_checked_at",
    "probe_attempts",
    "probe_result",
    "auto_restart_scheduled",
    "last_runtime_authenticated_at",
    "last_gateway_status_code",
    "last_recovery_source",
    "lock_owner",
    "lock_expires_at",
]
var rootScope = typeof globalThis !== "undefined" ? globalThis : this
if (!rootScope.__IBKR_2FA_DELIVERY_LOCKS) {
    rootScope.__IBKR_2FA_DELIVERY_LOCKS = {}
}
var DELIVERY_LOCKS = rootScope.__IBKR_2FA_DELIVERY_LOCKS

var STATUS_CONFIG = {
    requested: {
        emoji: "🔐",
        title: "IBKR 2FA 待触发",
        template: "yellow",
        summary: "点击按钮后才会开始登录与 2FA 推送。",
        button: "开始 2FA 验证"
    },
    triggered: {
        emoji: "🚀",
        title: "IBKR 2FA 已触发",
        template: "blue",
        summary: "登录流程已启动。当前已有一轮 2FA 在进行中，请打开 Runtime 跟当前轮次，不要重复触发。",
        button: "查看当前轮次"
    },
    waiting_confirm: {
        emoji: "📲",
        title: "IBKR 2FA 待确认",
        template: "yellow",
        summary: "请在 IBKR Mobile 上确认推送。当前已有一轮 2FA 在进行中，不要重复触发；如果后续切到 Challenge/Response，再去 Runtime 页面提交 Response Code。",
        button: "继续当前轮次"
    },
    waiting_response: {
        emoji: "🔢",
        title: "IBKR 2FA 待输入响应码",
        template: "orange",
        summary: "当前已进入 Challenge/Response。请在 App 输入 Challenge 生成 Response Code，并去 Runtime 页面提交；不要重复触发新一轮。",
        button: "打开 Runtime 提交响应码"
    },
    resume_pending: {
        emoji: "♻️",
        title: "IBKR Session 静默恢复中",
        template: "blue",
        summary: "当前正在尝试复用已有 Gateway Session，不会自动触发新的 2FA。",
        button: "查看恢复状态"
    },
    recovering: {
        emoji: "♻️",
        title: "IBKR 会话静默恢复中",
        template: "blue",
        summary: "系统正在尝试自动恢复当前会话或重启运行态，暂不需要立即重新 2FA。",
        button: "查看恢复状态"
    },
    success: {
        emoji: "✅",
        title: "IBKR 2FA 验证成功",
        template: "green",
        summary: "Gateway 已恢复认证。",
        button: "再次验证"
    },
    timeout: {
        emoji: "⏰",
        title: "IBKR 2FA 等待超时",
        template: "red",
        summary: "未在等待窗口内完成确认，可点击按钮重试。",
        button: "重新触发"
    },
    failed: {
        emoji: "🚨",
        title: "IBKR 2FA 触发失败",
        template: "red",
        summary: "登录流程未成功完成，可点击按钮重试。",
        button: "重新触发"
    }
}

function normalizeTwoFactorStatus(value) {
    var text = String(value || "").trim().toLowerCase()
    if (!text) return "requested"
    if (text === "pending" || text === "waiting_mobile_approval" || text === "mobile_approval" || text === "awaiting_mobile_approval") {
        return "waiting_confirm"
    }
    if (text === "complete" || text === "completed" || text === "authenticated") {
        return "success"
    }
    if (text === "error") return "failed"
    return text
}

function safeJsonParse(value) {
    if (!value) return {}
    if (typeof value === "object") return value
    try {
        var parsed = JSON.parse(String(value || ""))
        return parsed && typeof parsed === "object" ? parsed : {}
    } catch (_) {
        return {}
    }
}

function stringifyValue(value) {
    if (value == null) return "-"
    if (Array.isArray(value)) return value.join(", ") || "-"
    if (typeof value === "object") return JSON.stringify(value)
    var text = String(value)
    return text || "-"
}

function sanitizeResponseCode(value) {
    return String(value || "").replace(/[^0-9A-Za-z]/g, "").toUpperCase()
}

function resolve2faChatId(environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    if (feishuSystem && typeof feishuSystem.get2faChatId === "function") {
        var chatId = String(feishuSystem.get2faChatId(runtimeEnvironment) || "").trim()
        if (chatId) return chatId
    }
    if (envUtils && typeof envUtils.getConfigValue === "function") {
        var configured = String(envUtils.getConfigValue("system_2fa_chat_id", DEFAULT_TWO_FA_CHAT_ID, runtimeEnvironment) || "").trim()
        if (configured) return configured
    }
    return DEFAULT_TWO_FA_CHAT_ID
}

function normalizeManualAuthReason(reason) {
    var text = String(reason || "").trim().toLowerCase()
    if (!text) return "manual_reauth"
    if (text === "startup") return "manual_start"
    return text
}

function getManualAuthReasonLabel(reason) {
    var normalized = normalizeManualAuthReason(reason)
    return MANUAL_AUTH_REASON_LABELS[normalized] || "手动验证"
}

function syncStartupAuthProgress(environment, status, stateData) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    try {
        var startupState = feishuStartup.getStatePayload(runtimeEnvironment)
        var current = startupState && startupState.data ? startupState.data : {}
        if (!current || current.active !== true) {
            return { ok: true, skipped: true, reason: "no_active_startup_cycle" }
        }

        var normalizedStatus = normalizeTwoFactorStatus(status)
        var data = stateData && typeof stateData === "object" ? stateData : {}
        var currentBlocker = ""
        var operatorAction = ""
        var summary = ""
        var steps = {
            service_boot: {
                status: "done",
                detail: "Gateway 已启动并进入当前验证流程。",
            },
        }
        var reasonLabel = getManualAuthReasonLabel(data.reason || data.recovery_reason)

        if (normalizedStatus === "requested") {
            currentBlocker = "等待手动触发 2FA"
            operatorAction = "点击当前启动卡片下方“开始 2FA 验证”"
            summary = reasonLabel + "已准备好，等待你在飞书手动点开始。"
            steps.card_ready = {
                status: "done",
                detail: "已复用或刷新当前 2FA 卡片。",
            }
            steps.manual_trigger = {
                status: "waiting",
                detail: "点击当前启动卡片下方“开始 2FA 验证”。",
            }
        } else if (normalizedStatus === "triggered") {
            currentBlocker = "等待手机确认 2FA Push"
            operatorAction = "查看手机通知；如果切到 Challenge/Response，则去 Runtime 页面提交 Response Code"
            summary = "已手动触发当前轮次，等待手机确认或响应码流程。"
            steps.card_ready = {
                status: "done",
                detail: "当前 2FA 卡片已准备完成。",
            }
            steps.manual_trigger = {
                status: "done",
                detail: "飞书按钮已点下，不会自动补发新的 Push。",
            }
            steps.manual_confirm = {
                status: "waiting",
                detail: "等待手机确认或进入响应码模式。",
            }
        } else if (normalizedStatus === "waiting_confirm") {
            currentBlocker = "等待手机确认 2FA Push"
            operatorAction = "查看手机通知完成确认"
            summary = "当前 2FA Push 已发出，等待手机确认。"
            steps.card_ready = {
                status: "done",
                detail: "当前 2FA 卡片已准备完成。",
            }
            steps.manual_trigger = {
                status: "done",
                detail: "飞书触发步骤已完成。",
            }
            steps.manual_confirm = {
                status: "waiting",
                detail: "现在只需要点手机通知确认，不要重复触发。",
            }
        } else if (normalizedStatus === "waiting_response") {
            currentBlocker = "等待提交 Response Code"
            operatorAction = "去 Runtime 页面提交当前 Challenge 对应的 Response Code"
            summary = "2FA 已进入 Challenge/Response。"
            steps.card_ready = {
                status: "done",
                detail: "当前 2FA 卡片已准备完成。",
            }
            steps.manual_trigger = {
                status: "done",
                detail: "飞书触发步骤已完成。",
            }
            steps.manual_confirm = {
                status: "waiting",
                detail: data.challenge_code
                    ? ("等待提交当前 Challenge 的 Response Code: " + String(data.challenge_code))
                    : "等待提交当前轮次的 Response Code。",
            }
        } else if (normalizedStatus === "success") {
            currentBlocker = "2FA 已完成，等待 Runtime 继续启动"
            operatorAction = "等待系统继续装载订阅、线程和预热"
            summary = "Session / 2FA 已恢复认证。"
            steps.card_ready = {
                status: "done",
                detail: "当前 2FA 卡片阶段已完成。",
            }
            steps.manual_trigger = {
                status: "done",
                detail: "飞书手动触发已完成。",
            }
            steps.manual_confirm = {
                status: "done",
                detail: "当前 2FA 验证已完成。",
            }
            steps.runtime_resume = {
                status: "running",
                detail: "认证已恢复，正在继续恢复 Runtime。",
            }
        } else if (normalizedStatus === "resume_pending") {
            currentBlocker = "等待静默恢复 Gateway Session"
            operatorAction = data.probe_result === "resume_probe_timeout"
                ? "如需立即恢复，请去 Runtime 页面人工接管或手动触发 2FA"
                : "等待系统继续静默探测；当前不会自动触发新的 2FA"
            summary = data.probe_result === "resume_probe_timeout"
                ? "静默恢复尚未自动成功，当前仍不会自动补发新的 2FA。"
                : "当前启动先尝试复用已有 Gateway 会话，不会自动触发新的 2FA。"
            steps.card_ready = {
                status: "done",
                detail: "当前不会自动新开 2FA 卡片。",
            }
            steps.runtime_resume = {
                status: "running",
                detail: data.probe_result === "resume_probe_timeout"
                    ? "静默恢复未自动成功，系统仍会继续被动观察当前 Gateway Session。"
                    : "系统正在静默探测当前 Gateway Session 是否可直接复用。",
            }
        } else {
            currentBlocker = "2FA 未完成，需手动重新触发"
            operatorAction = "回到当前启动卡片，重新点击下方“开始 2FA 验证”"
            summary = "当前 2FA 轮次未成功建立可用 Session。"
            steps.card_ready = {
                status: "done",
                detail: "当前 2FA 卡片仍可复用。",
            }
            steps.manual_trigger = {
                status: "failed",
                detail: String(data.last_error || data.last_result || data.message || "请重新手动触发当前轮次。"),
            }
        }

        return feishuStartup.syncStartupProgress({
            environment: runtimeEnvironment,
            action: "update",
            create_if_missing: false,
            title: "IBKR Runtime 启动中",
            summary: summary,
            current_step: normalizedStatus === "success" || normalizedStatus === "resume_pending"
                ? "runtime_resume"
                : (normalizedStatus === "triggered" || normalizedStatus === "waiting_confirm" || normalizedStatus === "waiting_response"
                    ? "manual_confirm"
                    : "manual_trigger"),
            current_blocker: currentBlocker,
            operator_action: operatorAction,
            steps: steps,
        })
    } catch (err) {
        console.log("[Feishu2FA] 同步启动卡认证步骤失败:", err && err.message ? err.message : err)
        return { ok: false, error: err && err.message ? err.message : String(err) }
    }
}

function getModeLabel(mode) {
    var text = String(mode || "").trim().toLowerCase()
    if (text === "push_notification") return "Push Notification"
    if (text === "challenge_response") return "Challenge/Response"
    if (text === "success") return "Success"
    if (text === "login_form") return "Login Form"
    return text ? text : "-"
}

function toNumber(value, fallback) {
    var num = Number(value)
    return Number.isFinite(num) ? num : (fallback || 0)
}

function parseUsTimeMs(value) {
    return usEasternTime.parseUsEasternTimeMs(value)
}

function isActiveStatus(status) {
    return ACTIVE_STATUSES.indexOf(normalizeTwoFactorStatus(status)) !== -1
}

function isCurrentCycleActiveStatus(status) {
    var text = normalizeTwoFactorStatus(status)
    return ["triggered", "waiting_confirm", "waiting_response"].indexOf(text) !== -1
}

function isTerminalStatus(status) {
    return TERMINAL_STATUSES.indexOf(normalizeTwoFactorStatus(status)) !== -1
}

function isServerBootResumeRecoveryState(stateData) {
    var state = stateData && typeof stateData === "object" ? stateData : {}
    var interruptionKind = String(state.interruption_kind || "").trim().toLowerCase()
    var recoveryPhase = String(state.recovery_phase || "").trim().toLowerCase()
    var recoveryReason = String(state.recovery_reason || "").trim().toLowerCase()
    var lastRecoverySource = String(state.last_recovery_source || "").trim().toLowerCase()
    return (
        interruptionKind === "server_boot_resume"
        || recoveryPhase === "resume_waiting_manual"
        || (recoveryReason === "auto_restore" && lastRecoverySource === "server_boot")
    )
}

function isManualAuthRequiredRecoveryState(stateData) {
    var state = stateData && typeof stateData === "object" ? stateData : {}
    var recoveryClass = String(state.recovery_class || "").trim().toLowerCase()
    var recoveryPhase = String(state.recovery_phase || "").trim().toLowerCase()
    var probeResult = String(state.probe_result || "").trim().toLowerCase()
    return (
        recoveryClass === "manual_auth_required"
        || recoveryPhase === "requested"
        || probeResult === "manual_trigger_required"
        || probeResult === "timeout_after_self_heal"
    )
}

function isSilentRecoveryState(stateData) {
    var state = stateData && typeof stateData === "object" ? stateData : {}
    if (isManualAuthRequiredRecoveryState(state)) {
        return false
    }
    if (isServerBootResumeRecoveryState(state)) {
        return true
    }
    var recoveryPhase = String(state.recovery_phase || "").trim().toLowerCase()
    var recoveryClass = String(state.recovery_class || "").trim().toLowerCase()
    var probeResult = String(state.probe_result || "").trim().toLowerCase()
    return (
        recoveryPhase === "silent_probe"
        && (
            recoveryClass === "scheduled_restart"
            || recoveryClass === "stale_broker"
            || state.auto_restart_scheduled === true
            || ["pending", "self_heal", "self_heal_pending", "stale_broker_restart_scheduled", "stale_broker_restart_failed"].indexOf(probeResult) !== -1
        )
    )
}

function buildSilentRecoveryMessage(stateData) {
    var state = stateData && typeof stateData === "object" ? stateData : {}
    var recoveryClass = String(state.recovery_class || "").trim().toLowerCase()
    var interruptionKind = String(state.interruption_kind || "").trim().toLowerCase()
    var probeResult = String(state.probe_result || "").trim().toLowerCase()
    if (recoveryClass === "stale_broker" || probeResult.indexOf("stale_broker") === 0) {
        return {
            message: state.auto_restart_scheduled === true
                ? "检测到运行态内 broker 连接失配，系统已安排自动重启 Runtime 以恢复主连接；暂不需要立即重新 2FA。"
                : "检测到运行态内 broker 连接失配，系统正在尝试本地恢复主连接；暂不需要立即重新 2FA。",
            last_result: state.auto_restart_scheduled === true
                ? "已识别 stale in-process broker，正在等待自动重启恢复主连接。"
                : "已识别 stale in-process broker，正在继续静默恢复。",
        }
    }
    if (interruptionKind === "gateway_down") {
        return {
            message: "Gateway 刚经历中断或重启，系统正在静默探测并恢复当前会话；暂不需要立即重新 2FA。",
            last_result: "已进入 Gateway 中断后的静默恢复窗口。",
        }
    }
    return {
        message: "检测到会话认证中断，系统正在静默探测与本地重连；暂不需要立即重新 2FA。",
        last_result: "已进入静默恢复窗口，等待会话自动恢复。",
    }
}

function derive2faActionState(stateData, options) {
    var nowMs = toNumber(options && options.now_ms, Date.now())
    var state = { ...(stateData || {}) }
    var status = normalizeTwoFactorStatus(state.status || "")
    var responseStatus = String(state.response_status || "").trim().toLowerCase()
    var challengeCode = String(state.challenge_code || "").trim()
    var feedback = String(state.challenge_feedback || "").trim()
    var recoveryPhase = String(state.recovery_phase || "").trim().toLowerCase()
    var submittedMs = parseUsTimeMs(state.response_submitted_at)
    var rejectedMs = parseUsTimeMs(state.response_rejected_at)
    var submittedAgeMs = submittedMs > 0 ? Math.max(0, nowMs - submittedMs) : 0
    var rejectedAgeMs = rejectedMs > 0 ? Math.max(0, nowMs - rejectedMs) : 0
    var operatorAction = "request_approval"
    var resetRecommended = false
    var resetReason = ""

    if (recoveryPhase === "panic_resetting") {
        operatorAction = "panic_resetting"
    } else if (state.manual_takeover_active) {
        operatorAction = "manual_takeover"
    } else if (status === "waiting_response") {
        if (responseStatus === "received") {
            operatorAction = "wait_browser_submit"
        } else if (responseStatus === "submitted") {
            operatorAction = "wait_auth_restore"
            if (submittedAgeMs >= CHALLENGE_RESET_RECOMMEND_MS) {
                operatorAction = "panic_reset"
                resetRecommended = true
                resetReason = "submitted_no_recovery"
            }
        } else if (responseStatus === "gateway_rejected") {
            operatorAction = "retry_response_same_challenge"
            if (rejectedAgeMs >= CHALLENGE_RESET_RECOMMEND_MS) {
                operatorAction = "panic_reset"
                resetRecommended = true
                resetReason = "gateway_rejected_no_recovery"
            }
        } else if (responseStatus === "submit_failed") {
            operatorAction = "retry_response_same_challenge"
        } else {
            operatorAction = challengeCode ? "submit_response" : "wait_challenge"
        }
    } else if (status === "waiting_confirm") {
        operatorAction = "confirm_push"
    } else if (status === "triggered") {
        operatorAction = "wait_for_mode"
    } else if (status === "resume_pending" || status === "recovering") {
        operatorAction = recoveryPhase === "resume_waiting_manual" ? "check_runtime_status" : "wait_auth_restore"
    } else if (status === "success") {
        operatorAction = "none"
    } else if (status === "timeout" || status === "failed") {
        operatorAction = "request_new_cycle"
    }

    state.status = status
    state.response_status = responseStatus
    state.challenge_feedback = feedback
    state.operator_action = operatorAction
    state.reset_recommended = resetRecommended
    state.reset_reason = resetReason
    state.response_submitted_age_sec = submittedAgeMs > 0 ? Math.round(submittedAgeMs / 1000) : 0
    state.response_rejected_age_sec = rejectedAgeMs > 0 ? Math.round(rejectedAgeMs / 1000) : 0
    var deadlines = deadlineUtils.deriveTwoFactorDeadlines(state, { nowMs: nowMs })
    state.business_deadline_at = deadlines.business_deadline_at || ""
    state.business_deadline_cn = deadlines.business_deadline_cn || ""
    state.business_deadline_label = deadlines.business_deadline_label || ""
    state.business_deadline_overdue = deadlines.business_deadline_overdue === true
    state.confirm_window_seconds = toNumber(deadlines.confirm_window_seconds, deadlineUtils.DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    state.confirm_deadline_at = deadlines.confirm_deadline_at || ""
    state.confirm_deadline_cn = deadlines.confirm_deadline_cn || ""
    state.confirm_deadline_overdue = deadlines.confirm_deadline_overdue === true
    return state
}

function buildWaitingResponseSummary(stateData) {
    var state = derive2faActionState(stateData)
    var feedback = state.challenge_feedback || "Authentication failed"
    if (state.reset_recommended) {
        if (state.response_status === "submitted") {
            return "Response Code 已提交较久但 Gateway 仍未恢复认证。当前旧 2FA / Session 状态很可能已失配，请在 Runtime 页面执行“全量清空并重新验证”。"
        }
        if (state.response_status === "gateway_rejected") {
            return "Gateway 已拒绝当前 Response Code，且旧轮次长时间未恢复。请在 Runtime 页面执行“全量清空并重新验证”。"
        }
    }
    if (state.response_status === "received") {
        return "已收到 Response Code，等待浏览器提交流程。当前已有 active 轮次，请不要重复触发。"
    }
    if (state.response_status === "submitted") {
        return "Response Code 已提交，等待 Gateway 会话恢复认证。当前已有 active 轮次，请不要重复触发。"
    }
    if (state.response_status === "gateway_rejected") {
        return "Gateway 已拒绝当前 Response Code（" + feedback + "）。请核对当前 Challenge 后重新生成并提交。"
    }
    if (state.response_status === "submit_failed") {
        return "浏览器提交 Response Code 失败。请在 Runtime 页面重试，不要重复触发新一轮。"
    }
    return "当前已进入 Challenge/Response。请在 App 输入 Challenge 生成 Response Code，并去 Runtime 页面提交；不要重复触发新一轮。"
}

function buildWaitingResponsePrompt(stateData) {
    var state = derive2faActionState(stateData)
    var feedback = state.challenge_feedback || "Authentication failed"
    if (state.reset_recommended) {
        return "**操作提示**: 当前旧 2FA / Session 状态很可能已失配。不要继续围绕旧 Challenge 反复尝试；请打开 Runtime 页面执行“全量清空并重新验证”。"
    }
    if (state.response_status === "received") {
        return "**操作提示**: Runtime 已收到你的 Response Code，正在等待 compute 浏览器提交流程。先不要重复提交，也不要再触发新一轮。"
    }
    if (state.response_status === "submitted") {
        return "**操作提示**: 浏览器已提交 Response Code，正在等待 Gateway 恢复认证。此时不要再提交旧 Response，也不要重复触发新一轮。"
    }
    if (state.response_status === "gateway_rejected") {
        return "**操作提示**: Gateway 已返回失败反馈（" + feedback + "）。请核对当前 Challenge，用 App 重新生成新的 Response Code 后去 Runtime 页面重提。"
    }
    if (state.response_status === "submit_failed") {
        return "**操作提示**: 这一步不是点手机推送；但浏览器提交动作失败了。请打开 Runtime 页面重新提交当前 Challenge 对应的 Response Code。"
    }
    return "**操作提示**: 这一步不是点手机推送。请在 IBKR App 的 Two-Factor Authentication 输入当前 Challenge，拿到 Response Code 后打开 Runtime 页面提交。当前已有 active 轮次，请不要重复触发。"
}

function buildRequestedSummary(stateData, fallbackSummary) {
    var state = derive2faActionState(stateData)
    if (String(state.reason || "").trim().toLowerCase() !== "weekly_reauth") {
        return state.message || fallbackSummary
    }
    var confirmSeconds = toNumber(state.confirm_window_seconds, deadlineUtils.DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    if (state.business_deadline_overdue) {
        return "本周重登提醒仍待手动开始，当前已晚于美股周一盘前建议完成时间。你仍可从当前卡片开始验证；点击开始后需在 " + confirmSeconds + " 秒内完成当前 2FA。"
    }
    return "本周重登提醒已发出。你有空时可直接在当前卡片点击“开始 2FA 验证”；最晚请于美股周一盘前前完成。点击开始后需在 " + confirmSeconds + " 秒内完成当前 2FA。"
}

function buildCardSummary(stateData, fallbackSummary) {
    var state = derive2faActionState(stateData)
    if (state.status === "waiting_response") {
        return buildWaitingResponseSummary(state)
    }
    if (state.status === "requested") {
        return buildRequestedSummary(state, fallbackSummary)
    }
    return state.message || fallbackSummary
}

function buildWeeklyReminderDeadlineNote(stateData) {
    var state = derive2faActionState(stateData)
    if (String(state.reason || "").trim().toLowerCase() !== "weekly_reauth" || !state.business_deadline_cn) {
        return ""
    }
    var confirmSeconds = toNumber(state.confirm_window_seconds, deadlineUtils.DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    if (state.business_deadline_overdue) {
        return "**周验证提醒**: 已晚于美股周一盘前建议完成时间（北京时间 " + state.business_deadline_cn + " / 美东 " + (state.business_deadline_at || "-") + "）。你仍可从当前卡片开始验证，但请尽快完成恢复。"
    }
    return "**周验证提醒**: 你有空时可从当前卡片开始验证；最晚请于美股周一盘前前完成（北京时间 " + state.business_deadline_cn + " / 美东 " + (state.business_deadline_at || "-") + "）。点击开始后，本轮 2FA 需在 " + confirmSeconds + " 秒内完成。"
}

function buildConfirmDeadlineNote(stateData) {
    var state = derive2faActionState(stateData)
    if (!state.confirm_deadline_cn) return ""
    var confirmSeconds = toNumber(state.confirm_window_seconds, deadlineUtils.DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    if (state.confirm_deadline_overdue) {
        return "**本轮时限**: 当前轮次已超过 " + confirmSeconds + " 秒等待窗口（北京时间 " + state.confirm_deadline_cn + " / 美东 " + (state.confirm_deadline_at || "-") + "）。若 Gateway 仍未恢复，请准备重新开始本轮。"
    }
    return "**本轮时限**: 点击开始后，本轮 2FA 需在 " + confirmSeconds + " 秒内完成；当前预计截止为北京时间 " + state.confirm_deadline_cn + " / 美东 " + (state.confirm_deadline_at || "-") + "。"
}

function getActiveCyclePrimaryLabel(stateData) {
    var state = derive2faActionState(stateData)
    if (state.status === "waiting_response") {
        if (state.reset_recommended) return "打开 Runtime 干净重开"
        if (state.response_status === "gateway_rejected" || state.response_status === "submit_failed") return "打开 Runtime 重新提交响应码"
        if (state.response_status === "submitted") return "打开 Runtime 查看提交状态"
        if (state.response_status === "received") return "打开 Runtime 查看提交流程"
        return "打开 Runtime 提交响应码"
    }
    return "打开 Runtime 查看当前轮次"
}

function buildDeliveryFingerprint(stateData) {
    var derived = derive2faActionState(stateData)
    return JSON.stringify({
        status: derived.status || "",
        mode: derived.mode || "",
        challenge_code: derived.challenge_code || "",
        response_status: derived.response_status || "",
        response_rejected_at: derived.response_rejected_at || "",
        challenge_feedback: derived.challenge_feedback || "",
        operator_action: derived.operator_action || "",
        reset_recommended: derived.reset_recommended ? "yes" : "no",
        reset_reason: derived.reset_reason || "",
        recovery_phase: derived.recovery_phase || "",
        manual_takeover_active: derived.manual_takeover_active ? "yes" : "no",
        probe_result: derived.probe_result || "",
        reason: derived.reason || "",
        message: derived.message || "",
        last_result: derived.last_result || "",
        last_error: derived.last_error || "",
        requested_at: derived.requested_at || "",
        triggered_at: derived.triggered_at || "",
        result_at: derived.result_at || "",
        business_deadline_at: derived.business_deadline_at || "",
        business_deadline_cn: derived.business_deadline_cn || "",
        business_deadline_overdue: derived.business_deadline_overdue ? "yes" : "no",
        confirm_deadline_at: derived.confirm_deadline_at || "",
        confirm_deadline_cn: derived.confirm_deadline_cn || "",
        confirm_deadline_overdue: derived.confirm_deadline_overdue ? "yes" : "no",
        confirm_window_seconds: derived.confirm_window_seconds || 0,
        restarted_from_active_cycle: derived.restarted_from_active_cycle ? "yes" : "no",
        previous_cycle: {
            status: derived.previous_cycle && derived.previous_cycle.status || "",
            mode: derived.previous_cycle && derived.previous_cycle.mode || "",
            challenge_code: derived.previous_cycle && derived.previous_cycle.challenge_code || "",
            superseded_at: derived.previous_cycle && derived.previous_cycle.superseded_at || "",
        },
        detail: derived.detail || {},
    })
}

function acquireDeliveryLock(lockKey) {
    var now = Date.now()
    var lockedUntil = toNumber(DELIVERY_LOCKS[lockKey], 0)
    if (lockedUntil > now) {
        return false
    }
    DELIVERY_LOCKS[lockKey] = now + DELIVERY_LOCK_TTL_MS
    return true
}

function releaseDeliveryLock(lockKey) {
    delete DELIVERY_LOCKS[lockKey]
}

function buildDetailMarkdown(detail) {
    if (!detail || typeof detail !== "object") return ""
    var keys = Object.keys(detail)
    if (keys.length === 0) return ""
    return keys.map(function(key) {
        return "**" + key + "**: " + stringifyValue(detail[key])
    }).join("\n")
}

function getStateRecord(environment, dateToken) {
    try {
        return $app.findFirstRecordByFilter(
            "ibkr_state",
            "state_key = {:k} && date = {:d} && environment = {:env}",
            { k: IBKR_2FA_STATE_KEY, d: dateToken, env: environment }
        )
    } catch (_) {
        return null
    }
}

function getStatePayload(environment) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var times = timeUtils.getTimeStrings()
    var record = getStateRecord(runtimeEnvironment, IBKR_2FA_STATE_DATE) || getStateRecord(runtimeEnvironment, times.date)
    var rawData = ""
    if (record) {
        rawData = typeof record.getString === "function" ? (record.getString("data") || "") : record.get("data")
    }
    var data = record ? safeJsonParse(rawData) : {}
    if (!data.status) data.status = "requested"
    if (!data.recovery_phase) data.recovery_phase = "idle"
    return {
        ok: true,
        environment: runtimeEnvironment,
        date: record ? (typeof record.getString === "function" ? (record.getString("date") || IBKR_2FA_STATE_DATE) : (record.get("date") || IBKR_2FA_STATE_DATE)) : IBKR_2FA_STATE_DATE,
        state_key: IBKR_2FA_STATE_KEY,
        record: record,
        data: data,
    }
}

function normalizeStateWithRuntime(stateData, runtimeStatus) {
    var state = { ...(stateData || {}) }
    var runtime = runtimeStatus || {}
    var authRecovery = runtime.auth_recovery && typeof runtime.auth_recovery === "object" ? runtime.auth_recovery : {}
    var runtimeStarted = Boolean(
        runtime.starting
        || (runtime.session && runtime.session.running)
        || (runtime.websocket && runtime.websocket.running)
        || (runtime.order_tracker && runtime.order_tracker.running)
    )
    var runtimeAuthenticated = Boolean(runtime.session && runtime.session.authenticated)
    var gatewayReachable = Boolean(runtime.gateway && (runtime.gateway.running || runtime.gateway.reachable))
    var gatewayStatusCode = Number(runtime.gateway && runtime.gateway.status_code || 0) || 0

    state.runtime_started = runtimeStarted
    state.runtime_authenticated = runtimeAuthenticated
    state.gateway_status_code = gatewayStatusCode
    state.gateway_reachable = gatewayReachable
    RECOVERY_FIELDS.forEach(function(key) {
        if (Object.prototype.hasOwnProperty.call(authRecovery, key)) {
            state[key] = authRecovery[key]
        }
    })
    if (!state.recovery_phase) state.recovery_phase = runtimeAuthenticated ? "recovered" : "idle"
    var normalizedStatus = normalizeTwoFactorStatus(state.status || "")
    var serverBootResumePending = isServerBootResumeRecoveryState(state) && !runtimeAuthenticated

    if (runtimeAuthenticated && gatewayReachable && gatewayStatusCode !== 401) {
        state.status = "success"
        state.message = "Gateway 会话有效，无需再次确认。"
        state.last_result = "运行态会话正常。"
        state.last_error = ""
        state.mode = ""
        state.challenge_code = ""
        state.challenge_detected_at = ""
        state.response_code = ""
        state.response_status = ""
        state.response_received_at = ""
        state.response_submitted_at = ""
        state.response_rejected_at = ""
        state.challenge_feedback = ""
        state.page_title = ""
        state.page_url = ""
        state.gateway_trace = ""
        state.browser_authenticated = true
        state.gateway_authenticated = true
        state.backend_authenticated = true
        state.recovery_phase = "recovered"
        state.auto_restart_scheduled = false
        state.manual_takeover_active = false
        return derive2faActionState(state)
    }

    if (!runtimeAuthenticated || gatewayStatusCode === 401) {
        state.gateway_authenticated = false
        state.backend_authenticated = false
        if (!runtimeStarted) {
            state.browser_authenticated = false
        }
        if (
            serverBootResumePending
            && ["triggered", "waiting_confirm", "waiting_response"].indexOf(normalizedStatus) === -1
        ) {
            state.status = "resume_pending"
            state.message = state.probe_result === "resume_probe_timeout"
                ? "静默恢复尚未自动成功；当前不会自动补发新的 2FA，如需立即恢复请去 Runtime 页面人工处理。"
                : "Compute 重启后正在静默复用现有 Gateway Session，本轮不会自动重开 2FA。"
            state.last_result = state.probe_result === "resume_probe_timeout"
                ? "静默恢复未自动成功，当前保持被动等待，不会自动新开 2FA。"
                : "已进入 server_boot 静默恢复窗口。"
            state.last_error = ""
            state.mode = ""
            state.challenge_code = ""
            state.challenge_detected_at = ""
            state.response_code = ""
            state.response_status = ""
            state.response_received_at = ""
            state.response_submitted_at = ""
            state.response_rejected_at = ""
            state.challenge_feedback = ""
            state.page_title = ""
            state.page_url = ""
            state.gateway_trace = ""
        } else if (
            isSilentRecoveryState(state)
            && ["triggered", "waiting_confirm", "waiting_response"].indexOf(normalizedStatus) === -1
        ) {
            var silentRecovery = buildSilentRecoveryMessage(state)
            state.status = "recovering"
            state.message = silentRecovery.message
            state.last_result = silentRecovery.last_result
            state.last_error = ""
            state.mode = ""
            state.challenge_code = ""
            state.challenge_detected_at = ""
            state.response_code = ""
            state.response_status = ""
            state.response_received_at = ""
            state.response_submitted_at = ""
            state.response_rejected_at = ""
            state.challenge_feedback = ""
            state.page_title = ""
            state.page_url = ""
            state.gateway_trace = ""
        } else if (
            isManualAuthRequiredRecoveryState(state)
            && ["triggered", "waiting_confirm", "waiting_response"].indexOf(normalizedStatus) === -1
        ) {
            state.status = "requested"
            state.message = "静默恢复窗口已结束，当前需要手动触发 2FA。"
            state.last_result = "静默恢复未完成，等待手动触发新的 2FA 轮次。"
        } else if (normalizedStatus === "success") {
            state.status = "requested"
            state.message = "旧 Gateway 认证已失效，请重新触发 2FA。"
            state.last_result = "旧 Gateway 认证已失效，等待重新触发 2FA。"
        }
    }

    return derive2faActionState(state)
}

function saveState(environment, patch, options) {
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var times = timeUtils.getTimeStrings()
    var record = getStateRecord(runtimeEnvironment, IBKR_2FA_STATE_DATE) || getStateRecord(runtimeEnvironment, times.date)
    var collection = $app.findCollectionByNameOrId("ibkr_state")
    if (!record) {
        record = new Record(collection, {})
    }

    var existingRaw = ""
    if (record && record.id) {
        existingRaw = typeof record.getString === "function" ? (record.getString("data") || "") : record.get("data")
    }
    var current = record && record.id ? safeJsonParse(existingRaw) : {}
    var next = {
        ...current,
        ...(patch || {}),
        updated_at: times.us,
    }
    next.status = normalizeTwoFactorStatus(next.status || "requested")

    record.set("state_key", IBKR_2FA_STATE_KEY)
    record.set("date", IBKR_2FA_STATE_DATE)
    record.set("environment", runtimeEnvironment)
    record.set("data", next)
    $app.save(record)

    return {
        environment: runtimeEnvironment,
        date: IBKR_2FA_STATE_DATE,
        record: record,
        data: next,
    }
}

function ensureRequestedState(environment, options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || "", envUtils.LIVE_ENVIRONMENT)
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || {}
    var times = timeUtils.getTimeStrings()
    var status = normalizeTwoFactorStatus(currentData.status || "")
    var isActive = isActiveStatus(status)
    var forceReset = !!opts.forceReset
    var keepActiveFlow = !forceReset && ["triggered", "waiting_confirm", "waiting_response"].indexOf(status) !== -1
    var preserveCurrentDisplay = keepActiveFlow && !!currentData.message_id
    var nextStatus = (isActive && !forceReset) || keepActiveFlow ? status : "requested"
    var nextDetail = preserveCurrentDisplay
        ? (currentData.detail || opts.detail || {})
        : (opts.detail || (forceReset ? {} : (currentData.detail || {})))

    var patch = {
        status: nextStatus,
        reason: preserveCurrentDisplay
            ? (currentData.reason || opts.reason || "manual_reauth")
            : (opts.reason || currentData.reason || "manual_reauth"),
        detail: nextDetail,
        source: preserveCurrentDisplay
            ? (currentData.source || opts.source || "ibkr_compute")
            : (opts.source || currentData.source || "ibkr_compute"),
        requested_at: preserveCurrentDisplay ? (currentData.requested_at || times.us) : times.us,
        last_request_at: times.us,
        request_count: toNumber(currentData.request_count, 0) + 1,
    }

    if (!keepActiveFlow && (!isActive || forceReset)) {
        patch.triggered_at = ""
        patch.result_at = ""
        patch.last_error = ""
        patch.last_result = ""
        patch.mode = ""
        patch.mode_changed_at = ""
        patch.mode_timeline = ""
        patch.challenge_code = ""
        patch.challenge_detected_at = ""
        patch.response_code = ""
        patch.response_status = ""
        patch.response_received_at = ""
        patch.response_submitted_at = ""
        patch.response_rejected_at = ""
        patch.challenge_feedback = ""
        patch.page_title = ""
        patch.page_url = ""
        patch.gateway_trace = ""
        patch.passive_network_summary = ""
        patch.passive_network_history = ""
        patch.cookie_bridge_timeline = ""
        patch.push_body_sample_count = 0
        patch.browser_authenticated = false
        patch.backend_authenticated = false
        patch.gateway_authenticated = false
        patch.gateway_status_code = 0
        patch.gateway_sso_expires_ms = 0
        patch.next_retry_at = ""
        patch.recovery_phase = "idle"
        patch.recovery_reason = ""
        patch.interruption_kind = ""
        patch.manual_takeover_active = false
        patch.manual_takeover_started_at = ""
        patch.manual_takeover_until = ""
        patch.probe_started_at = ""
        patch.probe_last_checked_at = ""
        patch.probe_attempts = 0
        patch.probe_result = ""
        patch.auto_restart_scheduled = false
        patch.last_recovery_source = ""
        patch.lock_owner = ""
        patch.lock_expires_at = ""
        patch.previous_cycle = null
    }

    if (preserveCurrentDisplay && currentData.message) {
        patch.message = currentData.message
    } else if (nextStatus === "requested") {
        patch.message = opts.message ? opts.message : (forceReset ? "" : currentData.message)
    } else if (!currentData.message && opts.message) {
        patch.message = opts.message
    }

    return saveState(runtimeEnvironment, patch)
}

function shouldUpdateExistingRequestedCard(currentData, nextData) {
    var currentFingerprint = buildDeliveryFingerprint(currentData || {})
    var nextFingerprint = buildDeliveryFingerprint(nextData || {})
    return currentFingerprint !== nextFingerprint
}

function getStatusConfig(status) {
    return STATUS_CONFIG[status] || STATUS_CONFIG.requested
}

function buildActionButton(stateData, environment) {
    var cfg = getStatusConfig(stateData.status)
    return {
        tag: "button",
        type: "primary",
        width: "fill",
        text: { tag: "plain_text", content: cfg.button },
        action_type: "request",
        url: publicUrls.getFeishuCallbackUrl(runtimeEnvironment),
        value: {
            action: "ibkr_2fa_start",
            environment: environment,
            force_restart: false,
        },
    }
}

function buildOpenButton(label, url, type) {
    return {
        tag: "button",
        type: type || "default",
        width: "fill",
        text: { tag: "plain_text", content: label },
        multi_url: {
            url: url,
            pc_url: url,
            ios_url: url,
            android_url: url,
        }
    }
}

function build2faCard(stateData, environment) {
    var effectiveState = derive2faActionState(stateData)
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(environment || effectiveState.environment || "", envUtils.LIVE_ENVIRONMENT)
    var cfg = getStatusConfig(effectiveState.status)
    var reasonLabel = getManualAuthReasonLabel(effectiveState.reason || effectiveState.recovery_reason)
    var summary = buildCardSummary(effectiveState, cfg.summary)
    var detailMarkdown = buildDetailMarkdown(effectiveState.detail)
    var previousCycle = effectiveState.previous_cycle && typeof effectiveState.previous_cycle === "object" ? effectiveState.previous_cycle : null
    var currentCycleActive = isCurrentCycleActiveStatus(effectiveState.status)
    var consoleBaseUrl = publicUrls.getConsolePublicUrl(runtimeEnvironment)
    var runtimeUrl = consoleBaseUrl + "/ibkr_runtime.html?environment=" + encodeURIComponent(runtimeEnvironment)
    var systemUrl = consoleBaseUrl + "/ibkr_system.html?environment=" + encodeURIComponent(runtimeEnvironment)
    var metaLines = [
        "**环境**: " + envUtils.getEnvironmentTag(runtimeEnvironment),
        "**当前用途**: " + reasonLabel,
        "**状态**: " + cfg.emoji + " " + (effectiveState.status || "requested"),
        "**请求时间**: " + (effectiveState.requested_at || "-"),
    ]

    if (effectiveState.triggered_at) metaLines.push("**触发时间**: " + effectiveState.triggered_at)
    if (effectiveState.result_at) metaLines.push("**结果时间**: " + effectiveState.result_at)
    if (effectiveState.reason) metaLines.push("**触发原因**: " + effectiveState.reason)
    if (effectiveState.business_deadline_cn) metaLines.push("**周验证截止**: " + effectiveState.business_deadline_cn + " CN / " + (effectiveState.business_deadline_at || "-") + " US")
    if (effectiveState.confirm_deadline_cn) metaLines.push("**本轮截止**: " + effectiveState.confirm_deadline_cn + " CN / " + (effectiveState.confirm_deadline_at || "-") + " US")
    if (effectiveState.confirm_deadline_cn || String(effectiveState.reason || "").trim().toLowerCase() === "weekly_reauth") {
        metaLines.push("**本轮时限**: " + toNumber(effectiveState.confirm_window_seconds, deadlineUtils.DEFAULT_CONFIRM_TIMEOUT_SECONDS) + " 秒")
    }
    if (effectiveState.recovery_phase) metaLines.push("**恢复阶段**: " + effectiveState.recovery_phase)
    if (effectiveState.interruption_kind) metaLines.push("**中断类型**: " + effectiveState.interruption_kind)
    if (effectiveState.mode) metaLines.push("**验证模式**: " + getModeLabel(effectiveState.mode))
    if (effectiveState.challenge_code) metaLines.push("**Challenge**: " + effectiveState.challenge_code)
    if (effectiveState.response_status) metaLines.push("**响应状态**: " + effectiveState.response_status)
    if (effectiveState.challenge_feedback) metaLines.push("**Gateway反馈**: " + effectiveState.challenge_feedback)
    if (effectiveState.manual_takeover_active) metaLines.push("**人工接管**: yes")
    if (effectiveState.manual_takeover_until) metaLines.push("**人工接管到期**: " + effectiveState.manual_takeover_until)
    if (effectiveState.probe_result) metaLines.push("**静默探测**: " + effectiveState.probe_result)
    if (effectiveState.probe_attempts) metaLines.push("**探测次数**: " + stringifyValue(effectiveState.probe_attempts))
    if (effectiveState.last_runtime_authenticated_at) metaLines.push("**最近认证成功**: " + effectiveState.last_runtime_authenticated_at)
    if (effectiveState.response_received_at) metaLines.push("**响应码收到**: " + effectiveState.response_received_at)
    if (effectiveState.response_submitted_at) metaLines.push("**响应码提交**: " + effectiveState.response_submitted_at)
    if (effectiveState.response_rejected_at) metaLines.push("**响应码拒绝**: " + effectiveState.response_rejected_at)
    if (effectiveState.operator_action) metaLines.push("**建议动作**: " + effectiveState.operator_action)
    if (effectiveState.reset_recommended) metaLines.push("**建议重开**: yes")
    if (effectiveState.reset_reason) metaLines.push("**重开原因**: " + effectiveState.reset_reason)
    if (effectiveState.last_result) metaLines.push("**反馈**: " + effectiveState.last_result)
    if (effectiveState.last_error) metaLines.push("**异常**: " + effectiveState.last_error)
    if (effectiveState.restarted_from_active_cycle) metaLines.push("**当前轮次**: 已替换上一轮")
    if (previousCycle && previousCycle.superseded_at) metaLines.push("**上一轮替换时间**: " + previousCycle.superseded_at)
    if (previousCycle && previousCycle.status) metaLines.push("**上一轮状态**: " + previousCycle.status)
    if (previousCycle && previousCycle.mode) metaLines.push("**上一轮模式**: " + getModeLabel(previousCycle.mode))

    var elements = [
        {
            tag: "markdown",
            content: "**说明**: " + summary
        },
        {
            tag: "markdown",
            content: metaLines.join("\n")
        }
    ]

    if (detailMarkdown) {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: detailMarkdown
        })
    }

    var weeklyReminderDeadlineNote = buildWeeklyReminderDeadlineNote(effectiveState)
    if (weeklyReminderDeadlineNote) {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: weeklyReminderDeadlineNote
        })
    }

    if (previousCycle && previousCycle.superseded_at) {
        var supersededLines = [
            "**上一轮已被替换**: " + previousCycle.superseded_at,
            previousCycle.status ? ("**上一轮状态**: " + previousCycle.status) : "",
            previousCycle.mode ? ("**上一轮模式**: " + getModeLabel(previousCycle.mode)) : "",
            previousCycle.challenge_code ? ("**上一轮 Challenge**: " + previousCycle.challenge_code) : "",
        ].filter(Boolean).join("\n")
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: supersededLines
        })
    }

    if (effectiveState.status === "triggered") {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: "**当前阶段**: 当前已有一轮 2FA 在进行中，请打开 Runtime 页面查看当前轮次，不要重复触发。"
        })
    }

    if (effectiveState.status === "waiting_confirm") {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: "**当前阶段**: 只需要点手机通知确认；如果卡片稍后变成 Challenge/Response，再改去 Runtime 页面提交 Response Code。当前已有 active 轮次，请不要重复触发。"
        })
    }

    if (effectiveState.status === "waiting_response" && effectiveState.challenge_code) {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: buildWaitingResponsePrompt(effectiveState)
        })
    }

    var confirmDeadlineNote = buildConfirmDeadlineNote(effectiveState)
    if (confirmDeadlineNote) {
        elements.push({ tag: "hr" })
        elements.push({
            tag: "markdown",
            content: confirmDeadlineNote
        })
    }

    var primaryButton = null
    if ((effectiveState.status || "requested") !== "success") {
        if (effectiveState.status === "resume_pending") {
            primaryButton = buildOpenButton("查看恢复状态", runtimeUrl, "primary")
        } else {
            primaryButton = currentCycleActive
                ? buildOpenButton(
                    getActiveCyclePrimaryLabel(effectiveState),
                    runtimeUrl,
                    "primary"
                )
                : buildActionButton(effectiveState, runtimeEnvironment)
        }
    }

    elements.push({ tag: "hr" })
    elements.push({
        tag: "column_set",
        horizontal_spacing: "default",
        columns: [
            primaryButton
                ? {
                    tag: "column",
                    width: "weighted",
                    weight: 1,
                    elements: [primaryButton]
                }
                : null,
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [buildOpenButton("查看 Runtime", runtimeUrl)]
            },
            {
                tag: "column",
                width: "weighted",
                weight: 1,
                elements: [buildOpenButton("查看 System", systemUrl)]
            }
        ].filter(Boolean)
    })

    return {
        schema: "2.0",
        config: { update_multi: true, wide_screen_mode: true },
        header: {
            title: {
                tag: "plain_text",
                content: cfg.emoji + " " + envUtils.labelTitleWithEnvironment(cfg.title, runtimeEnvironment)
                    + " · " + reasonLabel
            },
            template: cfg.template
        },
        body: {
            direction: "vertical",
            elements: elements
        }
    }
}

function persistDeliveryState(record, data, patch) {
    if (!record) return data || {}
    var next = {
        ...(data || {}),
        ...(patch || {}),
    }
    record.set("data", next)
    $app.save(record)
    return next
}

function deliverCard(savedState, options) {
    var opts = options || {}
    var lockKey = savedState.environment + ":" + savedState.date
    if (!acquireDeliveryLock(lockKey)) {
        return {
            ok: true,
            skipped: true,
            skipped_reason: "delivery_locked",
            environment: savedState.environment,
            date: savedState.date,
            card: build2faCard(savedState.data || {}, savedState.environment),
            message_id: (savedState.data && savedState.data.message_id) || "",
            data: savedState.data || {},
            result: { success: true, skipped: true, reason: "delivery_locked" },
        }
    }

    try {
        var latest = getStatePayload(savedState.environment)
        var effectiveState = latest && latest.record ? latest : savedState
        var stateData = effectiveState.data || {}
        var card = build2faCard(stateData, effectiveState.environment)
        var messageId = stateData.message_id || ""
        var now = Date.now()
        var fingerprint = buildDeliveryFingerprint(stateData)
        var lastDeliveredMs = toNumber(stateData.last_delivered_ms, 0)
        var lastDeliveredHash = String(stateData.last_delivered_hash || "")
        var lastDeliveredStatus = String(stateData.last_delivered_status || "")
        var allowReplace = true
        var isSamePayload = lastDeliveredHash === fingerprint && lastDeliveredStatus === String(stateData.status || "")
        var throttled = (
            messageId &&
            !opts.forceNew &&
            !opts.bypassThrottle &&
            isActiveStatus(stateData.status) &&
            isSamePayload &&
            lastDeliveredMs > 0 &&
            (now - lastDeliveredMs) < CARD_UPDATE_COOLDOWN_MS
        )

        if (throttled) {
            return {
                ok: true,
                skipped: true,
                skipped_reason: "cooldown",
                environment: effectiveState.environment,
                date: effectiveState.date,
                card: card,
                message_id: messageId,
                data: stateData,
                result: { success: true, skipped: true, reason: "cooldown" },
            }
        }

        var result = null
        if (messageId && !opts.forceNew) {
            result = feishuApp.updateMessageCard(messageId, card, effectiveState.environment)
            if (!result.success && allowReplace) {
                result = feishuApp.sendMessageDetailed("interactive", card, resolve2faChatId(effectiveState.environment), "chat_id", effectiveState.environment)
            }
        } else {
            result = feishuApp.sendMessageDetailed("interactive", card, resolve2faChatId(effectiveState.environment), "chat_id", effectiveState.environment)
        }

        if (result && result.success) {
            var deliveryMode = messageId
                ? ((result.message_id && result.message_id !== messageId) ? "replace" : "update")
                : "send"
            var persisted = persistDeliveryState(effectiveState.record, stateData, {
                message_id: result.message_id || messageId || "",
                last_delivered_ms: now,
                last_delivered_at: timeUtils.getTimeStrings().us,
                last_delivered_hash: fingerprint,
                last_delivered_status: stateData.status || "",
                last_delivery_mode: deliveryMode,
                last_delivery_error: "",
            })
            effectiveState.data = persisted
            console.log("[Feishu2FA] card delivered:", deliveryMode, effectiveState.environment, persisted.message_id || "-")
            return {
                ok: true,
                environment: effectiveState.environment,
                date: effectiveState.date,
                card: card,
                message_id: persisted.message_id || result.message_id || "",
                data: persisted,
                result: result,
            }
        }

        if (effectiveState.record) {
            persistDeliveryState(effectiveState.record, stateData, {
                last_delivery_mode: messageId ? "update" : "send",
                last_delivery_error: String((result && result.error) || "send_failed"),
            })
        }
        console.log("[Feishu2FA] card delivery failed:", effectiveState.environment, String((result && result.error) || "send_failed"))

        return {
            ok: false,
            environment: effectiveState.environment,
            date: effectiveState.date,
            card: card,
            message_id: messageId,
            data: stateData,
            result: result || { success: false, error: "send_failed" },
        }
    } finally {
        releaseDeliveryLock(lockKey)
    }
}

function request2faApproval(options) {
    var opts = options || {}
    var current = getStatePayload(opts.environment)
    var currentData = current.data || {}
    var saved = ensureRequestedState(opts.environment, {
        reason: opts.reason,
        detail: opts.detail,
        source: opts.source,
        message: opts.message,
        forceReset: !!opts.forceReset,
    })
    var currentStatus = normalizeTwoFactorStatus(currentData.status || "")
    var currentMessageId = currentData.message_id || ""
    var lastRequestPushMs = toNumber(currentData.last_request_push_ms, 0)
    var alreadyActive = isCurrentCycleActiveStatus(currentStatus)
    var activeCardExists = isActiveStatus(currentStatus) && !!currentMessageId
    var shouldResetExistingCard = !!opts.forceReset && activeCardExists
    var existingRequestedCard = String(currentStatus || "").trim().toLowerCase() === "requested" && activeCardExists
    var shouldRefreshExistingCard = shouldResetExistingCard || (existingRequestedCard && shouldUpdateExistingRequestedCard(currentData, saved.data || {}))
    var renotifyRemainingMs = activeCardExists && lastRequestPushMs > 0
        ? Math.max(0, REQUEST_RENOTIFY_COOLDOWN_MS - (Date.now() - lastRequestPushMs))
        : 0
    var shouldRenotify = (
        activeCardExists &&
        !shouldRefreshExistingCard &&
        (Date.now() - lastRequestPushMs) >= REQUEST_RENOTIFY_COOLDOWN_MS
    )

    var delivered = activeCardExists && !opts.forceNew && !shouldRenotify && !shouldRefreshExistingCard
        ? {
            ok: true,
            skipped: true,
            skipped_reason: "active_card_reused",
            environment: saved.environment,
            date: saved.date,
            card: build2faCard(saved.data || {}, saved.environment),
            message_id: saved.data.message_id || currentMessageId,
            data: saved.data,
            result: { success: true, skipped: true, reason: "active_card_reused" },
        }
        : deliverCard(saved, {
            forceNew: !!opts.forceNew,
            bypassThrottle: shouldRefreshExistingCard,
        })

    if (delivered.ok && !delivered.skipped) {
        var refreshed = persistDeliveryState(saved.record, delivered.data || saved.data, {
            last_request_push_ms: Date.now(),
            last_request_push_at: timeUtils.getTimeStrings().us,
        })
        saved.data = refreshed
        delivered.data = refreshed
    }

    if (!(alreadyActive && delivered.skipped_reason === "active_card_reused")) {
        systemEvents.writeSystemEvent(
            "status_change",
            "info",
            "ibkr_compute",
            "IBKR 2FA 请求已发送",
            {
                reason: saved.data.reason || "manual_reauth",
                status: saved.data.status || "requested",
                ...(saved.data.detail || {}),
            },
            saved.environment,
            delivered.ok
        )
    }

    syncStartupAuthProgress(saved.environment, saved.data.status || "requested", saved.data || {})

    return {
        ok: delivered.ok,
        environment: saved.environment,
        date: saved.date,
        status: saved.data.status || "requested",
        message_id: delivered.message_id,
        state: saved.data,
        skipped: !!delivered.skipped,
        skipped_reason: delivered.skipped_reason || "",
        renotify_remaining_ms: renotifyRemainingMs,
        error: delivered.result && delivered.result.success ? "" : (delivered.result.error || "send_failed"),
        already_active: alreadyActive,
    }
}

function parseHttpPayload(raw) {
    if (!raw) return {}
    try {
        return JSON.parse(raw)
    } catch (_) {
        return { raw: raw }
    }
}

function trigger2faFlow(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || {}
    var callbackDriven = opts.source === "feishu_callback"
    var forceRestart = !!opts.forceRestart
    var restartedFromActiveCycle = forceRestart && isCurrentCycleActiveStatus(currentData.status)
    var triggerTime = timeUtils.getTimeStrings().us
    var previousCycle = restartedFromActiveCycle ? {
        status: currentData.status || "",
        mode: currentData.mode || "",
        requested_at: currentData.requested_at || "",
        triggered_at: currentData.triggered_at || "",
        challenge_code: currentData.challenge_code || "",
        response_status: currentData.response_status || "",
        source: currentData.source || "",
        superseded_at: triggerTime,
        superseded_reason: opts.reason || currentData.reason || "manual_reauth",
        superseded_source: opts.source || currentData.source || "feishu_2fa",
    } : null

    if (!forceRestart && isCurrentCycleActiveStatus(currentData.status)) {
        return {
            ok: true,
            environment: runtimeEnvironment,
            already_active: true,
            state: currentData,
            card: build2faCard(currentData, runtimeEnvironment),
        }
    }

    var triggerSaved = saveState(runtimeEnvironment, {
        status: "triggered",
        reason: opts.reason || currentData.reason || "manual_reauth",
        detail: opts.detail || currentData.detail || {},
        source: opts.source || currentData.source || "feishu_2fa",
        message: restartedFromActiveCycle ? "已放弃上一轮并开启新的一轮 2FA。请只跟当前这一轮。" : "",
        requested_at: triggerTime,
        last_request_at: triggerTime,
        triggered_at: triggerTime,
        result_at: "",
        last_error: "",
        last_result: restartedFromActiveCycle
            ? "已放弃上一轮并开启新的一轮 2FA。请只跟当前这一轮。"
            : (opts.message || "已触发登录流程，等待网关提交 2FA。"),
        mode: "",
        challenge_code: "",
        challenge_detected_at: "",
        response_code: "",
        response_status: "",
        response_received_at: "",
        response_submitted_at: "",
        response_rejected_at: "",
        challenge_feedback: "",
        next_retry_at: "",
        recovery_phase: "triggered",
        recovery_reason: opts.reason || currentData.reason || "manual_reauth",
        interruption_kind: "",
        manual_takeover_active: false,
        manual_takeover_started_at: "",
        manual_takeover_until: "",
        probe_started_at: "",
        probe_last_checked_at: "",
        probe_attempts: 0,
        probe_result: "",
        auto_restart_scheduled: false,
        last_recovery_source: opts.source || currentData.source || "feishu_2fa",
        lock_owner: "",
        lock_expires_at: "",
        restarted_from_active_cycle: restartedFromActiveCycle,
        previous_cycle: restartedFromActiveCycle ? previousCycle : null,
    })
    syncStartupAuthProgress(runtimeEnvironment, "triggered", triggerSaved.data || {})

    var computeBaseUrl = envUtils.getIbkrComputePublicUrl(runtimeEnvironment, "https://qc.lzw-glory.top")
    try {
        $http.send({
            url: `${computeBaseUrl}/ibkr/stop`,
            method: "POST",
            timeout: 30,
        })
    } catch (_) {}

    try {
        var resp = $http.send({
            url: `${computeBaseUrl}/ibkr/start`,
            method: "POST",
            body: JSON.stringify({
                environment: runtimeEnvironment,
                trigger_login: true,
                source: opts.source || "feishu_2fa",
                reason: opts.reason || currentData.reason || "manual_reauth",
            }),
            headers: { "Content-Type": "application/json" },
            timeout: 30,
        })
        var payload = parseHttpPayload(resp.raw)
        if ((resp.statusCode || 500) >= 400 || payload.ok === false) {
            throw new Error(payload.error || payload.raw || ("http_" + (resp.statusCode || 500)))
        }

        var delivered = callbackDriven
            ? {
                ok: true,
                skipped: true,
                skipped_reason: "callback_card_response",
                environment: runtimeEnvironment,
                date: triggerSaved.date,
                card: build2faCard(triggerSaved.data, runtimeEnvironment),
                message_id: (triggerSaved.data && triggerSaved.data.message_id) || "",
                data: triggerSaved.data,
                result: { success: true, skipped: true, reason: "callback_card_response" },
            }
            : deliverCard(triggerSaved, {
                bypassThrottle: true,
                forceNew: !!opts.forceNewCard,
            })
        return {
            ok: true,
            environment: runtimeEnvironment,
            status_code: resp.statusCode || 200,
            upstream: computeBaseUrl + "/ibkr/start",
            state: delivered.data || triggerSaved.data,
            card: delivered.card,
            payload: payload,
        }
    } catch (err) {
        var failed = saveState(runtimeEnvironment, {
            status: "failed",
            result_at: timeUtils.getTimeStrings().us,
            last_error: err.message || String(err),
            last_result: "触发失败，请稍后重试。",
        })
        syncStartupAuthProgress(runtimeEnvironment, "failed", failed.data || {})
        if (!callbackDriven) {
            deliverCard(failed, { bypassThrottle: true })
        }
        systemEvents.writeSystemEvent(
            "alert",
            "error",
            "manual",
            "IBKR 2FA 触发失败",
            {
                reason: failed.data.reason || "manual_reauth",
                error: err.message || String(err),
            },
            runtimeEnvironment,
            true
        )
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: err.message || String(err),
            state: failed.data,
            card: build2faCard(failed.data, runtimeEnvironment),
        }
    }
}

function report2faResult(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var status = normalizeTwoFactorStatus(opts.status || "requested")
    if (!STATUS_CONFIG[status]) status = "failed"
    var currentState = getStatePayload(runtimeEnvironment).data || {}
    var statePatch = opts.state_patch && typeof opts.state_patch === "object" ? opts.state_patch : {}
    var currentTimes = timeUtils.getTimeStrings()

    var patch = {
        status: status,
        detail: opts.detail || {},
        source: opts.source || "ibkr_compute",
        message: opts.message || "",
        last_result: opts.last_result || opts.message || "",
        ...statePatch,
    }

    if (status === "triggered" || status === "waiting_confirm" || status === "waiting_response") {
        patch.requested_at = String(statePatch.requested_at || currentState.requested_at || currentTimes.us)
        patch.triggered_at = String(statePatch.triggered_at || currentState.triggered_at || patch.requested_at || currentTimes.us)
    }
    if (["success", "timeout", "failed"].indexOf(status) !== -1) {
        patch.result_at = currentTimes.us
    }
    if (opts.error) {
        patch.last_error = String(opts.error)
    } else if (status === "success") {
        patch.last_error = ""
        patch.recovery_phase = "recovered"
        patch.mode = ""
        patch.challenge_code = ""
        patch.challenge_detected_at = ""
        patch.response_code = ""
        patch.response_status = ""
        patch.response_received_at = ""
        patch.response_submitted_at = ""
        patch.response_rejected_at = ""
        patch.challenge_feedback = ""
        patch.page_title = ""
        patch.page_url = ""
        patch.gateway_trace = ""
        patch.browser_authenticated = true
        patch.gateway_authenticated = true
        patch.backend_authenticated = true
        patch.runtime_authenticated = true
        patch.runtime_started = true
        patch.next_retry_at = ""
        patch.manual_takeover_active = false
        patch.manual_takeover_started_at = ""
        patch.manual_takeover_until = ""
        patch.probe_result = "authenticated"
        patch.auto_restart_scheduled = false
        patch.lock_owner = ""
        patch.lock_expires_at = ""
    }

    var saved = saveState(runtimeEnvironment, patch)
    syncStartupAuthProgress(runtimeEnvironment, status, saved.data || {})
    var delivered = deliverCard(saved, {
        bypassThrottle: isTerminalStatus(status),
    })

    if (["success", "timeout", "failed"].indexOf(status) !== -1) {
        systemEvents.writeSystemEvent(
            status === "success" ? "status_change" : "alert",
            status === "success" ? "info" : (status === "timeout" ? "warning" : "error"),
            "ibkr_compute",
            status === "success" ? "IBKR 2FA 完成" : (status === "timeout" ? "IBKR 2FA 超时" : "IBKR 2FA 失败"),
            {
                status: status,
                ...(saved.data.detail || {}),
                result: saved.data.last_result || "",
                error: saved.data.last_error || "",
            },
            runtimeEnvironment,
            delivered.ok
        )
    }

    return {
        ok: delivered.ok,
        environment: runtimeEnvironment,
        status: status,
        message_id: delivered.message_id,
        state: delivered.data || saved.data,
        error: delivered.result && delivered.result.success ? "" : (delivered.result.error || "send_failed"),
    }
}

function submit2faResponse(options) {
    var opts = options || {}
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || {}
    var responseCode = sanitizeResponseCode(opts.response_code)
    var expectedChallenge = String(currentData.challenge_code || "").trim()
    var submittedChallenge = String(opts.challenge_code || "").trim()

    if (!responseCode) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "response_code_required",
            state: currentData,
        }
    }

    if (!expectedChallenge) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "challenge_not_ready",
            state: currentData,
        }
    }

    if (["timeout", "failed"].indexOf(normalizeTwoFactorStatus(currentData.status || "")) !== -1) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "challenge_expired_retrigger_required",
            state: currentData,
        }
    }

    if (submittedChallenge && expectedChallenge && submittedChallenge !== expectedChallenge) {
        return {
            ok: false,
            environment: runtimeEnvironment,
            error: "challenge_mismatch",
            state: currentData,
        }
    }

    var saved = saveState(runtimeEnvironment, {
        status: normalizeTwoFactorStatus(currentData.status || "") === "success" ? "success" : "waiting_response",
        response_code: responseCode,
        response_status: "received",
        response_received_at: timeUtils.getTimeStrings().us,
        response_submitted_at: "",
        response_rejected_at: "",
        challenge_feedback: "",
        source: opts.source || currentData.source || "runtime_page",
        last_result: "已收到 Response Code，等待浏览器提交流程。",
        last_error: "",
    })
    var delivered = deliverCard(saved, { bypassThrottle: true })

    return {
        ok: delivered.ok,
        environment: runtimeEnvironment,
        status: (delivered.data || saved.data).status || "waiting_response",
        message_id: delivered.message_id || "",
        state: delivered.data || saved.data,
        error: delivered.result && delivered.result.success ? "" : (delivered.result.error || "send_failed"),
    }
}

function handle2faCardCallback(c, options) {
    var opts = options || {}
    var action = opts.action || ""
    var runtimeEnvironment = envUtils.normalizeRuntimeEnvironment(opts.environment || "", envUtils.LIVE_ENVIRONMENT)
    var updateToken = opts.updateToken || null
    var current = getStatePayload(runtimeEnvironment)
    var currentData = current.data || { status: "requested" }

    if (action !== "ibkr_2fa_start") {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "error", content: "未知操作: " + action },
            card: { type: "raw", data: build2faCard(currentData, runtimeEnvironment) }
        }, updateToken)
    }

    if (normalizeTwoFactorStatus(currentData.status || "") === "success") {
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: { type: "success", content: "当前已通过验证" },
            card: { type: "raw", data: build2faCard(currentData, runtimeEnvironment) }
        }, updateToken)
    }

    var effectiveState = derive2faActionState(currentData)
    var currentStatus = String(effectiveState.status || "").trim().toLowerCase()
    var currentCycleActive = isCurrentCycleActiveStatus(currentStatus)
    var forceRestart = !!opts.forceRestart

    if (currentCycleActive && !forceRestart) {
        var activeToast = "当前已有一轮 2FA 进行中，请继续当前轮次，不要重复触发。"
        if (currentStatus === "waiting_response") {
            if (effectiveState.reset_recommended) {
                activeToast = "当前旧 2FA / Session 状态很可能已失配，请打开 Runtime 页面执行“全量清空并重新验证”，不要重新触发。"
            } else if (effectiveState.response_status === "gateway_rejected") {
                activeToast = "Gateway 已拒绝当前 Response Code，请打开 Runtime 页面核对当前 Challenge 后重新提交，不要重新触发。"
            } else if (effectiveState.response_status === "submit_failed") {
                activeToast = "浏览器提交 Response Code 失败，请打开 Runtime 页面重新提交当前 Challenge 的响应码，不要重新触发。"
            } else if (effectiveState.response_status === "submitted") {
                activeToast = "当前 Response Code 已提交，正在等待 Gateway 恢复认证；不要重新触发。"
            } else if (effectiveState.response_status === "received") {
                activeToast = "Runtime 已收到 Response Code，正在等待浏览器提交流程；不要重新触发。"
            } else {
                activeToast = "当前已进入 Challenge/Response，请打开 Runtime 页面提交 Response Code，不要重新触发。"
            }
        }
        return feishuApp.sendFeishuCallbackResponse(c, {
            toast: {
                type: "warning",
                content: activeToast
            },
            card: { type: "raw", data: build2faCard(currentData, runtimeEnvironment) }
        }, updateToken)
    }

    var result = trigger2faFlow({
        environment: runtimeEnvironment,
        source: "feishu_callback",
        reason: currentData.reason || "manual_reauth",
        detail: currentData.detail || {},
        forceRestart: forceRestart,
    })

    return feishuApp.sendFeishuCallbackResponse(c, {
        toast: {
            type: result.ok ? "success" : "error",
            content: result.ok
                ? (forceRestart && currentCycleActive
                    ? "已放弃上一轮并开启新的一轮 2FA，请只跟当前这一轮。"
                    : (forceRestart ? "已强制重开新一轮 2FA，请立即查看 IBKR Mobile" : "2FA 已触发，请在 IBKR Mobile 确认"))
                : ("2FA 触发失败: " + (result.error || "unknown_error"))
        },
        card: { type: "raw", data: result.card || build2faCard(result.state || currentData, runtimeEnvironment) }
    }, updateToken)
}

module.exports = {
    IBKR_2FA_STATE_KEY: IBKR_2FA_STATE_KEY,
    getStatePayload: getStatePayload,
    normalizeStateWithRuntime: normalizeStateWithRuntime,
    derive2faActionState: derive2faActionState,
    build2faCard: build2faCard,
    request2faApproval: request2faApproval,
    trigger2faFlow: trigger2faFlow,
    report2faResult: report2faResult,
    submit2faResponse: submit2faResponse,
    handle2faCardCallback: handle2faCardCallback,
}
