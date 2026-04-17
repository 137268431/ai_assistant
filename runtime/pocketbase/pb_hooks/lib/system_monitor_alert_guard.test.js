const test = require("node:test")
const assert = require("node:assert/strict")

const {
    DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT,
    evaluateMonitorAlertFlags,
    normalizePositiveInt,
} = require("./system_monitor_alert_guard.js")

function codes(items) {
    return (items || []).map((item) => String(item && item.code || ""))
}

test("normalizePositiveInt falls back to safe defaults", () => {
    assert.equal(normalizePositiveInt("3", DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT), 3)
    assert.equal(normalizePositiveInt("0", DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT), DEFAULT_HOST_LOAD_CONSECUTIVE_COUNT)
    assert.equal(normalizePositiveInt("bad", 0), 1)
})

test("host load alerts immediately when threshold is 1", () => {
    const result = evaluateMonitorAlertFlags(
        [{ code: "host_load_high", severity: "warning" }],
        {},
        1,
        "2026-04-17 10:00:00"
    )

    assert.deepEqual(codes(result.effective_alert_flags), ["host_load_high"])
    assert.equal(result.pending_only, false)
    assert.equal(result.threshold_met, true)
    assert.equal(result.state_patch.pending_host_load_hits, 1)
})

test("first host load hit is suppressed when threshold is 2", () => {
    const result = evaluateMonitorAlertFlags(
        [{ code: "host_load_high", severity: "warning" }],
        {},
        2,
        "2026-04-17 10:00:00"
    )

    assert.deepEqual(codes(result.effective_alert_flags), [])
    assert.equal(result.pending_only, true)
    assert.equal(result.threshold_met, false)
    assert.deepEqual(result.state_patch, {
        pending_host_load_hits: 1,
        pending_host_load_since: "2026-04-17 10:00:00",
        pending_host_load_active_code: "host_load_high",
    })
})

test("second consecutive host load hit triggers the alert", () => {
    const result = evaluateMonitorAlertFlags(
        [{ code: "host_load_high", severity: "warning" }],
        {
            pending_host_load_hits: 1,
            pending_host_load_since: "2026-04-17 10:00:00",
            pending_host_load_active_code: "host_load_high",
        },
        2,
        "2026-04-17 10:05:00"
    )

    assert.deepEqual(codes(result.effective_alert_flags), ["host_load_high"])
    assert.equal(result.pending_only, false)
    assert.equal(result.threshold_met, true)
    assert.equal(result.state_patch.pending_host_load_hits, 2)
    assert.equal(result.state_patch.pending_host_load_since, "2026-04-17 10:00:00")
})

test("host load high to critical counts as the same streak", () => {
    const result = evaluateMonitorAlertFlags(
        [{ code: "host_load_critical", severity: "error" }],
        {
            pending_host_load_hits: 1,
            pending_host_load_since: "2026-04-17 10:00:00",
            pending_host_load_active_code: "host_load_high",
        },
        2,
        "2026-04-17 10:05:00"
    )

    assert.deepEqual(codes(result.effective_alert_flags), ["host_load_critical"])
    assert.equal(result.threshold_met, true)
    assert.equal(result.state_patch.pending_host_load_hits, 2)
    assert.equal(result.state_patch.pending_host_load_active_code, "host_load_critical")
})

test("non-load monitor alerts remain immediate while load is still pending", () => {
    const result = evaluateMonitorAlertFlags(
        [
            { code: "gateway_offline", severity: "error" },
            { code: "host_load_high", severity: "warning" },
        ],
        {},
        2,
        "2026-04-17 10:00:00"
    )

    assert.deepEqual(codes(result.effective_alert_flags), ["gateway_offline"])
    assert.equal(result.pending_only, false)
    assert.equal(result.threshold_met, false)
    assert.equal(result.state_patch.pending_host_load_hits, 1)
})

test("pending host load streak resets when the load flag disappears", () => {
    const result = evaluateMonitorAlertFlags(
        [],
        {
            pending_host_load_hits: 2,
            pending_host_load_since: "2026-04-17 10:00:00",
            pending_host_load_active_code: "host_load_high",
        },
        2,
        "2026-04-17 10:10:00"
    )

    assert.deepEqual(codes(result.effective_alert_flags), [])
    assert.equal(result.pending_only, false)
    assert.equal(result.threshold_met, false)
    assert.deepEqual(result.state_patch, {
        pending_host_load_hits: 0,
        pending_host_load_since: "",
        pending_host_load_active_code: "",
    })
})
