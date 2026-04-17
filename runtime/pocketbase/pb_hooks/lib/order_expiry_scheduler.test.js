const test = require("node:test")
const assert = require("node:assert/strict")

const {
    cancelFailureLooksClosed,
    processExpiredOrders,
    resolveCancelableOrderId,
} = require("./order_expiry_scheduler.js")

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

function createLogger() {
    return {
        info_messages: [],
        error_messages: [],
        log(message) {
            this.info_messages.push(String(message || ""))
        },
        error(message) {
            this.error_messages.push(String(message || ""))
        },
    }
}

test("resolveCancelableOrderId falls back across top-level and extra fields", () => {
    const record = new FakeRecord({
        order_id: "local-1",
        broker_order_id: "",
        extra: {
            broker_order_id: "12345",
        },
    }, "rec-1")

    assert.equal(resolveCancelableOrderId(record), "12345")
})

test("processExpiredOrders cancels overdue entry orders and records timeline updates", () => {
    const record = new FakeRecord({
        unique_id: "entry_XOM_long_20260417_111312",
        status: "Submitted",
        bar_time_ms: 1776438793593,
        us_time: "2026-04-17 11:13:13",
        cn_time: "2026-04-17 23:13:13",
        broker_order_id: "1",
        extra: {
            feishu_order_message_id: "om_old",
        },
    }, "order_rec_1")
    const saved = []
    const relationUpdates = []
    const metaUpdates = []
    const detailEvents = []
    const notifyCalls = []
    const cancelCalls = []
    const logger = createLogger()

    const result = processExpiredOrders({
        records: [record],
        environment: "live",
        validityMinutes: 30,
        cutoffMs: 1776440700000,
        app: {
            save(item) {
                saved.push(item)
            },
        },
        cancelBrokerOrder(environment, orderId) {
            cancelCalls.push({ environment, orderId })
            return {
                ok: true,
                statusCode: 200,
                payload: { ok: true },
                upstream: "http://127.0.0.1:5100/ibkr/orders/cancel",
            }
        },
        applyOrderRelationship(recordArg, payload) {
            relationUpdates.push({ recordArg, payload })
        },
        applyOrderStatusMeta(recordArg, payload) {
            metaUpdates.push({ recordArg, payload })
            return {
                eventTimes: {
                    us_time: "2026-04-17 11:45:00",
                    cn_time: "2026-04-17 23:45:00",
                    bar_time_ms: 1776440700000,
                },
            }
        },
        appendOrderDetail(recordArg, payload) {
            detailEvents.push({ recordArg, payload })
        },
        getOrderExtra(recordArg) {
            return recordArg.get("extra") || {}
        },
        mergeOrderExtra(recordArg, patch) {
            recordArg.set("extra", {
                ...(recordArg.get("extra") || {}),
                ...(patch || {}),
            })
        },
        notifyOrder(kind, recordArg, payload) {
            notifyCalls.push({ kind, recordArg, payload })
            return {
                success: true,
                message_id: "om_new",
            }
        },
        logger,
    })

    assert.equal(result.processed_count, 1)
    assert.equal(result.cancel_failure_count, 0)
    assert.equal(record.get("status"), "Canceled")
    assert.equal(saved.length, 1)
    assert.deepEqual(cancelCalls, [{ environment: "live", orderId: "1" }])
    assert.equal(relationUpdates.length, 1)
    assert.deepEqual(relationUpdates[0].payload, {
        relation_status: "closed",
        status: "Canceled",
    })
    assert.equal(metaUpdates.length, 1)
    assert.equal(detailEvents.length, 1)
    assert.equal(detailEvents[0].payload.source, "order_scheduler")
    assert.equal(detailEvents[0].payload.extra.previous_status, "Submitted")
    assert.equal(detailEvents[0].payload.extra.cancelled_broker_order_id, "1")
    assert.equal(notifyCalls.length, 1)
    assert.equal(notifyCalls[0].kind, "canceled")
    assert.equal(record.get("extra").feishu_order_message_id, "om_new")
    assert.ok(logger.info_messages.some((line) => line.includes("record_id=order_rec_1")))
    assert.ok(logger.info_messages.some((line) => line.includes("订单已取消")))
})

test("processExpiredOrders leaves local status untouched when broker cancel fails", () => {
    const record = new FakeRecord({
        unique_id: "entry_fail",
        status: "Submitted",
        bar_time_ms: 1776438793593,
        broker_order_id: "9",
        extra: {},
    }, "order_rec_fail")
    const saved = []
    const detailEvents = []
    const logger = createLogger()

    const result = processExpiredOrders({
        records: [record],
        environment: "live",
        validityMinutes: 30,
        cutoffMs: 1776440700000,
        app: {
            save(item) {
                saved.push(item)
            },
        },
        cancelBrokerOrder() {
            return {
                ok: false,
                statusCode: 502,
                payload: { ok: false, error: "gateway timeout" },
                upstream: "http://127.0.0.1:5100/ibkr/orders/cancel",
            }
        },
        appendOrderDetail(recordArg, payload) {
            detailEvents.push({ recordArg, payload })
        },
        logger,
    })

    assert.equal(result.processed_count, 0)
    assert.equal(result.cancel_failure_count, 1)
    assert.equal(record.get("status"), "Submitted")
    assert.equal(saved.length, 0)
    assert.equal(detailEvents.length, 0)
    assert.ok(logger.error_messages.some((line) => line.includes("IBKR 撤单失败")))
})

test("processExpiredOrders closes related planned child orders while canceling only the entry at broker", () => {
    const entryRecord = new FakeRecord({
        unique_id: "entry_XOM_long_20260417_111312",
        trade_group_id: "entry_XOM_long_20260417_111312",
        entry_order_unique_id: "entry_XOM_long_20260417_111312",
        role: "entry",
        status: "Submitted",
        bar_time_ms: 1776438793593,
        broker_order_id: "1",
        extra: {
            feishu_order_message_id: "om_group",
        },
    }, "entry_rec")
    const tpRecord = new FakeRecord({
        unique_id: "tp_XOM_long_20260417_111312",
        trade_group_id: "entry_XOM_long_20260417_111312",
        entry_order_unique_id: "entry_XOM_long_20260417_111312",
        role: "take_profit",
        status: "Init",
        bar_time_ms: 1776438793593,
        broker_order_id: "2",
    }, "tp_rec")
    const slRecord = new FakeRecord({
        unique_id: "sl_XOM_long_20260417_111312",
        trade_group_id: "entry_XOM_long_20260417_111312",
        entry_order_unique_id: "entry_XOM_long_20260417_111312",
        role: "stop_loss",
        status: "Init",
        bar_time_ms: 1776438793593,
        broker_order_id: "3",
    }, "sl_rec")
    const cancelCalls = []
    const detailEvents = []
    const notifyCalls = []

    const result = processExpiredOrders({
        records: [entryRecord],
        environment: "live",
        validityMinutes: 30,
        cutoffMs: 1776440700000,
        app: { save() {} },
        findRelatedRecords() {
            return [entryRecord, tpRecord, slRecord]
        },
        cancelBrokerOrder(environment, orderId) {
            cancelCalls.push({ environment, orderId })
            return { ok: true, statusCode: 200, payload: { ok: true } }
        },
        applyOrderStatusMeta() {
            return {
                eventTimes: {
                    us_time: "2026-04-17 11:45:00",
                    cn_time: "2026-04-17 23:45:00",
                    bar_time_ms: 1776440700000,
                },
            }
        },
        appendOrderDetail(recordArg, payload) {
            detailEvents.push({ unique_id: recordArg.get("unique_id"), payload })
        },
        getOrderExtra(recordArg) {
            return recordArg.get("extra") || {}
        },
        mergeOrderExtra(recordArg, patch) {
            recordArg.set("extra", {
                ...(recordArg.get("extra") || {}),
                ...(patch || {}),
            })
        },
        notifyOrder(kind, recordArg) {
            notifyCalls.push({ kind, unique_id: recordArg.get("unique_id") })
            return { success: true, message_id: "om_group_new" }
        },
    })

    assert.equal(result.processed_count, 3)
    assert.deepEqual(cancelCalls, [{ environment: "live", orderId: "1" }])
    assert.equal(entryRecord.get("status"), "Canceled")
    assert.equal(tpRecord.get("status"), "Canceled")
    assert.equal(slRecord.get("status"), "Canceled")
    assert.equal(detailEvents.length, 3)
    assert.deepEqual(
        detailEvents.map((item) => item.unique_id),
        [
            "entry_XOM_long_20260417_111312",
            "tp_XOM_long_20260417_111312",
            "sl_XOM_long_20260417_111312",
        ]
    )
    assert.deepEqual(notifyCalls, [{ kind: "canceled", unique_id: "entry_XOM_long_20260417_111312" }])
})

test("cancelFailureLooksClosed recognizes already-closed broker responses", () => {
    assert.equal(cancelFailureLooksClosed({ error: "Order already inactive" }), true)
    assert.equal(cancelFailureLooksClosed({ message: "filled" }), true)
    assert.equal(cancelFailureLooksClosed({ error: "temporary network error" }), false)
})
