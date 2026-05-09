"""Signal-mode-aware exit policy helpers.

The policy layer keeps the broker-side bracket structure intact while letting
signal modes choose different initial risk/reward and trailing-stop behavior.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


EXIT_POLICY_PROFILE_FIXED_ATR_RR = "fixed_atr_rr"
EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE = "signal_mode_adaptive_v1"
EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2 = "signal_mode_adaptive_v2"

# Compatibility aliases. Older config/backtest rows may still submit these
# values; normalize them to the strategy names above instead of breaking runs.
EXIT_POLICY_PROFILE_LEGACY = EXIT_POLICY_PROFILE_FIXED_ATR_RR
EXIT_POLICY_PROFILE_SETUP_AWARE = EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE
EXIT_POLICY_PROFILE_LEGACY_ALIAS = "legacy"
EXIT_POLICY_PROFILE_SETUP_AWARE_ALIAS = "setup_aware_v1"


_SIGNAL_MODE_ADAPTIVE_DEFAULTS: dict[str, dict[str, Any]] = {
    "mr_reversion": {
        "name": EXIT_POLICY_PROFILE_FIXED_ATR_RR,
        "sl_atr_mult": 2.0,
        "tp_rr": 1.5,
        "target_mode": "hard_rr",
        "target_is_hard": True,
        "trail_type": "atr_tighten",
        "trail_activation_r": 0.3,
        "time_stop_bars": 0,
        "time_stop_min_mfe_r": 0.0,
        "hard_time_stop_bars": 0,
    },
    "trend_pullback": {
        "name": "chandelier_runner",
        "sl_atr_mult": 2.0,
        "tp_rr": 2.0,
        "target_mode": "hard_rr",
        "target_is_hard": True,
        "trail_type": "chandelier",
        "trail_activation_r": 0.8,
        "chandelier_lookback": 22,
        "chandelier_atr_mult": 2.0,
        "time_stop_bars": 18,
        "time_stop_min_mfe_r": 0.5,
    },
    "breakout": {
        "name": "breakout_runner",
        "sl_atr_mult": 1.8,
        "tp_rr": 2.5,
        "target_mode": "hard_rr",
        "target_is_hard": True,
        "trail_type": "chandelier",
        "trail_activation_r": 1.0,
        "chandelier_lookback": 22,
        "chandelier_atr_mult": 2.5,
        "failure_exit_bars": 6,
    },
}


_SIGNAL_MODE_ADAPTIVE_V2_DEFAULTS: dict[str, dict[str, Any]] = {
    "mr_reversion": {
        "name": "mr_fixed_mean_target",
        "sl_atr_mult": 2.0,
        "tp_rr": 1.5,
        "target_mode": "hard_rr",
        "target_is_hard": True,
        "trail_type": "atr_tighten",
        "trail_activation_r": 0.3,
        "time_stop_bars": 10,
        "time_stop_min_mfe_r": 0.35,
        "hard_time_stop_bars": 0,
    },
    "trend_pullback": {
        "name": "trend_checkpoint_runner",
        "sl_atr_mult": 2.0,
        "tp_rr": 3.0,
        "target_mode": "checkpoint_then_trail",
        "target_is_hard": True,
        "checkpoint_r": 1.0,
        "checkpoint_lock_r": 0.1,
        "trail_type": "chandelier",
        "trail_activation_r": 0.8,
        "chandelier_lookback": 22,
        "chandelier_atr_mult": 2.0,
        "time_stop_bars": 18,
        "time_stop_min_mfe_r": 0.5,
    },
    "breakout": {
        "name": "breakout_checkpoint_runner",
        "sl_atr_mult": 1.8,
        "tp_rr": 3.0,
        "target_mode": "checkpoint_then_trail",
        "target_is_hard": True,
        "checkpoint_r": 1.0,
        "checkpoint_lock_r": 0.15,
        "trail_type": "chandelier",
        "trail_activation_r": 1.0,
        "chandelier_lookback": 22,
        "chandelier_atr_mult": 2.5,
        "failure_exit_enabled": True,
        "failure_exit_bars": 6,
        "failure_exit_min_mfe_r": 0.5,
    },
}


_FIXED_PROFILE_ALIASES = {
    "",
    EXIT_POLICY_PROFILE_FIXED_ATR_RR,
    EXIT_POLICY_PROFILE_LEGACY_ALIAS,
    "fixed_rr",
    "fixed_atr",
    "fixed_atr_rr_v1",
    "atr_rr",
    "atr_rr_tighten",
}


_ADAPTIVE_PROFILE_ALIASES = {
    EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE,
    EXIT_POLICY_PROFILE_SETUP_AWARE_ALIAS,
    "setup_aware",
    "setup-aware-v1",
    "signal_mode_adaptive",
    "signal-mode-adaptive-v1",
    "adaptive_by_signal",
    "adaptive_by_signal_v1",
}


_ADAPTIVE_V2_PROFILE_ALIASES = {
    EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2,
    "signal_mode_adaptive_v2",
    "signal-mode-adaptive-v2",
    "adaptive_by_signal_v2",
    "setup_aware_v2",
    "stateful_target_v2",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def _safe_str(value: Any, default: str = "") -> str:
    text = str(value if value is not None else "").strip()
    return text if text else default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _parse_overrides(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return deepcopy(raw)
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return deepcopy(parsed) if isinstance(parsed, dict) else {}


def normalize_exit_policy_profile(params: dict | None) -> str:
    params = params or {}
    profile = _safe_str(params.get("exit_policy_profile"), EXIT_POLICY_PROFILE_FIXED_ATR_RR).lower()
    if profile in _ADAPTIVE_V2_PROFILE_ALIASES:
        return EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2
    if profile in _ADAPTIVE_PROFILE_ALIASES:
        return EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE
    if profile in _FIXED_PROFILE_ALIASES:
        return EXIT_POLICY_PROFILE_FIXED_ATR_RR
    return EXIT_POLICY_PROFILE_FIXED_ATR_RR


def is_signal_mode_adaptive_exit_profile(profile_or_params: Any) -> bool:
    if isinstance(profile_or_params, dict):
        return normalize_exit_policy_profile(profile_or_params) in {
            EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE,
            EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2,
        }
    return normalize_exit_policy_profile({"exit_policy_profile": profile_or_params}) in {
        EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE,
        EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2,
    }


def classify_exit_policy(setup: str = "", signal_mode: str = "") -> str:
    setup_text = _safe_str(setup).lower()
    mode_text = _safe_str(signal_mode).lower()
    if "squeeze_breakout" in setup_text or setup_text.endswith("breakout") or "breakout" in setup_text:
        return "breakout"
    if "vwap_trend_pullback" in setup_text or mode_text == "trend":
        return "trend_pullback"
    return "mr_reversion"


def _policy_with_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    policy = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict):
            continue
        policy[str(key)] = value
    return policy


def resolve_exit_policy(params: dict | None, setup: str = "", signal_mode: str = "") -> dict[str, Any]:
    params = params or {}
    profile = normalize_exit_policy_profile(params)
    policy_type = classify_exit_policy(setup, signal_mode)

    if profile == EXIT_POLICY_PROFILE_FIXED_ATR_RR:
        return {
            "profile": EXIT_POLICY_PROFILE_FIXED_ATR_RR,
            "name": EXIT_POLICY_PROFILE_FIXED_ATR_RR,
            "policy_type": policy_type,
            "sl_atr_mult": _safe_float(params.get("sl_atr_mult"), 2.0),
            "tp_rr": _safe_float(params.get("rr_ratio"), 1.5),
            "trail_type": "atr_tighten",
            "trail_activation_r": _safe_float(params.get("atr_stop_min_profit_r"), 0.3),
            "time_stop_bars": 0,
            "hard_time_stop_bars": 0,
        }

    defaults = _SIGNAL_MODE_ADAPTIVE_V2_DEFAULTS if profile == EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2 else _SIGNAL_MODE_ADAPTIVE_DEFAULTS
    base = deepcopy(defaults.get(policy_type) or defaults["mr_reversion"])
    overrides = _parse_overrides(params.get("exit_policy_overrides"))
    profile_overrides = {}
    for profile_key in (profile, EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE, EXIT_POLICY_PROFILE_SETUP_AWARE_ALIAS):
        if isinstance(overrides.get(profile_key), dict):
            profile_overrides.update(overrides.get(profile_key) or {})
    direct_overrides = overrides.get(policy_type) if isinstance(overrides.get(policy_type), dict) else {}
    base = _policy_with_overrides(base, profile_overrides)
    base = _policy_with_overrides(base, direct_overrides)
    base["profile"] = profile
    base["policy_type"] = policy_type
    base["name"] = _safe_str(base.get("name"), policy_type)
    return base


def _reprice_with_policy(
    *,
    position: dict[str, Any],
    direction: str,
    entry: float,
    atr: float,
    shares: int,
    policy: dict[str, Any],
    max_loss: float,
) -> tuple[float, float, float]:
    sl_mult = max(0.0, _safe_float(policy.get("sl_atr_mult"), 0.0))
    rr = max(0.0, _safe_float(policy.get("tp_rr"), _safe_float(position.get("rr"), 0.0)))
    if entry <= 0 or atr <= 0 or shares <= 0 or sl_mult <= 0 or rr <= 0:
        stop = _safe_float(position.get("stop_loss"), 0.0)
        target = _safe_float(position.get("take_profit"), 0.0)
        return stop, target, abs(entry - stop) if entry > 0 and stop > 0 else 0.0

    per_share_cap = max_loss / shares if max_loss > 0 else 0.0
    if direction == "short":
        sl_atr = entry + atr * sl_mult
        sl_cap = entry + per_share_cap if per_share_cap > 0 else sl_atr
        stop = min(sl_atr, sl_cap)
        risk = max(0.0, stop - entry)
        target = entry - risk * rr
    else:
        sl_atr = entry - atr * sl_mult
        sl_cap = entry - per_share_cap if per_share_cap > 0 else sl_atr
        stop = max(sl_atr, sl_cap)
        risk = max(0.0, entry - stop)
        target = entry + risk * rr
    return stop, target, risk


def build_exit_policy_metadata(
    *,
    position: dict[str, Any],
    direction: str,
    setup: str,
    signal_mode: str,
    policy: dict[str, Any],
    risk_r: float,
    initial_stop_loss: float,
    initial_take_profit: float,
) -> dict[str, Any]:
    entry = _safe_float(position.get("entry"), 0.0)
    return {
        "exit_policy_profile": _safe_str(policy.get("profile"), EXIT_POLICY_PROFILE_FIXED_ATR_RR),
        "exit_policy": _safe_str(policy.get("name"), EXIT_POLICY_PROFILE_FIXED_ATR_RR),
        "exit_policy_type": _safe_str(policy.get("policy_type"), classify_exit_policy(setup, signal_mode)),
        "risk_r": round(float(risk_r or 0.0), 4),
        "initial_stop_loss": round(float(initial_stop_loss or 0.0), 4),
        "initial_take_profit": round(float(initial_take_profit or 0.0), 4),
        "exit_policy_settings": {
            "strategy": _safe_str(policy.get("name"), EXIT_POLICY_PROFILE_FIXED_ATR_RR),
            "sl_atr_mult": _safe_float(policy.get("sl_atr_mult"), 0.0),
            "tp_rr": _safe_float(policy.get("tp_rr"), 0.0),
            "target_mode": _safe_str(policy.get("target_mode"), "hard_rr"),
            "target_is_hard": _safe_bool(policy.get("target_is_hard"), True),
            "checkpoint_r": _safe_float(policy.get("checkpoint_r"), 0.0),
            "checkpoint_lock_r": _safe_float(policy.get("checkpoint_lock_r"), 0.0),
            "trail_type": _safe_str(policy.get("trail_type"), ""),
            "trail_activation_r": _safe_float(policy.get("trail_activation_r"), 0.0),
            "chandelier_lookback": _safe_int(policy.get("chandelier_lookback"), 0),
            "chandelier_atr_mult": _safe_float(policy.get("chandelier_atr_mult"), 0.0),
            "time_stop_bars": _safe_int(policy.get("time_stop_bars"), 0),
            "hard_time_stop_bars": _safe_int(policy.get("hard_time_stop_bars"), 0),
            "time_stop_min_mfe_r": _safe_float(policy.get("time_stop_min_mfe_r"), 0.0),
            "failure_exit_enabled": _safe_bool(policy.get("failure_exit_enabled"), False),
            "failure_exit_bars": _safe_int(policy.get("failure_exit_bars"), 0),
            "failure_exit_min_mfe_r": _safe_float(policy.get("failure_exit_min_mfe_r"), 0.0),
        },
        "target_state": {
            "target_mode": _safe_str(policy.get("target_mode"), "hard_rr"),
            "target_is_hard": _safe_bool(policy.get("target_is_hard"), True),
            "checkpoint_hit": False,
            "highest_mfe_r": 0.0,
        },
        "trail_state": {
            "high_water": entry if direction == "long" else 0.0,
            "low_water": entry if direction == "short" else 0.0,
            "max_favorable_r": 0.0,
            "adjust_count": 0,
        },
    }


def apply_exit_policy_to_position(
    position: dict[str, Any],
    *,
    direction: str,
    close: float,
    atr: float,
    params: dict | None,
    setup: str = "",
    signal_mode: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a position dict with policy-adjusted initial stop/target.

    In ``fixed_atr_rr`` profile, prices are intentionally left unchanged and
    only metadata is added, preserving the fixed ATR/RR bracket behavior.
    """
    params = params or {}
    pos = dict(position or {})
    policy = resolve_exit_policy(params, setup=setup, signal_mode=signal_mode)
    entry = _safe_float(pos.get("entry"), _safe_float(close, 0.0))
    stop = _safe_float(pos.get("stop_loss"), 0.0)
    target = _safe_float(pos.get("take_profit"), 0.0)
    shares = max(0, _safe_int(pos.get("shares"), 0))

    if policy.get("profile") in {EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE, EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2}:
        stop, target, risk = _reprice_with_policy(
            position=pos,
            direction=_safe_str(direction).lower(),
            entry=entry,
            atr=_safe_float(atr, 0.0),
            shares=shares,
            policy=policy,
            max_loss=_safe_float(params.get("max_loss_per_trade"), 150.0),
        )
        pos["stop_loss"] = stop
        pos["take_profit"] = target
        pos["rr"] = _safe_float(policy.get("tp_rr"), _safe_float(pos.get("rr"), 0.0))
    else:
        risk = abs(entry - stop) if entry > 0 and stop > 0 else 0.0

    atr_raw = _safe_float(params.get("atr_raw"), 0.0)
    if atr_raw <= 0:
        atr_mult = _safe_float(params.get("atr_multiplier"), 1.5)
        atr_raw = _safe_float(atr, 0.0) / atr_mult if atr_mult else _safe_float(atr, 0.0)
    pos["sl_dist_pct"] = (risk / entry * 100.0) if entry > 0 and risk > 0 else 0.0
    pos["sl_atr_ratio"] = (risk / atr_raw) if atr_raw > 0 and risk > 0 else 0.0

    metadata = build_exit_policy_metadata(
        position={**pos, "entry": entry},
        direction=_safe_str(direction).lower(),
        setup=setup,
        signal_mode=signal_mode,
        policy=policy,
        risk_r=risk,
        initial_stop_loss=stop,
        initial_take_profit=target,
    )
    pos["risk_r"] = metadata["risk_r"]
    pos["initial_stop_loss"] = metadata["initial_stop_loss"]
    pos["initial_take_profit"] = metadata["initial_take_profit"]
    pos["exit_policy"] = metadata["exit_policy"]
    return pos, metadata


__all__ = [
    "EXIT_POLICY_PROFILE_LEGACY",
    "EXIT_POLICY_PROFILE_FIXED_ATR_RR",
    "EXIT_POLICY_PROFILE_SETUP_AWARE",
    "EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE",
    "EXIT_POLICY_PROFILE_SIGNAL_MODE_ADAPTIVE_V2",
    "apply_exit_policy_to_position",
    "classify_exit_policy",
    "is_signal_mode_adaptive_exit_profile",
    "normalize_exit_policy_profile",
    "resolve_exit_policy",
]
