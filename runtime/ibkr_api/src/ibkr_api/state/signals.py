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


IBKR_SIGNALS_STATE_KEY = "ibkr_signals"


def _normalized_signal_state_data(payload: dict[str, Any], *, as_dict: AsDict) -> dict[str, Any]:
    raw = coerce_dict(payload.get("data"), as_dict=as_dict)
    return {
        "processed_ids": coerce_list(raw.get("processed_ids") if "processed_ids" in raw else payload.get("processed_ids")),
        "confirmed_ids": coerce_list(raw.get("confirmed_ids") if "confirmed_ids" in raw else payload.get("confirmed_ids")),
        "active_signals": coerce_list(raw.get("active_signals") if "active_signals" in raw else payload.get("active_signals")),
    }


def build_signal_state_get_response(
    *,
    date_str: Any,
    environment: Any,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    return build_state_get_response(
        state_key=IBKR_SIGNALS_STATE_KEY,
        date_str=date_str,
        environment=environment,
        normalize_environment=normalize_environment,
        get_state_payload=get_state_payload,
        as_dict=as_dict,
    )


def build_signal_state_upsert_response(
    *,
    payload: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    upsert_state: UpsertState,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    return build_state_upsert_response(
        state_key=IBKR_SIGNALS_STATE_KEY,
        payload=payload,
        state_data=_normalized_signal_state_data(payload, as_dict=as_dict),
        normalize_environment=normalize_environment,
        upsert_state=upsert_state,
    )
