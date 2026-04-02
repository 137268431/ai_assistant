"""
IBKR Trading Service — 主入口
整合所有模块: Gateway会话 + WebSocket数据 + 订单管理 + 信号处理

启动方式: python -m qc_compute.ibkr_service
"""

import os
import sys
import time
import signal
import logging
import threading
from datetime import datetime, timezone, timedelta

from qc_compute.integrations.pb_client import PBClient
from qc_compute.core.config import Config
from qc_compute.gateway.gateway_manager import GatewayManager
from qc_compute.gateway.session_keeper import SessionKeeper
from qc_compute.gateway.auth_handler import AuthHandler
from qc_compute.market.conid_resolver import ConidResolver
from qc_compute.market.ws_client import IBKRWebSocketClient
from qc_compute.market.bar_aggregator import BarAggregator
from qc_compute.market.data_writer import DataWriter
from qc_compute.market.data_backfill import DataBackfill
from qc_compute.market.data_retention import DataRetention
from qc_compute.order.order_placer import OrderPlacer
from qc_compute.order.order_tracker import OrderTracker
from qc_compute.order.order_modifier import OrderModifier
from qc_compute.order.order_lifecycle import OrderLifecycle
from qc_compute.signal.signal_router import SignalRouter
from qc_compute.signal.signal_processor import SignalProcessor
from qc_compute.signal.reverse_signal import ReverseSignalHandler
from qc_compute.core.indicator_engine import IndicatorEngine
from qc_compute.core.signal_generator import SignalGenerator

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

        self._engines = {}  # (symbol, interval) -> IndicatorEngine
        self._signal_gens = {}
        self._running = False
        self._signal_thread = None

    def start(self):
        logger.info("=" * 60)
        logger.info("IBKR Trading Service starting (env=%s)", ENVIRONMENT)
        logger.info("=" * 60)

        self.config.refresh()

        if not self._ensure_gateway():
            logger.error("Gateway setup failed, exiting")
            return

        self.session_keeper.start()
        time.sleep(3)

        if not self.session_keeper.is_authenticated:
            logger.info("Not authenticated, attempting login...")
            if not self.auth_handler.login():
                logger.error("Login failed, exiting")
                return
            time.sleep(3)

        self._load_watchlist_and_subscribe()

        self.order_tracker.start()
        self.order_lifecycle.start()

        self._running = True
        self._signal_thread = threading.Thread(
            target=self._signal_loop, daemon=True, name="signal-loop",
        )
        self._signal_thread.start()

        self._schedule_retention()

        logger.info("IBKR Trading Service fully started")

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
            symbols = [r.get("symbol", "").upper() for r in watchlist if r.get("symbol")]
        except Exception as e:
            logger.error("Failed to load watchlist: %s", e)
            symbols = []

        if not symbols:
            logger.warning("Empty watchlist, no symbols to subscribe")
            return

        self.conid_resolver.load_cache_from_pb()
        conid_map = self.conid_resolver.resolve_bulk(symbols)

        reverse_map = {cid: sym for sym, cid in conid_map.items()}
        self.bar_aggregator.set_symbol_map(reverse_map)

        logger.info("Resolved %d/%d conids, starting backfill...", len(conid_map), len(symbols))

        self.data_backfill.backfill_all(conid_map, interval="5m")

        self.ws_client.start()
        time.sleep(2)

        for symbol, conid in conid_map.items():
            self.ws_client.subscribe(conid)

        logger.info("Subscribed to %d symbols via WebSocket", len(conid_map))

    def _on_bar_close(self, bar_data: dict):
        self.data_writer.write_bar(bar_data)

        symbol = bar_data.get("symbol", "")
        interval = bar_data.get("interval", "5m")
        key = (symbol, interval)

        if key not in self._engines:
            self._engines[key] = IndicatorEngine(symbol, interval)
            self._signal_gens[key] = SignalGenerator(symbol, interval)

        engine = self._engines[key]
        snapshot = engine.update(bar_data)

        if snapshot and engine.is_ready():
            try:
                self.pb.upsert_indicator({
                    "environment": ENVIRONMENT,
                    "symbol": symbol,
                    "interval": interval,
                    "script_tag": "ibkr_ws",
                    "us_time": bar_data.get("us_time", ""),
                    "cn_time": bar_data.get("cn_time", ""),
                    "bar_time_ms": bar_data.get("bar_time_ms", 0),
                    "bar_index": engine.bar_count,
                    "extra": {**snapshot, "environment": ENVIRONMENT, "source": "ibkr"},
                })
            except Exception as e:
                logger.error("Indicator upsert failed: %s", e)

            sig_gen = self._signal_gens.get(key)
            if sig_gen:
                signal = sig_gen.update(snapshot)
                if signal:
                    self._process_qc_signal(symbol, signal, bar_data)

    def _process_qc_signal(self, symbol: str, signal: dict, bar_data: dict):
        try:
            self.pb.upsert_signal({
                "environment": ENVIRONMENT,
                "symbol": symbol,
                "signal_id": signal.get("signal_id", f"ibkr_{symbol}_{bar_data.get('bar_time_ms', 0)}"),
                "direction": signal.get("direction", ""),
                "signal": signal.get("signal", ""),
                "entry": signal.get("entry", 0),
                "stop_loss": signal.get("stop_loss", 0),
                "take_profit": signal.get("take_profit", 0),
                "rr": signal.get("rr", ""),
                "shares": signal.get("shares", 0),
                "interval": bar_data.get("interval", "5m"),
                "us_time": bar_data.get("us_time", ""),
                "date": bar_data.get("us_time", "")[:10],
                "bar_time_ms": bar_data.get("bar_time_ms", 0),
                "status": "pending",
                "extra": {"environment": ENVIRONMENT, "source": "ibkr"},
            })
        except Exception as e:
            logger.error("QC signal upsert failed: %s", e)

    def _signal_loop(self):
        logger.info("Signal processing loop started (interval=%ds)", SIGNAL_POLL_INTERVAL)
        while self._running:
            try:
                self.config.refresh()
                self._process_signals()
                self.reverse_handler.check_and_process()
            except Exception as e:
                logger.error("Signal loop error: %s", e)

            for _ in range(SIGNAL_POLL_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

    def _process_signals(self):
        signals = self.signal_router.fetch_pending_signals()

        for sig in signals:
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
        logger.warning("Session expired, attempting re-login...")
        self.auth_handler.login()

    def _on_gateway_down(self):
        logger.error("Gateway down, attempting restart...")
        self.gateway_manager.restart()
        time.sleep(10)
        self.auth_handler.login()

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
        self._running = False

        self.bar_aggregator.force_close_all()
        self.ws_client.stop()
        self.session_keeper.stop()
        self.order_tracker.stop()
        self.order_lifecycle.stop()

        if self._signal_thread:
            self._signal_thread.join(timeout=10)

        logger.info("IBKR Trading Service stopped")

    def status(self) -> dict:
        return {
            "environment": ENVIRONMENT,
            "gateway": self.gateway_manager.status(),
            "session": self.session_keeper.status(),
            "websocket": self.ws_client.status(),
            "bar_aggregator": self.bar_aggregator.status(),
            "data_writer": self.data_writer.status(),
            "data_retention": self.data_retention.status(),
            "order_placer": self.order_placer.status(),
            "order_tracker": self.order_tracker.status(),
            "order_lifecycle": self.order_lifecycle.status(),
            "signal_router": self.signal_router.status(),
            "signal_processor": self.signal_processor.status(),
        }


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
