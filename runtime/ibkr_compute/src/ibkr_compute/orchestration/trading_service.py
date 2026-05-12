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
import json
from datetime import datetime
from ibkr_compute.core.time_utils import ET

from ibkr_compute.broker import (
    AuthController,
    BrokerAdapter,
    GatewayServiceManager,
    SocketSessionKeeper,
)
from ibkr_compute.broker.cookie_store import clear_cookies
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.core.config import Config
from ibkr_compute.core.host_resources import HostResourceMonitor
from ibkr_compute.market.conid_resolver import ConidResolver
from ibkr_compute.market.ws_client import IBKRWebSocketClient
from ibkr_compute.market.bar_aggregator import BarAggregator
from ibkr_compute.market.realtime_quote_book import RealtimeQuoteBook
from ibkr_compute.market.data_writer import DataWriter
from ibkr_compute.market.data_backfill import DataBackfill, _regular_session_gap_summary
from ibkr_compute.market.data_retention import DataRetention
from ibkr_compute.market.bar_freshness import BarFreshnessPlanner
from ibkr_compute.market.bar_repair import BarRepairCoordinator
from ibkr_compute.market.timeframe_utils import (
    build_market_session_snapshot,
    bucket_start_ms,
    classify_market_session_kind,
    interval_to_ms,
)
from ibkr_compute.orchestration.market_universe_support import _target_row_is_daily_scan_active
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.core.indicator_engine import indicator_ready_bar_count
from ibkr_compute.order.order_placer import OrderPlacer
from ibkr_compute.order.order_tracker import OrderTracker
from ibkr_compute.order.order_modifier import OrderModifier
from ibkr_compute.order.order_lifecycle import OrderLifecycle
from ibkr_compute.orchestration.auth_recovery import TradingServiceAuthRecoveryMixin
from ibkr_compute.orchestration.integrity import TradingServiceIntegrityMixin
from ibkr_compute.orchestration.lifecycle import TradingServiceLifecycleMixin
from ibkr_compute.orchestration.market_universe import TradingServiceMarketUniverseMixin
from ibkr_compute.orchestration.runtime_ops import TradingServiceRuntimeOpsMixin
from ibkr_compute.orchestration.runtime_pipeline import TradingServiceRuntimePipelineMixin
from ibkr_compute.orchestration.runtime_status import TradingServiceRuntimeStatusMixin
from ibkr_compute.orchestration.service_support import TradingServiceSupportMixin
from ibkr_compute.orchestration.signals import TradingServiceSignalsMixin
from ibkr_compute.orchestration.startup import TradingServiceStartupMixin
from ibkr_compute.orchestration.warmup_cycle import TradingServiceWarmupCycleMixin
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


PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://127.0.0.1:8090")
CONSOLE_BASE_URL = (
    os.environ.get("CONSOLE_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or os.environ.get("IBKR_CONSOLE_PUBLIC_URL")
    or "https://quant.lzw-glory.top"
).rstrip("/")
PB_PUBLIC_URL = CONSOLE_BASE_URL
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
DEFAULT_SIGNAL_POLL_INTERVAL = 5
DEFAULT_WARMUP_REQUIRED_INTERVAL = "5m"
STARTUP_BACKGROUND_PRIME_INTERVALS = ("15m", "30m", "1h", "4h", "1d")
STARTUP_BACKGROUND_PRIME_CHUNK_SIZE = 8
STARTUP_HISTORY_REPAIR_SHORT_PERIOD = "1d"
DEFAULT_OFFICIAL_5M_CLOSE_DELAY_SECONDS = max(1, int(os.environ.get("IBKR_OFFICIAL_5M_CLOSE_DELAY_SEC", "3")))
DEFAULT_OFFICIAL_5M_REQUEST_PERIOD = os.environ.get("IBKR_OFFICIAL_5M_REQUEST_PERIOD", "1d").strip() or "1d"
DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVALS = ("15m", "30m", "1h", "4h", "1d")
DEFAULT_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SECONDS = max(1, int(os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_CLOSE_DELAY_SEC", "8")))
DEFAULT_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SECONDS = max(1.0, float(os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_LOOP_INTERVAL_SEC", "1")))
DEFAULT_RUNTIME_DIRECT_TOPUP_PARALLEL_ENABLED = str(
    os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_PARALLEL_ENABLED", "true")
).strip().lower() not in {"0", "false", "no", "off"}
DEFAULT_RUNTIME_DIRECT_TOPUP_INTERVAL_PRIORITY = tuple(
    item.strip()
    for item in os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_INTERVAL_PRIORITY", "4h,1h,30m,15m,1d").split(",")
    if item.strip()
)
DEFAULT_RUNTIME_DIRECT_TOPUP_PERIODS = {
    "15m": os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_PERIOD_15M", "2d").strip() or "2d",
    "30m": os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_PERIOD_30M", "3d").strip() or "3d",
    "1h": os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_PERIOD_1H", "5d").strip() or "5d",
    "4h": os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_PERIOD_4H", "20d").strip() or "20d",
    "1d": os.environ.get("IBKR_RUNTIME_DIRECT_TOPUP_PERIOD_1D", "60d").strip() or "60d",
}
BAR_INTEGRITY_STATE_KEY = "ibkr_bar_integrity_cursor"
BAR_INTEGRITY_STATE_DATE = "global"
DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE = 8
REALTIME_PRIORITY_ENVIRONMENTS = {"live", "paper"}
SESSION_EVENT_ALERT_COOLDOWN_SECONDS = int(os.environ.get("IBKR_SESSION_EVENT_ALERT_COOLDOWN", "1800"))
DAILY_SCAN_EVENT_ALERT_COOLDOWN_SECONDS = int(os.environ.get("IBKR_DAILY_SCAN_EVENT_ALERT_COOLDOWN", "1800"))
AUTH_PROBE_INTERVAL_SECONDS = max(2, int(os.environ.get("IBKR_AUTH_PROBE_INTERVAL_SECONDS", "5")))
AUTH_PROBE_WINDOW_SECONDS = max(AUTH_PROBE_INTERVAL_SECONDS, int(os.environ.get("IBKR_AUTH_PROBE_WINDOW_SECONDS", "45")))
AUTH_PROBE_LATE_SESSION_WINDOW_SECONDS = max(
    AUTH_PROBE_WINDOW_SECONDS,
    int(os.environ.get("IBKR_AUTH_PROBE_LATE_SESSION_WINDOW_SECONDS", "180")),
)
AUTH_PROBE_SELF_HEAL_GRACE_SECONDS = max(
    AUTH_PROBE_INTERVAL_SECONDS,
    int(os.environ.get("IBKR_AUTH_PROBE_SELF_HEAL_GRACE_SECONDS", "90")),
)
AUTH_POST_LOGIN_PROBE_WINDOW_SECONDS = max(
    AUTH_PROBE_WINDOW_SECONDS,
    int(os.environ.get("IBKR_AUTH_POST_LOGIN_PROBE_WINDOW_SECONDS", "240")),
)
AUTH_POST_LOGIN_PROBE_GRACE_SECONDS = max(
    AUTH_PROBE_SELF_HEAL_GRACE_SECONDS,
    int(os.environ.get("IBKR_AUTH_POST_LOGIN_PROBE_GRACE_SECONDS", "180")),
)
AUTH_PROBE_LATE_SESSION_SELF_HEAL_GRACE_SECONDS = max(
    AUTH_PROBE_SELF_HEAL_GRACE_SECONDS,
    int(os.environ.get("IBKR_AUTH_PROBE_LATE_SESSION_SELF_HEAL_GRACE_SECONDS", "300")),
)
AUTH_MANUAL_TAKEOVER_TTL_SECONDS = max(60, int(os.environ.get("IBKR_AUTH_MANUAL_TAKEOVER_TTL_SECONDS", "600")))
AUTH_RECOVERY_LOCK_TTL_SECONDS = max(30, int(os.environ.get("IBKR_AUTH_RECOVERY_LOCK_TTL_SECONDS", "120")))
AUTH_RECOVERY_PB_FIELDS = (
    "cycle_id",
    "recovery_phase",
    "recovery_class",
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
    TradingServiceSupportMixin,
    TradingServiceAuthRecoveryMixin,
    TradingServiceLifecycleMixin,
    TradingServiceStartupMixin,
    TradingServiceWarmupCycleMixin,
    TradingServiceWarmupMixin,
    TradingServiceIntegrityMixin,
    TradingServiceRuntimePipelineMixin,
    TradingServiceRuntimeOpsMixin,
    TradingServiceRuntimeStatusMixin,
    TradingServiceSignalsMixin,
    TradingServiceMarketUniverseMixin,
):
    def __init__(self):
        self.pb = PBClient(base_url=PB_BASE_URL)
        self.config = Config(pb_client=self.pb)
        self.config.refresh()

        self.broker = BrokerAdapter()
        self.gateway_manager = GatewayServiceManager(broker=self.broker)
        self.session_keeper = SocketSessionKeeper(
            broker=self.broker,
            gateway_manager=self.gateway_manager,
            pb_client=self.pb,
            on_session_expired=self._on_session_expired,
            on_gateway_down=self._on_gateway_down,
            environment=ENVIRONMENT,
        )
        self.auth_handler = AuthController(
            pb_client=self.pb,
            gateway_manager=self.gateway_manager,
            broker=self.broker,
            session_keeper=self.session_keeper,
            environment=ENVIRONMENT,
        )

        self.conid_resolver = ConidResolver(pb_client=self.pb, broker=self.broker)
        self.data_writer = DataWriter(pb_client=self.pb, config=self.config, environment=ENVIRONMENT)
        self.data_backfill = DataBackfill(
            data_writer=self.data_writer,
            config=self.config,
            environment=ENVIRONMENT,
            broker=self.broker,
        )
        self.bar_freshness_planner = BarFreshnessPlanner(self.pb, self.config, environment=ENVIRONMENT)
        self.bar_repair_coordinator = BarRepairCoordinator(
            pb_client=self.pb,
            config=self.config,
            environment=ENVIRONMENT,
            data_backfill=self.data_backfill,
            data_writer=self.data_writer,
            conid_resolver=self.conid_resolver,
            symbol_meta_provider=lambda symbols: {
                str(symbol or "").strip().upper(): dict(self._symbol_meta.get(str(symbol or "").strip().upper(), {}))
                for symbol in (symbols or [])
                if str(symbol or "").strip()
            },
            materialize_callback=lambda environment, symbols, interval: self._trigger_realtime_compute(
                source="bar_repair_queue",
                symbols=symbols,
                persist_signals=False,
                intervals=[interval],
                rollup_intervals=[],
            ),
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
            on_tick=self._on_ws_market_tick,
            config=self.config,
            environment=ENVIRONMENT,
            broker=self.broker,
        )

        self.order_placer = OrderPlacer(
            pb_client=self.pb,
            config=self.config,
            environment=ENVIRONMENT,
            broker=self.broker,
        )
        self.order_modifier = OrderModifier(pb_client=self.pb, broker=self.broker)
        self.order_tracker = OrderTracker(
            pb_client=self.pb,
            on_fill=self._on_order_fill,
            on_cancel=self._on_order_cancel,
            config=self.config,
            environment=ENVIRONMENT,
            broker=self.broker,
        )
        self.ws_client.set_order_update_callback(self.order_tracker.on_order_update)
        self.order_lifecycle = OrderLifecycle(
            pb_client=self.pb,
            order_modifier=self.order_modifier,
            config=self.config,
            environment=ENVIRONMENT,
            broker=self.broker,
        )

        self.signal_router = SignalRouter(
            pb_client=self.pb, config=self.config, environment=ENVIRONMENT,
        )
        self.signal_processor = SignalProcessor(
            config=self.config, order_lifecycle=self.order_lifecycle,
            environment=ENVIRONMENT,
            readiness_provider=self._trade_readiness_snapshot,
            target_direction_provider=self._active_target_direction_biases,
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
        self._direct_topup_thread = None
        self._bar_close_thread = None
        self._warmup_thread = None
        self.host_resource_monitor = HostResourceMonitor()
        self._resource_monitor_thread = None
        self._resource_monitor_stop = threading.Event()
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
        self._watchlist_idle_topup_cursor = 0
        self._watchlist_idle_topup_lock = threading.RLock()
        self._watchlist_idle_observations = {}
        self._watchlist_topup_wakeup = threading.Event()
        self._watchlist_topup_force_until = 0.0
        self._watchlist_topup_requested_at = 0.0
        self._watchlist_topup_request_count = 0
        self._watchlist_topup_last_consumed_request_count = 0
        self._last_watchlist_deep_maintenance_at = 0.0
        self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()
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
        self._quote_resubscribe_at = {}
        self._warmup_signature = ()
        self._warmup_state = self._initial_warmup_state()
        self._daily_scan_state = self._load_daily_scan_state(self._market_date())
        self._official_5m_lock = threading.RLock()
        self._official_5m_state = self._initial_official_5m_state()
        self._official_5m_last_cycle_at = 0.0
        self._direct_topup_lock = threading.RLock()
        self._direct_topup_state = self._initial_direct_topup_state()
        self._last_session_issue_kind = ""
        self._last_session_issue_title = ""
        self._last_session_issue_at = 0.0
        self._daily_scan_alert_market_date = ""
        self._daily_scan_alert_error = ""
        self._daily_scan_alert_title = ""
        self._daily_scan_alert_at = 0.0
        self._daily_scan_alert_active = False
        self._daily_scan_failure_count = 0
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

    def _active_target_direction_biases(self) -> dict[str, str]:
        market_date = self._market_date()
        try:
            rows = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{market_date}" && '
                    f'environment = "{ENVIRONMENT}" && '
                    'status = "active"'
                ),
                sort="-score,-updated",
                max_pages=10,
            )
        except Exception as exc:
            logger.warning("Failed to load active target direction biases: %s", exc)
            return {}

        configured_monitors = [
            item.strip().upper()
            for item in str(self.config.get_for_environment("ibkr_market_ws_symbols", ENVIRONMENT, "SPY,QQQ,VIX") or "").split(",")
            if item.strip()
        ]
        market_monitors = {
            str(symbol or "").strip().upper()
            for symbol in (list(self._watchlist_monitor_symbols or []) + configured_monitors)
            if str(symbol or "").strip()
        }
        target_limit = max(0, int(self.config.get_int_for_environment("ibkr_target_subscription_limit", ENVIRONMENT, 80) or 0))
        total_limit = max(0, int(self.config.get_int_for_environment("ibkr_total_subscription_limit", ENVIRONMENT, 80) or 0))
        trade_budget = target_limit if target_limit > 0 else None
        if total_limit > 0:
            remaining = max(0, total_limit - len(market_monitors))
            trade_budget = remaining if trade_budget is None else min(trade_budget, remaining)

        active_rows = [
            row for row in rows
            if str(row.get("symbol", "")).strip().upper() not in market_monitors
            and _target_row_is_daily_scan_active(row)
        ]
        prioritized_rows = list(active_rows)
        biases: dict[str, str] = {}
        for row in prioritized_rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol or symbol in biases:
                continue
            if trade_budget is not None and len(biases) >= trade_budget:
                break
            biases[symbol] = str(row.get("direction_bias", "") or "").strip().lower()
        return biases


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
