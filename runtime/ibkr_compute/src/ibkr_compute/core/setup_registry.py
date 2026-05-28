"""Canonical setup metadata shared by signal generation and backtests."""

from __future__ import annotations

from typing import Any


UNKNOWN_SETUP = "unknown_setup"
CORE_TWO_SETUP_PROFILE = "core_two_setup_v1"
CORE_TWO_SETUP_ACTIVE_SETUPS = ("vwap_trend_pullback_long", "sd_mr_reversal_short")


CANONICAL_INTRADAY_SETUPS: dict[str, dict[str, str]] = {
    "sd_squeeze_breakout_long": {
        "setup_label": "SD Squeeze Breakout Long",
        "setup_family": "breakout",
        "direction": "long",
        "signal_mode": "breakout",
        "exit_policy_type": "breakout",
    },
    "sd_squeeze_breakout_short": {
        "setup_label": "SD Squeeze Breakout Short",
        "setup_family": "breakout",
        "direction": "short",
        "signal_mode": "breakout",
        "exit_policy_type": "breakout",
    },
    "vwap_trend_pullback_long": {
        "setup_label": "VWAP Trend Pullback Long",
        "setup_family": "trend_pullback",
        "direction": "long",
        "signal_mode": "trend_pullback",
        "exit_policy_type": "trend_pullback",
    },
    "vwap_trend_pullback_short": {
        "setup_label": "VWAP Trend Pullback Short",
        "setup_family": "trend_pullback",
        "direction": "short",
        "signal_mode": "trend_pullback",
        "exit_policy_type": "trend_pullback",
    },
    "sd_trend_continuation_long": {
        "setup_label": "SD Trend Continuation Long",
        "setup_family": "trend_continuation",
        "direction": "long",
        "signal_mode": "trend_continuation",
        "exit_policy_type": "trend_pullback",
    },
    "sd_trend_continuation_short": {
        "setup_label": "SD Trend Continuation Short",
        "setup_family": "trend_continuation",
        "direction": "short",
        "signal_mode": "trend_continuation",
        "exit_policy_type": "trend_pullback",
    },
    "sd_mr_reversal_long": {
        "setup_label": "SD MR Reversal Long",
        "setup_family": "mean_reversion",
        "direction": "long",
        "signal_mode": "mr_reversal",
        "exit_policy_type": "mr_reversion",
    },
    "sd_mr_reversal_short": {
        "setup_label": "SD MR Reversal Short",
        "setup_family": "mean_reversion",
        "direction": "short",
        "signal_mode": "mr_reversal",
        "exit_policy_type": "mr_reversion",
    },
}


_SETUP_ALIASES = {
    "trend_sdupper": "sd_trend_continuation_long",
    "trend_sd_upper": "sd_trend_continuation_long",
    "trend_u": "sd_trend_continuation_long",
    "mr_sdlower": "sd_mr_reversal_long",
    "mr_sd_lower": "sd_mr_reversal_long",
    "mr_l": "sd_mr_reversal_long",
    "mr_sdupper": "sd_mr_reversal_short",
    "mr_sd_upper": "sd_mr_reversal_short",
    "mr_u": "sd_mr_reversal_short",
    "trend_sdlower": "sd_trend_continuation_short",
    "trend_sd_lower": "sd_trend_continuation_short",
    "trend_l": "sd_trend_continuation_short",
}


def _clean_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _humanize_token(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return text.replace("_", " ").replace("-", " ").title()


def _infer_direction(setup: str, direction: str = "") -> str:
    direction_text = _clean_text(direction).lower()
    if direction_text in {"long", "short"}:
        return direction_text
    setup_text = _clean_text(setup).lower()
    if setup_text.endswith("_long") or setup_text in {"mr_l", "trend_u"}:
        return "long"
    if setup_text.endswith("_short") or setup_text in {"mr_u", "trend_l"}:
        return "short"
    return direction_text


def _infer_family(setup: str, signal_mode: str = "") -> str:
    setup_text = _clean_text(setup).lower()
    mode_text = _clean_text(signal_mode).lower()
    if "breakout" in setup_text or mode_text == "breakout":
        return "breakout"
    if "pullback" in setup_text or "pullback" in mode_text:
        return "trend_pullback"
    if "trend_continuation" in setup_text or "continuation" in mode_text or mode_text == "trend":
        return "trend_continuation"
    if "mr" in setup_text or "reversal" in setup_text or "reversion" in mode_text:
        return "mean_reversion"
    return "custom"


def _infer_signal_mode(setup: str, signal_mode: str = "") -> str:
    mode_text = _clean_text(signal_mode).lower()
    if mode_text:
        return mode_text
    setup_text = _clean_text(setup).lower()
    if "breakout" in setup_text:
        return "breakout"
    if "pullback" in setup_text:
        return "trend_pullback"
    if "trend_continuation" in setup_text:
        return "trend_continuation"
    if "mr" in setup_text or "reversal" in setup_text:
        return "mr_reversal"
    return ""


def _infer_exit_policy_type(setup: str, signal_mode: str = "") -> str:
    family = _infer_family(setup, signal_mode)
    if family == "breakout":
        return "breakout"
    if family in {"trend_pullback", "trend_continuation"}:
        return "trend_pullback"
    return "mr_reversion"


def normalize_setup_name(setup: Any = "", fallback_signal: Any = "") -> str:
    """Return a canonical setup name when known, otherwise preserve the raw name."""

    for value in (setup, fallback_signal):
        text = _clean_text(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in CANONICAL_INTRADAY_SETUPS:
            return lowered
        if lowered in _SETUP_ALIASES:
            return _SETUP_ALIASES[lowered]
        return lowered
    return UNKNOWN_SETUP


def build_setup_metadata(
    setup: Any = "",
    *,
    fallback_signal: Any = "",
    direction: Any = "",
    signal_mode: Any = "",
    strategy_profile: Any = "",
    setup_priority: Any = None,
    exit_policy_type: Any = "",
) -> dict[str, Any]:
    """Build display/stat metadata for one independent setup."""

    setup_name = normalize_setup_name(setup, fallback_signal)
    info = CANONICAL_INTRADAY_SETUPS.get(setup_name, {})
    mode = _clean_text(signal_mode).lower() or info.get("signal_mode", "") or _infer_signal_mode(setup_name)
    family = info.get("setup_family") or _infer_family(setup_name, mode)
    metadata: dict[str, Any] = {
        "setup": setup_name,
        "setup_label": info.get("setup_label") or _humanize_token(setup_name),
        "setup_family": family,
        "direction": _infer_direction(setup_name, _clean_text(direction) or info.get("direction", "")),
        "signal_mode": mode,
        "strategy_profile": _clean_text(strategy_profile),
        "exit_policy_type": _clean_text(exit_policy_type) or info.get("exit_policy_type") or _infer_exit_policy_type(setup_name, mode),
    }
    if setup_priority is not None and setup_priority != "":
        try:
            metadata["setup_priority"] = int(setup_priority)
        except (TypeError, ValueError):
            metadata["setup_priority"] = setup_priority
    return metadata


__all__ = [
    "CANONICAL_INTRADAY_SETUPS",
    "CORE_TWO_SETUP_ACTIVE_SETUPS",
    "CORE_TWO_SETUP_PROFILE",
    "UNKNOWN_SETUP",
    "build_setup_metadata",
    "normalize_setup_name",
]
