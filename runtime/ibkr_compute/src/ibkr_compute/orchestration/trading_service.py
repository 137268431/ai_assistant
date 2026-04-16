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

from ibkr_compute.broker import (
    AuthController,
    BrokerAdapter,
    GatewayServiceManager,
    SocketSessionKeeper,
)
from ibkr_compute.broker.cookie_store import clear_cookies
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.core.config import Config
from ibkr_compute.market.conid_resolver import ConidResolver
from ibkr_compute.market.ws_client import IBKRWebSocketClient
from ibkr_compute.market.bar_aggregator import BarAggregator
from ibkr_compute.market.realtime_quote_book import RealtimeQuoteBook
from ibkr_compute.market.data_writer import DataWriter
from ibkr_compute.market.data_backfill import DataBackfill, _regular_session_gap_summary
from ibkr_compute.market.data_retention import DataRetention
from ibkr_compute.market.timeframe_utils import bucket_start_ms, interval_to_ms
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

ET = timezone(timedelta(hours=-4))

PB_BASE_URL = os.environ.get("PB_BASE_URL", "http://127.0.0.1:8090")
PB_PUBLIC_URL = os.environ.get("PB_PUBLIC_URL", "").rstrip("/")
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
