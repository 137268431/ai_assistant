from __future__ import annotations

from typing import Any

from ibkr_api.state.common import (
    AsDict,
    GetStatePayload,
    NormalizeEnvironment,
    UpsertState,
    build_state_get_response,
    build_state_upsert_response,
    coerce_dict,
    coerce_list,
)


IBKR_ORDERS_STATE_KEY = "orders"


def _normalized_order_state_data(payload: dict[str, Any], *, as_dict: AsDict) -> dict[str, Any]:
    raw = coerce_dict(payload.get("data"), as_dict=as_dict)
    return {
        "closed_today": coerce_list(raw.get("closed_today") if "closed_today" in raw else payload.get("closed_today")),
        "stop_loss_count_today": int(raw.get("stop_loss_count_today") if "stop_loss_count_today" in raw else payload.get("stop_loss_count_today") or 0),
        "pending": coerce_dict(raw.get("pending") if "pending" in raw else payload.get("pending"), as_dict=as_dict),
        "positions": coerce_dict(raw.get("positions") if "positions" in raw else payload.get("positions"), as_dict=as_dict),
        "order_id_map": coerce_dict(raw.get("order_id_map") if "order_id_map" in raw else payload.get("order_id_map"), as_dict=as_dict),
        "completed_signal_ids": coerce_list(
            raw.get("completed_signal_ids") if "completed_signal_ids" in raw else payload.get("completed_signal_ids")
        ),
        "completed_signal_outcomes": coerce_dict(
            raw.get("completed_signal_outcomes") if "completed_signal_outcomes" in raw else payload.get("completed_signal_outcomes"),
            as_dict=as_dict,
        ),
    }


def build_order_state_get_response(
    *,
    date_str: Any,
    environment: Any,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    return build_state_get_response(
        state_key=IBKR_ORDERS_STATE_KEY,
        date_str=date_str,
        environment=environment,
        normalize_environment=normalize_environment,
        get_state_payload=get_state_payload,
        as_dict=as_dict,
    )


def build_order_state_upsert_response(
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    upsert_state: UpsertState,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    return build_state_upsert_response(
        state_key=IBKR_ORDERS_STATE_KEY,
        payload=payload,
        state_data=_normalized_order_state_data(payload, as_dict=as_dict),
        normalize_environment=normalize_environment,
        upsert_state=upsert_state,
    )
