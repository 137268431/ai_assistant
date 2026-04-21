const test = require("node:test")
const assert = require("node:assert/strict")

const {
    buildDataFreshnessWindow,
    buildStatusAssessment,
    hasHeartbeatIssue,
    listDataHealthProblems,
} = require("./system_notify_scheduler.js")

function buildSnapshot(overrides = {}) {
    const base = {
        compute: { status: "running" },
        runtime: {
            starting: false,
            warmup_phase: "ready",
            warmup_last_success_at: "2026-04-20 09:39:00",
            warmup_ready_trade_symbols: 10,
            warmup_trade_symbols_total: 10,
            warmup_pending_symbols_total: 0,
        },
        market_session: { kind: "regular", label: "regular" },
        session: { authenticated: true },
        auth_recovery: {
            recovery_phase: "",
            recovery_class: "",
            probe_result: "",
            auto_restart_scheduled: false,
        },
        websocket: {
            connected: true,
            ready: true,
            label: "connected / ready=true",
        },
        latest_bar: {
            bar_time_ms: 1,
            age_min: 1,
            label: "AAPL / 1m / 2026-04-20 15:55:00",
            us_time: "2026-04-20 15:55:00",
        },
        latest_indicator: {
            bar_time_ms: 1,
            lag_min: 0,
            label: "AAPL / lag 0m / 2026-04-20 15:55:00",
        },
        bar_bucket: {
            enabled: true,
            status: "fresh",
            lag_s: 0,
            pending_symbols_total: 0,
            pending_symbols: [],
            pending_symbol_details: [],
            last_due_bucket_ms: 1,
            last_completed_bucket_ms: 1,
            last_due_bucket_us: "2026-04-20 15:55:00",
            last_completed_bucket_us: "2026-04-20 15:55:00",
            label: "fresh / due 2026-04-20 15:55:00 / done 2026-04-20 15:55:00",
        },
        account: { ok: true },
        trading_enabled: true,
        today: {
            bars: 10,
            indicators: 10,
            signals: 0,
            orders: 0,
            targets: 10,
        },
        active_target_count: 10,
        auth: { label: "ok" },
    }
    return {
        ...base,
        ...overrides,
        compute: { ...base.compute, ...overrides.compute },
        runtime: { ...base.runtime, ...overrides.runtime },
        market_session: { ...base.market_session, ...overrides.market_session },
        session: { ...base.session, ...overrides.session },
        auth_recovery: { ...base.auth_recovery, ...overrides.auth_recovery },
        websocket: { ...base.websocket, ...overrides.websocket },
        latest_bar: { ...base.latest_bar, ...overrides.latest_bar },
        latest_indicator: { ...base.latest_indicator, ...overrides.latest_indicator },
        bar_bucket: { ...base.bar_bucket, ...overrides.bar_bucket },
        account: { ...base.account, ...overrides.account },
        today: { ...base.today, ...overrides.today },
        auth: { ...base.auth, ...overrides.auth },
    }
}

test("close transition ignores stale latest-bar age when the canonical bucket is complete", () => {
    const snapshot = buildSnapshot({
        market_session: { kind: "close_transition" },
        latest_bar: {
            age_min: 11,
            label: "AAPL / 11m / 2026-04-20 15:55:00",
        },
    })
    const times = { date: "2026-04-20" }
    const clock = { weekday: 1, hour: 16, minute: 5 }

    const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
    const assessment = buildStatusAssessment(snapshot, false, freshnessWindow)

    assert.equal(freshnessWindow.session_kind, "close_transition")
    assert.equal(freshnessWindow.enforce_latest_bar_age, false)
    assert.equal(hasHeartbeatIssue(snapshot, freshnessWindow), false)
    assert.equal(assessment.kind, "healthy")
    assert.deepEqual(listDataHealthProblems(snapshot, freshnessWindow), [])
})

test("afterhours still fails when the current 5m bucket is incomplete", () => {
    const snapshot = buildSnapshot({
        market_session: { kind: "afterhours" },
        latest_bar: {
            age_min: 16,
            label: "AAPL / 16m / 2026-04-20 15:55:00",
        },
        bar_bucket: {
            status: "stale",
            lag_s: 300,
            last_due_bucket_ms: 2,
            last_completed_bucket_ms: 1,
            last_due_bucket_us: "2026-04-20 16:00:00",
            last_completed_bucket_us: "2026-04-20 15:55:00",
            label: "stale / due 2026-04-20 16:00:00 / done 2026-04-20 15:55:00",
        },
    })
    const times = { date: "2026-04-20" }
    const clock = { weekday: 1, hour: 16, minute: 11 }

    const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
    const assessment = buildStatusAssessment(snapshot, false, freshnessWindow)
    const problems = listDataHealthProblems(snapshot, freshnessWindow)

    assert.equal(freshnessWindow.session_kind, "afterhours")
    assert.equal(hasHeartbeatIssue(snapshot, freshnessWindow), true)
    assert.equal(assessment.kind, "broken")
    assert.ok(problems.some((item) => item.includes("当前 5m bar 桶未完成")))
})

test("silent auth recovery is watch-only instead of a broken unauthenticated state", () => {
    const snapshot = buildSnapshot({
        session: { authenticated: false },
        auth_recovery: {
            recovery_phase: "silent_probe",
            recovery_class: "stale_broker",
            probe_result: "stale_broker_restart_scheduled",
            auto_restart_scheduled: true,
        },
        auth: { label: "recovering" },
    })
    const times = { date: "2026-04-20" }
    const clock = { weekday: 1, hour: 15, minute: 30 }

    const freshnessWindow = buildDataFreshnessWindow(snapshot, times, clock)
    const assessment = buildStatusAssessment(snapshot, false, freshnessWindow)
    const problems = listDataHealthProblems(snapshot, freshnessWindow)

    assert.equal(assessment.kind, "watch")
    assert.match(assessment.summary, /静默恢复/)
    assert.ok(problems.includes("IBKR 会话正在静默恢复"))
    assert.ok(!problems.includes("IBKR 会话未认证"))
})
