const test = require("node:test")
const assert = require("node:assert/strict")

const {
    buildDailyOpenReminderDetail,
    buildDataFreshnessWindow,
    buildStatusAssessment,
    classifyMarketSession,
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
        account: { ok: true, positions: 0, open_orders: 0, net_liquidation: 0 },
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
        daily_scan: {
            market_date: "2026-04-20",
            status: "completed",
            reason: "poll",
            started_at: "2026-04-20 09:20:00",
            finished_at: "2026-04-20 09:21:00",
            last_error: "",
            result: {
                scanned: 117,
                active: 10,
                candidates: 0,
                errors: 0,
                rejection_summary: {},
                rejection_examples: [],
            },
        },
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
        daily_scan: { ...base.daily_scan, ...overrides.daily_scan, result: { ...base.daily_scan.result, ...(overrides.daily_scan && overrides.daily_scan.result) } },
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

test("completed daily scan with zero targets is not treated as a non-trading day", () => {
    const snapshot = buildSnapshot({
        today: {
            bars: 0,
            indicators: 0,
            signals: 0,
            orders: 0,
            targets: 0,
        },
        active_target_count: 0,
        daily_scan: {
            market_date: "2026-04-21",
            status: "completed",
            finished_at: "2026-04-21 09:22:14",
            result: {
                scanned: 117,
                active: 0,
                candidates: 0,
                errors: 0,
                rejection_summary: {
                    vote_tie: 22,
                    premarket_volume_below_threshold: 57,
                },
                rejection_examples: [
                    {
                        bucket: "vote_tie",
                        symbol: "AAPL",
                        actual: "long_votes=2, short_votes=2",
                        threshold: "long_votes != short_votes",
                    },
                ],
            },
        },
    })
    const times = { date: "2026-04-21", us: "2026-04-21 09:20:00" }
    const clock = { weekday: 2, hour: 9, minute: 20 }
    const assessment = buildStatusAssessment(snapshot, false, buildDataFreshnessWindow(snapshot, times, clock))
    const marketSession = classifyMarketSession(snapshot, times, clock)
    const detail = buildDailyOpenReminderDetail(snapshot, assessment, marketSession, buildDataFreshnessWindow(snapshot, times, clock), times, { events: 0, error_events: 0 })

    assert.equal(marketSession.kind, "trading")
    assert.equal(marketSession.reason, "daily_scan_zero_targets")
    assert.match(marketSession.open_title, /未筛出标的/)
    assert.match(detail["未筛出原因"], /premarket_volume_below_threshold:57/)
    assert.match(detail["日筛状态"], /scanned 117/)
})

test("failed daily scan is surfaced as a trading-day scan failure", () => {
    const snapshot = buildSnapshot({
        today: {
            bars: 0,
            indicators: 0,
            signals: 0,
            orders: 0,
            targets: 0,
        },
        active_target_count: 0,
        daily_scan: {
            market_date: "2026-04-21",
            status: "failed",
            last_error: "daily_scan_failed:write_timeout",
            result: {
                scanned: 35,
                active: 0,
                candidates: 0,
                errors: 1,
            },
        },
    })
    const times = { date: "2026-04-21", us: "2026-04-21 09:20:00" }
    const clock = { weekday: 2, hour: 9, minute: 20 }
    const marketSession = classifyMarketSession(snapshot, times, clock)
    const detail = buildDailyOpenReminderDetail(
        snapshot,
        buildStatusAssessment(snapshot, false, buildDataFreshnessWindow(snapshot, times, clock)),
        marketSession,
        buildDataFreshnessWindow(snapshot, times, clock),
        times,
        { events: 0, error_events: 0 }
    )

    assert.equal(marketSession.reason, "daily_scan_failed")
    assert.match(marketSession.open_title, /筛选失败/)
    assert.equal(detail["日筛错误"], "daily_scan_failed:write_timeout")
})

test("previous-day daily scan results do not make 09:20 look ready", () => {
    const snapshot = buildSnapshot({
        today: {
            bars: 0,
            indicators: 0,
            signals: 0,
            orders: 0,
            targets: 0,
        },
        active_target_count: 0,
        daily_scan: {
            market_date: "2026-04-20",
            status: "completed",
            finished_at: "2026-04-20 09:21:00",
            result: {
                scanned: 117,
                active: 6,
                candidates: 0,
                errors: 0,
            },
        },
    })
    const times = { date: "2026-04-21", us: "2026-04-21 09:20:00" }
    const clock = { weekday: 2, hour: 9, minute: 20 }
    const marketSession = classifyMarketSession(snapshot, times, clock)
    const detail = buildDailyOpenReminderDetail(
        snapshot,
        buildStatusAssessment(snapshot, false, buildDataFreshnessWindow(snapshot, times, clock)),
        marketSession,
        buildDataFreshnessWindow(snapshot, times, clock),
        times,
        { events: 0, error_events: 0 }
    )

    assert.equal(marketSession.reason, "daily_scan_pending")
    assert.match(marketSession.open_title, /待日筛/)
    assert.equal(detail["待扫说明"], "09:20 检查时 runtime 仍在等待本轮盘前日筛完成，目标池会在日筛结束后刷新。")
})
