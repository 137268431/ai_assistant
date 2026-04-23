from __future__ import annotations

from typing import Any, Callable


NormalizeEnvironment = Callable[[Any, str], str]
GetStatePayload = Callable[..., dict[str, Any]]
UpsertState = Callable[..., dict[str, Any]]
AsDict = Callable[[Any], dict[str, Any]]


def coerce_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def coerce_dict(value: Any, *, as_dict: AsDict | None = None) -> dict[str, Any]:
    if callable(as_dict):
        return as_dict(value)
    return dict(value) if isinstance(value, dict) else {}


def build_state_get_response(
    *,
    state_key: str,
    date_str: Any,
    environment: Any,
    normalize_environment: NormalizeEnvironment,
    get_state_payload: GetStatePayload,
    as_dict: AsDict,
) -> tuple[dict[str, Any], int]:
    normalized_date = str(date_str or "").strip()
    runtime_environment = normalize_environment(environment, "live")
    if not normalized_date:
        return {
            "ok": False,
            "error": "date required",
            "environment": runtime_environment,
            "source": "ibkr-api",
        }, 400

    payload = get_state_payload(state_key, runtime_environment, date=normalized_date)
    data = coerce_dict(payload.get("data") if isinstance(payload, dict) else {}, as_dict=as_dict)
    return {
        "ok": True,
        "date": normalized_date,
        "environment": runtime_environment,
        "data": data,
        "source": "ibkr-api",
    }, 200


def build_state_upsert_response(
    *,
    state_key: str,
    payload: dict[str, Any],
    state_data: dict[str, Any],
    normalize_environment: NormalizeEnvironment,
    upsert_state: UpsertState,
) -> tuple[dict[str, Any], int]:
    normalized_date = str(payload.get("date") or "").strip()
    runtime_environment = normalize_environment(payload.get("environment"), "live")
    if not normalized_date:
        return {
            "ok": False,
            "error": "date required",
            "environment": runtime_environment,
            "source": "ibkr-api",
        }, 400

    try:
        saved = upsert_state(state_key, runtime_environment, state_data, normalized_date)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "date": normalized_date,
            "environment": runtime_environment,
            "source": "ibkr-api",
        }, 500

    return {
        "ok": True,
        "date": normalized_date,
        "environment": runtime_environment,
        "id": str(saved.get("id") or "") if isinstance(saved, dict) else "",
        "source": "ibkr-api",
    }, 200
