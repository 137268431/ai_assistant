from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS


def _runtime_service_module():
    from . import runtime_service as service_module

    return service_module


def parse_symbols(raw_symbols: str) -> list[str]:
    items = []
    for chunk in str(raw_symbols or "").replace("\n", ",").split(","):
        symbol = str(chunk or "").strip().upper()
        if not symbol or symbol in items:
            continue
        items.append(symbol)
    return items


def normalize_bool(raw_value: Any, default: bool = False) -> bool:
    if raw_value is None:
        return bool(default)
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    text = str(raw_value or "").strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def normalize_positive_int(raw_value: Any, default: int, minimum: int = 0, maximum: int = 2000) -> int:
    try:
        value = int(raw_value)
    except Exception:
        value = int(default)
    return max(minimum, min(maximum, value))


def normalize_hhmm(raw_value: Any, default: str | None = None) -> str:
    service_module = _runtime_service_module()
    default_value = str(default or service_module.DEFAULT_SCAN_CUTOFF_TIME)
    text = str(raw_value or "").strip()
    if ":" not in text:
        return default_value
    hour_text, minute_text = text.split(":", 1)
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        return default_value
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return default_value
    return f"{hour:02d}:{minute:02d}"


def normalize_positive_float(raw_value: Any, default: float, minimum: float = 0.0, maximum: float = 1000000000.0) -> float:
    try:
        value = float(raw_value)
    except Exception:
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


def normalize_strategy_params(raw_params: dict, base_params: dict | None = None) -> dict:
    params = dict(base_params or DEFAULT_PARAMS)
    allowed_keys = set(DEFAULT_PARAMS.keys())
    if isinstance(raw_params, dict):
        translated = dict(raw_params)

        if "rr_ratio" not in translated and "risk_reward_ratio" in translated:
            translated["rr_ratio"] = translated.get("risk_reward_ratio")
        if "rr_ratio" not in translated and "tp_atr_mult" in translated:
            try:
                sl_atr_mult = float(translated.get("sl_atr_mult", params.get("sl_atr_mult", DEFAULT_PARAMS["sl_atr_mult"])))
                tp_atr_mult = float(translated.get("tp_atr_mult"))
                if sl_atr_mult:
                    translated["rr_ratio"] = tp_atr_mult / sl_atr_mult
            except Exception:
                pass

        for key, value in translated.items():
            if key not in allowed_keys:
                continue
            default = DEFAULT_PARAMS[key]
            try:
                if isinstance(default, bool):
                    params[key] = bool(value)
                elif isinstance(default, int) and not isinstance(default, bool):
                    params[key] = int(value)
                elif isinstance(default, float):
                    params[key] = float(value)
                else:
                    params[key] = value
            except Exception:
                params[key] = default
    return params


def normalize_variants(raw_variants: list, base_params: dict, default_strategy_tag: str) -> list[dict]:
    service_module = _runtime_service_module()
    if not isinstance(raw_variants, list):
        return []

    variants = []
    for index, item in enumerate(raw_variants[:service_module.MAX_BATCH_VARIANTS], start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or f"Variant {index}").strip() or f"Variant {index}"
        raw_params = item.get("strategy_params")
        if raw_params is None:
            raw_params = item.get("params")
        if raw_params is None:
            raw_params = {
                key: value
                for key, value in item.items()
                if key not in {"label", "name", "strategy_params", "params", "strategy_tag"}
            }
        variants.append(
            {
                "label": label,
                "strategy_params": normalize_strategy_params(raw_params or {}, base_params=base_params),
                "strategy_tag": str(item.get("strategy_tag") or default_strategy_tag).strip() or default_strategy_tag,
            }
        )
    return variants


def build_variant_request(base_request: dict, variant: dict, variant_index: int) -> dict:
    request = deepcopy(base_request)
    request["variant_index"] = variant_index
    request["variant_label"] = str(variant.get("label") or f"Variant {variant_index}").strip() or f"Variant {variant_index}"
    request["strategy_tag"] = str(variant.get("strategy_tag") or base_request["strategy_tag"]).strip() or base_request["strategy_tag"]
    request["params"] = {
        "strategy_params": deepcopy(variant.get("strategy_params") or base_request["params"]["strategy_params"]),
        "strategy_tag": request["strategy_tag"],
        "source_environment": base_request["source_environment"],
        "session_mode": base_request["session_mode"],
    }
    request["name"] = f'{base_request["name"]} · {request["variant_label"]}'
    return request


def normalize_request(payload: dict) -> dict:
    service_module = _runtime_service_module()
    now = datetime.now(service_module.ET)
    name = str(payload.get("name") or "").strip() or f"Backtest {now.strftime('%Y-%m-%d %H:%M')}"
    source_environment = str(payload.get("source_environment") or "live").strip().lower() or "live"
    symbol_source = str(payload.get("symbol_source") or "manual").strip().lower() or "manual"
    if symbol_source not in service_module.SYMBOL_SOURCE_VALUES:
        symbol_source = "manual"

    symbols = parse_symbols(payload.get("symbols") or payload.get("symbols_text") or "")
    benchmark_symbol = str(payload.get("benchmark_symbol") or "SPY").strip().upper() or "SPY"
    date_from = str(payload.get("date_from") or now.strftime("%Y-%m-%d")).strip()
    date_to = str(payload.get("date_to") or date_from).strip()
    historical_targets_replay = symbol_source == "targets" and (
        date_from != now.strftime("%Y-%m-%d") or date_to != now.strftime("%Y-%m-%d")
    )
    effective_symbol_source = "daily_scan_replay" if historical_targets_replay else symbol_source
    session_mode = str(payload.get("session_mode") or "extended").strip().lower() or "extended"
    if session_mode not in service_module.SESSION_MODE_VALUES:
        session_mode = "extended"

    initial_capital = max(1000.0, float(payload.get("initial_capital") or 10000))
    commission_per_share = max(0.0, float(payload.get("commission_per_share") or 0.005))
    slippage_bps = max(0.0, float(payload.get("slippage_bps") or 2.0))
    force_flat_eod = True
    max_symbols = max(1, min(service_module.DEFAULT_MAX_SYMBOLS, int(payload.get("max_symbols") or service_module.DEFAULT_MAX_SYMBOLS)))
    execution_model = str(payload.get("execution_model") or "portfolio_stream").strip().lower() or "portfolio_stream"
    if execution_model not in service_module.EXECUTION_MODEL_VALUES:
        execution_model = "portfolio_stream"
    borrow_limit_mode = str(payload.get("borrow_limit_mode") or "").strip().lower()
    if not borrow_limit_mode:
        borrow_limit_mode = "account_buying_power" if execution_model == "portfolio_stream" else "none"
    if borrow_limit_mode not in service_module.BORROW_LIMIT_MODE_VALUES:
        borrow_limit_mode = "none"
    max_borrow_amount = normalize_positive_float(payload.get("max_borrow_amount"), 0.0)
    position_limit_max = normalize_positive_int(
        payload.get("position_limit_max"),
        default=service_module.DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX,
        minimum=1,
        maximum=100,
    )
    signal_validity_minutes = normalize_positive_int(
        payload.get("signal_validity_minutes"),
        default=service_module.DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES,
        minimum=1,
        maximum=390,
    )
    cooldown_bars_after_sl = normalize_positive_int(payload.get("cooldown_bars_after_sl"), default=6, minimum=0, maximum=390)
    cooldown_bars_after_reverse = normalize_positive_int(payload.get("cooldown_bars_after_reverse"), default=3, minimum=0, maximum=390)
    atr_dynamic_stop_enabled = normalize_bool(payload.get("atr_dynamic_stop_enabled"), True)
    atr_stop_min_profit_r = normalize_positive_float(payload.get("atr_stop_min_profit_r"), 0.3, minimum=0.0, maximum=10.0)
    atr_stop_deviation_threshold = normalize_positive_float(payload.get("atr_stop_deviation_threshold"), 0.30, minimum=0.0, maximum=10.0)
    atr_stop_min_change = normalize_positive_float(payload.get("atr_stop_min_change"), 0.01, minimum=0.0, maximum=100.0)
    trade_window_start_time = normalize_hhmm(
        payload.get("trade_window_start_time"),
        service_module.DEFAULT_PORTFOLIO_TRADE_WINDOW_START,
    )
    trade_window_end_time = normalize_hhmm(
        payload.get("trade_window_end_time"),
        service_module.DEFAULT_PORTFOLIO_TRADE_WINDOW_END,
    )
    order_window_end_time = normalize_hhmm(
        payload.get("order_window_end_time"),
        service_module.DEFAULT_PORTFOLIO_ORDER_WINDOW_END,
    )
    simultaneous_signal_priority = str(payload.get("simultaneous_signal_priority") or "daily_target_rank").strip().lower()
    if simultaneous_signal_priority not in service_module.SIGNAL_PRIORITY_VALUES:
        simultaneous_signal_priority = "daily_target_rank"
    manual_confirm_mode = str(payload.get("manual_confirm_mode") or "auto").strip().lower() or "auto"
    if manual_confirm_mode not in service_module.MANUAL_CONFIRM_MODE_VALUES:
        manual_confirm_mode = "auto"
    confirm_delay_minutes = normalize_positive_int(
        payload.get("confirm_delay_minutes"),
        default=0,
        minimum=0,
        maximum=390,
    )
    compare_with_tv = normalize_bool(payload.get("compare_with_tv"), True)
    compare_tv_signals = normalize_bool(payload.get("compare_tv_signals"), False)
    persist_backtest_indicators = normalize_bool(payload.get("persist_backtest_indicators"), True)
    warmup_bars = normalize_positive_int(
        payload.get("warmup_bars") or payload.get("preheat_bars"),
        default=service_module.BACKTEST_WARMUP_BARS,
        minimum=0,
        maximum=service_module.MAX_BACKTEST_WARMUP_BARS,
    )
    scan_warmup_bars = normalize_positive_int(
        payload.get("scan_warmup_bars") or payload.get("selection_warmup_bars"),
        default=warmup_bars,
        minimum=0,
        maximum=service_module.MAX_BACKTEST_WARMUP_BARS,
    )
    premarket_cutoff_time = normalize_hhmm(payload.get("premarket_cutoff_time") or payload.get("scan_cutoff_time"))
    scan_session_mode = str(payload.get("scan_session_mode") or "extended").strip().lower() or "extended"
    if scan_session_mode not in service_module.SESSION_MODE_VALUES:
        scan_session_mode = "extended"
    retention_limit = normalize_positive_int(
        payload.get("retention_limit"),
        default=service_module.DEFAULT_BACKTEST_RETENTION_LIMIT,
        minimum=1,
        maximum=service_module.MAX_BACKTEST_RETENTION_LIMIT,
    )
    raw_params = payload.get("strategy_params") or payload.get("params") or {}
    strategy_params = normalize_strategy_params(raw_params)
    if payload.get("signal_window_max_bars") not in (None, ""):
        strategy_params["signal_window_max_bars"] = normalize_positive_int(
            payload.get("signal_window_max_bars"),
            default=int(DEFAULT_PARAMS.get("signal_window_max_bars", 12)),
            minimum=0,
            maximum=390,
        )
    strategy_tag = str(payload.get("strategy_tag") or "IBKR_SAC_BACKTEST_V1").strip() or "IBKR_SAC_BACKTEST_V1"
    variants = normalize_variants(payload.get("variants") or [], strategy_params, strategy_tag)

    return {
        "name": name,
        "source_environment": source_environment,
        "symbol_source": effective_symbol_source,
        "requested_symbol_source": symbol_source,
        "historical_targets_replay": historical_targets_replay,
        "symbols": symbols,
        "symbols_text": ",".join(symbols),
        "benchmark_symbol": benchmark_symbol,
        "date_from": date_from,
        "date_to": date_to,
        "session_mode": session_mode,
        "scan_session_mode": scan_session_mode,
        "initial_capital": initial_capital,
        "commission_per_share": commission_per_share,
        "slippage_bps": slippage_bps,
        "force_flat_eod": force_flat_eod,
        "max_symbols": max_symbols,
        "execution_model": execution_model,
        "borrow_limit_mode": borrow_limit_mode,
        "max_borrow_amount": max_borrow_amount,
        "position_limit_max": position_limit_max,
        "signal_validity_minutes": signal_validity_minutes,
        "cooldown_bars_after_sl": cooldown_bars_after_sl,
        "cooldown_bars_after_reverse": cooldown_bars_after_reverse,
        "atr_dynamic_stop_enabled": atr_dynamic_stop_enabled,
        "atr_stop_min_profit_r": atr_stop_min_profit_r,
        "atr_stop_deviation_threshold": atr_stop_deviation_threshold,
        "atr_stop_min_change": atr_stop_min_change,
        "trade_window_start_time": trade_window_start_time,
        "trade_window_end_time": trade_window_end_time,
        "order_window_end_time": order_window_end_time,
        "simultaneous_signal_priority": simultaneous_signal_priority,
        "manual_confirm_mode": manual_confirm_mode,
        "confirm_delay_minutes": confirm_delay_minutes,
        "compare_with_tv": compare_with_tv,
        "compare_tv_signals": compare_tv_signals,
        "persist_backtest_indicators": persist_backtest_indicators,
        "warmup_bars": warmup_bars,
        "scan_warmup_bars": scan_warmup_bars,
        "premarket_cutoff_time": premarket_cutoff_time,
        "retention_limit": retention_limit,
        "params": {
            "strategy_params": strategy_params,
            "strategy_tag": strategy_tag,
            "source_environment": source_environment,
            "session_mode": session_mode,
            "warmup_bars": warmup_bars,
            "scan_warmup_bars": scan_warmup_bars,
            "premarket_cutoff_time": premarket_cutoff_time,
            "persist_backtest_indicators": persist_backtest_indicators,
            "requested_symbol_source": symbol_source,
            "historical_targets_replay": historical_targets_replay,
            "execution_model": execution_model,
            "borrow_limit_mode": borrow_limit_mode,
            "max_borrow_amount": max_borrow_amount,
            "cooldown_bars_after_sl": cooldown_bars_after_sl,
            "cooldown_bars_after_reverse": cooldown_bars_after_reverse,
            "atr_dynamic_stop_enabled": atr_dynamic_stop_enabled,
            "atr_stop_min_profit_r": atr_stop_min_profit_r,
            "atr_stop_deviation_threshold": atr_stop_deviation_threshold,
            "atr_stop_min_change": atr_stop_min_change,
            "position_limit_max": position_limit_max,
            "signal_validity_minutes": signal_validity_minutes,
            "trade_window_start_time": trade_window_start_time,
            "trade_window_end_time": trade_window_end_time,
            "order_window_end_time": order_window_end_time,
            "simultaneous_signal_priority": simultaneous_signal_priority,
            "manual_confirm_mode": manual_confirm_mode,
            "confirm_delay_minutes": confirm_delay_minutes,
        },
        "variants": variants,
        "strategy_tag": strategy_tag,
    }
