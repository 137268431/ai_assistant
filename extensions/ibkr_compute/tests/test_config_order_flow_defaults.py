from __future__ import annotations

import re
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "runtime" / "ibkr_compute" / "src"


ORDER_FLOW_DEFAULTS = {
    "ibkr_order_flow_enabled": "true",
    "ibkr_order_flow_mode": "shadow",
    "ibkr_order_flow_active_limit": "3",
    "ibkr_order_flow_execution_pool_size": "3",
    "ibkr_order_flow_max_position_slots": "1",
    "ibkr_order_flow_tick_types": "Last",
    "ibkr_order_flow_confirm_window_sec": "60",
    "ibkr_order_flow_min_delta_ratio": "0.12",
    "ibkr_order_flow_max_spread_bps": "12",
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


def test_order_flow_defaults_are_shadow_safe() -> None:
    Config, _ = _load_config_modules()

    for key, expected in ORDER_FLOW_DEFAULTS.items():
        assert Config.DEFAULTS[key] == expected

    assert Config().get_bool("ibkr_order_flow_enabled", False) is True
    assert Config().get_for_environment("ibkr_order_flow_mode", "live") == "shadow"
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
