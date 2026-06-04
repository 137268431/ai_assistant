from __future__ import annotations

import queue
import importlib.util
import sys
import threading
import types
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_SRC = REPO_ROOT / "runtime" / "ibkr_compute" / "src"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))
RUNTIME_STATUS_PATH = (
    REPO_ROOT
    / "runtime"
    / "ibkr_compute"
    / "src"
    / "ibkr_compute"
    / "orchestration"
    / "runtime_status.py"
)
spec = importlib.util.spec_from_file_location("runtime_status_under_test", RUNTIME_STATUS_PATH)
runtime_status = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime_status
assert spec.loader is not None
spec.loader.exec_module(runtime_status)
TradingServiceRuntimeStatusMixin = runtime_status.TradingServiceRuntimeStatusMixin


class _Component:
    def __init__(self, payload=None):
        self.payload = dict(payload or {})

    def status(self):
        return dict(self.payload)


class _Session(_Component):
    def check_auth_status(self):
        raise AssertionError("status snapshots should not refresh auth by default")


class _Config:
    def get_int_for_environment(self, _key, _environment, default=0):
        return default

    def get_bool_for_environment(self, _key, _environment, default=False):
        return default


class _Runtime(TradingServiceRuntimeStatusMixin):
    def __init__(self):
        self.config = _Config()
        self.session_keeper = _Session({"authenticated": True})
        self._compute_queue = queue.Queue()
        self._compute_thread = None
        self._last_bar_close_at = 0.0
        self._last_realtime_compute_at = 0.0
        self._last_realtime_compute_started_at = 0.0
        self._last_realtime_compute_result = {}
        self._starting = False
        self._running = True
        self.data_writer = _Component({"bar_pipeline": {"enabled": False, "status": "disabled_tv_primary"}})
        self.data_backfill = _Component({"bar_pipeline": {"enabled": False, "status": "disabled_tv_primary"}})
        self.ws_client = _Component({"connected": True, "ready": True})
        self.realtime_quote_book = _Component({})
        self.broker = types.SimpleNamespace(client_id=31)
        self.bar_repair_coordinator = None
        self._watchlist_trade_symbols = ["AAPL"]
        self._watchlist_symbols = ["AAPL"]
        self._active_trade_symbols = ["AAPL"]
        self._active_subscription_symbols = ["AAPL"]
        self._subscription_lock = threading.RLock()
        self._realtime_compute_runs = 0
        self.gateway_manager = _Component({"running": True, "reachable": True})
        self.bar_aggregator = _Component({})
        self.data_retention = _Component({})
        self.order_placer = _Component({})
        self.order_tracker = _Component({})
        self.order_lifecycle = _Component({"max_strategy_open_positions": 12})
        self.order_flow_manager = None
        self.signal_router = _Component({})
        self.signal_processor = _Component({})
        self._current_market_date = "2026-06-02"
        self._active_target_date = "2026-06-02"
        self._last_daily_reset_at = 0.0
        self._last_watchlist_refresh_at = 0.0
        self._last_target_refresh_at = 0.0
        self._last_active_repair_at = 0.0
        self._last_active_repair_symbols = []
        self._last_active_repair_reasons = {}
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []
        self._last_watchlist_integrity_at = 0.0
        self._last_watchlist_integrity_symbols = []
        self._last_watchlist_integrity_repair_symbols = []
        self._last_history_repair_at = 0.0
        self._last_history_repair_symbols = []
        self._last_pipeline_repair_at = 0.0
        self._last_pipeline_repair_symbols = []

    def _data_universe_symbols(self):
        return ["AAPL"]

    def _runtime_market_session_snapshot(self, _service_mod, **_kwargs):
        return {"kind": "regular"}

    def _copy_auth_recovery_state(self):
        return {}

    def _copy_official_5m_state(self):
        return {}

    def _copy_direct_topup_state(self):
        return {}

    def _copy_warmup_state(self):
        return {"phase": "ready"}

    def _copy_daily_scan_state(self):
        return {"status": "completed", "market_date": "2026-06-02"}

    def _normalize_symbol_list(self, symbols):
        return [str(symbol).upper() for symbol in symbols or []]

    def _market_ws_symbols(self):
        return ["AAPL"]

    def _today_target_rows(self):
        return "2026-06-02", []

    def _active_target_direction_biases(self):
        return {"AAPL": "long"}

    def _non_monitor_pending_symbols(self, pending, _market_ws_symbols):
        return list(pending or [])

    def _host_resources_snapshot(self):
        return {}

    def _resource_governor_snapshot(self):
        return {"status": "ok"}

    def _watchlist_idle_topup_status(self):
        return {}

    def _runtime_phase_label(self):
        return "running"

    def startup_strategy(self):
        return {}

    def auto_restore_guard(self):
        return {}

    def _copy_interval_prime_state(self):
        return {}

    def _strategy_capacity_snapshot(self):
        raise AssertionError("status must not make live broker capacity calls")


def test_runtime_status_uses_lightweight_strategy_capacity(monkeypatch):
    fake_service_mod = types.SimpleNamespace(
        ENVIRONMENT="paper",
        BROKER_MODE="paper",
        GATEWAY_MODE="paper",
        DATA_ENVIRONMENT="live",
        ET=ZoneInfo("America/New_York"),
        DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE=20,
        logger=types.SimpleNamespace(debug=lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(runtime_status, "_service_mod", lambda: fake_service_mod)

    payload = _Runtime().status(refresh_auth=False)

    assert payload["strategy_capacity"] == {
        "available": False,
        "capacity_full": False,
        "max_strategy_open_positions": 12,
        "source": "runtime_status_lightweight",
        "error": "omitted_from_status_snapshot",
    }
    assert payload["order_lifecycle"]["max_strategy_open_positions"] == 12


def test_service_status_snapshot_can_refresh_calendar_without_auth():
    from ibkr_compute.api.shared.service_status import get_service_status_snapshot

    calls = {}

    class _ProbeService:
        def status(self, *, refresh_auth=True, refresh_calendar=False):
            calls["refresh_auth"] = refresh_auth
            calls["refresh_calendar"] = refresh_calendar
            return {"ok": True}

    payload = get_service_status_snapshot(_ProbeService(), refresh_auth=False, refresh_calendar=True)

    assert payload["ok"] is True
    assert calls == {"refresh_auth": False, "refresh_calendar": True}


def test_runtime_status_market_session_can_skip_ibkr_calendar_refresh(monkeypatch):
    ibkr_pkg = types.ModuleType("ibkr_compute")
    ibkr_pkg.__path__ = []
    market_pkg = types.ModuleType("ibkr_compute.market")
    market_pkg.__path__ = []
    calendar_mod = types.ModuleType("ibkr_compute.market.calendar")
    calendar_mod.IBKR_SCHEDULE_SOURCE = "ibkr"
    calendar_mod.build_ibkr_calendar_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("lightweight status must not request IBKR calendar")
    )
    calendar_mod.build_local_nyse_calendar_snapshot = lambda market_date, **kwargs: {
        "source": "local_nyse",
        "source_error": kwargs.get("source_error", ""),
        "market_date": market_date,
        "symbol": kwargs.get("symbol", ""),
        "exchange": kwargs.get("exchange", ""),
        "sec_type": kwargs.get("sec_type", ""),
    }
    calendar_mod.build_market_session_from_calendar = lambda _payload, **_kwargs: {"kind": "regular"}
    monkeypatch.setitem(sys.modules, "ibkr_compute", ibkr_pkg)
    monkeypatch.setitem(sys.modules, "ibkr_compute.market", market_pkg)
    monkeypatch.setitem(sys.modules, "ibkr_compute.market.calendar", calendar_mod)

    class _Broker:
        def resolve_contract(self, **_kwargs):
            raise AssertionError("lightweight status must not call IBKR contract resolution")

    class _CalendarRuntime(TradingServiceRuntimeStatusMixin):
        broker = _Broker()
        gateway_manager = _Component({"running": True, "reachable": True})
        session_keeper = _Session({"authenticated": True})
        _current_market_date = "2026-06-02"
        _market_session_calendar_cache = None

        def _market_calendar_contract_args(self, _service_mod):
            return {"symbol": "SPY", "exchange": "SMART", "sec_type": "STK"}

    fake_service_mod = types.SimpleNamespace(
        ET=ZoneInfo("America/New_York"),
        build_market_session_snapshot=lambda _now: {"kind": "regular", "source": "local_fallback"},
    )

    payload = _CalendarRuntime()._runtime_market_session_snapshot(
        fake_service_mod,
        refresh_ibkr_calendar=False,
    )

    assert payload["kind"] == "regular"
    assert payload["calendar"]["source_error"] == "ibkr_calendar_refresh_omitted"


def test_runtime_status_market_session_reuses_valid_ibkr_calendar_cache(monkeypatch):
    ibkr_pkg = types.ModuleType("ibkr_compute")
    ibkr_pkg.__path__ = []
    market_pkg = types.ModuleType("ibkr_compute.market")
    market_pkg.__path__ = []
    calendar_mod = types.ModuleType("ibkr_compute.market.calendar")
    calendar_mod.IBKR_SCHEDULE_SOURCE = "ibkr_schedule"
    calendar_mod.build_ibkr_calendar_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("valid IBKR cache should avoid calendar refresh")
    )
    calendar_mod.build_local_nyse_calendar_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("valid IBKR cache should avoid local fallback")
    )
    calendar_mod.build_market_session_from_calendar = lambda payload, **_kwargs: {
        "kind": "regular",
        "source": payload.get("source"),
    }
    monkeypatch.setitem(sys.modules, "ibkr_compute", ibkr_pkg)
    monkeypatch.setitem(sys.modules, "ibkr_compute.market", market_pkg)
    monkeypatch.setitem(sys.modules, "ibkr_compute.market.calendar", calendar_mod)

    class _Broker:
        def resolve_contract(self, **_kwargs):
            raise AssertionError("valid IBKR cache should avoid contract resolution")

    class _CalendarRuntime(TradingServiceRuntimeStatusMixin):
        broker = _Broker()
        gateway_manager = _Component({"running": True, "reachable": True})
        session_keeper = _Session({"authenticated": True})
        _current_market_date = "2026-06-02"
        _market_session_calendar_cache = {
            "signature": ("2026-06-02", "SPY", "SMART", "STK"),
            "expires_at": 9_999_999_999,
            "payload": {
                "ok": True,
                "source": "ibkr_schedule",
                "market_date": "2026-06-02",
                "symbol": "SPY",
                "exchange": "SMART",
                "sec_type": "STK",
            },
        }

        def _market_calendar_contract_args(self, _service_mod):
            return {"symbol": "SPY", "exchange": "SMART", "sec_type": "STK"}

    fake_service_mod = types.SimpleNamespace(
        ET=ZoneInfo("America/New_York"),
        build_market_session_snapshot=lambda _now: {"kind": "regular", "source": "local_fallback"},
    )

    payload = _CalendarRuntime()._runtime_market_session_snapshot(
        fake_service_mod,
        refresh_ibkr_calendar=True,
    )

    assert payload["kind"] == "regular"
    assert payload["source"] == "ibkr_schedule"
    assert payload["calendar"]["source"] == "ibkr_schedule"


def test_runtime_status_market_session_refreshes_expired_calendar_cache(monkeypatch):
    calls = []
    ibkr_pkg = types.ModuleType("ibkr_compute")
    ibkr_pkg.__path__ = []
    market_pkg = types.ModuleType("ibkr_compute.market")
    market_pkg.__path__ = []
    calendar_mod = types.ModuleType("ibkr_compute.market.calendar")
    calendar_mod.IBKR_SCHEDULE_SOURCE = "ibkr_schedule"

    def _build_ibkr_calendar_snapshot(contract, **kwargs):
        calls.append({"contract": contract, **kwargs})
        return {
            "ok": True,
            "source": "ibkr_schedule",
            "market_date": kwargs.get("market_date"),
            "symbol": kwargs.get("symbol"),
            "exchange": kwargs.get("exchange"),
            "sec_type": kwargs.get("sec_type"),
        }

    calendar_mod.build_ibkr_calendar_snapshot = _build_ibkr_calendar_snapshot
    calendar_mod.build_local_nyse_calendar_snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("successful IBKR refresh should avoid local fallback")
    )
    calendar_mod.build_market_session_from_calendar = lambda payload, **_kwargs: {
        "kind": "regular",
        "source": payload.get("source"),
    }
    monkeypatch.setitem(sys.modules, "ibkr_compute", ibkr_pkg)
    monkeypatch.setitem(sys.modules, "ibkr_compute.market", market_pkg)
    monkeypatch.setitem(sys.modules, "ibkr_compute.market.calendar", calendar_mod)

    class _Broker:
        def resolve_contract(self, **kwargs):
            calls.append({"resolve_contract": kwargs})
            return {"symbol": kwargs.get("symbol"), "exchange": kwargs.get("exchange")}

    class _CalendarRuntime(TradingServiceRuntimeStatusMixin):
        broker = _Broker()
        gateway_manager = _Component({"running": True, "reachable": True})
        session_keeper = _Session({"authenticated": True})
        _current_market_date = "2026-06-02"
        _market_session_calendar_cache = {
            "signature": ("2026-06-02", "SPY", "SMART", "STK"),
            "expires_at": 0,
            "payload": {"ok": True, "source": "ibkr_schedule"},
        }

        def _market_calendar_contract_args(self, _service_mod):
            return {"symbol": "SPY", "exchange": "SMART", "sec_type": "STK"}

    fake_service_mod = types.SimpleNamespace(
        ET=ZoneInfo("America/New_York"),
        build_market_session_snapshot=lambda _now: {"kind": "regular", "source": "local_fallback"},
    )
    runtime = _CalendarRuntime()

    payload = runtime._runtime_market_session_snapshot(fake_service_mod, refresh_ibkr_calendar=True)

    assert payload["source"] == "ibkr_schedule"
    assert len(calls) == 2
    assert calls[0]["resolve_contract"] == {"symbol": "SPY", "conid": 0, "exchange": "SMART", "sec_type": "STK"}
    assert calls[1]["contract"] == {"symbol": "SPY", "exchange": "SMART"}
    assert calls[1]["market_date"] == "2026-06-02"
    assert calls[1]["symbol"] == "SPY"
    assert calls[1]["exchange"] == "SMART"
    assert calls[1]["sec_type"] == "STK"
    assert runtime._market_session_calendar_cache["payload"]["source"] == "ibkr_schedule"
