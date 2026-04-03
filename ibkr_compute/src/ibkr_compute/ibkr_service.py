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
GATEWAY_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5000")
ENVIRONMENT = os.environ.get("IBKR_ENVIRONMENT", "live")
SIGNAL_POLL_INTERVAL = 120


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
        )

        self.signal_router = SignalRouter(
            pb_client=self.pb, config=self.config, environment=ENVIRONMENT,
        )
        self.signal_processor = SignalProcessor(
            config=self.config, order_lifecycle=self.order_lifecycle,
            environment=ENVIRONMENT,
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
        self._auth_required_reason = ""
        self._symbol_meta = {}

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

            self._load_watchlist_and_subscribe()

            self.order_tracker.start()
            self.order_lifecycle.start()

            self._running = True
            startup_ok = True
            self._signal_thread = threading.Thread(
                target=self._signal_loop, daemon=True, name="signal-loop",
            )
            self._signal_thread.start()

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

    def _load_watchlist_and_subscribe(self):
        logger.info("Loading watchlist and resolving conids...")

        try:
            watchlist = self.pb.get_all_records("watchlist")
            symbols = []
            symbol_meta = {}
            for row in watchlist:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol:
                    continue
                symbols.append(symbol)
                symbol_meta[symbol] = {
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "industry": str(row.get("industry", "") or ""),
                }
            self._symbol_meta = symbol_meta
        except Exception as e:
            logger.error("Failed to load watchlist: %s", e)
            symbols = []
            self._symbol_meta = {}

        if not symbols:
            logger.warning("Empty watchlist, no symbols to subscribe")
            return

        self.conid_resolver.load_cache_from_pb()
        conid_map = self.conid_resolver.resolve_bulk(symbols)

        reverse_map = {cid: sym for sym, cid in conid_map.items()}
        self.bar_aggregator.set_symbol_map(reverse_map)

        logger.info("Resolved %d/%d conids, starting live subscriptions...", len(conid_map), len(symbols))
        self.ws_client.start()
        time.sleep(2)

        for symbol, conid in conid_map.items():
            self.ws_client.subscribe(conid)

        logger.info("Subscribed to %d symbols via WebSocket", len(conid_map))
        logger.info("Starting historical backfill after subscriptions...")
        self.data_backfill.backfill_all(conid_map, symbol_meta=self._symbol_meta)

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

        for derived_bar in self.timeframe_builder.consume(payload):
            derived_bar["environment"] = ENVIRONMENT
            derived_bar["exchange"] = str(meta.get("exchange") or derived_bar.get("exchange") or "").upper()
            self.data_writer.write_bar(derived_bar)

    def _signal_loop(self):
        logger.info("Signal processing loop started (interval=%ds)", SIGNAL_POLL_INTERVAL)
        while self._running:
            try:
                self.config.refresh()
                if self.session_keeper.is_authenticated:
                    self._process_signals()
                    self.reverse_handler.check_and_process()
                else:
                    logger.info("Skip signal/reverse processing while session is unauthenticated")
            except Exception as e:
                logger.error("Signal loop error: %s", e)

            for _ in range(SIGNAL_POLL_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

    def _process_signals(self):
        if not self.session_keeper.is_authenticated:
            logger.info("Skip signal processing while session is unauthenticated")
            return

        pending_signals = self.signal_router.fetch_pending_signals()

        for sig in pending_signals:
            valid, reason = self.signal_processor.validate_signal(sig)
            if not valid:
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
                self.signal_processor.register_position(symbol, {
                    "direction": sig["direction"],
                    "bracket_group": result.get("bracket_group"),
                })
                self.order_lifecycle.increment_position_count()
            else:
                logger.error("Order failed: %s - %s", symbol, result.get("error"))

            self.signal_router.mark_processed(sig["signal_id"])

    def _on_order_fill(self, order: dict):
        logger.info("Order filled: %s", order.get("ticker"))

    def _on_order_cancel(self, order: dict):
        logger.info("Order cancelled: %s", order.get("ticker"))

    def _on_session_expired(self):
        logger.warning("Session expired; requesting manual 2FA")
        self._request_manual_2fa(
            "session_expired",
            "检测到 IBKR 会话失效，等待你点击飞书按钮后再触发 2FA。",
        )

    def _on_gateway_down(self):
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

        self.auth_handler.cancel()
        self.bar_aggregator.force_close_all()
        self.timeframe_builder.reset()
        self.ws_client.stop()
        self.session_keeper.stop()
        self.order_tracker.stop()
        self.order_lifecycle.stop()

        if self._signal_thread:
            self._signal_thread.join(timeout=10)

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
