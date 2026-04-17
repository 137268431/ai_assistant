const test = require("node:test")
const assert = require("node:assert/strict")

const {
    reconcileOrderDetails,
} = require("./order_detail_reconcile.js")

class FakeRecord {
    constructor(fields, id) {
        this.fields = { ...(fields || {}) }
        this.id = id || this.fields.id || ""
    }

    get(key) {
        if (key === "id") return this.id
        return this.fields[key]
    }

    set(key, value) {
        if (key === "id") {
            this.id = value
            return
        }
        this.fields[key] = value
    }
}

test("reconcileOrderDetails skips when latest detail already matches current status", () => {
    const orderRecord = new FakeRecord({
        signal_id: "sig_1",
        unique_id: "entry_xom_1",
        symbol: "XOM",
        status: "Canceled",
        order_type: "LMT",
    }, "order_1")
    const appendCalls = []

    const result = reconcileOrderDetails({
        orderRecords: [orderRecord],
        environment: "live",
        getDetailRecords() {
            return [
                new FakeRecord({ status: "Canceled" }, "detail_latest"),
                new FakeRecord({ status: "Submitted" }, "detail_old"),
            ]
        },
        appendOrderDetail(record, payload) {
            appendCalls.push({ record, payload })
            return { id: "detail_new" }
        },
    })

    assert.equal(result.summary.scanned, 1)
    assert.equal(result.summary.repaired, 0)
    assert.equal(result.summary.skipped, 1)
    assert.equal(appendCalls.length, 0)
    assert.equal(result.results[0].status, "skipped_current_status_detail_exists")
    assert.equal(result.results[0].latest_detail_status, "Canceled")
})

test("reconcileOrderDetails repairs when latest detail differs from current status", () => {
    const orderRecord = new FakeRecord({
        signal_id: "sig_2",
        unique_id: "entry_xom_2",
        symbol: "XOM",
        status: "Submitted",
        order_type: "LMT",
        order_id: "2001",
        broker_order_id: "2001",
        us_time: "2026-04-18 10:10:00",
        cn_time: "2026-04-18 22:10:00",
        bar_time_ms: 1776507000000,
    }, "order_2")
    const appendCalls = []

    const result = reconcileOrderDetails({
        orderRecords: [orderRecord],
        environment: "live",
        onlyMissing: true,
        getDetailRecords() {
            return [
                new FakeRecord({ status: "PendingCancel" }, "detail_latest"),
                new FakeRecord({ status: "Submitted" }, "detail_old_same_status"),
            ]
        },
        appendOrderDetail(record, payload) {
            appendCalls.push({ record, payload })
            return { id: "detail_repaired" }
        },
    })

    assert.equal(result.summary.scanned, 1)
    assert.equal(result.summary.repaired, 1)
    assert.equal(result.summary.skipped, 0)
    assert.equal(appendCalls.length, 1)
    assert.equal(appendCalls[0].payload.reason, "reconciled_missing_current_status_detail")
    assert.equal(appendCalls[0].payload.extra.previous_detail_status, "PendingCancel")
    assert.equal(appendCalls[0].payload.extra.repair_mode, "missing_current_status_detail")
    assert.equal(result.results[0].status, "repaired_detail")
})

test("reconcileOrderDetails dry run reports missing detail rows", () => {
    const orderRecord = new FakeRecord({
        signal_id: "sig_3",
        unique_id: "entry_xom_3",
        symbol: "XOM",
        status: "Submitted",
        order_type: "LMT",
    }, "order_3")

    const result = reconcileOrderDetails({
        orderRecords: [orderRecord],
        environment: "paper",
        dryRun: true,
        getDetailRecords() {
            return []
        },
    })

    assert.equal(result.summary.scanned, 1)
    assert.equal(result.summary.repaired, 1)
    assert.equal(result.summary.failed, 0)
    assert.equal(result.results[0].status, "dry_run_ready")
    assert.equal(result.results[0].payload.repair_reason, "reconciled_missing_ibkr_order_details")
    assert.equal(result.results[0].payload.repair_mode, "missing_ibkr_order_details")
    assert.equal(result.results[0].payload.environment, "paper")
})

test("reconcileOrderDetails scanAll paginates order queries", () => {
    const orderRecords = [
        new FakeRecord({ unique_id: "u1", symbol: "AAPL", status: "Submitted" }, "order_a"),
        new FakeRecord({ unique_id: "u2", symbol: "MSFT", status: "Submitted" }, "order_b"),
        new FakeRecord({ unique_id: "u3", symbol: "NVDA", status: "Submitted" }, "order_c"),
    ]
    const calls = []

    const result = reconcileOrderDetails({
        app: {
            findRecordsByFilter(collection, filter, sort, limit, offset, params) {
                calls.push({ collection, filter, sort, limit, offset, params })
                assert.equal(collection, "orders")
                return orderRecords.slice(offset, offset + limit)
            },
        },
        environment: "live",
        scanAll: true,
        pageSize: 2,
        maxScan: 5,
        getDetailRecords() {
            return [new FakeRecord({ status: "Submitted" }, "detail_ok")]
        },
    })

    assert.equal(result.summary.scanned, 3)
    assert.equal(result.summary.skipped, 3)
    assert.equal(calls.length, 2)
    assert.equal(calls[0].offset, 0)
    assert.equal(calls[1].offset, 2)
})
