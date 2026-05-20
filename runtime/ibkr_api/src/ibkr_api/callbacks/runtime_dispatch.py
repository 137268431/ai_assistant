from __future__ import annotations

from typing import Any, Callable


def build_dispatch_feishu_2fa_callback(
    *,
    globals_dict: dict[str, Any],
    pb_base_url: str,
    support: Callable[..., dict[str, Any]],
):
    def _dispatch_feishu_2fa_callback(action: str, environment: str) -> dict[str, Any]:
        return support(
            action,
            environment,
            request_json_request=globals_dict["_request_json_request"],
            pb_base_url=pb_base_url,
            as_dict=globals_dict["_as_dict"],
            callback_toast_fn=globals_dict["_callback_toast"],
        )

    return _dispatch_feishu_2fa_callback


def build_dispatch_feishu_signal_callback(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    support: Callable[..., tuple[dict[str, Any], int]],
):
    def _dispatch_feishu_signal_callback(
        action: str,
        signal_id: str,
        environment: str,
        data_environment: str = "",
    ) -> tuple[dict[str, Any], int]:
        return support(
            action,
            signal_id,
            environment,
            data_environment=data_environment,
            pb=pb,
            normalize_environment=globals_dict["_normalize_environment"],
            escape_filter_string=globals_dict["_escape_filter_string"],
            cancel_broker_order=globals_dict["_cancel_broker_order_via_runtime"],
            build_signal_confirm_webhook_response_fn=globals_dict["build_signal_confirm_webhook_response"],
            build_signal_cancel_webhook_response_fn=globals_dict["build_signal_cancel_webhook_response"],
            callback_toast_fn=globals_dict["_callback_toast"],
            update_signal_card=globals_dict["_feishu_update_interactive"],
            notify_order_status=globals_dict["_notify_order_status"],
            console_base_url=globals_dict["_console_base_url"](),
            config_value=lambda key, default, env: globals_dict["_config_value"](key, default, env),
        )

    return _dispatch_feishu_signal_callback


def build_dispatch_feishu_order_callback(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    support: Callable[..., tuple[dict[str, Any], int]],
):
    def _dispatch_feishu_order_callback(
        action: str,
        order_id: str,
        environment: str,
        data_environment: str = "",
    ) -> tuple[dict[str, Any], int]:
        return support(
            action,
            order_id,
            environment,
            data_environment=data_environment,
            pb=pb,
            normalize_environment=globals_dict["_normalize_environment"],
            escape_filter_string=globals_dict["_escape_filter_string"],
            cancel_broker_order=globals_dict["_cancel_broker_order_via_runtime"],
            build_order_cancel_group_response_fn=globals_dict["build_order_cancel_group_response"],
            build_order_close_group_response_fn=globals_dict["build_order_close_group_response"],
            callback_toast_fn=globals_dict["_callback_toast"],
            notify_order_status=globals_dict["_notify_order_status"],
            console_base_url=globals_dict["_console_base_url"](),
        )

    return _dispatch_feishu_order_callback


__all__ = [
    "build_dispatch_feishu_2fa_callback",
    "build_dispatch_feishu_order_callback",
    "build_dispatch_feishu_signal_callback",
]
