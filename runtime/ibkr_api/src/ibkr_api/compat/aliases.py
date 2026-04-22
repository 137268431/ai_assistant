from __future__ import annotations


COMPAT_MODULE_ALIASES = {
    "ibkr_api.signal_ack": "ibkr_api.signals.ack",
    "ibkr_api.signal_notifications": "ibkr_api.signals.notifications",
    "ibkr_api.signal_order_cancel": "ibkr_api.signals.order_cancel",
    "ibkr_api.signal_values": "ibkr_api.signals.values",
    "ibkr_api.signal_webhooks": "ibkr_api.signals.webhooks",
    "ibkr_api.signals_api": "ibkr_api.signals.api",
    "ibkr_api.order_details": "ibkr_api.orders.details",
    "ibkr_api.order_group_cancel": "ibkr_api.orders.group_cancel",
    "ibkr_api.order_group_close": "ibkr_api.orders.group_close",
    "ibkr_api.order_group_common": "ibkr_api.orders.group_common",
    "ibkr_api.order_reconcile_support": "ibkr_api.orders.reconcile_support",
    "ibkr_api.order_relationships": "ibkr_api.orders.relationships",
    "ibkr_api.order_timestamps": "ibkr_api.orders.timestamps",
    "ibkr_api.order_upsert": "ibkr_api.orders.upsert",
    "ibkr_api.order_values": "ibkr_api.orders.values",
    "ibkr_api.order_webhooks": "ibkr_api.orders.webhooks",
    "ibkr_api.orders_api": "ibkr_api.orders.api",
    "ibkr_api.orders_reconcile": "ibkr_api.orders.reconcile",
    "ibkr_api.reverse_actions": "ibkr_api.reverse.actions",
    "ibkr_api.reverse_calculate": "ibkr_api.reverse.calculate",
    "ibkr_api.reverse_common": "ibkr_api.reverse.common",
    "ibkr_api.reverse_queries": "ibkr_api.reverse.queries",
    "ibkr_api.webhook_pages": "ibkr_api.webhooks.pages",
}


__all__ = ["COMPAT_MODULE_ALIASES"]
