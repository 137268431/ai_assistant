"""
IBKR Trading Service — 主入口
整合所有模块: Gateway会话 + WebSocket数据 + 订单管理 + 信号处理

启动方式: python -m ibkr_compute.ibkr_service
"""

import os
import sys
import time
import signal
import json
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
from ibkr_compute.market.realtime_quote_book import RealtimeQuoteBook
from ibkr_compute.market.data_writer import DataWriter
from ibkr_compute.market.data_backfill import DataBackfill, _regular_session_gap_summary
from ibkr_compute.market.data_retention import DataRetention
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import interval_to_ms
from ibkr_compute.core.indicator_engine import indicator_ready_bar_count
from ibkr_compute.order.order_placer import OrderPlacer
from ibkr_compute.order.order_tracker import OrderTracker
from ibkr_compute.order.order_modifier import OrderModifier
from ibkr_compute.order.order_lifecycle import OrderLifecycle
from ibkr_compute.orchestration.integrity import TradingServiceIntegrityMixin
from ibkr_compute.orchestration.lifecycle import TradingServiceLifecycleMixin
from ibkr_compute.orchestration.market_universe import TradingServiceMarketUniverseMixin
from ibkr_compute.orchestration.runtime_pipeline import TradingServiceRuntimePipelineMixin
from ibkr_compute.orchestration.warmup import TradingServiceWarmupMixin
from ibkr_compute.signal.signal_router import SignalRouter
from ibkr_compute.signal.signal_processor import (
    DEFAULT_TRADE_WINDOW_END,
    DEFAULT_TRADE_WINDOW_START,
    SignalProcessor,
)
from ibkr_compute.signal.reverse_signal import ReverseSignalHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ibkr_service")

ET = timezone(timedelta(hours=-4))

PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
PB_PUBLIC_URL = os.environ.get("PB_PUBLIC_URL", "").rstrip("/")
GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
DEFAULT_SIGNAL_POLL_INTERVAL = 120
DEFAULT_WARMUP_REQUIRED_INTERVAL = "5m"
STARTUP_BACKGROUND_PRIME_INTERVALS = ("15m", "30m", "1h")
STARTUP_BACKGROUND_PRIME_CHUNK_SIZE = 8
STARTUP_HISTORY_REPAIR_SHORT_PERIOD = "1d"
DEFAULT_OFFICIAL_5M_CLOSE_DELAY_SECONDS = max(1, int(os.environ.get("IBKR_OFFICIAL_5M_CLOSE_DELAY_SEC", "8")))
DEFAULT_OFFICIAL_5M_REQUEST_PERIOD = os.environ.get("IBKR_OFFICIAL_5M_REQUEST_PERIOD", "1d").strip() or "1d"
BAR_INTEGRITY_STATE_KEY = "ibkr_bar_integrity_cursor"
BAR_INTEGRITY_STATE_DATE = "global"
DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE = 8
REALTIME_PRIORITY_ENVIRONMENTS = {"live", "paper"}
SESSION_EVENT_ALERT_COOLDOWN_SECONDS = int(os.environ.get("IBKR_SESSION_EVENT_ALERT_COOLDOWN", "1800"))
AUTH_PROBE_INTERVAL_SECONDS = max(2, int(os.environ.get("IBKR_AUTH_PROBE_INTERVAL_SECONDS", "5")))
AUTH_PROBE_WINDOW_SECONDS = max(AUTH_PROBE_INTERVAL_SECONDS, int(os.environ.get("IBKR_AUTH_PROBE_WINDOW_SECONDS", "45")))
AUTH_MANUAL_TAKEOVER_TTL_SECONDS = max(60, int(os.environ.get("IBKR_AUTH_MANUAL_TAKEOVER_TTL_SECONDS", "600")))
AUTH_RECOVERY_LOCK_TTL_SECONDS = max(30, int(os.environ.get("IBKR_AUTH_RECOVERY_LOCK_TTL_SECONDS", "120")))
AUTH_RECOVERY_PB_FIELDS = (
    "cycle_id",
    "recovery_phase",
    "recovery_reason",
    "interruption_kind",
    "manual_takeover_active",
    "manual_takeover_started_at",
    "manual_takeover_until",
    "probe_started_at",
    "probe_last_checked_at",
    "probe_attempts",
    "probe_result",
    "auto_restart_scheduled",
    "last_runtime_authenticated_at",
    "last_gateway_status_code",
    "last_recovery_source",
    "lock_owner",
    "lock_expires_at",
)
WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR = "market_monitor"
VALID_WATCHLIST_SYMBOL_ROLES = {
    WATCHLIST_SYMBOL_ROLE_TRADE,
    WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR,
}
DEFAULT_MARKET_WS_SYMBOLS = ("SPY", "QQQ", "VIX")
DAILY_SCAN_STATE_KEY = "ibkr_daily_scan_state"
DAILY_SCAN_STATE_DATE = "global"
STARTUP_PROGRESS_STATE_KEY = "ibkr_runtime_startup"
STARTUP_PROGRESS_STATE_DATE = "global"
FORCE_FRESH_MANUAL_AUTH_CYCLE = str(
    os.environ.get("IBKR_FORCE_FRESH_MANUAL_AUTH_CYCLE", "true")
).strip().lower() not in {"0", "false", "no", "off"}
FRESH_MANUAL_AUTH_SOURCES = {"feishu_callback", "runtime_page", "feishu_2fa", "codex_validation"}


def normalize_watchlist_symbol_role(value, default: str = WATCHLIST_SYMBOL_ROLE_TRADE) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_WATCHLIST_SYMBOL_ROLES:
        return normalized
    return default


class IBKRTradingService(
    TradingServiceLifecycleMixin,
    TradingServiceWarmupMixin,
    TradingServiceIntegrityMixin,
    TradingServiceRuntimePipelineMixin,
    TradingServiceMarketUniverseMixin,
):
    def __init__(self):
        self.pb = PBClient(base_url=PB_BASE_URL)
        self.config = Config(pb_client=self.pb)
        self.config.refresh()

        self.gateway_manager = GatewayManager()
        self.auth_handler = AuthHandler(
            gateway_url=GATEWAY_URL,
            pb_client=self.pb,
            gateway_manager=self.gateway_manager,
        )
        self.session_keeper = SessionKeeper(
            gateway_url=GATEWAY_URL,
            pb_client=self.pb,
            on_session_expired=self._on_session_expired,
            on_gateway_down=self._on_gateway_down,
        )

        self.conid_resolver = ConidResolver(gateway_url=GATEWAY_URL, pb_client=self.pb)
        self.data_writer = DataWriter(pb_client=self.pb, config=self.config, environment=ENVIRONMENT)
        self.data_backfill = DataBackfill(
            gateway_url=GATEWAY_URL,
            data_writer=self.data_writer,
            config=self.config,
            environment=ENVIRONMENT,
        )
        self.data_retention = DataRetention(
            pb_client=self.pb,
            config=self.config,
            default_environments=[ENVIRONMENT],
        )
        self.timeframe_builder = TimeframeBarBuilder()

        self.bar_aggregator = BarAggregator()
        self.realtime_quote_book = RealtimeQuoteBook(
            prev_close_provider=self._get_prev_close_for_quote,
        )
        self.ws_client = IBKRWebSocketClient(
            gateway_url=GATEWAY_URL,
            on_tick=self._on_ws_market_tick,
            config=self.config,
            environment=ENVIRONMENT,
        )

        self.order_placer = OrderPlacer(
            gateway_url=GATEWAY_URL,
            pb_client=self.pb,
            config=self.config,
            environment=ENVIRONMENT,
        )
        self.order_modifier = OrderModifier(gateway_url=GATEWAY_URL, pb_client=self.pb)
        self.order_tracker = OrderTracker(
            gateway_url=GATEWAY_URL, pb_client=self.pb,
            on_fill=self._on_order_fill,
            on_cancel=self._on_order_cancel,
            config=self.config,
            environment=ENVIRONMENT,
        )
        self.ws_client.set_order_update_callback(self.order_tracker.on_order_update)
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
        self._official_close_thread = None
        self._bar_close_thread = None
        self._warmup_thread = None
        self._auth_required_reason = ""
        self._symbol_meta = {}
        self._watchlist_monitor_symbols = []
        self._watchlist_trade_symbols = []
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
        self._scan_state_lock = threading.Lock()
        self._compute_queue = queue.Queue()
        self._signal_wakeup = threading.Event()
        self._warmup_wakeup = threading.Event()
        self._realtime_compute_runs = 0
        self._last_realtime_compute_at = 0.0
        self._last_realtime_compute_started_at = 0.0
        self._last_realtime_compute_result = {}
        self._last_bar_close_at = 0.0
        self._current_market_date = ""
        self._last_daily_reset_at = 0.0
        self._last_session_authenticated = False
        self._quote_prev_close_cache = {}
        self._quote_prev_close_cache_date = ""
        self._warmup_signature = ()
        self._warmup_state = self._initial_warmup_state()
        self._daily_scan_state = self._load_daily_scan_state(self._market_date())
        self._official_5m_lock = threading.RLock()
        self._official_5m_state = self._initial_official_5m_state()
        self._official_5m_last_cycle_at = 0.0
        self._last_session_issue_kind = ""
        self._last_session_issue_title = ""
        self._last_session_issue_at = 0.0
        self._auth_recovery_lock = threading.RLock()
        self._auth_recovery_state = self._initial_auth_recovery_state()
        self._auth_probe_thread = None
        self._auth_probe_stop = threading.Event()
        self._auth_cycle_seq = 0
        self._auth_restart_thread = None
        self._startup_progress_enabled = False
        self._startup_cycle_id = ""
        self._startup_reason = ""
        self._startup_source = ""
        self._startup_trigger_login = False
        self._startup_status_message_id = ""
        self._manual_start_restart_gateway = True
        self._weekly_reauth_restart_gateway = True
        self._server_boot_resume_only = True
        self._server_boot_publish_startup_card = False
        self._interval_prime_thread = None
        self._interval_prime_lock = threading.Lock()
        self._interval_prime_state = {
            "running": False,
            "intervals": list(STARTUP_BACKGROUND_PRIME_INTERVALS),
            "completed_intervals": [],
            "chunk_size": STARTUP_BACKGROUND_PRIME_CHUNK_SIZE,
            "symbol_count": 0,
            "last_started_at": "",
            "last_finished_at": "",
            "last_duration_s": 0.0,
            "last_error": "",
            "last_source": "",
        }
        self._refresh_runtime_settings()

    def _refresh_runtime_settings(self):
        self._manual_start_restart_gateway = self.config.get_bool_for_environment(
            "ibkr_manual_start_restart_gateway",
            ENVIRONMENT,
            True,
        )
        self._weekly_reauth_restart_gateway = self.config.get_bool_for_environment(
            "ibkr_weekly_reauth_restart_gateway",
            ENVIRONMENT,
            True,
        )
        self._server_boot_resume_only = self.config.get_bool_for_environment(
            "ibkr_server_boot_resume_only",
            ENVIRONMENT,
            True,
        )
        self._server_boot_publish_startup_card = self.config.get_bool_for_environment(
            "ibkr_server_boot_publish_startup_card",
            ENVIRONMENT,
            False,
        )
        self.ws_client.set_order_updates_enabled(self.order_tracker.uses_websocket_updates())

    def _get_prev_close_for_quote(self, symbol: str) -> float | None:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        current_date = self._current_market_date or self._market_date()
        if self._quote_prev_close_cache_date != current_date:
            self._quote_prev_close_cache = {}
            self._quote_prev_close_cache_date = current_date
        if normalized_symbol in self._quote_prev_close_cache:
            return self._quote_prev_close_cache.get(normalized_symbol)

        safe_symbol = normalized_symbol.replace('"', '\\"')
        safe_environment = str(ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        environment_filter = f'(environment = "{safe_environment}"'
        if safe_environment == "live":
            environment_filter += ' || environment = "")'
        else:
            environment_filter += ")"

        prev_close = None
        current_date_ms = int(
            datetime.strptime(current_date, "%Y-%m-%d").replace(tzinfo=ET).timestamp() * 1000
        )
        try:
            row = self.pb.get_first_record(
                "ibkr_bars",
                filter=(
                    f'symbol = "{safe_symbol}" && '
                    'interval = "1d" && '
                    f"{environment_filter} && "
                    f"bar_time_ms < {current_date_ms}"
                ),
                sort="-bar_time_ms",
            )
            close_value = float((row or {}).get("close", 0) or 0)
            if close_value > 0:
                prev_close = close_value
        except Exception as exc:
            logger.debug("Prev close daily lookup failed for %s: %s", normalized_symbol, exc)

        if prev_close is None:
            previous_date_start_ms = max(0, current_date_ms - interval_to_ms("1d"))
            try:
                row = self.pb.get_first_record(
                    "ibkr_bars",
                    filter=(
                        f'symbol = "{safe_symbol}" && '
                        'interval = "5m" && '
                        f"{environment_filter} && "
                        f"bar_time_ms >= {previous_date_start_ms} && "
                        f"bar_time_ms < {current_date_ms}"
                    ),
                    sort="-bar_time_ms",
                )
                close_value = float((row or {}).get("close", 0) or 0)
                if close_value > 0:
                    prev_close = close_value
            except Exception as exc:
                logger.debug("Prev close fallback lookup failed for %s: %s", normalized_symbol, exc)

        self._quote_prev_close_cache[normalized_symbol] = prev_close
        return prev_close

    def _on_ws_market_tick(self, tick_data: dict):
        self.realtime_quote_book.on_tick(tick_data)
        self.bar_aggregator.on_tick(tick_data)

    def _emit_system_event(
        self,
        event_type: str,
        level: str,
        title: str,
        detail: dict,
        *,
        message_id: str = "",
    ):
        if not self.pb:
            return {}
        try:
            return self.pb.notify_system_event(
                title=title,
                detail=detail,
                event_type=event_type,
                level=level,
                source="ibkr_compute",
                environment=ENVIRONMENT,
                message_id=message_id,
            )
        except Exception as exc:
            logger.warning("System event emit failed (%s/%s): %s", event_type, title, exc)
            return {}

    def _notify_session_issue(
        self,
        kind: str,
        title: str,
        summary: str,
        recommendation: str,
        extra_detail: dict | None = None,
    ):
        now = time.time()
        should_send = (
            self._last_session_issue_kind != kind
            or self._last_session_issue_at <= 0
            or (now - self._last_session_issue_at) >= SESSION_EVENT_ALERT_COOLDOWN_SECONDS
        )
        self._last_session_issue_kind = kind
        self._last_session_issue_title = title
        if not should_send:
            return

        detail = {
            "异常结论": summary,
            "检查时间": self._now_et(),
            "Session认证": "no",
            "Runtime阶段": self._runtime_phase_label(),
            "处理建议": recommendation,
        }
        if self._auth_required_reason:
            detail["触发原因"] = self._auth_required_reason
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        if extra_detail:
            detail.update(extra_detail)

        self._emit_system_event("alert", "warning", title, detail)
        self._last_session_issue_at = now

    def _notify_session_recovered(self, previous_kind: str):
        if not previous_kind:
            return
        detail = {
            "状态结论": "IBKR Session 已恢复认证，当前运行态重新正确。",
            "检查时间": self._now_et(),
            "Session认证": "yes",
            "Runtime阶段": self._runtime_phase_label(),
            "恢复来源": previous_kind,
            "后续动作": "系统将继续 warmup、订阅刷新和信号处理。",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        self._emit_system_event("alert", "info", "IBKR Session 已恢复认证", detail)
        self._last_session_issue_kind = ""
        self._last_session_issue_title = ""
        self._last_session_issue_at = 0.0

    def _initial_daily_scan_state(self, market_date: str = "") -> dict:
        return {
            "market_date": str(market_date or self._market_date()),
            "status": "idle",
            "reason": "",
            "started_at": "",
            "finished_at": "",
            "last_error": "",
            "result": {},
        }

    def _load_daily_scan_state(self, market_date: str) -> dict:
        target_date = str(market_date or self._market_date())
        try:
            state = self.pb.get_state(
                DAILY_SCAN_STATE_KEY,
                ENVIRONMENT,
                date=DAILY_SCAN_STATE_DATE,
            )
        except Exception:
            state = None
        payload = state.get("data") if isinstance(state, dict) else {}
        if not isinstance(payload, dict):
            return self._initial_daily_scan_state(target_date)
        loaded = {
            **self._initial_daily_scan_state(target_date),
            **payload,
        }
        if str(loaded.get("market_date") or "") != target_date:
            return self._initial_daily_scan_state(target_date)
        return loaded

    def _copy_daily_scan_state(self) -> dict:
        with self._scan_state_lock:
            return dict(self._daily_scan_state or {})

    def _set_daily_scan_state(self, **updates) -> dict:
        with self._scan_state_lock:
            next_state = dict(self._daily_scan_state or self._initial_daily_scan_state())
            next_state.update(updates)
            next_state["market_date"] = str(next_state.get("market_date") or self._market_date())
            self._daily_scan_state = next_state
            try:
                self.pb.upsert_state(
                    DAILY_SCAN_STATE_KEY,
                    ENVIRONMENT,
                    next_state,
                    date=DAILY_SCAN_STATE_DATE,
                )
            except Exception:
                logger.warning("Persist daily scan state failed", exc_info=True)
            return dict(next_state)

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
        warmup_started_perf = time.perf_counter()
        warmup_timings = {}
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
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            backfill_written=0,
            backfill_result={},
            compute_result={},
            timings={},
            last_duration_s=0.0,
        )

        logger.info(
            "Warmup started: symbols=%d trade=%d monitor=%d target_date=%s",
            snapshot["symbols_total"],
            snapshot["trade_symbols_total"],
            snapshot["monitor_symbols_total"],
            snapshot["target_date"] or "n/a",
        )
        self._sync_startup_progress(
            action="update",
            title="IBKR Runtime 启动中",
            summary="核心线程已启动，Warmup / 预检修复 / 历史回补已开始。",
            current_step="warmup",
            current_blocker="等待 warmup 基线统计与首轮预检修复结果",
            operator_action="等待系统推进预检修复、历史回补与交易门判断",
            steps={
                "warmup": {
                    "status": "running",
                    "detail": (
                        f"symbols={snapshot['symbols_total']} "
                        f"trade={snapshot['trade_symbols_total']} "
                        f"monitor={snapshot['monitor_symbols_total']}"
                    ),
                },
            },
            fields=self._build_startup_progress_fields(
                str(self._warmup_state.get("reason") or "warmup"),
                self._startup_source or "api_start",
                bool(self._startup_trigger_login),
            ),
            reason=self._startup_reason or "warmup",
            source=self._startup_source or "api_start",
            trigger_login=bool(self._startup_trigger_login),
        )

        backfill_result = {}
        backfill_written = 0
        compute_result = {}
        last_error = ""
        startup_gate_open_once = False
        from ibkr_compute.api import server as compute_server

        try:
            step_started = time.perf_counter()
            with compute_server.compute_lock:
                compute_server.load_persisted_compute_cursors(ENVIRONMENT)
            warmup_timings["cursor_load_s"] = round(time.perf_counter() - step_started, 3)

            step_started = time.perf_counter()
            preflight_result = self._run_warmup_preflight_repairs(snapshot)
            warmup_timings["preflight_repair_s"] = round(time.perf_counter() - step_started, 3)
            compute_result["preflight_repair"] = preflight_result
            preflight_blockers = {
                symbol: {
                    "repair_reason": (preflight_result.get("repair_reasons") or {}).get(symbol, "history_repair_pending")
                }
                for symbol in (preflight_result.get("remaining_repair_symbols") or [])
            }
            if preflight_result.get("history_written_total"):
                backfill_written += int(preflight_result.get("history_written_total", 0) or 0)
                self._last_history_repair_at = time.time()
                self._last_history_repair_symbols = sorted(preflight_result.get("attempted_repair_symbols") or [])

            step_started = time.perf_counter()
            storage_bootstrap = compute_server.materialize_engines_from_storage(
                ENVIRONMENT,
                snapshot["symbols"],
                DEFAULT_WARMUP_REQUIRED_INTERVAL,
                hydrate_signal_state=False,
            )
            warmup_timings["storage_bootstrap_s"] = round(time.perf_counter() - step_started, 3)
            if storage_bootstrap:
                compute_result["storage_bootstrap"] = storage_bootstrap

            readiness = self._collect_warmup_readiness(snapshot)
            readiness = self._apply_integrity_readiness(readiness, snapshot, preflight_blockers)
            if readiness["pending_symbols"]:
                step_started = time.perf_counter()
                pending_storage_bootstrap = compute_server.materialize_engines_from_storage(
                    ENVIRONMENT,
                    readiness["pending_symbols"],
                    DEFAULT_WARMUP_REQUIRED_INTERVAL,
                    hydrate_signal_state=False,
                )
                warmup_timings["pending_storage_bootstrap_s"] = round(time.perf_counter() - step_started, 3)
                if pending_storage_bootstrap:
                    compute_result["pending_storage_bootstrap"] = pending_storage_bootstrap
                    readiness = self._collect_warmup_readiness(snapshot)
                    readiness = self._apply_integrity_readiness(readiness, snapshot, preflight_blockers)
            compute_result["warmup_timings"] = dict(warmup_timings)
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
                integrity_pending_symbols=readiness["integrity_pending_symbols"],
                integrity_repair_reasons=readiness["integrity_repair_reasons"],
                preflight_repair=preflight_result,
                trading_gate_open=readiness["trading_gate_open"],
                trading_gate_reason=readiness["trading_gate_reason"],
                compute_result=compute_result,
                timings=warmup_timings,
                last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
            )
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Warmup 已进入预热判断阶段，正在核对 readiness 与完整性阻塞。",
                current_step="warmup",
                current_blocker=(
                    "待完成标的: " + self._format_symbol_list(readiness["pending_symbols"])
                    if readiness["pending_symbols"]
                    else "等待交易门判断"
                ),
                operator_action="等待预检修复、历史回补与交易门开放",
                steps={
                    "warmup": {
                        "status": "running",
                        "detail": (
                            f"ready={readiness['ready_symbols']}/{snapshot['symbols_total']} "
                            f"trade={readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} "
                            f"integrity={self._format_symbol_list(readiness['integrity_pending_symbols'])}"
                        ),
                    },
                },
                fields=self._build_startup_progress_fields(
                    self._startup_reason or "warmup",
                    self._startup_source or "api_start",
                    bool(self._startup_trigger_login),
                    {
                        "预检修复标的": self._format_symbol_list(preflight_result.get("attempted_repair_symbols") or []),
                    },
                ),
                reason=self._startup_reason or "warmup",
                source=self._startup_source or "api_start",
                trigger_login=bool(self._startup_trigger_login),
            )

            if readiness["trading_gate_open"] and not readiness["pending_symbols"]:
                finished_at = self._now_iso()
                phase = readiness["phase"]
                warmup_timings["total_elapsed_s"] = round(time.perf_counter() - warmup_started_perf, 3)
                compute_result["warmup_timings"] = dict(warmup_timings)
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
                    integrity_pending_symbols=readiness["integrity_pending_symbols"],
                    integrity_repair_reasons=readiness["integrity_repair_reasons"],
                    preflight_repair=preflight_result,
                    backfill_written=backfill_written,
                    backfill_result=backfill_result,
                    compute_result=compute_result,
                    timings=warmup_timings,
                    last_duration_s=warmup_timings["total_elapsed_s"],
                )
                logger.info(
                    "Warmup finished early after bootstrap: phase=%s gate=open ready=%d/%d trade_ready=%d/%d",
                    phase,
                    readiness["ready_symbols"],
                    snapshot["symbols_total"],
                    readiness["ready_trade_symbols"],
                    snapshot["trade_symbols_total"],
                )
                self._complete_startup_success(
                    "IBKR Runtime 启动完成",
                    {
                        "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
                        "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
                        "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
                        "预检修复标的": self._format_symbol_list(preflight_result.get("attempted_repair_symbols") or []),
                        "回补写入Bars": backfill_written,
                        "预热开始": started_at,
                        "预热完成": finished_at,
                        "预热耗时": f"{warmup_timings['total_elapsed_s']:.3f}s",
                        "交易门": "open",
                    },
                )
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
                self._signal_wakeup.set()
                return
            if readiness["trading_gate_open"] and readiness["pending_symbols"]:
                startup_gate_open_once = True
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
                    integrity_pending_symbols=readiness["integrity_pending_symbols"],
                    integrity_repair_reasons=readiness["integrity_repair_reasons"],
                    preflight_repair=preflight_result,
                    trading_gate_open=True,
                    trading_gate_reason=readiness["trading_gate_reason"],
                    compute_result=compute_result,
                    timings=warmup_timings,
                    last_duration_s=round(time.perf_counter() - warmup_started_perf, 3),
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
                step_started = time.perf_counter()
                backfill_result = self.data_backfill.backfill_all(
                    pending_map,
                    symbol_meta=snapshot["symbol_meta"],
                    intervals=[DEFAULT_WARMUP_REQUIRED_INTERVAL],
                    repair_symbols=list(pending_map.keys()),
                )
                warmup_timings["pending_backfill_s"] = round(time.perf_counter() - step_started, 3)
                backfill_written += sum(
                    int(count or 0)
                    for per_symbol in backfill_result.values()
                    for count in per_symbol.values()
                )
                self.data_writer.flush()
                if readiness["trading_gate_open"]:
                    self._last_history_repair_at = time.time()
                    self._last_history_repair_symbols = sorted(pending_map.keys())
                step_started = time.perf_counter()
                after_backfill_result = compute_server.materialize_engines_from_storage(
                    ENVIRONMENT,
                    list(pending_map.keys()),
                    DEFAULT_WARMUP_REQUIRED_INTERVAL,
                    hydrate_signal_state=False,
                )
                warmup_timings["after_backfill_bootstrap_s"] = round(time.perf_counter() - step_started, 3)
                compute_result["after_backfill"] = after_backfill_result
        except Exception as exc:
            last_error = str(exc)
            logger.error("Warmup cycle failed: %s", exc)

        readiness = self._collect_warmup_readiness(snapshot)
        final_preflight = dict(compute_result.get("preflight_repair") or {})
        if final_preflight.get("initial_repair_symbols") or backfill_result:
            # Startup readiness should only block on base 5m freshness/integrity.
            # Higher-timeframe rollups are refreshed by the compute path after
            # startup completes and should not keep the runtime stuck forever.
            final_remaining_plan = self._build_startup_history_repair_plan(snapshot["symbols"])
            final_preflight["remaining_repair_symbols"] = sorted(final_remaining_plan.keys())
            final_preflight["repair_reasons"] = {
                symbol: str((data or {}).get("repair_reason") or "history_repair_pending")
                for symbol, data in final_remaining_plan.items()
            }
        final_blockers = {
            symbol: {
                "repair_reason": (final_preflight.get("repair_reasons") or {}).get(symbol, "history_repair_pending")
            }
            for symbol in (final_preflight.get("remaining_repair_symbols") or [])
        }
        readiness = self._apply_integrity_readiness(readiness, snapshot, final_blockers)
        if startup_gate_open_once and not readiness["trading_gate_open"]:
            # Once the full trade set has already opened the gate in this cycle,
            # late storage-driven repair blockers should be treated as background work
            # instead of regressing startup back to "not ready".
            readiness["trading_gate_open"] = True
            if str(readiness.get("trading_gate_reason") or "").strip() != "ready":
                readiness["trading_gate_reason"] = "background_repair"
        finished_at = self._now_iso()
        phase = "failed" if last_error and readiness["ready_symbols"] == 0 else readiness["phase"]
        warmup_timings["total_elapsed_s"] = round(time.perf_counter() - warmup_started_perf, 3)
        compute_result["warmup_timings"] = dict(warmup_timings)
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
            integrity_pending_symbols=readiness["integrity_pending_symbols"],
            integrity_repair_reasons=readiness["integrity_repair_reasons"],
            preflight_repair=final_preflight,
            backfill_written=backfill_written,
            backfill_result=backfill_result,
            compute_result=compute_result,
            timings=warmup_timings,
            last_duration_s=warmup_timings["total_elapsed_s"],
        )

        logger.info(
            "Warmup finished: phase=%s gate=%s ready=%d/%d trade_ready=%d/%d backfill_written=%d compute_processed=%s total_elapsed_s=%.3f",
            phase,
            "open" if readiness["trading_gate_open"] else "closed",
            readiness["ready_symbols"],
            snapshot["symbols_total"],
            readiness["ready_trade_symbols"],
            snapshot["trade_symbols_total"],
            backfill_written,
            compute_result.get("processed", 0) if isinstance(compute_result, dict) else 0,
            warmup_timings["total_elapsed_s"],
        )
        startup_ready = bool(readiness["trading_gate_open"])
        if snapshot["trade_symbols_total"] <= 0:
            # Monitor-only startup must still leave the runtime state, otherwise
            # canonical 5m close polling and compute stay blocked forever.
            startup_ready = True

        if startup_ready:
            startup_title = "IBKR Runtime 启动完成"
            startup_detail = {
                "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
                "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
                "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
                "预检修复标的": self._format_symbol_list((final_preflight.get("attempted_repair_symbols") or [])),
                "回补写入Bars": backfill_written,
                "预热开始": started_at,
                "预热完成": finished_at,
                "预热耗时": f"{warmup_timings['total_elapsed_s']:.3f}s",
                "交易门": "open" if readiness["trading_gate_open"] else "closed",
            }
            if snapshot["trade_symbols_total"] <= 0:
                startup_title = "IBKR Runtime 启动完成（监控模式）"
                startup_detail["后续动作"] = "当前无 trade symbols，runtime 将继续执行 canonical 5m close、指标和 scan 链路。"
            if phase != "ready":
                startup_title = "IBKR Runtime 启动完成（后台继续预热）"
                if snapshot["trade_symbols_total"] <= 0:
                    startup_detail["后续动作"] = "当前无 trade symbols，runtime 将在后台继续 monitor/integrity repair，并保持 canonical 5m close 链路运行。"
                else:
                    startup_detail["后续动作"] = "交易链路已开放，剩余 monitor/integrity repair 在后台继续。"
                startup_detail["待完成标的"] = self._format_symbol_list(readiness.get("pending_symbols") or [])
                startup_detail["完整性阻塞"] = self._format_symbol_list(readiness.get("integrity_pending_symbols") or [])
            if self._complete_startup_success(startup_title, startup_detail):
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
            self._signal_wakeup.set()
        else:
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Warmup 尚未满足交易门开放条件，启动流程停在预热阶段。",
                current_step="warmup",
                current_blocker="交易门尚未开放，仍有待完成标的或完整性阻塞",
                operator_action="等待后续行情 / 回补推进，必要时人工检查阻塞标的",
                steps={
                    "warmup": {
                        "status": "failed" if last_error else "waiting",
                        "detail": (
                            f"ready={readiness['ready_symbols']}/{snapshot['symbols_total']} "
                            f"trade={readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} "
                            f"pending={self._format_symbol_list(readiness['pending_symbols'])}"
                        ),
                    },
                    "trading_gate": {
                        "status": "pending",
                        "detail": "等待交易门开放。",
                    },
                },
                fields=self._build_startup_progress_fields(
                    self._startup_reason or "warmup",
                    self._startup_source or "api_start",
                    bool(self._startup_trigger_login),
                    {
                        "完整性阻塞": self._format_symbol_list(readiness['integrity_pending_symbols']),
                        "最近异常": last_error or "",
                    },
                ),
                reason=self._startup_reason or "warmup",
                source=self._startup_source or "api_start",
                trigger_login=bool(self._startup_trigger_login),
            )

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

    def start(self, trigger_login: bool = False, reason: str = "manual_start", source: str = "api_start"):
        with self._state_lock:
            if self._running:
                logger.info("IBKR Trading Service already running")
                return
            if self._starting:
                logger.info("IBKR Trading Service already starting")
                return
            self._starting = True
            self._startup_progress_enabled = self._should_publish_startup_progress(reason, source, trigger_login)
            self._startup_cycle_id = ""
            self._startup_reason = reason
            self._startup_source = source
            self._startup_trigger_login = bool(trigger_login)
            self._startup_status_message_id = ""

        startup_ok = False
        startup_exit_notice = None
        reset_startup_progress = False

        try:
            logger.info("=" * 60)
            logger.info(
                "IBKR Trading Service starting (env=%s reason=%s source=%s trigger_login=%s)",
                ENVIRONMENT,
                reason or "-",
                source or "-",
                bool(trigger_login),
            )
            logger.info("=" * 60)
            self._set_auth_recovery_state(
                recovery_phase="starting_runtime",
                recovery_reason=reason,
                last_recovery_source=source,
                lock_owner="runtime_start",
                lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
            )

            self.auth_handler.reset_cancel()
            self.config.refresh()
            self._refresh_runtime_settings()
            self._announce_startup_pending(reason, source, trigger_login)

            if self._should_restart_gateway_before_start(reason, source, trigger_login):
                self._sync_startup_progress(
                    action="update",
                    title="IBKR Runtime 启动中",
                    summary="手动启动默认先重启 Gateway，确保本轮启动与 2FA 使用全新会话。",
                    current_step="gateway",
                    current_blocker="正在重启 Gateway 并清空旧 Session",
                    operator_action="等待 Gateway 重启完成后进入当前轮次",
                    steps={
                        "gateway": {
                            "status": "running",
                            "detail": "手动启动 / 每周重验会先重启 Gateway，再进入人工 2FA。",
                        },
                    },
                    fields=self._build_startup_progress_fields(reason, source, trigger_login),
                    reason=reason,
                    source=source,
                    trigger_login=trigger_login,
                )
                prepared, gateway_detail = self._restart_gateway_with_clean_session(reason, source)
                if not prepared:
                    logger.error("Gateway preflight restart failed, exiting")
                    startup_exit_notice = {
                        "level": "error",
                        "title": "IBKR Runtime 启动失败",
                        "detail": {
                            "异常结论": "Gateway 重启失败，Runtime 未能进入新的启动轮次。",
                            "检查时间": self._now_et(),
                            "失败阶段": "gateway",
                            "启动原因": reason or "manual_start",
                            "启动来源": source or "api_start",
                            **gateway_detail,
                        },
                    }
                    return

            if not self._ensure_gateway():
                logger.error("Gateway setup failed, exiting")
                startup_exit_notice = {
                    "level": "error",
                    "title": "IBKR Runtime 启动失败",
                    "detail": {
                        "异常结论": "Gateway 启动失败，Runtime 未能进入运行态。",
                        "检查时间": self._now_et(),
                        "失败阶段": "gateway",
                        "启动原因": reason or "manual_start",
                        "启动来源": source or "api_start",
                    },
                }
                return
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Gateway 已可用，正在检查 Session / 2FA 状态。",
                current_step="auth",
                current_blocker="等待确认当前 Gateway Session 是否已认证",
                operator_action="等待系统完成认证检查；如未认证则转入手动 2FA",
                steps={
                    "gateway": {
                        "status": "done",
                        "detail": "Gateway 已启动并可访问。",
                    },
                    "auth": {
                        "status": "running",
                        "detail": "正在检查 Session / 2FA 认证状态。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            # Do a one-shot auth check WITHOUT starting the keeper loop.
            # Starting session_keeper here would cause it to periodically
            # tickle + check_auth during the 2FA wait, creating a second/third
            # HTTP client hitting the Gateway and interfering with the SSO flow.
            self.session_keeper.check_auth_status()
            time.sleep(1)

            if not self.session_keeper.is_authenticated:
                if not trigger_login:
                    if self._should_promote_auth_wait_to_startup_cycle(reason, source, trigger_login):
                        logger.info(
                            "Promoting hidden auth wait into visible startup cycle (reason=%s source=%s)",
                            reason or "-",
                            source or "-",
                        )
                        self._startup_progress_enabled = True
                        self._announce_startup_pending(reason, source, False)
                    logger.warning("Not authenticated and trigger_login disabled; requesting manual 2FA")
                    self._request_manual_2fa(reason, "检测到会话未认证，请在准备好时点击按钮触发 2FA。")
                    self._set_auth_recovery_state(
                        recovery_phase="requested",
                        recovery_reason=reason,
                        probe_result="manual_trigger_required",
                        last_recovery_source=source,
                        lock_owner="",
                        lock_expires_at="",
                    )
                    waiting_detail = {
                        "状态结论": "检测到 Gateway 当前未认证，Runtime 尚未完成启动。",
                        "检查时间": self._now_et(),
                        "当前动作": "等待当前启动卡片下方的人工按钮触发当前轮次。",
                    }
                    self._sync_startup_progress(
                        action="update",
                        title="IBKR Runtime 启动中",
                        summary="Gateway 尚未认证，启动流程等待人工触发 2FA。",
                        current_step="auth",
                        current_blocker="等待手动触发 2FA",
                        operator_action="点击当前启动卡片下方“开始 2FA 验证”",
                        steps={
                            "auth": {
                                "status": "waiting",
                                "detail": "当前不会自动发送新的 Push，需人工点击 2FA 卡片触发。",
                            },
                        },
                        fields=self._build_startup_progress_fields(reason, source, False, waiting_detail),
                        reason=reason,
                        source=source,
                        trigger_login=False,
                        record_event=True,
                        event_type="status_change",
                        event_title="IBKR Runtime 等待手动 2FA",
                        event_detail=waiting_detail,
                        level="warning",
                    )
                    return
                if self._should_force_fresh_manual_auth_cycle(trigger_login, source):
                    self._sync_startup_progress(
                        action="update",
                        title="IBKR Runtime 启动中",
                        summary="正在清空旧 Session 并重启 Gateway，确保本轮 2FA 使用全新会话。",
                        current_step="auth",
                        current_blocker="等待旧 Session 清理完成并生成新的 2FA 会话",
                        operator_action="等待系统完成 Gateway 重置后自动进入当前轮次",
                        steps={
                            "auth": {
                                "status": "running",
                                "detail": "当前轮次会先重置旧 Session，再发起新的 2FA。",
                            },
                        },
                        fields=self._build_startup_progress_fields(reason, source, True),
                        reason=reason,
                        source=source,
                        trigger_login=True,
                    )
                    prepared, fresh_detail = self._prepare_fresh_manual_auth_cycle(reason, source)
                    if not prepared:
                        logger.error("Fresh manual auth preparation failed, exiting")
                        self._set_auth_recovery_state(
                            recovery_phase="failed",
                            recovery_reason=reason,
                            probe_result="fresh_auth_prepare_failed",
                            last_recovery_source=source,
                            lock_owner="",
                            lock_expires_at="",
                        )
                        self._sync_startup_progress(
                            action="update",
                            title="IBKR Runtime 启动中",
                            summary="旧 Session 清理失败，当前轮次未启动。",
                            current_step="auth",
                            current_blocker="Gateway 重置失败",
                            operator_action="稍后重新点击“开始 2FA 验证”",
                            steps={
                                "auth": {
                                    "status": "failed",
                                    "detail": "本轮在清理旧 Session / 重启 Gateway 时失败。",
                                },
                            },
                            fields=self._build_startup_progress_fields(reason, source, True, fresh_detail),
                            reason=reason,
                            source=source,
                            trigger_login=True,
                            record_event=True,
                            event_type="status_change",
                            event_title="IBKR Runtime 重置旧 Session 失败",
                            event_detail=fresh_detail,
                            level="error",
                        )
                        return
                logger.info("Not authenticated, attempting login (session_keeper paused)...")
                self._sync_startup_progress(
                    action="update",
                    title="IBKR Runtime 启动中",
                    summary="已显式触发 2FA，等待当前轮次完成认证。",
                    current_step="auth",
                    current_blocker="等待手机确认 2FA Push",
                    operator_action="查看手机通知；如切到 Challenge/Response，则去 Runtime 页面提交 Response Code",
                    steps={
                        "auth": {
                            "status": "running",
                            "detail": "当前轮次已启动，不会自动补发新的 Push。",
                        },
                    },
                    fields=self._build_startup_progress_fields(reason, source, True),
                    reason=reason,
                    source=source,
                    trigger_login=True,
                )
                if not self.auth_handler.login(
                    reason=reason,
                    source=source,
                    detail=self._build_2fa_detail(reason),
                ):
                    logger.error("Login failed, exiting")
                    self._auth_required_reason = reason
                    self._set_auth_recovery_state(
                        recovery_phase="failed",
                        recovery_reason=reason,
                        probe_result="login_failed",
                        last_recovery_source=source,
                        lock_owner="",
                        lock_expires_at="",
                    )
                    retry_detail = {
                        "状态结论": "当前 2FA 轮次未成功建立可用 Session，启动流程暂停。",
                        "检查时间": self._now_et(),
                        "当前动作": "请重新点击当前启动卡片下方按钮，手动触发下一轮。",
                    }
                    self._sync_startup_progress(
                        action="update",
                        title="IBKR Runtime 启动中",
                        summary="当前 2FA 轮次未完成认证，启动流程等待人工重新触发。",
                        current_step="auth",
                        current_blocker="2FA 未完成，需手动重新触发",
                        operator_action="回到当前启动卡片，重新点击下方“开始 2FA 验证”",
                        steps={
                            "auth": {
                                "status": "failed",
                                "detail": "本轮不会自动重试新的 Push，请人工重新触发。",
                            },
                        },
                        fields=self._build_startup_progress_fields(reason, source, True, retry_detail),
                        reason=reason,
                        source=source,
                        trigger_login=True,
                        record_event=True,
                        event_type="status_change",
                        event_title="IBKR Runtime 等待重新触发 2FA",
                        event_detail=retry_detail,
                        level="warning",
                    )
                    return

                # Login succeeded — re-check auth once with the shared cookie store
                # before enabling the periodic keeper loop.
                self.session_keeper.check_auth_status()
                time.sleep(1)
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
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Session / 2FA 已认证，正在装载订阅与核心线程。",
                current_step="subscriptions",
                current_blocker="等待 Targets / Subscriptions 装载完成",
                operator_action="等待系统继续装载订阅、线程与预热",
                steps={
                    "auth": {
                        "status": "done",
                        "detail": "Session / 2FA 已通过认证。",
                    },
                    "subscriptions": {
                        "status": "running",
                        "detail": "正在装载活动标的与订阅。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            self._last_session_authenticated = bool(self.session_keeper.is_authenticated)
            if self._last_session_authenticated:
                self._mark_auth_recovered(source=source, reason=reason or "startup_authenticated")
            self.conid_resolver.load_cache_from_pb()
            self._reset_for_new_market_day(force=True)
            self._refresh_watchlist_pool(force=True)
            self._restore_watchlist_integrity_cursor()
            self.ws_client.start()
            time.sleep(2)
            self._refresh_target_subscriptions(force=True, reason="startup")
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="Targets / Subscriptions 已装载，正在启动 WebSocket、订单与后台线程。",
                current_step="core_threads",
                current_blocker="等待 WebSocket、订单链路与后台线程全部启动",
                operator_action="等待系统拉起核心线程与 warmup 线程",
                steps={
                    "subscriptions": {
                        "status": "done",
                        "detail": f"活动订阅标的: {len(self._active_subscription_symbols)}",
                    },
                    "core_threads": {
                        "status": "running",
                        "detail": "正在启动 WebSocket、订单与后台线程。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            self.order_tracker.start()
            self.order_lifecycle.start()

            self._running = True
            startup_ok = True
            self.session_keeper.start()
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
            self._official_close_thread = threading.Thread(
                target=self._official_5m_close_loop,
                daemon=True,
                name="official-5m-close",
            )
            self._official_close_thread.start()
            self._bar_close_thread = threading.Thread(
                target=self._bar_close_loop,
                daemon=True,
                name="bar-close-guard",
            )
            self._bar_close_thread.start()
            self._warmup_thread = threading.Thread(
                target=self._warmup_loop,
                daemon=True,
                name="runtime-warmup",
            )
            self._warmup_thread.start()
            self._sync_startup_progress(
                action="update",
                title="IBKR Runtime 启动中",
                summary="核心线程已启动，正在进入 Warmup / 预检修复 / 历史回补。",
                current_step="warmup",
                current_blocker="等待 Warmup、预检修复与历史回补完成",
                operator_action="等待 warmup 线程推进预检修复与交易门开放",
                steps={
                    "core_threads": {
                        "status": "done",
                        "detail": "WebSocket、订单链路与后台线程已启动。",
                    },
                    "warmup": {
                        "status": "running",
                        "detail": "正在执行 Warmup、预检修复与历史回补。",
                    },
                },
                fields=self._build_startup_progress_fields(reason, source, trigger_login),
                reason=reason,
                source=source,
                trigger_login=trigger_login,
            )

            self._schedule_retention()

            if not self._active_subscription_symbols:
                self._complete_startup_success(
                    "IBKR Runtime 启动完成（无活动标的）",
                    {
                        "Warmup结果": "0/0 ready",
                        "交易标的": "0/0 ready",
                        "监控标的": "0/0 ready",
                        "预检修复标的": "-",
                        "回补写入Bars": 0,
                        "交易门": "closed",
                    },
                )
            logger.info("IBKR Trading Service core components started; waiting for warmup readiness")
        except Exception as exc:
            startup_exit_notice = {
                "event_type": "alert",
                "level": "error",
                "title": "IBKR Runtime 启动失败",
                "detail": {
                    "异常结论": "启动过程中发生未处理异常，Runtime 未能完成启动。",
                    "检查时间": self._now_et(),
                    "失败阶段": "startup_exception",
                    "启动原因": reason or "manual_start",
                    "启动来源": source or "api_start",
                    "异常": str(exc),
                },
            }
            raise
        finally:
            exit_notice = None
            with self._state_lock:
                if not startup_ok and not self._running:
                    self._starting = False
                    self._startup_cycle_id = ""
                    self._startup_reason = ""
                    self._startup_source = ""
                    self._startup_trigger_login = False
                    self._startup_status_message_id = ""
                    if startup_exit_notice:
                        exit_notice = dict(startup_exit_notice)
                    reset_startup_progress = True
            if exit_notice:
                exit_detail = dict(exit_notice.get("detail", {}) or {})
                failure_stage = str(exit_detail.get("失败阶段") or "").strip().lower()
                failed_step = "core_threads"
                if failure_stage == "gateway":
                    failed_step = "gateway"
                elif failure_stage == "session_login":
                    failed_step = "auth"
                elif failure_stage == "startup_exception":
                    failed_step = "core_threads"
                operator_action = "检查失败阶段并在处理后重新启动 Runtime"
                if failed_step == "gateway":
                    operator_action = "检查 Gateway 进程与服务状态后重新启动 Runtime"
                elif failed_step == "auth":
                    operator_action = "检查 2FA 当前轮次并手动重新触发"
                self._sync_startup_progress(
                    action="fail",
                    title=exit_notice.get("title", "IBKR Runtime 启动失败"),
                    summary=str(exit_detail.get("异常结论") or "启动流程未能完成。"),
                    current_step=failed_step,
                    current_blocker=str(exit_detail.get("异常结论") or "启动流程未能完成。"),
                    operator_action=operator_action,
                    steps={
                        failed_step: {
                            "status": "failed",
                            "detail": str(exit_detail.get("异常") or exit_detail.get("失败阶段") or "启动失败"),
                        },
                    },
                    fields=self._build_startup_progress_fields(reason, source, trigger_login, exit_detail),
                    reason=reason,
                    source=source,
                    trigger_login=trigger_login,
                    record_event=True,
                    event_type="alert",
                    event_title=exit_notice.get("title", "IBKR Runtime 启动失败"),
                    event_detail=exit_detail,
                    level=exit_notice.get("level", "error"),
                )
            if reset_startup_progress:
                with self._state_lock:
                    self._startup_progress_enabled = False

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
        self.realtime_quote_book.reset()
        self._quote_prev_close_cache = {}
        self._quote_prev_close_cache_date = current_date
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
        if previous_date and previous_date != current_date:
            self._set_daily_scan_state(**self._initial_daily_scan_state(current_date))
        else:
            with self._scan_state_lock:
                self._daily_scan_state = self._load_daily_scan_state(current_date)
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
        self._official_5m_state = self._initial_official_5m_state()
        if previous_date and previous_date != current_date:
            self._persist_watchlist_integrity_cursor()
        return True

    def _should_defer_background_repairs(self) -> tuple[bool, dict]:
        queue_size = int(self._compute_queue.qsize())
        active_subscription_count = len(self._active_subscription_symbols)
        active_target_count = len(self._active_trade_symbols)
        websocket_connected = bool(self.ws_client.is_connected)
        authenticated = bool(self.session_keeper.is_authenticated)
        runtime_active = bool(
            self._running
            and ENVIRONMENT in REALTIME_PRIORITY_ENVIRONMENTS
            and authenticated
            and websocket_connected
            and active_subscription_count > 0
        )
        snapshot = {
            "reason": "realtime_priority_active" if runtime_active else "",
            "queue_size": queue_size,
            "active_target_count": active_target_count,
            "active_subscription_count": active_subscription_count,
            "websocket_connected": websocket_connected,
            "authenticated": authenticated,
        }
        return runtime_active, snapshot

    def _signal_loop(self):
        signal_poll_interval = DEFAULT_SIGNAL_POLL_INTERVAL
        last_logged_interval = None
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
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
            duplicate_order = self.order_tracker.find_duplicate_open_entry(
                symbol=symbol,
                direction=sig["direction"],
                quantity=sig["shares"],
                entry_price=sig["entry"],
                entry_order_type="LMT",
            )
            if duplicate_order:
                broker_order_id = str(duplicate_order.get("orderId") or duplicate_order.get("id") or "").strip()
                broker_status = str(duplicate_order.get("status") or "").strip()
                broker_price = duplicate_order.get("price")
                try:
                    self.order_tracker.sync_live_orders_snapshot([duplicate_order])
                except Exception as sync_err:
                    logger.error("Duplicate broker order sync failed: %s", sync_err)
                self._mark_signal_duplicate_open_order(sig, duplicate_order)
                logger.warning(
                    "Skip duplicate order submission: signal_id=%s symbol=%s direction=%s broker_order_id=%s status=%s price=%s",
                    sig.get("signal_id"),
                    symbol,
                    sig.get("direction"),
                    broker_order_id or "-",
                    broker_status or "-",
                    broker_price,
                )
                self.signal_router.mark_processed(sig["signal_id"])
                continue

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
                    self.order_tracker.register_submitted_orders(result.get("order_ids") or [], {
                        "symbol": symbol,
                        "direction": sig["direction"],
                        "quantity": sig["shares"],
                        "entry_price": sig["entry"],
                        "tp_price": sig["take_profit"],
                        "sl_price": sig["stop_loss"],
                        "entry_unique_id": result.get("entry_coid") or result.get("bracket_group") or "",
                        "tp_unique_id": result.get("tp_coid") or "",
                        "sl_unique_id": result.get("sl_coid") or "",
                    })
                except Exception as track_err:
                    logger.error("Order tracker register failed: %s", track_err)
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

    def _mark_signal_duplicate_open_order(self, sig: dict, broker_order: dict):
        if not self.pb:
            return

        signal_id = str(sig.get("signal_id") or "").strip()
        if not signal_id:
            return

        safe_signal_id = signal_id.replace('"', '\\"')
        safe_environment = str(ENVIRONMENT or "live").replace('"', '\\"')

        try:
            record = self.pb.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{safe_signal_id}" && '
                    f'environment = "{safe_environment}"'
                ),
            )
            if not record or not record.get("id"):
                return

            existing_extra = record.get("extra") or {}
            if isinstance(existing_extra, str):
                try:
                    existing_extra = json.loads(existing_extra)
                except Exception:
                    existing_extra = {}
            if not isinstance(existing_extra, dict):
                existing_extra = {}

            broker_order_id = str(broker_order.get("orderId") or broker_order.get("id") or "").strip()
            broker_coid = str(
                broker_order.get("cOID")
                or broker_order.get("coid")
                or broker_order.get("order_ref")
                or broker_order.get("orderRef")
                or ""
            ).strip()
            patch = {
                "status": "rejected",
                "note": "duplicate_existing_broker_order",
                "extra": {
                    **existing_extra,
                    "status_reason": "duplicate_existing_broker_order",
                    "duplicate_broker_order_detected": True,
                    "duplicate_broker_order_id": broker_order_id,
                    "duplicate_broker_order_status": str(broker_order.get("status") or "").strip(),
                    "duplicate_broker_order_price": broker_order.get("price"),
                    "duplicate_broker_order_quantity": broker_order.get("totalSize") if broker_order.get("totalSize") is not None else broker_order.get("quantity"),
                    "duplicate_broker_order_side": str(broker_order.get("side") or "").strip(),
                    "duplicate_broker_order_type": str(broker_order.get("orderType") or "").strip(),
                    "duplicate_broker_order_coid": broker_coid,
                    "duplicate_detected_at": self._now_iso(),
                    "duplicate_action": "skip_submit_existing_broker_order",
                },
            }
            self.pb.update_record("ibkr_signals", record["id"], patch)
        except Exception as exc:
            logger.error(
                "Failed to mark signal duplicate-open-order: signal_id=%s error=%s",
                signal_id,
                exc,
            )

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
        if self._starting and not self._running:
            logger.info("Ignoring session_expired callback during runtime startup")
            return
        self._last_session_authenticated = False
        self._close_warmup_gate("session_unauthenticated")
        logger.warning("Session expired; starting auth recovery probe")
        self._start_auth_recovery(
            interruption_kind="session_expired",
            recovery_reason="session_expired",
            source="session_keeper",
        )
        self._notify_session_issue(
            "session_expired",
            "IBKR Session 已失效，需重新触发 2FA",
            "检测到 IBKR Session 已失效，运行态已降为未认证，实时行情和交易链路可能不可用。",
            "系统会先尝试静默探测恢复；若仍未恢复，会自动重开一轮 2FA。你也可以在 Runtime 页面开启人工接管或手动全量重置。",
            {
                "恢复动作": "已进入静默探测窗口",
            },
        )

    def _on_gateway_down(self):
        if self._starting and not self._running:
            logger.info("Ignoring gateway_down callback during runtime startup")
            return
        self._last_session_authenticated = False
        self._close_warmup_gate("gateway_down")
        logger.error("Gateway down, restarting gateway and starting auth recovery probe...")
        self.gateway_manager.restart()
        time.sleep(10)
        self.session_keeper.check_auth_status()
        self._start_auth_recovery(
            interruption_kind="gateway_down",
            recovery_reason="gateway_down",
            source="gateway_down",
        )
        self._notify_session_issue(
            "gateway_down",
            "IBKR Gateway 不可达，已触发重启",
            "检测到 Gateway 一度不可达，已执行自动重启；当前运行态不可用，通常需要重新完成 2FA。",
            "系统会先尝试静默探测恢复；若仍未恢复，会自动重开一轮 2FA。必要时可在 Runtime 页面执行全量清空后重试。",
            {
                "Gateway动作": "已自动重启",
                "恢复动作": "已进入静默探测窗口",
            },
        )

    def _schedule_retention(self):
        def retention_loop():
            last_handled_hour = ""
            while self._running:
                et_now = datetime.now(ET)
                hour_key = et_now.strftime("%Y-%m-%d %H")
                if et_now.minute == 12 and hour_key != last_handled_hour:
                    result = self.data_retention.cleanup(source="runtime_thread")
                    env_result = (result.get("environments") or [{}])[0]
                    logger.info(
                        "Runtime retention cleanup finished: environment=%s deleted=%s errors=%s skipped=%s reason=%s",
                        ENVIRONMENT,
                        int(result.get("total_deleted", 0) or 0),
                        int(result.get("total_errors", 0) or 0),
                        bool(env_result.get("skipped")),
                        str(env_result.get("reason") or ""),
                    )
                    last_handled_hour = hour_key
                time.sleep(30)

        t = threading.Thread(target=retention_loop, daemon=True, name="data-retention")
        t.start()

    def stop(self):
        logger.info("Stopping IBKR Trading Service...")
        with self._state_lock:
            self._starting = False
            self._startup_progress_enabled = False
            self._startup_cycle_id = ""
            self._startup_reason = ""
            self._startup_source = ""
            self._startup_trigger_login = False
            self._startup_status_message_id = ""
        self._running = False
        self._last_session_authenticated = False
        self._auth_probe_stop.set()

        self.auth_handler.cancel()
        self.bar_aggregator.force_close_all()
        self.timeframe_builder.reset()
        self.realtime_quote_book.reset()
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
        if self._official_close_thread:
            self._official_close_thread.join(timeout=10)
        if self._bar_close_thread:
            self._bar_close_thread.join(timeout=10)
        if self._warmup_thread:
            self._warmup_thread.join(timeout=10)
        if self._interval_prime_thread:
            self._interval_prime_thread.join(timeout=10)
        if self._auth_probe_thread and self._auth_probe_thread is not threading.current_thread():
            self._auth_probe_thread.join(timeout=5)

        self._set_warmup_state(
            phase="stopped",
            reason="service_stopped",
            finished_at=self._now_iso(),
            trading_gate_open=False,
            trading_gate_reason="runtime_stopped",
        )
        self._set_auth_recovery_state(
            recovery_phase="runtime_stopped",
            last_recovery_source="runtime_stop",
            lock_owner="",
            lock_expires_at="",
        )

        logger.info("IBKR Trading Service stopped")

    def status(self) -> dict:
        now_ts = time.time()
        queue_size = int(self._compute_queue.qsize())
        last_bar_close_at = float(self._last_bar_close_at or 0.0)
        last_run_at = float(self._last_realtime_compute_at or 0.0)
        last_started_at = float(self._last_realtime_compute_started_at or 0.0)
        compute_thread_alive = bool(self._compute_thread and self._compute_thread.is_alive())
        inflight = bool(last_started_at and last_started_at > last_run_at)
        inflight_age_s = (
            round(max(0.0, now_ts - last_started_at), 1)
            if inflight else None
        )
        lag_since_last_run_s = 0.0
        if last_bar_close_at and last_run_at and last_bar_close_at > last_run_at:
            lag_since_last_run_s = round(max(0.0, last_bar_close_at - last_run_at), 1)
        stalled = False
        stall_reason = ""
        if queue_size > 0 and not self._starting:
            if not compute_thread_alive:
                stalled = True
                stall_reason = "thread_dead"
            elif inflight and inflight_age_s is not None and inflight_age_s >= 300:
                stalled = True
                stall_reason = "inflight_timeout"
            elif lag_since_last_run_s >= 600:
                stalled = True
                stall_reason = "lagging"
        official_5m = self._copy_official_5m_state()
        due_bucket_ms = int(official_5m.get("last_due_bucket_ms", 0) or 0)
        completed_bucket_ms = int(official_5m.get("last_completed_bucket_ms", 0) or 0)
        official_5m["lag_s"] = round(max(0.0, (due_bucket_ms - completed_bucket_ms) / 1000.0), 1) if due_bucket_ms > completed_bucket_ms else 0.0
        realtime_quotes = self.realtime_quote_book.status()
        warmup_state = self._copy_warmup_state()
        daily_scan_state = self._copy_daily_scan_state()
        data_symbols = self._data_universe_symbols()
        scan_symbols = self._normalize_symbol_list(self._watchlist_trade_symbols)
        market_ws_symbols = self._market_ws_symbols()
        blocking_canonical_pending_symbols = self._non_monitor_pending_symbols(
            official_5m.get("pending_symbols") or [],
            market_ws_symbols,
        )
        if str(daily_scan_state.get("status") or "").strip().lower() == "running":
            pipeline_stage = "run_daily_scan"
            pipeline_status = "running"
        elif str(warmup_state.get("phase") or "").strip().lower() in {"pending", "running"}:
            pipeline_stage = "materialize_indicators"
            pipeline_status = "running"
        elif str(daily_scan_state.get("status") or "").strip().lower() == "completed":
            pipeline_stage = "run_target_realtime"
            pipeline_status = "ready"
        elif str(warmup_state.get("phase") or "").strip().lower() in {"ready", "degraded"}:
            pipeline_stage = "activate_targets"
            pipeline_status = str(warmup_state.get("phase") or "ready")
        else:
            pipeline_stage = "resolve_universe"
            pipeline_status = str(warmup_state.get("phase") or "idle")
        bar_freshness_status = (
            "fresh"
            if completed_bucket_ms > 0
            and float(official_5m.get("lag_s", 0) or 0) <= 90
            and not blocking_canonical_pending_symbols
            else "stale"
        )
        indicator_freshness_status = (
            "fresh"
            if last_run_at > 0 and lag_since_last_run_s <= 90 and not stalled
            else "stale"
        )
        return {
            "gateway_control_available": True,
            "starting": self._starting,
            "startup_complete": bool(self._running and not self._starting),
            "runtime_phase": self._runtime_phase_label(),
            "startup_strategy": self.startup_strategy(),
            "auto_restore_guard": self.auto_restore_guard(),
            "environment": ENVIRONMENT,
            "gateway": self.gateway_manager.status(),
            "auth_recovery": self._copy_auth_recovery_state(),
            "session": self.session_keeper.status(),
            "websocket": self.ws_client.status(),
            "bar_aggregator": self.bar_aggregator.status(),
            "realtime_quotes": realtime_quotes,
            "canonical_5m": official_5m,
            "data_writer": self.data_writer.status(),
            "data_backfill": self.data_backfill.status(),
            "data_retention": self.data_retention.status(),
            "order_placer": self.order_placer.status(),
            "order_tracker": self.order_tracker.status(),
            "order_lifecycle": self.order_lifecycle.status(),
            "signal_router": self.signal_router.status(),
            "signal_processor": self.signal_processor.status(),
            "warmup": warmup_state,
            "daily_scan": daily_scan_state,
            "realtime_compute": {
                "runs": self._realtime_compute_runs,
                "queue_size": queue_size,
                "thread_alive": compute_thread_alive,
                "inflight": inflight,
                "inflight_age_s": inflight_age_s,
                "stalled": stalled,
                "stall_reason": stall_reason,
                "last_bar_close": (
                    datetime.fromtimestamp(last_bar_close_at, ET).isoformat()
                    if last_bar_close_at else None
                ),
                "last_started": (
                    datetime.fromtimestamp(last_started_at, ET).isoformat()
                    if last_started_at else None
                ),
                "last_run": (
                    datetime.fromtimestamp(last_run_at, ET).isoformat()
                    if last_run_at else None
                ),
                "lag_since_last_run_s": lag_since_last_run_s,
                "last_result": self._last_realtime_compute_result,
            },
            "interval_prime": self._copy_interval_prime_state(),
            "market_universe": {
                "market_date": self._current_market_date,
                "pipeline_stage": pipeline_stage,
                "pipeline_status": pipeline_status,
                "last_daily_reset": (
                    datetime.fromtimestamp(self._last_daily_reset_at, ET).isoformat()
                    if self._last_daily_reset_at else None
                ),
                "watchlist_pool_count": len(self._watchlist_symbols),
                "watchlist_trade_count": len(self._watchlist_trade_symbols),
                "data_symbols_total": len(data_symbols),
                "scan_symbols_total": len(scan_symbols),
                "market_ws_symbols_total": len(market_ws_symbols),
                "data_symbols": list(data_symbols),
                "scan_symbols": list(scan_symbols),
                "market_ws_symbols": list(market_ws_symbols),
                "active_target_date": self._active_target_date,
                "active_target_count": len(self._active_trade_symbols),
                "active_subscription_count": len(self._active_subscription_symbols),
                "active_target_symbols": list(self._active_trade_symbols),
                "active_subscription_symbols": list(self._active_subscription_symbols),
                "active_trade_symbols": list(self._active_trade_symbols),
                "last_successful_scan_market_date": (
                    str(daily_scan_state.get("market_date") or "")
                    if str(daily_scan_state.get("status") or "").strip().lower() == "completed"
                    else ""
                ),
                "last_successful_scan_at": (
                    str(daily_scan_state.get("finished_at") or "")
                    if str(daily_scan_state.get("status") or "").strip().lower() == "completed"
                    else ""
                ),
                "bar_freshness": {
                    "status": bar_freshness_status,
                    "lag_s": float(official_5m.get("lag_s", 0) or 0),
                    "last_completed_bucket_us": str(official_5m.get("last_completed_bucket_us") or ""),
                    "pending_symbols_total": int(official_5m.get("pending_symbols_total", 0) or 0),
                },
                "indicator_freshness": {
                    "status": indicator_freshness_status,
                    "lag_since_last_run_s": lag_since_last_run_s,
                    "last_run": (
                        datetime.fromtimestamp(last_run_at, ET).isoformat()
                        if last_run_at else None
                    ),
                    "stalled": stalled,
                    "stall_reason": stall_reason,
                },
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
