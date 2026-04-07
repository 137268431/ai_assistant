"""
IBKR Trading Service — 主入口
整合所有模块: Gateway会话 + WebSocket数据 + 订单管理 + 信号处理

启动方式: python -m ibkr_compute.ibkr_service
"""

import os
import sys
import time
import signal
import logging
import threading
import queue
from datetime import datetime, timezone, timedelta

from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.core.config import Config
from ibkr_compute.gateway.gateway_manager import GatewayManager
from ibkr_compute.gateway.session_keeper import SessionKeeper
from ibkr_compute.gateway.auth_handler import AuthHandler
from ibkr_compute.market.conid_resolver import ConidResolver
from ibkr_compute.market.ws_client import IBKRWebSocketClient
from ibkr_compute.market.bar_aggregator import BarAggregator
from ibkr_compute.market.data_writer import DataWriter
from ibkr_compute.market.data_backfill import DataBackfill
from ibkr_compute.market.data_retention import DataRetention
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import HIGHER_INTERVALS, bucket_start_ms, format_us_time, interval_to_ms
from ibkr_compute.order.order_placer import OrderPlacer
from ibkr_compute.order.order_tracker import OrderTracker
from ibkr_compute.order.order_modifier import OrderModifier
from ibkr_compute.order.order_lifecycle import OrderLifecycle
from ibkr_compute.signal.signal_router import SignalRouter
from ibkr_compute.signal.signal_processor import SignalProcessor
from ibkr_compute.signal.reverse_signal import ReverseSignalHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ibkr_service")

ET = timezone(timedelta(hours=-4))

PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
DEFAULT_SIGNAL_POLL_INTERVAL = 120
DEFAULT_WARMUP_REQUIRED_INTERVAL = "5m"
BAR_INTEGRITY_STATE_KEY = "ibkr_bar_integrity_cursor"
BAR_INTEGRITY_STATE_DATE = "global"
DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE = 8


class IBKRTradingService:
    def __init__(self):
        self.pb = PBClient(base_url=PB_BASE_URL)
        self.config = Config(pb_client=self.pb)

        self.gateway_manager = GatewayManager()
        self.auth_handler = AuthHandler(gateway_url=GATEWAY_URL, pb_client=self.pb)
        self.session_keeper = SessionKeeper(
            gateway_url=GATEWAY_URL,
            pb_client=self.pb,
            on_session_expired=self._on_session_expired,
            on_gateway_down=self._on_gateway_down,
        )

        self.conid_resolver = ConidResolver(gateway_url=GATEWAY_URL, pb_client=self.pb)
        self.data_writer = DataWriter(pb_client=self.pb)
        self.data_backfill = DataBackfill(gateway_url=GATEWAY_URL, data_writer=self.data_writer)
        self.data_retention = DataRetention(pb_client=self.pb)
        self.timeframe_builder = TimeframeBarBuilder()

        self.bar_aggregator = BarAggregator(on_bar_close=self._on_bar_close)
        self.ws_client = IBKRWebSocketClient(
            gateway_url=GATEWAY_URL,
            on_tick=self.bar_aggregator.on_tick,
        )

        self.order_placer = OrderPlacer(gateway_url=GATEWAY_URL, pb_client=self.pb)
        self.order_modifier = OrderModifier(gateway_url=GATEWAY_URL, pb_client=self.pb)
        self.order_tracker = OrderTracker(
            gateway_url=GATEWAY_URL, pb_client=self.pb,
            on_fill=self._on_order_fill,
            on_cancel=self._on_order_cancel,
        )
        self.order_lifecycle = OrderLifecycle(
            gateway_url=GATEWAY_URL, pb_client=self.pb,
            order_modifier=self.order_modifier,
            config=self.config, environment=ENVIRONMENT,
        )

        self.signal_router = SignalRouter(
            pb_client=self.pb, config=self.config, environment=ENVIRONMENT,
        )
        self.signal_processor = SignalProcessor(
            config=self.config, order_lifecycle=self.order_lifecycle,
            environment=ENVIRONMENT,
            readiness_provider=self._trade_readiness_snapshot,
        )
        self.reverse_handler = ReverseSignalHandler(
            pb_client=self.pb, order_placer=self.order_placer,
            order_modifier=self.order_modifier, order_lifecycle=self.order_lifecycle,
            signal_processor=self.signal_processor, conid_resolver=self.conid_resolver,
            environment=ENVIRONMENT,
        )

        self._running = False
        self._starting = False
        self._state_lock = threading.Lock()
        self._signal_thread = None
        self._subscription_thread = None
        self._active_repair_thread = None
        self._watchlist_backfill_thread = None
        self._compute_thread = None
        self._warmup_thread = None
        self._auth_required_reason = ""
        self._symbol_meta = {}
        self._market_index_symbols = []
        self._watchlist_symbols = []
        self._watchlist_records = {}
        self._active_subscription_symbols = []
        self._active_subscription_map = {}
        self._active_trade_symbols = []
        self._active_target_date = ""
        self._last_watchlist_refresh_at = 0.0
        self._last_target_refresh_at = 0.0
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []
        self._last_active_repair_at = 0.0
        self._last_active_repair_symbols = []
        self._last_active_repair_reasons = {}
        self._last_history_repair_at = 0.0
        self._last_history_repair_symbols = []
        self._last_pipeline_repair_at = 0.0
        self._last_pipeline_repair_symbols = []
        self._watchlist_backfill_cursor = 0
        self._watchlist_integrity_cursor = 0
        self._last_watchlist_integrity_at = 0.0
        self._last_watchlist_integrity_symbols = []
        self._last_watchlist_integrity_repair_symbols = []
        self._subscription_lock = threading.Lock()
        self._warmup_lock = threading.Lock()
        self._compute_queue = queue.Queue()
        self._signal_wakeup = threading.Event()
        self._warmup_wakeup = threading.Event()
        self._realtime_compute_runs = 0
        self._last_realtime_compute_at = 0.0
        self._last_realtime_compute_result = {}
        self._current_market_date = ""
        self._last_daily_reset_at = 0.0
        self._last_session_authenticated = False
        self._warmup_signature = ()
        self._warmup_state = self._initial_warmup_state()

    def _build_2fa_detail(self, reason: str) -> dict:
        return {
            "环境": ENVIRONMENT,
            "原因": reason,
        }

    def _request_manual_2fa(self, reason: str, message: str):
        self._auth_required_reason = reason
        requested = self.auth_handler.request_2fa_approval(
            reason=reason,
            source="ibkr_service",
            detail=self._build_2fa_detail(reason),
            message=message,
        )
        if requested:
            logger.info("Manual 2FA request sent: %s", reason)
        else:
            logger.warning("Manual 2FA request failed to send: %s", reason)

    def _now_iso(self) -> str:
        return datetime.now(ET).isoformat()

    def _initial_warmup_state(self) -> dict:
        return {
            "phase": "idle",
            "reason": "",
            "required_interval": DEFAULT_WARMUP_REQUIRED_INTERVAL,
            "requested_at": None,
            "started_at": None,
            "finished_at": None,
            "last_success_at": None,
            "last_error": "",
            "trading_gate_open": False,
            "trading_gate_reason": "warmup_idle",
            "target_date": "",
            "symbols_total": 0,
            "trade_symbols_total": 0,
            "monitor_symbols_total": 0,
            "ready_symbols": 0,
            "ready_trade_symbols": 0,
            "ready_monitor_symbols": 0,
            "symbols": [],
            "trade_symbols": [],
            "monitor_symbols": [],
            "ready_symbols_list": [],
            "pending_symbols": [],
            "symbol_status": [],
            "backfill_written": 0,
            "backfill_result": {},
            "compute_result": {},
        }

    def _copy_warmup_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._warmup_state
        copied = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

    def _set_warmup_state(self, **updates) -> dict:
        with self._warmup_lock:
            next_state = self._copy_warmup_state()
            for key, value in updates.items():
                if isinstance(value, dict):
                    next_state[key] = dict(value)
                elif isinstance(value, list):
                    next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    next_state[key] = value
            self._warmup_state = next_state
            return self._copy_warmup_state(next_state)

    def _reset_warmup_state(self, reason: str = ""):
        with self._warmup_lock:
            self._warmup_signature = ()
            self._warmup_state = self._initial_warmup_state()
            if reason:
                self._warmup_state["reason"] = reason
                self._warmup_state["trading_gate_reason"] = "warmup_reset"

    def _warmup_snapshot_from_subscriptions(self) -> dict:
        with self._subscription_lock:
            symbols = sorted(self._active_subscription_symbols)
            trade_symbols = sorted(self._active_trade_symbols)
            target_date = self._active_target_date
            conid_map = dict(self._active_subscription_map)
            symbol_meta = {
                symbol: dict(self._symbol_meta.get(symbol) or {})
                for symbol in symbols
            }
        market_index_set = set(self._market_index_symbols)
        monitor_symbols = [symbol for symbol in symbols if symbol in market_index_set]
        return {
            "target_date": target_date,
            "symbols": symbols,
            "trade_symbols": trade_symbols,
            "monitor_symbols": monitor_symbols,
            "symbols_total": len(symbols),
            "trade_symbols_total": len(trade_symbols),
            "monitor_symbols_total": len(monitor_symbols),
            "conid_map": conid_map,
            "symbol_meta": symbol_meta,
            "signature": (target_date, tuple(symbols), tuple(trade_symbols)),
        }

    def _trade_readiness_snapshot(self) -> dict:
        if not self._running:
            return {"open": False, "reason": "runtime_stopped"}
        if not self.session_keeper.is_authenticated:
            return {"open": False, "reason": "session_unauthenticated"}
        state = self._copy_warmup_state()
        if state.get("trading_gate_open"):
            return {
                "open": True,
                "reason": str(state.get("trading_gate_reason") or "ready"),
                "phase": state.get("phase"),
            }
        return {
            "open": False,
            "reason": str(state.get("trading_gate_reason") or "warmup_incomplete"),
            "phase": state.get("phase"),
        }

    def _close_warmup_gate(self, reason: str):
        snapshot = self._warmup_snapshot_from_subscriptions()
        phase = "blocked" if snapshot["symbols_total"] else "idle"
        self._set_warmup_state(
            phase=phase,
            reason=reason,
            trading_gate_open=False,
            trading_gate_reason=reason,
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
        )

    def _schedule_warmup(self, reason: str = "subscriptions_changed", force: bool = False) -> bool:
        snapshot = self._warmup_snapshot_from_subscriptions()
        if snapshot["symbols_total"] == 0:
            self._set_warmup_state(
                phase="idle",
                reason=reason,
                requested_at=self._now_iso(),
                started_at=None,
                finished_at=self._now_iso(),
                last_error="",
                trading_gate_open=False,
                trading_gate_reason="no_active_symbols",
                target_date=snapshot["target_date"],
                symbols_total=0,
                trade_symbols_total=0,
                monitor_symbols_total=0,
                ready_symbols=0,
                ready_trade_symbols=0,
                ready_monitor_symbols=0,
                symbols=[],
                trade_symbols=[],
                monitor_symbols=[],
                ready_symbols_list=[],
                pending_symbols=[],
                symbol_status=[],
                backfill_written=0,
                backfill_result={},
                compute_result={},
            )
            return False

        with self._warmup_lock:
            same_signature = self._warmup_signature == snapshot["signature"]
            current_phase = str(self._warmup_state.get("phase") or "")
            pending_symbols = list(self._warmup_state.get("pending_symbols") or [])
            if (
                not force
                and same_signature
                and (
                    current_phase in {"pending", "running"}
                    or (current_phase == "ready" and not pending_symbols)
                )
            ):
                return False
            self._warmup_signature = snapshot["signature"]

        self._set_warmup_state(
            phase="pending",
            reason=reason,
            requested_at=self._now_iso(),
            started_at=None,
            finished_at=None,
            last_error="",
            trading_gate_open=False,
            trading_gate_reason="warmup_pending",
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            ready_symbols=0,
            ready_trade_symbols=0,
            ready_monitor_symbols=0,
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            ready_symbols_list=[],
            pending_symbols=snapshot["symbols"],
            symbol_status=[],
            backfill_written=0,
            backfill_result={},
            compute_result={},
        )
        self._warmup_wakeup.set()
        logger.info(
            "Warmup scheduled (%s): symbols=%d trade=%d monitor=%d",
            reason,
            snapshot["symbols_total"],
            snapshot["trade_symbols_total"],
            snapshot["monitor_symbols_total"],
        )
        return True

    def _sync_session_transition(self):
        authenticated = bool(self.session_keeper.is_authenticated)
        if authenticated and not self._last_session_authenticated:
            self._last_session_authenticated = True
            logger.info("IBKR session restored; scheduling warmup refresh")
            self._schedule_warmup(reason="session_restored", force=True)
        elif not authenticated and self._last_session_authenticated:
            self._last_session_authenticated = False
            self._close_warmup_gate("session_unauthenticated")

    def _collect_warmup_readiness(self, snapshot: dict) -> dict:
        from ibkr_compute.api import server as compute_server

        ready_symbols = []
        pending_symbols = []
        symbol_status = []
        ready_set = set()
        trade_symbol_set = set(snapshot["trade_symbols"])
        monitor_symbol_set = set(snapshot["monitor_symbols"])

        for symbol in snapshot["symbols"]:
            engine = compute_server.engines.get((ENVIRONMENT, symbol, DEFAULT_WARMUP_REQUIRED_INTERVAL))
            is_ready = bool(engine and engine.is_ready())
            bar_count = int(getattr(engine, "bar_count", 0) or 0) if engine else 0
            last_bar_time_ms = int(getattr(engine, "last_bar_time_ms", 0) or 0) if engine else 0
            role = "trade" if symbol in trade_symbol_set else "monitor" if symbol in monitor_symbol_set else "active"
            symbol_status.append({
                "symbol": symbol,
                "role": role,
                "ready": is_ready,
                "bar_count": bar_count,
                "last_bar_time_ms": last_bar_time_ms,
            })
            if is_ready:
                ready_symbols.append(symbol)
                ready_set.add(symbol)
            else:
                pending_symbols.append(symbol)

        ready_trade_symbols = len([symbol for symbol in snapshot["trade_symbols"] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot["monitor_symbols"] if symbol in ready_set])
        trading_gate_open = bool(snapshot["trade_symbols"]) and ready_trade_symbols == snapshot["trade_symbols_total"]
        return {
            "phase": "ready" if not pending_symbols else "degraded",
            "required_interval": DEFAULT_WARMUP_REQUIRED_INTERVAL,
            "ready_symbols": len(ready_symbols),
            "ready_trade_symbols": ready_trade_symbols,
            "ready_monitor_symbols": ready_monitor_symbols,
            "ready_symbols_list": ready_symbols,
            "pending_symbols": pending_symbols,
            "symbol_status": symbol_status,
            "trading_gate_open": trading_gate_open,
            "trading_gate_reason": "ready" if trading_gate_open else ("no_trade_symbols" if not snapshot["trade_symbols"] else "warmup_incomplete"),
        }

    def _is_warmup_active(self) -> bool:
        phase = str(self._warmup_state.get("phase") or "").strip().lower()
        return phase in {"pending", "running"}

    def _run_warmup_cycle(self):
        snapshot = self._warmup_snapshot_from_subscriptions()
        if snapshot["symbols_total"] == 0:
            self._set_warmup_state(
                phase="idle",
                reason="no_active_symbols",
                trading_gate_open=False,
                trading_gate_reason="no_active_symbols",
            )
            return
        if not self.session_keeper.is_authenticated:
            self._close_warmup_gate("session_unauthenticated")
            return

        started_at = self._now_iso()
        self._set_warmup_state(
            phase="running",
            reason=str(self._warmup_state.get("reason") or "warmup"),
            started_at=started_at,
            finished_at=None,
            last_error="",
            trading_gate_open=False,
            trading_gate_reason="warmup_running",
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            pending_symbols=snapshot["symbols"],
            symbol_status=[],
            backfill_written=0,
            backfill_result={},
            compute_result={},
        )

        logger.info(
            "Warmup started: symbols=%d trade=%d monitor=%d target_date=%s",
            snapshot["symbols_total"],
            snapshot["trade_symbols_total"],
            snapshot["monitor_symbols_total"],
            snapshot["target_date"] or "n/a",
        )

        backfill_result = {}
        backfill_written = 0
        compute_result = {}
        last_error = ""
        from ibkr_compute.api import server as compute_server

        try:
            bootstrap_result = self._trigger_realtime_compute(source="warmup_bootstrap")
            compute_result["bootstrap"] = bootstrap_result
            if bootstrap_result.get("ok") is False:
                last_error = str(bootstrap_result.get("error") or "warmup_bootstrap_failed")

            readiness = self._collect_warmup_readiness(snapshot)
            if readiness["pending_symbols"]:
                storage_bootstrap = compute_server.materialize_engines_from_storage(
                    ENVIRONMENT,
                    readiness["pending_symbols"],
                    DEFAULT_WARMUP_REQUIRED_INTERVAL,
                )
                if storage_bootstrap:
                    compute_result["storage_bootstrap"] = storage_bootstrap
                    readiness = self._collect_warmup_readiness(snapshot)
            self._set_warmup_state(
                phase="running",
                required_interval=readiness["required_interval"],
                started_at=started_at,
                target_date=snapshot["target_date"],
                symbols_total=snapshot["symbols_total"],
                trade_symbols_total=snapshot["trade_symbols_total"],
                monitor_symbols_total=snapshot["monitor_symbols_total"],
                ready_symbols=readiness["ready_symbols"],
                ready_trade_symbols=readiness["ready_trade_symbols"],
                ready_monitor_symbols=readiness["ready_monitor_symbols"],
                symbols=snapshot["symbols"],
                trade_symbols=snapshot["trade_symbols"],
                monitor_symbols=snapshot["monitor_symbols"],
                ready_symbols_list=readiness["ready_symbols_list"],
                pending_symbols=readiness["pending_symbols"],
                symbol_status=readiness["symbol_status"],
                trading_gate_open=readiness["trading_gate_open"],
                trading_gate_reason=readiness["trading_gate_reason"] if readiness["trading_gate_open"] else "warmup_running",
                compute_result=compute_result,
            )

            if readiness["trading_gate_open"] and not readiness["pending_symbols"]:
                finished_at = self._now_iso()
                phase = readiness["phase"]
                self._set_warmup_state(
                    phase=phase,
                    required_interval=readiness["required_interval"],
                    started_at=started_at,
                    finished_at=finished_at,
                    last_success_at=finished_at,
                    last_error=last_error,
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    target_date=snapshot["target_date"],
                    symbols_total=snapshot["symbols_total"],
                    trade_symbols_total=snapshot["trade_symbols_total"],
                    monitor_symbols_total=snapshot["monitor_symbols_total"],
                    ready_symbols=readiness["ready_symbols"],
                    ready_trade_symbols=readiness["ready_trade_symbols"],
                    ready_monitor_symbols=readiness["ready_monitor_symbols"],
                    symbols=snapshot["symbols"],
                    trade_symbols=snapshot["trade_symbols"],
                    monitor_symbols=snapshot["monitor_symbols"],
                    ready_symbols_list=readiness["ready_symbols_list"],
                    pending_symbols=readiness["pending_symbols"],
                    symbol_status=readiness["symbol_status"],
                    backfill_written=0,
                    backfill_result={},
                    compute_result=compute_result,
                )
                logger.info(
                    "Warmup finished early after bootstrap: phase=%s gate=open ready=%d/%d trade_ready=%d/%d",
                    phase,
                    readiness["ready_symbols"],
                    snapshot["symbols_total"],
                    readiness["ready_trade_symbols"],
                    snapshot["trade_symbols_total"],
                )
                self._signal_wakeup.set()
                return
            if readiness["trading_gate_open"] and readiness["pending_symbols"]:
                self._set_warmup_state(
                    phase="running",
                    required_interval=readiness["required_interval"],
                    started_at=started_at,
                    target_date=snapshot["target_date"],
                    symbols_total=snapshot["symbols_total"],
                    trade_symbols_total=snapshot["trade_symbols_total"],
                    monitor_symbols_total=snapshot["monitor_symbols_total"],
                    ready_symbols=readiness["ready_symbols"],
                    ready_trade_symbols=readiness["ready_trade_symbols"],
                    ready_monitor_symbols=readiness["ready_monitor_symbols"],
                    symbols=snapshot["symbols"],
                    trade_symbols=snapshot["trade_symbols"],
                    monitor_symbols=snapshot["monitor_symbols"],
                    ready_symbols_list=readiness["ready_symbols_list"],
                    pending_symbols=readiness["pending_symbols"],
                    symbol_status=readiness["symbol_status"],
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    compute_result=compute_result,
                )
                logger.info(
                    "Warmup trade gate open after bootstrap; continuing repair for pending symbols: %s",
                    ",".join(readiness["pending_symbols"]),
                )
                self._signal_wakeup.set()

            pending_map = {
                symbol: snapshot["conid_map"][symbol]
                for symbol in readiness["pending_symbols"]
                if symbol in snapshot["conid_map"]
            }
            if pending_map:
                logger.info(
                    "Warmup backfilling pending symbols: %d of %d",
                    len(pending_map),
                    snapshot["symbols_total"],
                )
                backfill_result = self.data_backfill.backfill_all(
                    pending_map,
                    symbol_meta=snapshot["symbol_meta"],
                    intervals=[DEFAULT_WARMUP_REQUIRED_INTERVAL],
                    repair_symbols=list(pending_map.keys()),
                )
                backfill_written = sum(
                    int(count or 0)
                    for per_symbol in backfill_result.values()
                    for count in per_symbol.values()
                )
                self.data_writer.flush()
                if readiness["trading_gate_open"]:
                    self._last_history_repair_at = time.time()
                    self._last_history_repair_symbols = sorted(pending_map.keys())
                after_backfill_result = self._run_symbol_pipeline_repair(
                    list(pending_map.keys()),
                    source="warmup_backfill",
                )
                compute_result["after_backfill"] = after_backfill_result
                if after_backfill_result.get("ok") is False:
                    last_error = str(after_backfill_result.get("error") or "warmup_compute_failed")
        except Exception as exc:
            last_error = str(exc)
            logger.error("Warmup cycle failed: %s", exc)

        readiness = self._collect_warmup_readiness(snapshot)
        finished_at = self._now_iso()
        phase = "failed" if last_error and readiness["ready_symbols"] == 0 else readiness["phase"]
        self._set_warmup_state(
            phase=phase,
            required_interval=readiness["required_interval"],
            started_at=started_at,
            finished_at=finished_at,
            last_success_at=finished_at if readiness["phase"] == "ready" else self._warmup_state.get("last_success_at"),
            last_error=last_error,
            trading_gate_open=readiness["trading_gate_open"],
            trading_gate_reason=readiness["trading_gate_reason"],
            target_date=snapshot["target_date"],
            symbols_total=snapshot["symbols_total"],
            trade_symbols_total=snapshot["trade_symbols_total"],
            monitor_symbols_total=snapshot["monitor_symbols_total"],
            ready_symbols=readiness["ready_symbols"],
            ready_trade_symbols=readiness["ready_trade_symbols"],
            ready_monitor_symbols=readiness["ready_monitor_symbols"],
            symbols=snapshot["symbols"],
            trade_symbols=snapshot["trade_symbols"],
            monitor_symbols=snapshot["monitor_symbols"],
            ready_symbols_list=readiness["ready_symbols_list"],
            pending_symbols=readiness["pending_symbols"],
            symbol_status=readiness["symbol_status"],
            backfill_written=backfill_written,
            backfill_result=backfill_result,
            compute_result=compute_result,
        )

        logger.info(
            "Warmup finished: phase=%s gate=%s ready=%d/%d trade_ready=%d/%d backfill_written=%d compute_processed=%s",
            phase,
            "open" if readiness["trading_gate_open"] else "closed",
            readiness["ready_symbols"],
            snapshot["symbols_total"],
            readiness["ready_trade_symbols"],
            snapshot["trade_symbols_total"],
            backfill_written,
            compute_result.get("processed", 0) if isinstance(compute_result, dict) else 0,
        )
        if readiness["trading_gate_open"]:
            self._signal_wakeup.set()

    def _warmup_loop(self):
        logger.info("Runtime warmup loop started")
        while self._running:
            triggered = self._warmup_wakeup.wait(timeout=1)
            if not self._running:
                break
            if not triggered:
                continue
            self._warmup_wakeup.clear()
            try:
                self._run_warmup_cycle()
            except Exception as exc:
                logger.error("Warmup loop error: %s", exc)
                self._set_warmup_state(
                    phase="failed",
                    finished_at=self._now_iso(),
                    last_error=str(exc),
                    trading_gate_open=False,
                    trading_gate_reason="warmup_failed",
                )

    def start(self, trigger_login: bool = True, reason: str = "manual_start", source: str = "api_start"):
        with self._state_lock:
            if self._running:
                logger.info("IBKR Trading Service already running")
                return
            if self._starting:
                logger.info("IBKR Trading Service already starting")
                return
            self._starting = True

        startup_ok = False

        try:
            logger.info("=" * 60)
            logger.info("IBKR Trading Service starting (env=%s)", ENVIRONMENT)
            logger.info("=" * 60)

            self.auth_handler.reset_cancel()
            self.config.refresh()

            if not self._ensure_gateway():
                logger.error("Gateway setup failed, exiting")
                return

            self.session_keeper.start()
            time.sleep(1)
            self.session_keeper.check_auth_status()
            time.sleep(2)

            if not self.session_keeper.is_authenticated:
                if not trigger_login:
                    logger.warning("Not authenticated and trigger_login disabled; requesting manual 2FA")
                    self._request_manual_2fa(reason, "检测到会话未认证，请在准备好时点击按钮触发 2FA。")
                    self.session_keeper.stop()
                    return
                logger.info("Not authenticated, attempting login...")
                if not self.auth_handler.login(
                    reason=reason,
                    source=source,
                    detail=self._build_2fa_detail(reason),
                ):
                    logger.error("Login failed, exiting")
                    self._auth_required_reason = reason
                    self.session_keeper.stop()
                    return
                self.session_keeper.check_auth_status()
                time.sleep(3)
            else:
                self._auth_required_reason = ""
                if reason != "manual_start" or source in ("feishu_callback", "runtime_page"):
                    self.auth_handler._report_2fa_status(
                        status="success",
                        reason=reason,
                        source=source,
                        detail=self._build_2fa_detail(reason),
                        message="Gateway 已处于认证状态，无需再次确认。",
                        last_result="复用现有认证会话。",
                    )

            self._last_session_authenticated = bool(self.session_keeper.is_authenticated)
            self.conid_resolver.load_cache_from_pb()
            self._reset_for_new_market_day(force=True)
            self._refresh_watchlist_pool(force=True)
            self._restore_watchlist_integrity_cursor()
            self.ws_client.start()
            time.sleep(2)
            self._refresh_target_subscriptions(force=True, reason="startup")

            self.order_tracker.start()
            self.order_lifecycle.start()

            self._running = True
            startup_ok = True
            self._signal_thread = threading.Thread(
                target=self._signal_loop, daemon=True, name="signal-loop",
            )
            self._signal_thread.start()
            self._subscription_thread = threading.Thread(
                target=self._subscription_refresh_loop,
                daemon=True,
                name="target-refresh",
            )
            self._subscription_thread.start()
            self._active_repair_thread = threading.Thread(
                target=self._active_repair_loop,
                daemon=True,
                name="active-repair",
            )
            self._active_repair_thread.start()
            self._watchlist_backfill_thread = threading.Thread(
                target=self._watchlist_backfill_loop,
                daemon=True,
                name="watchlist-backfill",
            )
            self._watchlist_backfill_thread.start()
            self._compute_thread = threading.Thread(
                target=self._compute_loop,
                daemon=True,
                name="close-compute",
            )
            self._compute_thread.start()
            self._warmup_thread = threading.Thread(
                target=self._warmup_loop,
                daemon=True,
                name="runtime-warmup",
            )
            self._warmup_thread.start()

            self._schedule_retention()

            logger.info("IBKR Trading Service fully started")
        finally:
            with self._state_lock:
                if not startup_ok and not self._running:
                    self._starting = False
                elif startup_ok:
                    self._starting = False

    def _ensure_gateway(self) -> bool:
        if not self.gateway_manager.is_running:
            logger.info("Starting IB Gateway...")
            if not self.gateway_manager.start():
                logger.error("Failed to start IB Gateway")
                return False
            time.sleep(8)
        return True

    def _market_date(self) -> str:
        return datetime.now(ET).strftime("%Y-%m-%d")

    def _drain_compute_queue(self) -> int:
        drained = 0
        while True:
            try:
                self._compute_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        return drained

    def _remove_stale_target_rows(self, active_date: str) -> int:
        safe_env = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        try:
            rows = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'environment = "{safe_env}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=20,
            )
        except Exception as exc:
            logger.warning("Failed to load stale target rows: %s", exc)
            return 0

        removed = 0
        removed_at = datetime.now(ET).isoformat()
        for row in rows:
            row_date = str(row.get("date", "") or "").strip()
            if not row_date or row_date == active_date:
                continue

            record_id = str(row.get("id") or "")
            if not record_id:
                continue

            payload = {"status": "removed"}
            extra = row.get("extra")
            if isinstance(extra, dict):
                next_extra = dict(extra)
                next_extra["removed_reason"] = "market_day_reset"
                next_extra["removed_at"] = removed_at
                next_extra["removed_market_date"] = active_date
                payload["extra"] = next_extra

            try:
                self.pb.update_record("ibkr_targets", record_id, payload)
                removed += 1
            except Exception as exc:
                logger.warning(
                    "Failed to remove stale target row %s (%s %s): %s",
                    record_id,
                    row_date,
                    str(row.get("symbol", "")).upper(),
                    exc,
                )

        if removed > 0:
            logger.info("Removed %d stale target rows before activating %s", removed, active_date)
        return removed

    def _reset_for_new_market_day(self, force: bool = False):
        current_date = self._market_date()
        previous_date = self._current_market_date
        if not force and previous_date == current_date:
            return False

        logger.info(
            "Market day reset: previous=%s current=%s force=%s",
            previous_date or "n/a",
            current_date,
            force,
        )
        self._current_market_date = current_date
        self._last_daily_reset_at = time.time()

        self.signal_router.daily_reset()
        self.signal_processor.daily_reset()
        self.reverse_handler.daily_reset()
        self.order_lifecycle.daily_reset()
        self.timeframe_builder.reset()
        self.bar_aggregator.reset()
        self._signal_wakeup.clear()
        drained = self._drain_compute_queue()
        if drained > 0:
            logger.info("Cleared %d queued realtime compute tasks during market day reset", drained)

        try:
            from ibkr_compute.api import server as compute_server

            reset_result = compute_server.reset_daily_runtime_state([ENVIRONMENT], reason="market_day_reset")
            logger.info("Compute daily reset result: %s", reset_result)
        except Exception as exc:
            logger.warning("Compute daily reset failed: %s", exc)

        self._reset_warmup_state(reason="market_day_reset")
        self._remove_stale_target_rows(current_date)
        self._apply_live_subscriptions(current_date, {}, reason="market_day_reset")
        self._active_target_date = ""
        self._last_target_refresh_at = 0.0
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []
        self._last_active_repair_at = 0.0
        self._last_active_repair_symbols = []
        self._last_active_repair_reasons = {}
        self._last_history_repair_at = 0.0
        self._last_history_repair_symbols = []
        self._last_pipeline_repair_at = 0.0
        self._last_pipeline_repair_symbols = []
        self._watchlist_integrity_cursor = 0
        self._last_watchlist_integrity_at = 0.0
        self._last_watchlist_integrity_symbols = []
        self._last_watchlist_integrity_repair_symbols = []
        if previous_date and previous_date != current_date:
            self._persist_watchlist_integrity_cursor()
        return True

    def _environment_watchlist_filter(self) -> str:
        safe_env = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        return f'environment = "{safe_env}" || environment = "global" || environment = ""'

    def _configured_market_index_symbols(self) -> list[str]:
        raw_value = ""
        try:
            raw_value = self.config.get_for_environment("market_index_symbols", ENVIRONMENT, "SPY,QQQ,VIX")
        except Exception:
            raw_value = self.config.get("market_index_symbols", "SPY,QQQ,VIX")
        symbols = []
        seen = set()
        for item in str(raw_value or "SPY,QQQ,VIX").split(","):
            symbol = str(item or "").strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _default_symbol_meta(self, symbol: str) -> dict:
        defaults = {
            "SPY": {"exchange": "ARCA", "industry": "ETF"},
            "QQQ": {"exchange": "NASDAQ", "industry": "ETF"},
            "VIX": {"exchange": "CBOE", "industry": "INDEX"},
        }
        return defaults.get(symbol, {"exchange": "SMART", "industry": "INDEX"})

    def _refresh_watchlist_pool(self, force: bool = False):
        refresh_minutes = max(1, self.config.get_int_for_environment("watchlist_interval_min", ENVIRONMENT, 5))
        now = time.time()
        if (
            not force
            and self._watchlist_symbols
            and (now - self._last_watchlist_refresh_at) < (refresh_minutes * 60)
        ):
            return

        logger.info("Refreshing watchlist pool for env=%s", ENVIRONMENT)
        merged = {}
        applied = {}
        priority = {"": 0, "global": 1, str(ENVIRONMENT or "live").strip().lower(): 2}

        try:
            rows = self.pb.get_all_records(
                "watchlist",
                filter=self._environment_watchlist_filter(),
                max_pages=20,
            )
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol:
                    continue
                row_env = str(row.get("environment", "") or "").strip().lower()
                rank = priority.get(row_env, -1)
                if symbol in applied and applied[symbol] > rank:
                    continue
                applied[symbol] = rank
                merged[symbol] = row
        except Exception as exc:
            logger.error("Failed to refresh watchlist pool: %s", exc)
            return

        symbol_meta = {}
        for symbol, row in merged.items():
            symbol_meta[symbol] = {
                "exchange": str(row.get("exchange", "") or "").upper(),
                "industry": str(row.get("industry", "") or ""),
            }

        market_index_symbols = self._configured_market_index_symbols()
        for symbol in market_index_symbols:
            if symbol not in merged:
                default_meta = self._default_symbol_meta(symbol)
                merged[symbol] = {
                    "symbol": symbol,
                    "exchange": default_meta["exchange"],
                    "industry": default_meta["industry"],
                }
            if symbol not in symbol_meta:
                default_meta = self._default_symbol_meta(symbol)
                symbol_meta[symbol] = {
                    "exchange": default_meta["exchange"],
                    "industry": default_meta["industry"],
                }

        self._watchlist_records = merged
        self._watchlist_symbols = sorted(merged.keys())
        self._market_index_symbols = market_index_symbols
        self._symbol_meta = symbol_meta
        self._last_watchlist_refresh_at = now
        logger.info("Watchlist pool refreshed: %d symbols", len(self._watchlist_symbols))

    def _get_target_subscription_limit(self) -> int:
        return max(0, self.config.get_int_for_environment("ibkr_target_subscription_limit", ENVIRONMENT, 60))

    def _today_target_rows(self):
        today = datetime.now(ET).strftime("%Y-%m-%d")
        safe_env = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        rows = self.pb.get_all_records(
            "ibkr_targets",
            filter=(
                f'date = "{today}" && '
                f'environment = "{safe_env}" && '
                '(status = "candidate" || status = "active")'
            ),
            sort="-score,-updated",
            max_pages=10,
        )
        return today, rows

    def _build_target_subscription_plan(self):
        target_date, rows = self._today_target_rows()
        limit = self._get_target_subscription_limit()
        selected_symbols = []
        selected_meta = {}
        selected_rows = []
        seen = set()

        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol or symbol in seen:
                continue
            if limit and len(selected_symbols) >= limit:
                break
            score = float(row.get("score", 0) or 0)
            if score <= 0:
                continue

            watchlist_row = self._watchlist_records.get(symbol) or {}
            selected_symbols.append(symbol)
            selected_rows.append(row)
            selected_meta[symbol] = {
                "exchange": str(
                    row.get("exchange")
                    or watchlist_row.get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    watchlist_row.get("industry")
                    or ""
                ),
            }
            seen.add(symbol)

        for symbol in self._market_index_symbols:
            if symbol in seen:
                continue
            default_meta = self._default_symbol_meta(symbol)
            selected_symbols.append(symbol)
            selected_meta[symbol] = {
                "exchange": str(
                    self._symbol_meta.get(symbol, {}).get("exchange")
                    or default_meta.get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    self._symbol_meta.get(symbol, {}).get("industry")
                    or default_meta.get("industry")
                    or ""
                ),
            }
            seen.add(symbol)

        return target_date, selected_symbols, selected_meta, selected_rows

    def _mark_target_statuses(self, target_date: str, selected_rows):
        safe_env = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        try:
            existing = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{target_date}" && '
                    f'environment = "{safe_env}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=10,
            )
        except Exception as exc:
            logger.warning("Failed to load target rows for status sync: %s", exc)
            return

        selected_ids = {str(row.get("id") or "") for row in selected_rows}
        for row in existing:
            record_id = str(row.get("id") or "")
            if not record_id:
                continue
            desired = "active" if record_id in selected_ids else "candidate"
            current = str(row.get("status", "") or "").strip().lower()
            if current == desired:
                continue
            try:
                self.pb.update_record("ibkr_targets", record_id, {"status": desired})
            except Exception as exc:
                logger.warning("Failed to update target status %s -> %s: %s", record_id, desired, exc)

    def _apply_live_subscriptions(self, target_date: str, conid_map: dict, reason: str = "", trade_symbols: list[str] | None = None):
        with self._subscription_lock:
            previous_map = dict(self._active_subscription_map)
            previous_target_date = self._active_target_date
            previous_trade_symbols = list(self._active_trade_symbols)
            previous_conids = set(previous_map.values())
            next_conids = set(conid_map.values())
            removed_conids = previous_conids - next_conids
            added_symbols = [
                symbol for symbol, conid in conid_map.items()
                if previous_map.get(symbol) != conid
            ]
            normalized_trade_symbols = sorted(
                symbol for symbol in (trade_symbols or [])
                if symbol in conid_map
            )

            if removed_conids:
                self.bar_aggregator.remove_conids(removed_conids)
                for conid in sorted(removed_conids):
                    self.ws_client.unsubscribe(conid)

            reverse_map = {cid: sym for sym, cid in conid_map.items()}
            self.bar_aggregator.set_symbol_map(reverse_map)

            for symbol in added_symbols:
                conid = conid_map.get(symbol)
                if conid:
                    self.ws_client.subscribe(conid)

            self._active_subscription_map = dict(conid_map)
            self._active_subscription_symbols = sorted(conid_map.keys())
            self._active_trade_symbols = normalized_trade_symbols
            self._active_target_date = target_date
            self._last_target_refresh_at = time.time()
            subscriptions_changed = (
                previous_target_date != target_date
                or sorted(previous_map.items()) != sorted(conid_map.items())
                or previous_trade_symbols != normalized_trade_symbols
            )

        if added_symbols and reason not in {"startup", "session_restored"}:
            added_map = {symbol: conid_map[symbol] for symbol in added_symbols if symbol in conid_map}
            logger.info(
                "Backfilling newly subscribed target symbols: %s",
                ",".join(sorted(added_map.keys())),
            )
            self.data_backfill.backfill_all(added_map, symbol_meta=self._symbol_meta, intervals=["5m"])
            self.data_writer.flush()
        elif added_symbols:
            logger.info(
                "Skipping inline backfill during %s; warmup will backfill %d symbols asynchronously",
                reason or "startup",
                len(added_symbols),
            )

        logger.info(
            "Applied target subscriptions (%s): active=%d added=%d removed=%d",
            reason or "refresh",
            len(conid_map),
            len(added_symbols),
            len(removed_conids),
        )
        if subscriptions_changed or reason in {"startup", "session_restored"}:
            self._schedule_warmup(reason=reason or "subscriptions_changed", force=reason in {"startup", "session_restored"})

    def _refresh_target_subscriptions(self, force: bool = False, reason: str = "loop"):
        self._reset_for_new_market_day(force=False)
        refresh_seconds = max(15, self.config.get_int_for_environment("ibkr_target_refresh_sec", ENVIRONMENT, 60))
        now = time.time()
        if not force and (now - self._last_target_refresh_at) < refresh_seconds:
            return

        self._refresh_watchlist_pool(force=force)
        try:
            target_date, symbols, target_meta, selected_rows = self._build_target_subscription_plan()
        except Exception as exc:
            logger.error("Failed to build target subscription plan: %s", exc)
            return

        if not symbols:
            logger.info("No target symbols selected for %s (%s)", target_date, reason)
            self._mark_target_statuses(target_date, [])
            self._apply_live_subscriptions(target_date, {}, reason=reason, trade_symbols=[])
            return

        for symbol, meta in target_meta.items():
            base_meta = self._symbol_meta.get(symbol, {})
            self._symbol_meta[symbol] = {
                "exchange": str(meta.get("exchange") or base_meta.get("exchange") or "").upper(),
                "industry": str(meta.get("industry") or base_meta.get("industry") or ""),
            }

        conid_map = self.conid_resolver.resolve_bulk(symbols)
        if not conid_map:
            logger.warning("No conids resolved for target plan (%s)", reason)
            return

        trade_symbols = sorted(
            {
                str(row.get("symbol", "")).upper()
                for row in selected_rows
                if str(row.get("symbol", "")).upper() in conid_map
            }
        )
        self._mark_target_statuses(target_date, selected_rows)
        self._apply_live_subscriptions(target_date, conid_map, reason=reason, trade_symbols=trade_symbols)

    def _subscription_refresh_loop(self):
        logger.info("Target subscription loop started")
        while self._running:
            try:
                self.config.refresh()
                self._sync_session_transition()
                self._reset_for_new_market_day(force=False)
                if self.session_keeper.is_authenticated:
                    self._refresh_target_subscriptions(reason="poll")
                else:
                    logger.info("Skip target refresh while session is unauthenticated")
            except Exception as exc:
                logger.error("Target subscription loop error: %s", exc)

            sleep_seconds = max(15, self.config.get_int_for_environment("ibkr_target_refresh_sec", ENVIRONMENT, 60))
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _bar_integrity_market_date(self) -> str:
        return str(self._current_market_date or self._market_date())

    def _bar_integrity_cursor_payload(self) -> dict:
        return {
            "market_date": self._bar_integrity_market_date(),
            "cursor": int(self._watchlist_integrity_cursor or 0),
            "last_scan_at": (
                datetime.fromtimestamp(self._last_watchlist_integrity_at, ET).isoformat()
                if self._last_watchlist_integrity_at else ""
            ),
            "last_symbols": list(self._last_watchlist_integrity_symbols),
            "watchlist_pool_count": len(self._watchlist_symbols),
        }

    def _persist_watchlist_integrity_cursor(self):
        try:
            self.pb.upsert_state(
                BAR_INTEGRITY_STATE_KEY,
                ENVIRONMENT,
                self._bar_integrity_cursor_payload(),
                date=BAR_INTEGRITY_STATE_DATE,
            )
        except Exception as exc:
            logger.warning("Failed to persist watchlist integrity cursor: %s", exc)

    def _restore_watchlist_integrity_cursor(self):
        try:
            record = self.pb.get_state(
                BAR_INTEGRITY_STATE_KEY,
                ENVIRONMENT,
                date=BAR_INTEGRITY_STATE_DATE,
            )
        except Exception as exc:
            logger.warning("Failed to load watchlist integrity cursor: %s", exc)
            return

        payload = record.get("data") if isinstance(record, dict) else {}
        if not isinstance(payload, dict):
            return
        if str(payload.get("market_date") or "") != self._bar_integrity_market_date():
            self._watchlist_integrity_cursor = 0
            return

        try:
            self._watchlist_integrity_cursor = max(0, int(payload.get("cursor", 0) or 0))
        except Exception:
            self._watchlist_integrity_cursor = 0

    def _watchlist_integrity_candidates(self):
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_integrity_batch_size",
                ENVIRONMENT,
                DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE,
            ),
        )
        start = self._watchlist_integrity_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_integrity_cursor = (start + batch_size) % max(len(pool), 1)
        self._persist_watchlist_integrity_cursor()
        return ordered[:batch_size]

    def _watchlist_backfill_candidates(self):
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(1, self.config.get_int_for_environment("ibkr_watchlist_backfill_batch_size", ENVIRONMENT, 12))
        start = self._watchlist_backfill_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_backfill_cursor = (start + batch_size) % max(len(pool), 1)
        return ordered[:batch_size]

    def _active_repair_loop(self):
        logger.info("Active target repair loop started")
        while self._running:
            try:
                self.config.refresh()
                self._run_active_repair_cycle()
            except Exception as exc:
                logger.error("Active target repair loop error: %s", exc)

            sleep_seconds = max(300, self.config.get_int_for_environment("ibkr_active_repair_interval_min", ENVIRONMENT, 5) * 60)
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_active_repair_cycle(self):
        if self._is_warmup_active():
            logger.info("Active target repair skipped while startup warmup is active")
            return
        if not self.session_keeper.is_authenticated:
            logger.info("Active target repair skipped while session is unauthenticated")
            return

        result = self.scan_bar_integrity(
            list(self._active_subscription_symbols),
            scan_scope="active_target",
            persist=True,
            repair=True,
        )
        summary = result.get("summary") or {}
        repair_symbols = list(summary.get("attempted_repair_symbols") or summary.get("repair_candidate_symbols") or [])
        if not repair_symbols:
            logger.info("Active target repair skipped: no repair needed")
            return

        self._last_active_repair_at = time.time()
        self._last_active_repair_symbols = repair_symbols
        self._last_active_repair_reasons = {
            symbol: str((summary.get("initial_repair_reasons") or summary.get("repair_reasons") or {}).get(symbol) or "")
            for symbol in repair_symbols
        }
        history_symbols = list(summary.get("history_fetch_symbols") or [])
        if history_symbols:
            self._last_history_repair_at = self._last_active_repair_at
            self._last_history_repair_symbols = history_symbols

    def _watchlist_backfill_loop(self):
        logger.info("Watchlist backfill loop started")
        while self._running:
            try:
                self.config.refresh()
                self._run_watchlist_backfill_cycle()
            except Exception as exc:
                logger.error("Watchlist backfill loop error: %s", exc)

            sleep_seconds = max(300, self.config.get_int_for_environment("ibkr_watchlist_backfill_interval_min", ENVIRONMENT, 30) * 60)
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_watchlist_backfill_cycle(self):
        if self._is_warmup_active():
            logger.info("Watchlist backfill skipped while startup warmup is active")
            return
        self._refresh_watchlist_pool()

        candidates = self._watchlist_backfill_candidates()
        if not candidates:
            logger.info("Watchlist backfill skipped: no non-target symbols in pool")
            return

        stale_minutes = max(5, self.config.get_int_for_environment("ibkr_watchlist_backfill_stale_min", ENVIRONMENT, 20))
        now_ms = int(time.time() * 1000)
        stale_ms = stale_minutes * 60 * 1000
        eligible = []
        for symbol in candidates:
            latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            if latest_ms <= 0 or (now_ms - latest_ms) >= stale_ms:
                eligible.append(symbol)

        if not eligible:
            logger.info("Watchlist backfill skipped: batch is fresh enough")
        else:
            conid_map = self.conid_resolver.resolve_bulk(eligible)
            if not conid_map:
                logger.warning("Watchlist backfill skipped: no conids resolved")
            else:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                logger.info("Running incremental watchlist backfill for %d symbols", len(conid_map))
                self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
                self.data_writer.flush()
                self._last_backfill_at = time.time()
                self._last_backfill_symbols = sorted(conid_map.keys())

        if not self.config.get_bool_for_environment("ibkr_watchlist_integrity_enabled", ENVIRONMENT, True):
            return

        integrity_candidates = self._watchlist_integrity_candidates()
        if not integrity_candidates:
            logger.info("Watchlist integrity scan skipped: empty candidate batch")
            return

        result = self.scan_bar_integrity(
            integrity_candidates,
            scan_scope="watchlist",
            persist=True,
            repair=True,
        )
        summary = result.get("summary") or {}
        self._last_watchlist_integrity_at = time.time()
        self._last_watchlist_integrity_symbols = list(summary.get("symbols") or integrity_candidates)
        self._last_watchlist_integrity_repair_symbols = list(
            summary.get("attempted_repair_symbols")
            or summary.get("initial_repair_symbols")
            or summary.get("repair_candidate_symbols")
            or []
        )
        self._persist_watchlist_integrity_cursor()

    def _collect_bar_integrity_snapshot(
        self,
        symbol: str,
        min_bars: int,
        gap_lookback: int,
        rollup_repair_enabled: bool,
    ) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        snapshot = self.data_backfill.get_integrity_snapshot(
            normalized_symbol,
            "5m",
            min_bars=min_bars,
            gap_lookback=gap_lookback,
        )
        derived_sync = self._inspect_derived_interval_sync(normalized_symbol) if rollup_repair_enabled else {
            "symbol": normalized_symbol,
            "latest_5m_ms": int(snapshot.get("latest_stored_ms", 0) or 0),
            "missing_intervals": [],
            "stale_intervals": [],
            "latest_interval_ms": {},
            "expected_closed_ms": {},
        }

        reasons = []
        if int(snapshot.get("stored_bar_count", 0) or 0) < min_bars:
            reasons.append(f"bars<{min_bars}")
        if int(snapshot.get("gap_count", 0) or 0) > 0:
            reasons.append(f"gaps={int(snapshot.get('gap_count', 0) or 0)}")
        if int(snapshot.get("duplicate_count", 0) or 0) > 0:
            reasons.append(f"duplicates={int(snapshot.get('duplicate_count', 0) or 0)}")
        if int(snapshot.get("bad_ohlc_count", 0) or 0) > 0:
            reasons.append(f"bad_ohlc={int(snapshot.get('bad_ohlc_count', 0) or 0)}")
        if derived_sync["missing_intervals"]:
            reasons.append(f"rollup_missing={','.join(derived_sync['missing_intervals'])}")
        if derived_sync["stale_intervals"]:
            reasons.append(f"rollup_stale={','.join(derived_sync['stale_intervals'])}")

        needs_history_fetch = (
            int(snapshot.get("stored_bar_count", 0) or 0) < min_bars
            or int(snapshot.get("gap_count", 0) or 0) > 0
        )
        needs_manual_review = (
            int(snapshot.get("duplicate_count", 0) or 0) > 0
            or int(snapshot.get("bad_ohlc_count", 0) or 0) > 0
        )
        needs_pipeline_repair = bool(
            needs_history_fetch
            or derived_sync["missing_intervals"]
            or derived_sync["stale_intervals"]
        )
        integrity_status = "ok"
        if needs_manual_review:
            integrity_status = "error"
        elif needs_pipeline_repair:
            integrity_status = "warn"

        snapshot["derived_sync"] = derived_sync
        snapshot["needs_history_fetch"] = needs_history_fetch
        snapshot["needs_manual_review"] = needs_manual_review
        snapshot["needs_pipeline_repair"] = needs_pipeline_repair
        snapshot["needs_repair"] = needs_pipeline_repair
        snapshot["safe_repair"] = needs_pipeline_repair and not needs_manual_review
        snapshot["repair_reason"] = ",".join(reasons)
        snapshot["integrity_status"] = integrity_status
        return snapshot

    def _build_bar_integrity_row(self, snapshot: dict, scan_scope: str, repair_state: dict | None = None) -> dict:
        symbol = str(snapshot.get("symbol") or "").strip().upper()
        derived_sync = snapshot.get("derived_sync") or {}
        missing_intervals = list(derived_sync.get("missing_intervals") or [])
        stale_intervals = list(derived_sync.get("stale_intervals") or [])
        repair_state = repair_state or {}
        repair_attempted = bool(repair_state.get("attempted"))

        status = str(snapshot.get("integrity_status") or "ok")
        if repair_attempted:
            if bool(snapshot.get("needs_pipeline_repair")):
                status = "repair_failed"
            elif status == "ok":
                status = "repaired"

        return {
            "environment": ENVIRONMENT,
            "market_date": self._bar_integrity_market_date(),
            "symbol": symbol,
            "interval": "5m",
            "scan_scope": str(scan_scope or "manual"),
            "status": status,
            "needs_repair": bool(snapshot.get("needs_pipeline_repair")),
            "safe_repair": bool(snapshot.get("safe_repair")),
            "bar_count": int(snapshot.get("stored_bar_count", 0) or 0),
            "latest_bar_time_ms": int(snapshot.get("latest_stored_ms", 0) or 0),
            "latest_bar_us_time": str(format_us_time(int(snapshot.get("latest_stored_ms", 0) or 0)) or "") if int(snapshot.get("latest_stored_ms", 0) or 0) > 0 else "",
            "oldest_loaded_ms": int(snapshot.get("oldest_loaded_ms", 0) or 0),
            "gap_count": int(snapshot.get("gap_count", 0) or 0),
            "duplicate_count": int(snapshot.get("duplicate_count", 0) or 0),
            "bad_ohlc_count": int(snapshot.get("bad_ohlc_count", 0) or 0),
            "missing_intervals": missing_intervals,
            "stale_intervals": stale_intervals,
            "gap_examples": list(snapshot.get("gap_examples") or []),
            "duplicate_examples": list(snapshot.get("duplicate_examples") or []),
            "bad_ohlc_examples": list(snapshot.get("bad_ohlc_examples") or []),
            "repair_attempts": 1 if repair_attempted else 0,
            "increment_repair_attempts": repair_attempted,
            "last_scan_at": self._now_iso(),
            "last_repair_at": self._now_iso() if repair_attempted else "",
            "last_repair_result": {
                **(repair_state.get("result") or {}),
                "attempted": repair_attempted,
            } if repair_attempted else {},
            "extra": {
                "repair_reason": str(snapshot.get("repair_reason") or ""),
                "needs_history_fetch": bool(snapshot.get("needs_history_fetch")),
                "needs_manual_review": bool(snapshot.get("needs_manual_review")),
                "derived_sync": derived_sync,
                "scanned_row_count": int(snapshot.get("scanned_row_count", 0) or 0),
                "scan_scope": str(scan_scope or "manual"),
                "source": "ibkr_service",
            },
        }

    def _run_bar_integrity_repairs(self, snapshots: dict[str, dict], source: str) -> dict:
        repair_symbols = [
            symbol for symbol, snapshot in snapshots.items()
            if bool(snapshot.get("safe_repair"))
        ]
        history_symbols = [
            symbol for symbol in repair_symbols
            if bool((snapshots.get(symbol) or {}).get("needs_history_fetch"))
        ]
        per_symbol = {
            symbol: {
                "attempted": symbol in repair_symbols,
                "result": {
                    "source": source,
                    "history_needed": bool((snapshots.get(symbol) or {}).get("needs_history_fetch")),
                    "pipeline_needed": bool((snapshots.get(symbol) or {}).get("needs_pipeline_repair")),
                },
            }
            for symbol in snapshots.keys()
        }
        if not repair_symbols:
            return {
                "repair_symbols": [],
                "history_symbols": [],
                "per_symbol": per_symbol,
            }

        backfill_result = {}
        unresolved_history = []
        conid_map = self.conid_resolver.resolve_bulk(history_symbols) if history_symbols else {}
        if history_symbols:
            unresolved_history = [symbol for symbol in history_symbols if symbol not in conid_map]
            for symbol in unresolved_history:
                per_symbol[symbol]["result"]["history_error"] = "conid_unresolved"
            if conid_map:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                backfill_result = self.data_backfill.backfill_all(
                    conid_map,
                    symbol_meta=symbol_meta,
                    intervals=["5m"],
                    repair_symbols=list(conid_map.keys()),
                )
                self.data_writer.flush()
                self._last_history_repair_at = time.time()
                self._last_history_repair_symbols = sorted(conid_map.keys())
                for symbol in conid_map.keys():
                    per_symbol[symbol]["result"]["history_written"] = int(
                        ((backfill_result.get(symbol) or {}).get("5m", 0) or 0)
                    )

        pipeline_result = self._run_symbol_pipeline_repair(repair_symbols, source=source)
        pipeline_ok = bool(pipeline_result.get("ok", False))
        for symbol in repair_symbols:
            per_symbol[symbol]["result"]["pipeline_ok"] = pipeline_ok
            per_symbol[symbol]["result"]["pipeline"] = {
                "processed": int(((pipeline_result.get("compute") or {}).get("processed", 0) or 0)),
                "errors": int(((pipeline_result.get("compute") or {}).get("errors", 0) or 0)),
                "rollup_written": int(((pipeline_result.get("rollup") or {}).get("written", 0) or 0)),
            }

        return {
            "repair_symbols": sorted(repair_symbols),
            "history_symbols": sorted(history_symbols),
            "unresolved_history_symbols": sorted(unresolved_history),
            "per_symbol": per_symbol,
        }

    def scan_bar_integrity(
        self,
        symbols,
        scan_scope: str = "manual",
        persist: bool = True,
        repair: bool = False,
    ) -> dict:
        normalized_symbols = sorted({str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()})
        if not normalized_symbols:
            return {"ok": True, "rows": [], "summary": {"symbols": [], "repair_candidate_symbols": []}}

        self.config.refresh()
        min_bars = max(60, self.config.get_int_for_environment("ibkr_history_repair_min_bars_5m", ENVIRONMENT, 260))
        gap_lookback = max(20, self.config.get_int_for_environment("ibkr_history_repair_gap_lookback", ENVIRONMENT, 80))
        rollup_repair_enabled = self.config.get_bool_for_environment(
            "ibkr_history_repair_rollup_enabled",
            ENVIRONMENT,
            True,
        )

        snapshots = {
            symbol: self._collect_bar_integrity_snapshot(
                symbol,
                min_bars=min_bars,
                gap_lookback=gap_lookback,
                rollup_repair_enabled=rollup_repair_enabled,
            )
            for symbol in normalized_symbols
        }
        initial_repair_symbols = sorted(
            symbol for symbol, snapshot in snapshots.items()
            if bool(snapshot.get("needs_pipeline_repair"))
        )
        initial_repair_reasons = {
            symbol: str((snapshot.get("repair_reason") or ""))
            for symbol, snapshot in snapshots.items()
            if str(snapshot.get("repair_reason") or "")
        }
        repair_summary = {"repair_symbols": [], "history_symbols": [], "per_symbol": {}}
        if repair:
            repair_summary = self._run_bar_integrity_repairs(snapshots, source=f"{scan_scope}_integrity")
            for symbol in repair_summary.get("repair_symbols") or []:
                snapshots[symbol] = self._collect_bar_integrity_snapshot(
                    symbol,
                    min_bars=min_bars,
                    gap_lookback=gap_lookback,
                    rollup_repair_enabled=rollup_repair_enabled,
                )

        rows = [
            self._build_bar_integrity_row(
                snapshots[symbol],
                scan_scope=scan_scope,
                repair_state=(repair_summary.get("per_symbol") or {}).get(symbol),
            )
            for symbol in normalized_symbols
        ]
        if persist and rows:
            try:
                self.pb.upsert_bar_integrity_items(rows)
            except Exception as exc:
                logger.warning("Failed to persist bar integrity rows: %s", exc)

        summary = {
            "symbols": normalized_symbols,
            "scan_scope": scan_scope,
            "attempted_repair_symbols": sorted(repair_summary.get("repair_symbols") or []),
            "repair_candidate_symbols": sorted(
                symbol for symbol, snapshot in snapshots.items()
                if bool(snapshot.get("needs_pipeline_repair"))
            ),
            "initial_repair_symbols": initial_repair_symbols,
            "initial_repair_reasons": initial_repair_reasons,
            "manual_review_symbols": sorted(
                symbol for symbol, snapshot in snapshots.items()
                if bool(snapshot.get("needs_manual_review"))
            ),
            "history_fetch_symbols": sorted(repair_summary.get("history_symbols") or []),
            "repair_reasons": {
                symbol: str((snapshots.get(symbol) or {}).get("repair_reason") or "")
                for symbol in normalized_symbols
                if str((snapshots.get(symbol) or {}).get("repair_reason") or "")
            },
            "status_counts": {
                "ok": sum(1 for row in rows if row.get("status") == "ok"),
                "warn": sum(1 for row in rows if row.get("status") == "warn"),
                "error": sum(1 for row in rows if row.get("status") == "error"),
                "repaired": sum(1 for row in rows if row.get("status") == "repaired"),
                "repair_failed": sum(1 for row in rows if row.get("status") == "repair_failed"),
            },
        }
        return {
            "ok": True,
            "rows": rows,
            "summary": summary,
        }

    def _build_history_repair_plan(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        if not self.config.get_bool_for_environment("ibkr_history_repair_enabled", ENVIRONMENT, True):
            return {}

        min_bars = max(60, self.config.get_int_for_environment("ibkr_history_repair_min_bars_5m", ENVIRONMENT, 260))
        gap_lookback = max(20, self.config.get_int_for_environment("ibkr_history_repair_gap_lookback", ENVIRONMENT, 80))
        rollup_repair_enabled = self.config.get_bool_for_environment(
            "ibkr_history_repair_rollup_enabled",
            ENVIRONMENT,
            True,
        )
        plan = {}

        for symbol in sorted({str(item or "").upper() for item in symbols if str(item or "").strip()}):
            snapshot = self._collect_bar_integrity_snapshot(
                symbol,
                min_bars=min_bars,
                gap_lookback=gap_lookback,
                rollup_repair_enabled=rollup_repair_enabled,
            )
            if bool(snapshot.get("needs_pipeline_repair")):
                plan[symbol] = snapshot
        return plan

    def _inspect_derived_interval_sync(self, symbol: str) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        result = {
            "symbol": normalized_symbol,
            "latest_5m_ms": 0,
            "missing_intervals": [],
            "stale_intervals": [],
            "latest_interval_ms": {},
            "expected_closed_ms": {},
        }
        if not normalized_symbol:
            return result

        base_row = self.pb.get_first_record(
            "ibkr_bars",
            filter=(
                f'symbol = "{normalized_symbol}" && '
                'interval = "5m" && '
                f'{self._build_bar_environment_filter()}'
            ),
            sort="-bar_time_ms",
        )
        latest_5m_ms = int((base_row or {}).get("bar_time_ms", 0) or 0)
        result["latest_5m_ms"] = latest_5m_ms
        if latest_5m_ms <= 0:
            return result

        for interval in HIGHER_INTERVALS:
            row = self.pb.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{normalized_symbol}" && '
                    f'interval = "{interval}" && '
                    f'{self._build_bar_environment_filter()}'
                ),
                sort="-bar_time_ms",
            )
            latest_interval_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            result["latest_interval_ms"][interval] = latest_interval_ms

            current_bucket_ms = bucket_start_ms(latest_5m_ms, interval)
            previous_source_row = self.pb.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{normalized_symbol}" && '
                    'interval = "5m" && '
                    f'bar_time_ms < {int(current_bucket_ms)} && '
                    f'{self._build_bar_environment_filter()}'
                ),
                sort="-bar_time_ms",
            )
            previous_source_ms = int((previous_source_row or {}).get("bar_time_ms", 0) or 0)
            expected_closed_ms = bucket_start_ms(previous_source_ms, interval) if previous_source_ms > 0 else 0
            result["expected_closed_ms"][interval] = expected_closed_ms

            if expected_closed_ms <= 0:
                continue
            if latest_interval_ms <= 0:
                result["missing_intervals"].append(interval)
            elif latest_interval_ms < expected_closed_ms:
                result["stale_intervals"].append(interval)

        return result

    def _build_bar_environment_filter(self) -> str:
        runtime_environment = str(ENVIRONMENT or "").strip().lower() or "live"
        clauses = [f'environment = "{runtime_environment}"']
        if runtime_environment == "live":
            clauses.append('environment = ""')
        return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]

    def _run_symbol_pipeline_repair(self, symbols: list[str], source: str) -> dict:
        normalized_symbols = sorted({str(symbol or "").upper() for symbol in symbols if str(symbol or "").strip()})
        if not normalized_symbols:
            return {"ok": True, "symbols": []}
        try:
            from ibkr_compute.api import server as compute_server

            result = compute_server.repair_symbol_pipeline_from_storage(
                ENVIRONMENT,
                normalized_symbols,
            )
            self._last_pipeline_repair_at = time.time()
            self._last_pipeline_repair_symbols = normalized_symbols
            logger.info(
                "Pipeline repair finished (%s): symbols=%s processed=%s rollup_written=%s errors=%s",
                source,
                ",".join(normalized_symbols),
                ((result.get("compute") or {}).get("processed", 0)),
                ((result.get("rollup") or {}).get("written", 0)),
                ((result.get("compute") or {}).get("errors", 0)),
            )
            return result
        except Exception as exc:
            logger.warning("Pipeline repair failed (%s): %s", source, exc)
            return {"ok": False, "symbols": normalized_symbols, "error": str(exc)}

    def _trigger_realtime_compute(self, source: str = "bar_close") -> dict:
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.app.test_request_context(
                "/compute",
                method="POST",
                json={"source": source, "environments": [ENVIRONMENT]},
            ):
                response = compute_server.compute()
            if hasattr(response, "get_json"):
                return response.get_json() or {}
        except Exception as exc:
            logger.error("Realtime compute trigger failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "empty_response"}

    def _compute_loop(self):
        logger.info("Realtime close-driven compute loop started")
        while self._running or not self._compute_queue.empty():
            try:
                first_item = self._compute_queue.get(timeout=1)
            except queue.Empty:
                continue

            if first_item is None:
                continue

            close_events = 1
            bar_count = int(first_item or 0)
            drain_until = time.time() + 0.25
            while time.time() < drain_until:
                try:
                    next_item = self._compute_queue.get_nowait()
                except queue.Empty:
                    break
                if next_item is None:
                    continue
                close_events += 1
                bar_count += int(next_item or 0)

            try:
                self.data_writer.flush()
                result = self._trigger_realtime_compute()
                self._realtime_compute_runs += 1
                self._last_realtime_compute_at = time.time()
                self._last_realtime_compute_result = result or {}
                logger.info(
                    "Realtime compute finished: events=%d bars=%d processed=%s signals=%s errors=%s elapsed_s=%s",
                    close_events,
                    bar_count,
                    result.get("processed", 0),
                    result.get("signals", 0),
                    result.get("errors", 0),
                    result.get("elapsed_s", 0),
                )
                if int(result.get("signals", 0) or 0) > 0:
                    self._signal_wakeup.set()
            except Exception as exc:
                logger.error("Realtime compute loop error: %s", exc)

    def _on_bar_close(self, bar_data: dict):
        symbol = str(bar_data.get("symbol", "")).upper()
        meta = self._symbol_meta.get(symbol, {})
        payload = {
            **bar_data,
            "environment": ENVIRONMENT,
            "exchange": str(meta.get("exchange") or bar_data.get("exchange") or "").upper(),
        }

        if not self.data_writer.write_bar(payload):
            return

        queued_bars = 1
        for derived_bar in self.timeframe_builder.consume(payload):
            derived_bar["environment"] = ENVIRONMENT
            derived_bar["exchange"] = str(meta.get("exchange") or derived_bar.get("exchange") or "").upper()
            self.data_writer.write_bar(derived_bar)
            queued_bars += 1

        if self._running:
            self._compute_queue.put(queued_bars)

    def _signal_loop(self):
        signal_poll_interval = DEFAULT_SIGNAL_POLL_INTERVAL
        last_logged_interval = None
        while self._running:
            try:
                self.config.refresh()
                self._sync_session_transition()
                signal_poll_interval = max(15, self.config.get_int_for_environment("signal_poll_interval_sec", ENVIRONMENT, DEFAULT_SIGNAL_POLL_INTERVAL))
                if signal_poll_interval != last_logged_interval:
                    logger.info("Signal processing loop running (interval=%ds)", signal_poll_interval)
                    last_logged_interval = signal_poll_interval
                if self.session_keeper.is_authenticated:
                    self._process_signals()
                    self.reverse_handler.check_and_process()
                else:
                    logger.info("Skip signal/reverse processing while session is unauthenticated")
            except Exception as e:
                logger.error("Signal loop error: %s", e)
                signal_poll_interval = DEFAULT_SIGNAL_POLL_INTERVAL
            self._signal_wakeup.wait(timeout=signal_poll_interval)
            self._signal_wakeup.clear()

    def _process_signals(self):
        if not self.session_keeper.is_authenticated:
            logger.info("Skip signal processing while session is unauthenticated")
            return

        pending_signals = self.signal_router.fetch_pending_signals()

        for sig in pending_signals:
            valid, reason = self.signal_processor.validate_signal(sig)
            if not valid:
                if (
                    str(reason or "").startswith("warmup")
                    or reason in {"session_unauthenticated", "runtime_stopped", "no_trade_symbols"}
                ):
                    logger.info("Signal deferred: %s %s - %s", sig.get("symbol"), sig.get("direction"), reason)
                    continue
                logger.info("Signal rejected: %s %s - %s", sig.get("symbol"), sig.get("direction"), reason)
                self.signal_router.mark_processed(sig["signal_id"])
                continue

            symbol = sig["symbol"]
            conid = self.conid_resolver.resolve(symbol)
            if not conid:
                logger.warning("Cannot resolve conid for %s, skipping", symbol)
                continue

            use_paper = ENVIRONMENT == "paper"
            result = self.order_placer.place_bracket_order(
                conid=conid,
                symbol=symbol,
                direction=sig["direction"],
                quantity=sig["shares"],
                entry_price=sig["entry"],
                take_profit_price=sig["take_profit"],
                stop_loss_price=sig["stop_loss"],
                use_paper=use_paper,
                signal_id=sig["signal_id"],
            )

            if result.get("ok"):
                logger.info("Order placed: %s %s bracket_group=%s",
                            symbol, sig["direction"], result.get("bracket_group"))
                try:
                    self._ack_signal_after_order_submission(sig, result)
                except Exception as ack_err:
                    logger.error(
                        "Signal ack failed after order placement: %s signal_id=%s",
                        ack_err,
                        sig.get("signal_id"),
                    )
                self.signal_processor.register_position(symbol, {
                    "direction": sig["direction"],
                    "bracket_group": result.get("bracket_group"),
                })
                self.order_lifecycle.increment_position_count()
            else:
                logger.error("Order failed: %s - %s", symbol, result.get("error"))

            self.signal_router.mark_processed(sig["signal_id"])

    def _ack_signal_after_order_submission(self, sig: dict, result: dict):
        raw = sig.get("raw") or {}
        order_ids = result.get("order_ids") or []
        entry_order_id = str(order_ids[0]) if len(order_ids) > 0 and order_ids[0] else ""
        tp_order_id = str(order_ids[1]) if len(order_ids) > 1 and order_ids[1] else ""
        sl_order_id = str(order_ids[2]) if len(order_ids) > 2 and order_ids[2] else ""
        entry_unique_id = result.get("entry_coid") or result.get("bracket_group") or ""
        tp_unique_id = result.get("tp_coid") or ""
        sl_unique_id = result.get("sl_coid") or ""
        trade_group_id = result.get("bracket_group") or entry_unique_id
        bar_time_ms = int(raw.get("bar_time_ms") or 0)
        us_time = raw.get("us_time") or sig.get("signal_time") or ""
        cn_time = raw.get("cn_time") or ""

        child_orders = []
        if tp_unique_id:
            child_orders.append({
                "unique_id": tp_unique_id,
                "order_id": tp_order_id,
                "broker_order_id": tp_order_id,
                "order_type": "TakeProfit",
                "role": "take_profit",
                "relation_status": "planned",
                "parent_order_unique_id": entry_unique_id,
                "sibling_order_unique_id": sl_unique_id,
                "limit_price": sig["take_profit"],
                "status": "Init",
            })
        if sl_unique_id:
            child_orders.append({
                "unique_id": sl_unique_id,
                "order_id": sl_order_id,
                "broker_order_id": sl_order_id,
                "order_type": "StopLoss",
                "role": "stop_loss",
                "relation_status": "planned",
                "parent_order_unique_id": entry_unique_id,
                "sibling_order_unique_id": tp_unique_id,
                "limit_price": sig["stop_loss"],
                "status": "Init",
            })

        ack_payload = {
            "unique_id": entry_unique_id,
            "order_id": entry_order_id,
            "broker_order_id": entry_order_id,
            "order_type": "Entry",
            "role": "entry",
            "relation_status": "active",
            "direction": sig["direction"],
            "position_side": sig["direction"],
            "quantity": sig["shares"],
            "limit_price": sig["entry"],
            "status": "Submitted",
            "stop_loss": sig["stop_loss"],
            "take_profit": sig["take_profit"],
            "trade_group_id": trade_group_id,
            "entry_order_unique_id": entry_unique_id,
            "us_time": us_time,
            "cn_time": cn_time,
            "bar_time_ms": bar_time_ms,
            "extra": {
                "source": "ibkr_compute",
                "reason": "order_submitted_by_ibkr_compute",
                "ack_source": "ibkr_service",
            },
        }

        ack_result = self.pb.ack_ibkr_signal(
            signal_id=sig["signal_id"],
            status="executed",
            note="order_submitted_by_ibkr_compute",
            order=ack_payload,
            child_orders=child_orders,
            environment=ENVIRONMENT,
        )
        logger.info(
            "Signal acked after order submission: signal_id=%s status=%s fallback=%s",
            sig.get("signal_id"),
            ack_result.get("status", "unknown"),
            ack_result.get("fallback", False),
        )

    def _on_order_fill(self, order: dict):
        logger.info("Order filled: %s", order.get("ticker"))

    def _on_order_cancel(self, order: dict):
        logger.info("Order cancelled: %s", order.get("ticker"))

    def _on_session_expired(self):
        self._last_session_authenticated = False
        self._close_warmup_gate("session_unauthenticated")
        logger.warning("Session expired; requesting manual 2FA")
        self._request_manual_2fa(
            "session_expired",
            "检测到 IBKR 会话失效，等待你点击飞书按钮后再触发 2FA。",
        )

    def _on_gateway_down(self):
        self._last_session_authenticated = False
        self._close_warmup_gate("gateway_down")
        logger.error("Gateway down, restarting gateway and requesting manual 2FA...")
        self.gateway_manager.restart()
        time.sleep(10)
        self.session_keeper.check_auth_status()
        self._request_manual_2fa(
            "gateway_down",
            "Gateway 已重启，等待你点击飞书按钮后再触发 2FA。",
        )

    def _schedule_retention(self):
        def retention_loop():
            while self._running:
                et_now = datetime.now(ET)
                if et_now.hour == 3 and et_now.minute < 5:
                    self.data_retention.cleanup()
                time.sleep(300)

        t = threading.Thread(target=retention_loop, daemon=True, name="data-retention")
        t.start()

    def stop(self):
        logger.info("Stopping IBKR Trading Service...")
        with self._state_lock:
            self._starting = False
        self._running = False
        self._last_session_authenticated = False

        self.auth_handler.cancel()
        self.bar_aggregator.force_close_all()
        self.timeframe_builder.reset()
        self.ws_client.stop()
        self.session_keeper.stop()
        self.order_tracker.stop()
        self.order_lifecycle.stop()
        self.data_writer.close()
        self._signal_wakeup.set()
        self._warmup_wakeup.set()
        self._compute_queue.put(None)

        if self._signal_thread:
            self._signal_thread.join(timeout=10)
        if self._subscription_thread:
            self._subscription_thread.join(timeout=10)
        if self._active_repair_thread:
            self._active_repair_thread.join(timeout=10)
        if self._watchlist_backfill_thread:
            self._watchlist_backfill_thread.join(timeout=10)
        if self._compute_thread:
            self._compute_thread.join(timeout=10)
        if self._warmup_thread:
            self._warmup_thread.join(timeout=10)

        self._set_warmup_state(
            phase="stopped",
            reason="service_stopped",
            finished_at=self._now_iso(),
            trading_gate_open=False,
            trading_gate_reason="runtime_stopped",
        )

        logger.info("IBKR Trading Service stopped")

    def status(self) -> dict:
        return {
            "starting": self._starting,
            "environment": ENVIRONMENT,
            "gateway": self.gateway_manager.status(),
            "session": self.session_keeper.status(),
            "websocket": self.ws_client.status(),
            "bar_aggregator": self.bar_aggregator.status(),
            "data_writer": self.data_writer.status(),
            "data_backfill": self.data_backfill.status(),
            "data_retention": self.data_retention.status(),
            "order_placer": self.order_placer.status(),
            "order_tracker": self.order_tracker.status(),
            "order_lifecycle": self.order_lifecycle.status(),
            "signal_router": self.signal_router.status(),
            "signal_processor": self.signal_processor.status(),
            "warmup": self._copy_warmup_state(),
            "realtime_compute": {
                "runs": self._realtime_compute_runs,
                "queue_size": self._compute_queue.qsize(),
                "last_run": (
                    datetime.fromtimestamp(self._last_realtime_compute_at, ET).isoformat()
                    if self._last_realtime_compute_at else None
                ),
                "last_result": self._last_realtime_compute_result,
            },
            "market_universe": {
                "market_date": self._current_market_date,
                "last_daily_reset": (
                    datetime.fromtimestamp(self._last_daily_reset_at, ET).isoformat()
                    if self._last_daily_reset_at else None
                ),
                "watchlist_pool_count": len(self._watchlist_symbols),
                "active_target_date": self._active_target_date,
                "active_target_count": len(self._active_subscription_symbols),
                "active_target_symbols": list(self._active_subscription_symbols),
                "active_trade_symbols": list(self._active_trade_symbols),
                "last_watchlist_refresh": (
                    datetime.fromtimestamp(self._last_watchlist_refresh_at, ET).isoformat()
                    if self._last_watchlist_refresh_at else None
                ),
                "last_target_refresh": (
                    datetime.fromtimestamp(self._last_target_refresh_at, ET).isoformat()
                    if self._last_target_refresh_at else None
                ),
                "active_repair_interval_min": self.config.get_int_for_environment("ibkr_active_repair_interval_min", ENVIRONMENT, 5),
                "last_active_repair": (
                    datetime.fromtimestamp(self._last_active_repair_at, ET).isoformat()
                    if self._last_active_repair_at else None
                ),
                "last_active_repair_symbols": list(self._last_active_repair_symbols),
                "last_active_repair_reasons": dict(self._last_active_repair_reasons),
                "watchlist_backfill_interval_min": self.config.get_int_for_environment("ibkr_watchlist_backfill_interval_min", ENVIRONMENT, 30),
                "watchlist_integrity_enabled": self.config.get_bool_for_environment("ibkr_watchlist_integrity_enabled", ENVIRONMENT, True),
                "watchlist_integrity_batch_size": self.config.get_int_for_environment(
                    "ibkr_watchlist_integrity_batch_size",
                    ENVIRONMENT,
                    DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE,
                ),
                "last_watchlist_backfill": (
                    datetime.fromtimestamp(self._last_backfill_at, ET).isoformat()
                    if self._last_backfill_at else None
                ),
                "last_watchlist_backfill_symbols": list(self._last_backfill_symbols),
                "last_watchlist_integrity": (
                    datetime.fromtimestamp(self._last_watchlist_integrity_at, ET).isoformat()
                    if self._last_watchlist_integrity_at else None
                ),
                "last_watchlist_integrity_symbols": list(self._last_watchlist_integrity_symbols),
                "last_watchlist_integrity_repair_symbols": list(self._last_watchlist_integrity_repair_symbols),
                "last_history_repair": (
                    datetime.fromtimestamp(self._last_history_repair_at, ET).isoformat()
                    if self._last_history_repair_at else None
                ),
                "last_history_repair_symbols": list(self._last_history_repair_symbols),
                "last_pipeline_repair": (
                    datetime.fromtimestamp(self._last_pipeline_repair_at, ET).isoformat()
                    if self._last_pipeline_repair_at else None
                ),
                "last_pipeline_repair_symbols": list(self._last_pipeline_repair_symbols),
            },
        }

    @property
    def is_starting(self) -> bool:
        return self._starting

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_busy(self) -> bool:
        return self._starting or self._running


_service = None


def main():
    global _service
    _service = IBKRTradingService()

    def shutdown_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        if _service:
            _service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    _service.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        _service.stop()


if __name__ == "__main__":
    main()
