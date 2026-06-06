from __future__ import annotations

import re
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "runtime" / "ibkr_compute" / "src"


ORDER_FLOW_DEFAULTS = {
    "ibkr_order_flow_enabled": "false",
    "ibkr_order_flow_mode": "enforce",
    "ibkr_order_flow_active_limit": "3",
    "ibkr_order_flow_execution_pool_size": "3",
    "ibkr_order_flow_max_position_slots": "1",
    "ibkr_order_flow_tick_types": "Last",
    "ibkr_order_flow_confirm_window_sec": "60",
    "ibkr_order_flow_tbt_freshness_sec": "120",
    "ibkr_order_flow_min_delta_ratio": "0.12",
    "ibkr_order_flow_max_spread_bps": "12",
    "ibkr_order_flow_auto_entry_enabled": "true",
    "ibkr_order_flow_auto_exit_enabled": "true",
    "ibkr_order_flow_stop_tighten_enabled": "true",
    "ibkr_order_flow_entry_timeout_sec": "60",
    "ibkr_order_flow_exit_poll_sec": "2",
    "ibkr_order_flow_marketable_limit_bps": "8",
    "ibkr_order_flow_exit_delta_ratio": "0.18",
    "ibkr_order_flow_stop_delta_ratio": "0.12",
    "ibkr_order_flow_close_fill_timeout_sec": "5",
    "candidate_queue_max": "10",
    "candidate_breakout_ttl_sec": "120",
    "candidate_pullback_ttl_sec": "300",
    "candidate_reversal_ttl_sec": "600",
    "quality_auto_full_min": "80",
    "quality_auto_small_min": "75",
    "quality_shadow_min": "70",
    "entry_breakout_order_timeout_sec": "15",
    "entry_pullback_order_timeout_sec": "90",
    "entry_watch_after_fill_sec": "180",
    "partial_take_profit_r": "1.0",
    "partial_take_profit_fraction": "0.6",
    "breakeven_trigger_r": "0.6",
    "runner_enabled": "true",
    "runner_fraction": "0.4",
    "mean_reversion_runner_enabled": "false",
    "breakout_runner_enabled": "true",
    "trend_pullback_runner_enabled": "true",
    "new_entry_cutoff_time": "14:45",
    "force_flat_time": "15:45",
    "never_widen_stop_by_order_flow": "true",
    "cvd_flip_exit_enabled": "true",
    "cvd_divergence_take_profit_enabled": "true",
}

TV_PRIMARY_DEFAULTS = {
    "ibkr_require_target_direction_alignment": "false",
    "max_strategy_open_positions": "0",
    "intraday_symbol_daily_entry_limit": "3",
    "position_limit_max": "36",
    "entry_pre_submit_temp_subscription_limit": "8",
    "tv_max_active_targets": "100",
    "tv_max_same_direction_targets": "0",
    "tv_entry_requires_active_target": "false",
    "tv_entry_requires_authorized_symbol": "true",
    "tv_primary_trade_universe_symbols": "",
    "tv_webhook_async_route_enabled": "true",
    "tv_quality_window_rank_enforce_enabled": "false",
    "tv_risk_update_seq_guard_enabled": "true",
    "tv_risk_update_require_monotonic_seq": "true",
    "tv_risk_update_never_widen_stop": "true",
    "tv_risk_update_missing_child_order_retry_pending": "true",
    "tv_risk_update_retry_missing_child_orders": "true",
}

GATEWAY_ORDER_DEFAULTS = {
    "ibkr_gateway_order_serial_enabled": "true",
    "ibkr_gateway_order_serial_timeout_sec": "12",
}

DEPRECATED_PAPER_RISK_CONFIG_KEYS = {
    "ibkr_buying_power_guard_paper_source",
    "ibkr_paper_risk_buying_power_usd",
    "ibkr_paper_risk_net_liquidation_usd",
    "ibkr_paper_risk_default_entry_exposure_usd",
}


def _load_config_modules():
    import importlib.util

    module_names = [
        "ibkr_compute",
        "ibkr_compute.core",
        "ibkr_compute.core.config_registry",
        "ibkr_compute.core.config",
        "ibkr_compute.integrations",
        "ibkr_compute.integrations.pb_client",
    ]
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in module_names}

    try:
        package = types.ModuleType("ibkr_compute")
        package.__path__ = [str(SRC_ROOT / "ibkr_compute")]
        core_package = types.ModuleType("ibkr_compute.core")
        core_package.__path__ = [str(SRC_ROOT / "ibkr_compute" / "core")]
        integrations_package = types.ModuleType("ibkr_compute.integrations")
        integrations_package.__path__ = [str(SRC_ROOT / "ibkr_compute" / "integrations")]
        pb_client = types.ModuleType("ibkr_compute.integrations.pb_client")
        pb_client.PBClient = type("PBClient", (), {})

        sys.modules["ibkr_compute"] = package
        sys.modules["ibkr_compute.core"] = core_package
        sys.modules["ibkr_compute.integrations"] = integrations_package
        sys.modules["ibkr_compute.integrations.pb_client"] = pb_client

        registry_path = SRC_ROOT / "ibkr_compute" / "core" / "config_registry.py"
        registry_spec = importlib.util.spec_from_file_location("ibkr_compute.core.config_registry", registry_path)
        registry_module = importlib.util.module_from_spec(registry_spec)
        sys.modules["ibkr_compute.core.config_registry"] = registry_module
        registry_spec.loader.exec_module(registry_module)

        config_path = SRC_ROOT / "ibkr_compute" / "core" / "config.py"
        config_spec = importlib.util.spec_from_file_location("ibkr_compute.core.config", config_path)
        config_module = importlib.util.module_from_spec(config_spec)
        sys.modules["ibkr_compute.core.config"] = config_module
        config_spec.loader.exec_module(config_module)

        return config_module.Config, registry_module.deprecated_aliases_for
    finally:
        for name, module in saved.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _seed_config_values() -> dict[str, tuple[str, str]]:
    seed_path = REPO_ROOT / "extensions" / "pocketbase" / "seeds" / "import.js"
    text = seed_path.read_text(encoding="utf-8")
    rows = re.findall(r"cfg\('([^']+)',\s*'([^']*)',\s*'([^']*)'", text)
    return {key: (value, default_value) for key, value, default_value in rows}


def test_order_flow_defaults_disable_local_confirmation_but_keep_hard_stop_guard() -> None:
    Config, _ = _load_config_modules()

    for key, expected in ORDER_FLOW_DEFAULTS.items():
        assert Config.DEFAULTS[key] == expected

    assert Config().get_bool("ibkr_order_flow_enabled", True) is False
    assert Config().get_for_environment("ibkr_order_flow_mode", "live") == "enforce"
    assert Config().get_bool("signal_manual_confirm_enabled", True) is False
    assert Config().get_bool("never_widen_stop_by_order_flow", False) is True


def test_order_flow_defaults_are_registered() -> None:
    Config, deprecated_aliases_for = _load_config_modules()
    registry = Config.REGISTRY

    assert registry["ibkr_order_flow_enabled"].group == "order_flow"
    assert registry["ibkr_order_flow_enabled"].value_type == "bool"
    assert registry["ibkr_order_flow_execution_pool_size"].group == "order_flow"
    assert registry["ibkr_order_flow_execution_pool_size"].value_type == "int"
    assert registry["ibkr_order_flow_min_delta_ratio"].value_type == "float"
    assert registry["never_widen_stop_by_order_flow"].group == "order_flow"
    assert registry["never_widen_stop_by_order_flow"].value_type == "bool"
    assert deprecated_aliases_for(registry, "ibkr_order_flow_execution_pool_size") == (
        "ibkr_order_flow_active_limit",
    )


def test_order_flow_seed_values_match_defaults() -> None:
    seed_values = _seed_config_values()

    for key, expected in ORDER_FLOW_DEFAULTS.items():
        assert key in seed_values
        seed_value, seed_default = seed_values[key]
        assert seed_value.lower() == expected.lower()
        assert seed_default.lower() == expected.lower()


def test_tv_primary_defaults_are_widened_and_hardened() -> None:
    Config, _ = _load_config_modules()

    for key, expected in TV_PRIMARY_DEFAULTS.items():
        assert Config.DEFAULTS[key] == expected
    for key in DEPRECATED_PAPER_RISK_CONFIG_KEYS:
        assert key not in Config.DEFAULTS

    cfg = Config()
    assert cfg.get_bool("ibkr_require_target_direction_alignment", True) is False
    assert cfg.get_int("max_strategy_open_positions", 0) == 0
    assert cfg.get_int("intraday_symbol_daily_entry_limit", 0) == 3
    assert cfg.get_int("position_limit_max", 0) == 36
    assert cfg.get_bool("tv_entry_requires_active_target", True) is False
    assert cfg.get_bool("tv_entry_requires_authorized_symbol", False) is True
    assert cfg.get_bool("tv_webhook_async_route_enabled", False) is True
    assert cfg.get_bool("tv_quality_window_rank_enforce_enabled", True) is False
    assert cfg.get_bool("tv_risk_update_seq_guard_enabled", False) is True
    assert cfg.get_bool("tv_risk_update_require_monotonic_seq", False) is True
    assert cfg.get_bool("tv_risk_update_never_widen_stop", False) is True
    assert cfg.get_bool("tv_risk_update_retry_missing_child_orders", False) is True


def test_tv_primary_seed_values_match_defaults() -> None:
    seed_values = _seed_config_values()

    for key, expected in TV_PRIMARY_DEFAULTS.items():
        assert key in seed_values
        seed_value, seed_default = seed_values[key]
        assert seed_value.lower() == expected.lower()
        assert seed_default.lower() == expected.lower()
    for key in DEPRECATED_PAPER_RISK_CONFIG_KEYS:
        assert key not in seed_values


def test_gateway_order_serial_defaults_match_seed_values() -> None:
    Config, _ = _load_config_modules()
    seed_values = _seed_config_values()

    for key, expected in GATEWAY_ORDER_DEFAULTS.items():
        assert Config.DEFAULTS[key] == expected
        assert key in seed_values
        seed_value, seed_default = seed_values[key]
        assert seed_value.lower() == expected.lower()
        assert seed_default.lower() == expected.lower()
