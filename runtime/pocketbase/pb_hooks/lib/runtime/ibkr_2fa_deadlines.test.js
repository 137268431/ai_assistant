const test = require("node:test")
const assert = require("node:assert/strict")

const {
    DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    deriveTwoFactorDeadlines,
    getWeeklyReauthBusinessDeadline,
} = require("./ibkr_2fa_deadlines.js")

test("weekly reauth deadline anchors to Monday premarket for Monday US reminder", () => {
    const baseMs = Date.parse("2026-04-20T13:00:00+08:00")
    const result = getWeeklyReauthBusinessDeadline(baseMs)

    assert.equal(result.business_deadline_at, "2026-04-20 04:00:00")
    assert.equal(result.business_deadline_cn, "2026-04-20 16:00:00")
    assert.equal(result.business_deadline_overdue, false)
})

test("weekly reauth deadline shifts to Beijing 17:00 during US standard time", () => {
    const baseMs = Date.parse("2026-01-05T13:00:00+08:00")
    const result = getWeeklyReauthBusinessDeadline(baseMs)

    assert.equal(result.business_deadline_at, "2026-01-05 04:00:00")
    assert.equal(result.business_deadline_cn, "2026-01-05 17:00:00")
    assert.equal(result.business_deadline_overdue, false)
})

test("weekly reauth deadline becomes overdue after Monday premarket", () => {
    const result = deriveTwoFactorDeadlines(
        {
            reason: "weekly_reauth",
            status: "requested",
            requested_at: "2026-01-05 00:00:00",
        },
        {
            nowMs: Date.parse("2026-01-05T17:30:00+08:00"),
        }
    )

    assert.equal(result.business_deadline_at, "2026-01-05 04:00:00")
    assert.equal(result.business_deadline_cn, "2026-01-05 17:00:00")
    assert.equal(result.business_deadline_overdue, true)
})

test("confirm deadline tracks 180 second window from trigger time", () => {
    const result = deriveTwoFactorDeadlines(
        {
            status: "waiting_confirm",
            triggered_at: "2026-04-20 07:00:00",
        },
        {
            nowMs: Date.parse("2026-04-20T19:01:00+08:00"),
        }
    )

    assert.equal(result.confirm_window_seconds, DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    assert.equal(result.confirm_deadline_at, "2026-04-20 07:03:00")
    assert.equal(result.confirm_deadline_cn, "2026-04-20 19:03:00")
    assert.equal(result.confirm_deadline_overdue, false)
})

test("confirm deadline tracks 180 second window during US standard time", () => {
    const result = deriveTwoFactorDeadlines(
        {
            status: "waiting_confirm",
            triggered_at: "2026-01-05 03:30:00",
        },
        {
            nowMs: Date.parse("2026-01-05T16:31:00+08:00"),
        }
    )

    assert.equal(result.confirm_window_seconds, DEFAULT_CONFIRM_TIMEOUT_SECONDS)
    assert.equal(result.confirm_deadline_at, "2026-01-05 03:33:00")
    assert.equal(result.confirm_deadline_cn, "2026-01-05 16:33:00")
    assert.equal(result.confirm_deadline_overdue, false)
})
