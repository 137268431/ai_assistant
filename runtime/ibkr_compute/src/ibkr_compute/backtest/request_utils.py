from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from ibkr_compute.core.broker_mode import configured_broker_mode, normalize_broker_mode, resolve_data_environment
from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS
from ibkr_compute.backtest import constants
from ibkr_compute.backtest.delta_proxy import (
    DEFAULT_BACKTEST_DELTA_PROXY_THRESHOLD,
    normalize_backtest_delta_mode,
    normalize_backtest_delta_proxy_threshold,
)


def parse_symbols(raw_symbols: Any) -> list[str]:
    items = []

    def consume(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                consume(item)
            return
        for chunk in str(value or "").replace("\n", ",").split(","):
            symbol = str(chunk or "").strip().upper()
            if not symbol or symbol in items:
                continue
            items.append(symbol)

    consume(raw_symbols)
    return items


def merge_symbols(*symbol_groups: Any) -> list[str]:
    merged = []
    for group in symbol_groups:
        for symbol in parse_symbols(group):
            if symbol not in merged:
                merged.append(symbol)
    return merged


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
    default_value = str(default or constants.DEFAULT_SCAN_CUTOFF_TIME)
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
    if not isinstance(raw_variants, list):
        return []

    variants = []
    for index, item in enumerate(raw_variants[:constants.MAX_BATCH_VARIANTS], start=1):
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
        "broker_mode": base_request.get("broker_mode") or configured_broker_mode(),
        "session_mode": base_request["session_mode"],
    }
    for key in (
        "account_model_mode",
        "fee_model",
        "slippage_model",
        "execution_cost_profile",
        "include_regulatory_fees",
        "slippage_cap_to_bar",
        "limit_price_protection",
        "stop_gap_to_open",
        "backtest_delta_mode",
        "backtest_delta_proxy_threshold",
        "backtest_require_truth_proof",
    ):
        if key in base_request.get("params", {}):
            request["params"][key] = deepcopy(base_request["params"][key])
    request["name"] = f'{base_request["name"]} · {request["variant_label"]}'
    return request


def normalize_request(payload: dict) -> dict:
    now = datetime.now(constants.ET)
    latest_complete_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    name = str(payload.get("name") or "").strip() or f"Backtest {now.strftime('%Y-%m-%d %H:%M')}"
    source_environment = resolve_data_environment(
        payload.get("market_data_mode")
        or payload.get("data_environment")
        or payload.get("source_environment")
        or payload.get("environment")
        or "live"
    )
    broker_mode = normalize_broker_mode(
        payload.get("broker_mode"),
        configured_broker_mode(),
    )
    machine_profile = str(payload.get("machine_profile") or payload.get("resource_profile") or "").strip().lower()
    symbol_source = str(payload.get("symbol_source") or "manual").strip().lower() or "manual"
    if symbol_source not in constants.SYMBOL_SOURCE_VALUES:
        symbol_source = "manual"

    benchmark_symbol = str(payload.get("benchmark_symbol") or "SPY").strip().upper() or "SPY"
    allow_market_context_symbols = normalize_bool(
        payload.get("allow_market_context_symbols") or payload.get("allow_market_context_trading"),
        False,
    )
    exclude_market_monitors = not allow_market_context_symbols and normalize_bool(
        payload.get("exclude_market_monitors"),
        True,
    )
    default_excludes = merge_symbols(
        constants.DEFAULT_FIXED_SYMBOLS,
        constants.DEFAULT_MARKET_MONITOR_SYMBOLS if exclude_market_monitors else (),
    )
    exclude_symbols = merge_symbols(default_excludes, payload.get("exclude_symbols"), payload.get("exclude_symbols_text"))
    exclude_set = set(exclude_symbols)
    symbols = [
        symbol
        for symbol in parse_symbols(payload.get("symbols") or payload.get("symbols_text") or "")
        if symbol not in exclude_set
    ]
    date_from = str(payload.get("date_from") or latest_complete_date).strip()
    date_to = str(payload.get("date_to") or date_from).strip()
    clamped_date_to = False
    if date_to > latest_complete_date:
        date_to = latest_complete_date
        clamped_date_to = True
    if date_from > date_to:
        date_from = date_to
    historical_targets_replay = symbol_source == "targets" and (
        date_from != now.strftime("%Y-%m-%d") or date_to != now.strftime("%Y-%m-%d")
    )
    effective_symbol_source = "daily_scan_replay" if historical_targets_replay else symbol_source
    session_mode = str(payload.get("session_mode") or "extended").strip().lower() or "extended"
    if session_mode not in constants.SESSION_MODE_VALUES:
        session_mode = "extended"

    initial_capital = max(1000.0, float(payload.get("initial_capital") or 10000))
    commission_per_share = max(0.0, float(payload.get("commission_per_share") if payload.get("commission_per_share") not in (None, "") else constants.DEFAULT_COMMISSION_PER_SHARE))
    slippage_bps = max(0.0, float(payload.get("slippage_bps") if payload.get("slippage_bps") not in (None, "") else constants.DEFAULT_SLIPPAGE_BPS))
    account_model_mode = str(payload.get("account_model_mode") or constants.DEFAULT_ACCOUNT_MODEL_MODE).strip().lower()
    if account_model_mode not in constants.ACCOUNT_MODEL_MODE_VALUES:
        account_model_mode = constants.DEFAULT_ACCOUNT_MODEL_MODE
    fee_model = str(payload.get("fee_model") or constants.DEFAULT_FEE_MODEL).strip().lower()
    if fee_model not in constants.FEE_MODEL_VALUES:
        fee_model = constants.DEFAULT_FEE_MODEL
    slippage_model = str(payload.get("slippage_model") or constants.DEFAULT_SLIPPAGE_MODEL).strip().lower()
    if slippage_model not in constants.SLIPPAGE_MODEL_VALUES:
        slippage_model = constants.DEFAULT_SLIPPAGE_MODEL
    execution_cost_profile = payload.get("execution_cost_profile") if payload.get("execution_cost_profile") is not None else {}
    include_regulatory_fees = normalize_bool(payload.get("include_regulatory_fees"), False)
    slippage_cap_to_bar = normalize_bool(payload.get("slippage_cap_to_bar"), slippage_model in {"bar_capped_bps_v1", "volume_share_v1"})
    limit_price_protection = normalize_bool(payload.get("limit_price_protection"), slippage_model in {"bar_capped_bps_v1", "volume_share_v1"})
    stop_gap_to_open = normalize_bool(payload.get("stop_gap_to_open"), slippage_model in {"bar_capped_bps_v1", "volume_share_v1"})
    force_flat_eod = True
    if effective_symbol_source == "daily_scan_replay":
        default_max_symbols = 0
    elif effective_symbol_source == "watchlist" and not symbols:
        default_max_symbols = constants.DEFAULT_WATCHLIST_MAX_SYMBOLS
    elif symbols:
        default_max_symbols = min(constants.MAX_BACKTEST_SYMBOLS, max(constants.DEFAULT_MAX_SYMBOLS, len(symbols)))
    else:
        default_max_symbols = constants.DEFAULT_MAX_SYMBOLS
    max_symbols = normalize_positive_int(
        payload.get("max_symbols"),
        default=default_max_symbols,
        minimum=0,
        maximum=constants.MAX_BACKTEST_SYMBOLS,
    )
    execution_model = str(payload.get("execution_model") or "portfolio_stream").strip().lower() or "portfolio_stream"
    if execution_model not in constants.EXECUTION_MODEL_VALUES:
        execution_model = "portfolio_stream"
    borrow_limit_mode = str(payload.get("borrow_limit_mode") or "").strip().lower()
    if not borrow_limit_mode:
        borrow_limit_mode = "account_buying_power" if execution_model == "portfolio_stream" else "none"
    if borrow_limit_mode not in constants.BORROW_LIMIT_MODE_VALUES:
        borrow_limit_mode = "none"
    max_borrow_amount = normalize_positive_float(payload.get("max_borrow_amount"), 0.0)
    position_limit_max = normalize_positive_int(
        payload.get("position_limit_max"),
        default=constants.DEFAULT_PORTFOLIO_POSITION_LIMIT_MAX,
        minimum=0,
        maximum=100,
    )
    max_strategy_open_positions = normalize_positive_int(
        payload.get("max_strategy_open_positions"),
        default=constants.DEFAULT_MAX_STRATEGY_OPEN_POSITIONS,
        minimum=0,
        maximum=100,
    )
    consecutive_stop_loss_limit = normalize_positive_int(
        payload.get("consecutive_stop_loss_limit"),
        default=constants.DEFAULT_PORTFOLIO_CONSECUTIVE_STOP_LOSS_LIMIT,
        minimum=1,
        maximum=20,
    )
    target_pool_defaults = effective_symbol_source == "daily_scan_replay"
    portfolio_require_target_direction_alignment = normalize_bool(
        payload.get("portfolio_require_target_direction_alignment"),
        target_pool_defaults,
    )
    portfolio_use_target_strategy_policy = normalize_bool(
        payload.get("portfolio_use_target_strategy_policy"),
        False,
    )
    portfolio_max_target_rank = normalize_positive_int(
        payload.get("portfolio_max_target_rank"),
        default=12 if target_pool_defaults else 0,
        minimum=0,
        maximum=constants.MAX_BACKTEST_SYMBOLS,
    )
    portfolio_min_target_score = normalize_positive_float(
        payload.get("portfolio_min_target_score"),
        default=10.0 if target_pool_defaults else 0.0,
        minimum=0.0,
        maximum=1000000.0,
    )
    portfolio_block_mr_overextended_state = normalize_bool(
        payload.get("portfolio_block_mr_overextended_state"),
        False,
    )
    portfolio_block_early_trend_without_ema_touch = normalize_bool(
        payload.get("portfolio_block_early_trend_without_ema_touch"),
        False,
    )
    signal_validity_minutes = normalize_positive_int(
        payload.get("signal_validity_minutes"),
        default=constants.DEFAULT_PORTFOLIO_SIGNAL_VALIDITY_MINUTES,
        minimum=1,
        maximum=390,
    )
    cooldown_bars_after_sl = normalize_positive_int(payload.get("cooldown_bars_after_sl"), default=6, minimum=0, maximum=390)
    cooldown_bars_after_reverse = normalize_positive_int(payload.get("cooldown_bars_after_reverse"), default=3, minimum=0, maximum=390)
    atr_dynamic_stop_enabled = normalize_bool(payload.get("atr_dynamic_stop_enabled"), True)
    atr_stop_min_profit_r = normalize_positive_float(payload.get("atr_stop_min_profit_r"), 0.3, minimum=0.0, maximum=10.0)
    atr_stop_deviation_threshold = normalize_positive_float(payload.get("atr_stop_deviation_threshold"), 0.30, minimum=0.0, maximum=10.0)
    atr_stop_min_change = normalize_positive_float(payload.get("atr_stop_min_change"), 0.01, minimum=0.0, maximum=100.0)
    intraday_harvest_enabled = normalize_bool(payload.get("intraday_harvest_enabled"), False)
    intraday_harvest_settings = (
        deepcopy(payload.get("intraday_harvest_settings"))
        if isinstance(payload.get("intraday_harvest_settings"), dict)
        else {}
    )
    trade_window_start_time = normalize_hhmm(
        payload.get("trade_window_start_time"),
        constants.DEFAULT_PORTFOLIO_TRADE_WINDOW_START,
    )
    trade_window_end_time = normalize_hhmm(
        payload.get("trade_window_end_time"),
        constants.DEFAULT_PORTFOLIO_TRADE_WINDOW_END,
    )
    order_window_end_time = normalize_hhmm(
        payload.get("order_window_end_time"),
        constants.DEFAULT_PORTFOLIO_ORDER_WINDOW_END,
    )
    simultaneous_signal_priority = str(payload.get("simultaneous_signal_priority") or "daily_target_rank").strip().lower()
    if simultaneous_signal_priority not in constants.SIGNAL_PRIORITY_VALUES:
        simultaneous_signal_priority = "daily_target_rank"
    manual_confirm_mode = str(payload.get("manual_confirm_mode") or "auto").strip().lower() or "auto"
    if manual_confirm_mode not in constants.MANUAL_CONFIRM_MODE_VALUES:
        manual_confirm_mode = "auto"
    confirm_delay_minutes = normalize_positive_int(
        payload.get("confirm_delay_minutes"),
        default=0,
        minimum=0,
        maximum=390,
    )
    compare_with_tv = normalize_bool(payload.get("compare_with_tv"), True)
    compare_tv_signals = normalize_bool(payload.get("compare_tv_signals"), False)
    persist_backtest_indicators = normalize_bool(payload.get("persist_backtest_indicators"), False)
    backtest_delta_mode = normalize_backtest_delta_mode(payload.get("backtest_delta_mode"))
    backtest_delta_proxy_threshold = normalize_backtest_delta_proxy_threshold(
        payload.get("backtest_delta_proxy_threshold")
        if payload.get("backtest_delta_proxy_threshold") not in (None, "")
        else DEFAULT_BACKTEST_DELTA_PROXY_THRESHOLD
    )
    warmup_bars = normalize_positive_int(
        payload.get("warmup_bars") or payload.get("preheat_bars"),
        default=constants.BACKTEST_WARMUP_BARS,
        minimum=0,
        maximum=constants.MAX_BACKTEST_WARMUP_BARS,
    )
    scan_warmup_bars = normalize_positive_int(
        payload.get("scan_warmup_bars") or payload.get("selection_warmup_bars"),
        default=warmup_bars,
        minimum=0,
        maximum=constants.MAX_BACKTEST_WARMUP_BARS,
    )
    force_daily_selected_only = bool(
        effective_symbol_source == "daily_scan_replay"
        and execution_model == "portfolio_stream"
    )
    daily_selected_only = (
        True
        if force_daily_selected_only
        else normalize_bool(payload.get("daily_selected_only"), False)
    )
    daily_selection_require_sd_trigger = normalize_bool(payload.get("daily_selection_require_sd_trigger"), False)
    daily_selection_reuse_live_admission = normalize_bool(payload.get("daily_selection_reuse_live_admission"), daily_selection_require_sd_trigger)
    daily_selection_sd_mode = str(payload.get("daily_selection_sd_mode") or "hard").strip().lower() or "hard"
    if daily_selection_sd_mode not in {"hard", "rank", "off"}:
        daily_selection_sd_mode = "hard"
    daily_selection_candidate_limit = normalize_positive_int(
        payload.get("daily_selection_candidate_limit"),
        default=max(20, max_symbols * 5) if max_symbols > 0 else 0,
        minimum=0,
        maximum=constants.MAX_BACKTEST_SYMBOLS,
    )
    default_daily_selection_cache = bool(
        effective_symbol_source == "daily_scan_replay"
        and execution_model == "portfolio_stream"
        and daily_selected_only
    )
    daily_selection_cache_enabled = normalize_bool(
        payload.get("daily_selection_cache_enabled"),
        default_daily_selection_cache,
    )
    daily_selection_cache_mode = str(payload.get("daily_selection_cache_mode") or "use_or_build").strip().lower() or "use_or_build"
    if daily_selection_cache_mode not in {"use_or_build", "read_only", "bypass"}:
        daily_selection_cache_mode = "use_or_build"
    daily_selection_cache_force_rebuild = normalize_bool(payload.get("daily_selection_cache_force_rebuild"), False)
    daily_selection_cache_trust_existing = normalize_bool(payload.get("daily_selection_cache_trust_existing"), False)
    daily_selection_cache_revalidate_input_hash = normalize_bool(payload.get("daily_selection_cache_revalidate_input_hash"), False)
    backtest_require_truth_proof = normalize_bool(
        payload.get("backtest_require_truth_proof")
        if payload.get("backtest_require_truth_proof") is not None
        else payload.get("data_correctness_guard_enabled"),
        True,
    )
    daily_scan_min_avg_10d_volume = normalize_positive_float(
        payload.get("daily_scan_min_avg_10d_volume"),
        0.0,
        minimum=0.0,
        maximum=1000000000.0,
    )
    daily_scan_min_premarket_volume = normalize_positive_float(
        payload.get("daily_scan_min_premarket_volume"),
        0.0,
        minimum=0.0,
        maximum=1000000000.0,
    )
    daily_scan_min_atr_pct = normalize_positive_float(
        payload.get("daily_scan_min_atr_pct"),
        0.0,
        minimum=0.0,
        maximum=100.0,
    )
    daily_scan_min_abs_day_change_pct = normalize_positive_float(
        payload.get("daily_scan_min_abs_day_change_pct"),
        0.0,
        minimum=0.0,
        maximum=100.0,
    )
    premarket_cutoff_time = normalize_hhmm(payload.get("premarket_cutoff_time") or payload.get("scan_cutoff_time"))
    scan_session_mode = str(payload.get("scan_session_mode") or "extended").strip().lower() or "extended"
    if scan_session_mode not in constants.SESSION_MODE_VALUES:
        scan_session_mode = "extended"
    retention_limit = normalize_positive_int(
        payload.get("retention_limit"),
        default=constants.DEFAULT_BACKTEST_RETENTION_LIMIT,
        minimum=1,
        maximum=constants.MAX_BACKTEST_RETENTION_LIMIT,
    )
    preflight_backfill = normalize_bool(payload.get("preflight_backfill"), True)
    backfill_concurrency = normalize_positive_int(
        payload.get("backfill_concurrency"),
        default=constants.DEFAULT_BACKTEST_BACKFILL_CONCURRENCY,
        minimum=1,
        maximum=constants.MAX_BACKTEST_BACKFILL_CONCURRENCY,
    )
    backfill_symbol_timeout_s = normalize_positive_int(
        payload.get("backfill_symbol_timeout_s") or payload.get("backfill_symbol_timeout_seconds"),
        default=constants.DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
        minimum=30,
        maximum=constants.MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
    )
    backfill_max_batches = normalize_positive_int(
        payload.get("backfill_max_batches"),
        default=constants.DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES,
        minimum=1,
        maximum=constants.MAX_BACKTEST_BACKFILL_MAX_BATCHES,
    )
    backfill_history_timeout_s = normalize_positive_int(
        payload.get("backfill_history_timeout_s") or payload.get("backfill_history_timeout_seconds"),
        default=constants.DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
        minimum=5,
        maximum=constants.MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
    )
    backfill_history_max_retries = normalize_positive_int(
        payload.get("backfill_history_max_retries"),
        default=constants.DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
        minimum=0,
        maximum=constants.MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
    )
    resource_guard_enabled = normalize_bool(
        payload.get("resource_guard_enabled"),
        constants.DEFAULT_BACKTEST_RESOURCE_GUARD_ENABLED,
    )
    resource_guard_max_load = normalize_positive_float(
        payload.get("resource_guard_max_load"),
        constants.DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_LOAD,
        minimum=0.0,
        maximum=128.0,
    )
    resource_guard_min_available_mb = normalize_positive_int(
        payload.get("resource_guard_min_available_mb"),
        default=constants.DEFAULT_BACKTEST_RESOURCE_GUARD_MIN_AVAILABLE_MB,
        minimum=0,
        maximum=65536,
    )
    resource_guard_max_rss_mb = normalize_positive_int(
        payload.get("resource_guard_max_rss_mb"),
        default=constants.DEFAULT_BACKTEST_RESOURCE_GUARD_MAX_RSS_MB,
        minimum=0,
        maximum=65536,
    )
    resource_guard_sleep_s = normalize_positive_float(
        payload.get("resource_guard_sleep_s") or payload.get("resource_guard_sleep_seconds"),
        constants.DEFAULT_BACKTEST_RESOURCE_GUARD_SLEEP_SECONDS,
        minimum=0.0,
        maximum=30.0,
    )
    resource_guard_check_steps = normalize_positive_int(
        payload.get("resource_guard_check_steps"),
        default=constants.DEFAULT_BACKTEST_RESOURCE_GUARD_CHECK_STEPS,
        minimum=1,
        maximum=10000,
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
        "broker_mode": broker_mode,
        "machine_profile": machine_profile,
        "symbol_source": effective_symbol_source,
        "requested_symbol_source": symbol_source,
        "historical_targets_replay": historical_targets_replay,
        "symbols": symbols,
        "symbols_text": ",".join(symbols),
        "exclude_symbols": exclude_symbols,
        "exclude_symbols_text": ",".join(exclude_symbols),
        "exclude_market_monitors": exclude_market_monitors,
        "allow_market_context_symbols": allow_market_context_symbols,
        "benchmark_symbol": benchmark_symbol,
        "date_from": date_from,
        "date_to": date_to,
        "latest_complete_date": latest_complete_date,
        "date_to_clamped": clamped_date_to,
        "session_mode": session_mode,
        "scan_session_mode": scan_session_mode,
        "initial_capital": initial_capital,
        "commission_per_share": commission_per_share,
        "slippage_bps": slippage_bps,
        "account_model_mode": account_model_mode,
        "fee_model": fee_model,
        "slippage_model": slippage_model,
        "execution_cost_profile": execution_cost_profile,
        "include_regulatory_fees": include_regulatory_fees,
        "slippage_cap_to_bar": slippage_cap_to_bar,
        "limit_price_protection": limit_price_protection,
        "stop_gap_to_open": stop_gap_to_open,
        "force_flat_eod": force_flat_eod,
        "max_symbols": max_symbols,
        "execution_model": execution_model,
        "borrow_limit_mode": borrow_limit_mode,
        "max_borrow_amount": max_borrow_amount,
        "position_limit_max": position_limit_max,
        "max_strategy_open_positions": max_strategy_open_positions,
        "consecutive_stop_loss_limit": consecutive_stop_loss_limit,
        "portfolio_require_target_direction_alignment": portfolio_require_target_direction_alignment,
        "portfolio_use_target_strategy_policy": portfolio_use_target_strategy_policy,
        "portfolio_max_target_rank": portfolio_max_target_rank,
        "portfolio_min_target_score": portfolio_min_target_score,
        "portfolio_block_mr_overextended_state": portfolio_block_mr_overextended_state,
        "portfolio_block_early_trend_without_ema_touch": portfolio_block_early_trend_without_ema_touch,
        "signal_validity_minutes": signal_validity_minutes,
        "cooldown_bars_after_sl": cooldown_bars_after_sl,
        "cooldown_bars_after_reverse": cooldown_bars_after_reverse,
        "atr_dynamic_stop_enabled": atr_dynamic_stop_enabled,
        "atr_stop_min_profit_r": atr_stop_min_profit_r,
        "atr_stop_deviation_threshold": atr_stop_deviation_threshold,
        "atr_stop_min_change": atr_stop_min_change,
        "intraday_harvest_enabled": intraday_harvest_enabled,
        "intraday_harvest_settings": intraday_harvest_settings,
        "trade_window_start_time": trade_window_start_time,
        "trade_window_end_time": trade_window_end_time,
        "order_window_end_time": order_window_end_time,
        "simultaneous_signal_priority": simultaneous_signal_priority,
        "manual_confirm_mode": manual_confirm_mode,
        "confirm_delay_minutes": confirm_delay_minutes,
        "compare_with_tv": compare_with_tv,
        "compare_tv_signals": compare_tv_signals,
        "persist_backtest_indicators": persist_backtest_indicators,
        "backtest_delta_mode": backtest_delta_mode,
        "backtest_delta_proxy_threshold": backtest_delta_proxy_threshold,
        "warmup_bars": warmup_bars,
        "scan_warmup_bars": scan_warmup_bars,
        "daily_selected_only": daily_selected_only,
        "daily_selection_require_sd_trigger": daily_selection_require_sd_trigger,
        "daily_selection_reuse_live_admission": daily_selection_reuse_live_admission,
        "daily_selection_sd_mode": daily_selection_sd_mode,
        "daily_selection_candidate_limit": daily_selection_candidate_limit,
        "daily_selection_cache_enabled": daily_selection_cache_enabled,
        "daily_selection_cache_mode": daily_selection_cache_mode,
        "daily_selection_cache_force_rebuild": daily_selection_cache_force_rebuild,
        "daily_selection_cache_trust_existing": daily_selection_cache_trust_existing,
        "daily_selection_cache_revalidate_input_hash": daily_selection_cache_revalidate_input_hash,
        "backtest_require_truth_proof": backtest_require_truth_proof,
        "daily_scan_min_avg_10d_volume": daily_scan_min_avg_10d_volume,
        "daily_scan_min_premarket_volume": daily_scan_min_premarket_volume,
        "daily_scan_min_atr_pct": daily_scan_min_atr_pct,
        "daily_scan_min_abs_day_change_pct": daily_scan_min_abs_day_change_pct,
        "premarket_cutoff_time": premarket_cutoff_time,
        "retention_limit": retention_limit,
        "preflight_backfill": preflight_backfill,
        "backfill_concurrency": backfill_concurrency,
        "backfill_symbol_timeout_s": backfill_symbol_timeout_s,
        "backfill_max_batches": backfill_max_batches,
        "backfill_history_timeout_s": backfill_history_timeout_s,
        "backfill_history_max_retries": backfill_history_max_retries,
        "resource_guard_enabled": resource_guard_enabled,
        "resource_guard_max_load": resource_guard_max_load,
        "resource_guard_min_available_mb": resource_guard_min_available_mb,
        "resource_guard_max_rss_mb": resource_guard_max_rss_mb,
        "resource_guard_sleep_s": resource_guard_sleep_s,
        "resource_guard_check_steps": resource_guard_check_steps,
        "params": {
            "strategy_params": strategy_params,
            "strategy_tag": strategy_tag,
            "source_environment": source_environment,
            "broker_mode": broker_mode,
            "machine_profile": machine_profile,
            "session_mode": session_mode,
            "account_model_mode": account_model_mode,
            "fee_model": fee_model,
            "slippage_model": slippage_model,
            "execution_cost_profile": execution_cost_profile,
            "include_regulatory_fees": include_regulatory_fees,
            "slippage_cap_to_bar": slippage_cap_to_bar,
            "limit_price_protection": limit_price_protection,
            "stop_gap_to_open": stop_gap_to_open,
            "warmup_bars": warmup_bars,
            "scan_warmup_bars": scan_warmup_bars,
            "premarket_cutoff_time": premarket_cutoff_time,
            "persist_backtest_indicators": persist_backtest_indicators,
            "backtest_delta_mode": backtest_delta_mode,
            "backtest_delta_proxy_threshold": backtest_delta_proxy_threshold,
            "daily_selected_only": daily_selected_only,
            "daily_selection_require_sd_trigger": daily_selection_require_sd_trigger,
            "daily_selection_reuse_live_admission": daily_selection_reuse_live_admission,
            "daily_selection_sd_mode": daily_selection_sd_mode,
            "daily_selection_candidate_limit": daily_selection_candidate_limit,
            "daily_selection_cache_enabled": daily_selection_cache_enabled,
            "daily_selection_cache_mode": daily_selection_cache_mode,
            "daily_selection_cache_force_rebuild": daily_selection_cache_force_rebuild,
            "daily_selection_cache_trust_existing": daily_selection_cache_trust_existing,
            "daily_selection_cache_revalidate_input_hash": daily_selection_cache_revalidate_input_hash,
            "backtest_require_truth_proof": backtest_require_truth_proof,
            "daily_scan_min_avg_10d_volume": daily_scan_min_avg_10d_volume,
            "daily_scan_min_premarket_volume": daily_scan_min_premarket_volume,
            "daily_scan_min_atr_pct": daily_scan_min_atr_pct,
            "daily_scan_min_abs_day_change_pct": daily_scan_min_abs_day_change_pct,
            "preflight_backfill": preflight_backfill,
            "backfill_concurrency": backfill_concurrency,
            "backfill_symbol_timeout_s": backfill_symbol_timeout_s,
            "backfill_max_batches": backfill_max_batches,
            "backfill_history_timeout_s": backfill_history_timeout_s,
            "backfill_history_max_retries": backfill_history_max_retries,
            "resource_guard_enabled": resource_guard_enabled,
            "resource_guard_max_load": resource_guard_max_load,
            "resource_guard_min_available_mb": resource_guard_min_available_mb,
            "resource_guard_max_rss_mb": resource_guard_max_rss_mb,
            "resource_guard_sleep_s": resource_guard_sleep_s,
            "resource_guard_check_steps": resource_guard_check_steps,
            "requested_symbol_source": symbol_source,
            "historical_targets_replay": historical_targets_replay,
            "exclude_symbols": exclude_symbols,
            "exclude_market_monitors": exclude_market_monitors,
            "allow_market_context_symbols": allow_market_context_symbols,
            "execution_model": execution_model,
            "latest_complete_date": latest_complete_date,
            "date_to_clamped": clamped_date_to,
            "borrow_limit_mode": borrow_limit_mode,
            "max_borrow_amount": max_borrow_amount,
            "cooldown_bars_after_sl": cooldown_bars_after_sl,
            "cooldown_bars_after_reverse": cooldown_bars_after_reverse,
            "atr_dynamic_stop_enabled": atr_dynamic_stop_enabled,
            "atr_stop_min_profit_r": atr_stop_min_profit_r,
            "atr_stop_deviation_threshold": atr_stop_deviation_threshold,
            "atr_stop_min_change": atr_stop_min_change,
            "intraday_harvest_enabled": intraday_harvest_enabled,
            "intraday_harvest_settings": intraday_harvest_settings,
            "position_limit_max": position_limit_max,
            "max_strategy_open_positions": max_strategy_open_positions,
            "consecutive_stop_loss_limit": consecutive_stop_loss_limit,
            "portfolio_require_target_direction_alignment": portfolio_require_target_direction_alignment,
            "portfolio_use_target_strategy_policy": portfolio_use_target_strategy_policy,
            "portfolio_max_target_rank": portfolio_max_target_rank,
            "portfolio_min_target_score": portfolio_min_target_score,
            "portfolio_block_mr_overextended_state": portfolio_block_mr_overextended_state,
            "portfolio_block_early_trend_without_ema_touch": portfolio_block_early_trend_without_ema_touch,
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
