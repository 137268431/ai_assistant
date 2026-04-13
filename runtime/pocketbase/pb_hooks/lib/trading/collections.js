/**
 * PocketBase collection registry used by hooks and audit tooling.
 * Keep business roles explicit so schema cleanup can be phased safely.
 */

const COLLECTIONS = Object.freeze({
    ORDERS: "orders",
    ORDER_DETAILS: "ibkr_order_details",
    REVERSE_SIGNALS: "ibkr_reverse_signals",
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
        cleanup_stage: "retain",
        rename_target: "",
        note: "Canonical IBKR order event log collection.",
    }),
    [COLLECTIONS.REVERSE_SIGNALS]: Object.freeze({
        role: "canonical_reverse_workflow",
        cleanup_stage: "retain",
        rename_target: "",
        note: "Canonical IBKR reverse-intent workflow collection.",
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
