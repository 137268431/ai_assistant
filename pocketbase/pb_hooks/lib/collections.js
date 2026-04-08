/**
 * PocketBase collection registry used by hooks and audit tooling.
 * Keep business roles explicit so schema cleanup can be phased safely.
 */

const COLLECTIONS = Object.freeze({
    ORDERS: "orders",
    ORDER_DETAILS: "order_details",
    REVERSE_SIGNALS: "reverse_signals",
    IBKR_BACKTEST_REVERSE_SIGNALS: "ibkr_backtest_reverse_signals",
})

const COLLECTION_META = Object.freeze({
    [COLLECTIONS.ORDERS]: Object.freeze({
        role: "canonical_app_orders",
        cleanup_stage: "retain",
        rename_target: "",
        note: "App-facing order lifecycle table used by hooks, UI, and reconcile flow.",
    }),
    [COLLECTIONS.ORDER_DETAILS]: Object.freeze({
        role: "canonical_app_order_events",
        cleanup_stage: "rename_after_readers_switch",
        rename_target: "ibkr_order_details",
        note: "App-facing order event log; naming is ambiguous but still actively used.",
    }),
    [COLLECTIONS.REVERSE_SIGNALS]: Object.freeze({
        role: "canonical_reverse_workflow",
        cleanup_stage: "rename_after_readers_switch",
        rename_target: "ibkr_reverse_signals",
        note: "Live reverse-intent workflow table; not safe to delete while hooks, compute, and UI still read it.",
    }),
    [COLLECTIONS.IBKR_BACKTEST_REVERSE_SIGNALS]: Object.freeze({
        role: "backtest_only",
        cleanup_stage: "retain_isolated",
        rename_target: "",
        note: "Backtest-only reverse capture table isolated by run_id.",
    }),
})

function getCollectionMeta(name) {
    return COLLECTION_META[name] || null
}

module.exports = {
    COLLECTIONS: COLLECTIONS,
    COLLECTION_META: COLLECTION_META,
    getCollectionMeta: getCollectionMeta,
}
