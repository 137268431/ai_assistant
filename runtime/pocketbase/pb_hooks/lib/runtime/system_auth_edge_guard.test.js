const test = require("node:test")
const assert = require("node:assert/strict")

const {
    buildAuthImmediateIssue,
    isOperational2faIssue,
} = require("./system_auth_edge_guard.js")

test("stale broker silent probe is treated as recovering instead of expired", () => {
    const issue = buildAuthImmediateIssue({
        status: "recovering",
        has_request: false,
        active: false,
        gateway_reachable: true,
        gateway_status_code: 401,
        runtime_authenticated: false,
        runtime_started: false,
        recovery_phase: "silent_probe",
        recovery_class: "stale_broker",
        probe_result: "stale_broker_restart_scheduled",
        auto_restart_scheduled: true,
    })

    assert.ok(issue)
    assert.equal(issue.kind, "stale_broker_recovering")
    assert.match(issue.title, /恢复中/)
    assert.equal(isOperational2faIssue(issue), true)
})

test("plain 401 without recovery context still escalates to manual 2fa", () => {
    const issue = buildAuthImmediateIssue({
        status: "success",
        has_request: false,
        active: false,
        gateway_reachable: true,
        gateway_status_code: 401,
        runtime_authenticated: false,
        runtime_started: true,
        recovery_phase: "idle",
        recovery_class: "",
        probe_result: "",
        auto_restart_scheduled: false,
    })

    assert.ok(issue)
    assert.equal(issue.kind, "session_expired")
    assert.equal(isOperational2faIssue(issue), false)
})
