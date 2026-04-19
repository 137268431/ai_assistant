const test = require("node:test")
const assert = require("node:assert/strict")

const {
    DEFAULT_CONFIRM_TIMEOUT_SECONDS,
    deriveTwoFactorDeadlines,
    getWeeklyReauthBusinessDeadline,
} = require("./ibkr_2fa_deadlines.js")

test("weekly reauth deadline anchors to Monday premarket for Sunday US reminder", () => {
    const baseMs = Date.parse("2026-04-20T09:20:00+08:00")
    const result = getWeeklyReauthBusinessDeadline(baseMs)

    assert.equal(result.business_deadline_at, "2026-04-20 09:20:00")
    assert.equal(result.business_deadline_cn, "2026-04-20 21:20:00")
    assert.equal(result.business_deadline_overdue, false)
})

test("weekly reauth deadline becomes overdue after Monday premarket", () => {
    const result = deriveTwoFactorDeadlines(
        {
            reason: "weekly_reauth",
            status: "requested",
            requested_at: "2026-04-19 21:20:00",
        },
        {
            nowMs: Date.parse("2026-04-20T22:00:00+08:00"),
        }
    )

    assert.equal(result.business_deadline_at, "2026-04-20 09:20:00")
    assert.equal(result.business_deadline_cn, "2026-04-20 21:20:00")
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
