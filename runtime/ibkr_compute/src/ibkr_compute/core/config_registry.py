from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    default: str
    value_type: str = "string"
    group: str = "runtime"
    display_name: str = ""
    description: str = ""
    owner: str = "ibkr_compute"
    sort_order: float = 0.0
    environment_scoped: bool = True
    deprecated_aliases: tuple[str, ...] = field(default_factory=tuple)
    hidden: bool = False


CONFIG_ALIASES: dict[str, tuple[str, ...]] = {
    "pb_cron_system_market_open_reminder_enabled": ("pb_cron_system_scan_summary_enabled",),
    "pb_cron_ibkr_data_quality_repair_sweep_enabled": (
        "pb_cron_ibkr_data_quality_open_sweep_enabled",
        "pb_cron_ibkr_data_quality_close_sweep_enabled",
    ),
    "pb_cron_ibkr_data_quality_truth_audit_enabled": (
        "pb_cron_ibkr_data_quality_premarket_truth_audit_enabled",
    ),
    "ibkr_order_flow_execution_pool_size": ("ibkr_order_flow_active_limit",),
}

CONFIG_GROUP_OVERRIDES: dict[str, str] = {
    "pb_scheduler_enabled": "scheduler",
    "status_notify_enabled": "notification",
    "daily_summary_notify_enabled": "notification",
    "health_check_notify_enabled": "notification",
    "market_closed_notify_enabled": "notification",
    "market_closed_notify_weekends": "notification",
    "inspection_notify_enabled": "notification",
    "manual_stop_notify_enabled": "notification",
}


def _infer_type(value: Any) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    if lower in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        return "bool"
    try:
        int(text)
        return "int"
    except Exception:
        pass
    try:
        float(text)
        return "float"
    except Exception:
        pass
    if text.startswith("{") or text.startswith("["):
        return "json"
    return "string"


def _infer_group(key: str) -> str:
    normalized = str(key or "").strip()
    if normalized in CONFIG_GROUP_OVERRIDES:
        return CONFIG_GROUP_OVERRIDES[normalized]
    if normalized.startswith("pb_cron_"):
        return "scheduler"
    if normalized.endswith("_chat_id") or normalized.endswith("_notify_enabled"):
        return "notification"
    if normalized.endswith("_url") or normalized.endswith("_internal_url") or normalized.endswith("_public_url"):
        return "service_topology"
    if normalized.startswith("storage_cleanup_"):
        return "storage"
    if normalized.startswith("ibkr_daily_scan_") or normalized.startswith("ibkr_dynamic_admission_"):
        return "target_universe"
    if normalized.startswith("ibkr_history_") or normalized.startswith("ibkr_bar_"):
        return "market_data"
    if normalized.startswith("ibkr_order_flow_") or normalized == "never_widen_stop_by_order_flow":
        return "order_flow"
    if (
        normalized.startswith("intraday_")
        or normalized.startswith("signal_")
        or normalized.startswith("exit_")
        or normalized.startswith("candidate_")
        or normalized.startswith("quality_")
        or normalized.startswith("runner_")
        or normalized in {"cvd_flip_exit_enabled", "cvd_divergence_take_profit_enabled"}
    ):
        return "strategy"
    return "runtime"


def build_config_registry(defaults: dict[str, Any]) -> dict[str, ConfigEntry]:
    registry: dict[str, ConfigEntry] = {}
    for key, default in sorted((defaults or {}).items()):
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        registry[normalized_key] = ConfigEntry(
            key=normalized_key,
            default=str(default),
            value_type=_infer_type(default),
            group=_infer_group(normalized_key),
            owner="ibkr_scheduler" if normalized_key.startswith("pb_cron_") or normalized_key == "pb_scheduler_enabled" else "ibkr_compute",
            deprecated_aliases=tuple(CONFIG_ALIASES.get(normalized_key, ())),
            hidden=_infer_group(normalized_key) == "runtime",
        )
    return registry


def deprecated_aliases_for(registry: dict[str, ConfigEntry], key: str) -> tuple[str, ...]:
    entry = (registry or {}).get(str(key or "").strip())
    return tuple(entry.deprecated_aliases) if entry else tuple(CONFIG_ALIASES.get(str(key or "").strip(), ()))


def defaults_from_registry(registry: dict[str, ConfigEntry]) -> dict[str, str]:
    return {key: entry.default for key, entry in (registry or {}).items()}
