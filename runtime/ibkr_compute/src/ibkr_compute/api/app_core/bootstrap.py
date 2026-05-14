from __future__ import annotations

import os
import sys
import threading
import time

from ibkr_compute.backtest import BacktestService
from ibkr_compute.core.config import Config
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.bar_repair import BarRepairCoordinator
from ibkr_compute.market.backtest_preload import BacktestPreloadCoordinator
from ibkr_compute.market.timeframe_utils import COMPUTE_INTERVALS
from ibkr_compute.api.compute.lock_manager import ComputeLockManager
from ibkr_compute.api.service_topology import get_runtime_mode, get_service_profile
from ibkr_compute.workflows.history_rebuild import HistoryRebuildManager


def register_canonical_module_alias(module_name: str, module_globals: dict):
    module = sys.modules.get(module_name)
    spec = module_globals.get("__spec__")
    canonical_name = str(getattr(spec, "name", "") or "").strip()
    if not module or not canonical_name:
        return
    existing = sys.modules.get(canonical_name)
    if existing is None or existing is module:
        sys.modules[canonical_name] = module


def build_service_bundle(
    *,
    compute_runner,
    scan_runner,
    current_market_date_resolver,
    runtime_status_resolver,
) -> dict:
    pb_base_url = os.environ.get("PB_BASE_URL", "http://127.0.0.1:8090")
    console_base_url = (
        os.environ.get("CONSOLE_BASE_URL")
        or os.environ.get("QUANT_BASE_URL")
        or os.environ.get("IBKR_CONSOLE_PUBLIC_URL")
        or "https://quant.lzw-glory.top"
    )
    pb_client = PBClient(base_url=pb_base_url)
    config = Config(pb_client=pb_client)

    def _backtest_account_snapshot_provider(environment: str = "live") -> dict:
        import requests

        from ibkr_compute.api.account.snapshot_builder import _build_ibkr_account_snapshot
        from ibkr_compute.api.runtime.common import get_ibkr_service
        from ibkr_compute.api.service_topology import get_api_internal_url, get_runtime_internal_url

        runtime_environment = str(environment or "live").strip().lower() or "live"

        service = get_ibkr_service()
        if service is not None:
            return _build_ibkr_account_snapshot(service)

        errors = []
        for base_url, path in (
            (get_runtime_internal_url(), "/ibkr/account"),
            (get_api_internal_url(), "/api/custom/ibkr/account_snapshot"),
        ):
            try:
                response = requests.get(
                    f"{base_url.rstrip('/')}{path}",
                    params={"environment": runtime_environment},
                    timeout=20,
                )
                payload = response.json() if response.content else {}
                if response.ok and isinstance(payload, dict):
                    return payload
                errors.append(f"{path}:{response.status_code}:{str((payload or {}).get('error') or '')[:120]}")
            except Exception as exc:
                errors.append(f"{path}:{str(exc)[:120]}")
        return {"ok": False, "error": "; ".join(errors) or "account_snapshot_unavailable", "summary": {}}

    backtest_service = BacktestService(pb_client, account_snapshot_provider=_backtest_account_snapshot_provider)
    history_rebuild_manager = HistoryRebuildManager(
        pb_client,
        config,
        compute_runner=compute_runner,
        scan_runner=scan_runner,
        current_market_date_resolver=current_market_date_resolver,
        runtime_status_resolver=runtime_status_resolver,
    )
    bar_freshness_planner = BarFreshnessPlanner(pb_client, config, environment=os.environ.get("IBKR_ENVIRONMENT", "live"))
    bar_repair_coordinator = None
    if not (get_runtime_mode() == "remote" and get_service_profile() == "compute"):
        bar_repair_coordinator = BarRepairCoordinator(
            pb_client=pb_client,
            config=config,
            environment=os.environ.get("IBKR_ENVIRONMENT", "live"),
            symbol_meta_provider=lambda symbols: {
                str(symbol or "").strip().upper(): {}
                for symbol in (symbols or [])
                if str(symbol or "").strip()
            },
        )
    backtest_preload_coordinator = BacktestPreloadCoordinator(
        pb_client=pb_client,
        config=config,
        environment=os.environ.get("IBKR_ENVIRONMENT", "live"),
        backtest_service=backtest_service,
    )
    return {
        "PB_BASE_URL": pb_base_url,
        "CONSOLE_BASE_URL": console_base_url,
        "PB_PUBLIC_URL": console_base_url,
        "pb": pb_client,
        "cfg": config,
        "bar_freshness_planner": bar_freshness_planner,
        "bar_repair_coordinator": bar_repair_coordinator,
        "backtest_preload_coordinator": backtest_preload_coordinator,
        "backtest_service": backtest_service,
        "history_rebuild_manager": history_rebuild_manager,
    }


def build_runtime_state_bundle() -> dict:
    return {
        "engines": {},
        "signal_gens": {},
        "last_compute_time": 0.0,
        "last_scan_time": 0.0,
        "last_processed_ms": {},
        "last_interval_fetch_ms": {},
        "compute_count": 0,
        "error_count": 0,
        "rollup_bootstrap_checked": set(),
        "engine_bootstrap_checked": set(),
        "signal_bootstrap_checked": set(),
        "persistent_cursor_envs_loaded": set(),
        "symbol_metadata_cache": {},
        "daily_close_cache": {},
        "daily_close_cache_date": "",
        "metadata_cache_updated_at": 0.0,
        "compute_lock": threading.RLock(),
        "compute_lock_manager": ComputeLockManager(),
        "ibkr_account_snapshot_cache": {},
        "ibkr_account_snapshot_cache_lock": threading.Lock(),
        "host_cpu_snapshot_lock": threading.Lock(),
        "host_cpu_snapshot_cache": None,
        "_start_time": time.time(),
        "_compute_startup_preload_state_lock": threading.Lock(),
        "_compute_startup_preload_state": {},
        "_scan_attempt_state_lock": threading.RLock(),
        "_scan_attempt_state": {"by_key": {}, "by_run_id": {}},
        "_ibkr_service": None,
        "_ibkr_service_lock": threading.Lock(),
        "_ibkr_restore_attempted": False,
        "_ibkr_restore_lock": threading.Lock(),
    }


def build_constant_bundle() -> dict:
    return {
        "INTERVALS": list(COMPUTE_INTERVALS),
        "SUPPORTED_COMPUTE_ENVIRONMENTS": ["live", "paper", "backtest"],
        "DEFAULT_COMPUTE_ENVIRONMENTS": ["live", "paper"],
        "IBKR_SCRIPT_TAG": os.environ.get("IBKR_SCRIPT_TAG", "IBKR_SAC_v1_20260403"),
        "BOOTSTRAP_LOOKBACK_BARS": {
            "5m": 260,
            "15m": 260,
            "30m": 260,
            "1h": 260,
            "4h": 260,
            "1d": 260,
        },
        "MATERIALIZE_MAX_WORKERS": max(1, min(8, int(os.environ.get("IBKR_MATERIALIZE_MAX_WORKERS", "1")))),
        "ROLLUP_BATCH_SIZE": 100,
        "INDICATOR_BATCH_SIZE": max(1, int(os.environ.get("IBKR_INDICATOR_BATCH_SIZE", "60"))),
        "SIGNAL_BATCH_SIZE": max(1, int(os.environ.get("IBKR_SIGNAL_BATCH_SIZE", "30"))),
        "CHART_TIMELINE_VISIBLE_LIMIT": max(200, int(os.environ.get("IBKR_CHART_TIMELINE_VISIBLE_LIMIT", "3000"))),
        "CHART_COMPARE_MAX_MISMATCH_EXAMPLES": max(
            8,
            int(os.environ.get("IBKR_CHART_COMPARE_MAX_MISMATCH_EXAMPLES", "18")),
        ),
        "IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS": max(
            1.0,
            float(os.environ.get("IBKR_ACCOUNT_SNAPSHOT_TTL_SECONDS", "5.0")),
        ),
        "COMPUTE_CURSOR_STATE_KEY": "compute_cursors",
        "COMPUTE_CURSOR_STATE_DATE": "global",
        "IBKR_RUNTIME_CONTROL_STATE_KEY": "ibkr_runtime_control",
        "IBKR_RUNTIME_CONTROL_STATE_DATE": "global",
        "DEFAULT_TRADE_WINDOW_START": (9, 35),
        "DEFAULT_TRADE_WINDOW_END": (15, 30),
        "DEFAULT_ORDER_WINDOW_END": (15, 0),
        "SIGNAL_SUPPRESSED_COMPUTE_SOURCES": {
            "history_repair",
            "history_rebuild",
            "recompute",
            "targeted_recompute",
            "direct_history_topup",
            "runtime_direct_topup",
        },
    }
