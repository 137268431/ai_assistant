from __future__ import annotations

from ibkr_compute.api.account.live.runtime import _app_coerce_float


def _summary_lookup(summary: dict) -> dict:
    if not isinstance(summary, dict):
        return {}
    return {str(key).strip().lower(): value for key, value in summary.items()}


def _extract_summary_number(summary_map: dict, *keys: str) -> float:
    for key in keys:
        raw_value = summary_map.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("amount", "value"):
                number = _app_coerce_float(lowered.get(field))
                if number is not None:
                    return float(number)
        else:
            number = _app_coerce_float(raw_value)
            if number is not None:
                return float(number)
    return 0.0


def _extract_summary_text(summary_map: dict, *keys: str) -> str:
    for key in keys:
        raw_value = summary_map.get(str(key).strip().lower())
        if isinstance(raw_value, dict):
            lowered = {str(k).strip().lower(): v for k, v in raw_value.items()}
            for field in ("value", "displayvalue", "text"):
                value = lowered.get(field)
                if value not in (None, ""):
                    return str(value)
            amount = lowered.get("amount")
            if amount not in (None, ""):
                return str(amount)
        elif raw_value not in (None, ""):
            return str(raw_value)
    return ""


__all__ = [
    "_extract_summary_number",
    "_extract_summary_text",
    "_summary_lookup",
]
