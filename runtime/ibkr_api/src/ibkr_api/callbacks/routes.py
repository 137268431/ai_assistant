from __future__ import annotations

from typing import Any

from flask import request


def register_callback_routes(app, *, deps: dict[str, Any]) -> dict[str, Any]:
    handle_feishu_callback = deps["handle_feishu_callback"]
    as_dict = deps["as_dict"]
    normalize_environment = deps["normalize_environment"]
    dispatch_feishu_2fa_callback = deps["dispatch_feishu_2fa_callback"]
    dispatch_feishu_order_callback = deps["dispatch_feishu_order_callback"]
    dispatch_feishu_signal_callback = deps["dispatch_feishu_signal_callback"]
    callback_toast = deps["callback_toast"]
    callback_response = deps["callback_response"]
    exports: dict[str, Any] = {}

    @app.route("/webhook/feishu/callback", methods=["POST"])
    def webhook_feishu_callback():
        return handle_feishu_callback(
            request.get_json(silent=True) or {},
            as_dict=as_dict,
            normalize_environment=normalize_environment,
            dispatch_feishu_2fa_callback_fn=dispatch_feishu_2fa_callback,
            dispatch_feishu_order_callback_fn=dispatch_feishu_order_callback,
            dispatch_feishu_signal_callback_fn=dispatch_feishu_signal_callback,
            callback_toast_fn=callback_toast,
            callback_response_fn=callback_response,
        )
    exports["webhook_feishu_callback"] = webhook_feishu_callback

    return exports


__all__ = ["register_callback_routes"]
