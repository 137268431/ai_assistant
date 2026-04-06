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
        self._subscription_thread = None
        self._watchlist_backfill_thread = None
        self._compute_thread = None
        self._auth_required_reason = ""
        self._symbol_meta = {}
        self._market_index_symbols = []
        self._watchlist_symbols = []
        self._watchlist_records = {}
        self._active_subscription_symbols = []
        self._active_subscription_map = {}
        self._active_target_date = ""
        self._last_watchlist_refresh_at = 0.0
        self._last_target_refresh_at = 0.0
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []
        self._watchlist_backfill_cursor = 0
        self._subscription_lock = threading.Lock()
        self._compute_queue = queue.Queue()
        self._signal_wakeup = threading.Event()
        self._realtime_compute_runs = 0
        self._last_realtime_compute_at = 0.0
        self._last_realtime_compute_result = {}
        self._current_market_date = ""
        self._last_daily_reset_at = 0.0

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

            self.conid_resolver.load_cache_from_pb()
            self._reset_for_new_market_day(force=True)
            self._refresh_watchlist_pool(force=True)
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

        self._remove_stale_target_rows(current_date)
        self._apply_live_subscriptions(current_date, {}, reason="market_day_reset")
        self._active_target_date = ""
        self._last_target_refresh_at = 0.0
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
        refresh_minutes = max(1, self.config.get_int("watchlist_interval_min", 5))
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
        return max(0, self.config.get_int("ibkr_target_subscription_limit", 60))

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

    def _apply_live_subscriptions(self, target_date: str, conid_map: dict, reason: str = ""):
        with self._subscription_lock:
            previous_map = dict(self._active_subscription_map)
            previous_conids = set(previous_map.values())
            next_conids = set(conid_map.values())
            removed_conids = previous_conids - next_conids
            added_symbols = [
                symbol for symbol, conid in conid_map.items()
                if previous_map.get(symbol) != conid
            ]

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
            self._active_target_date = target_date
            self._last_target_refresh_at = time.time()

        if added_symbols:
            added_map = {symbol: conid_map[symbol] for symbol in added_symbols if symbol in conid_map}
            logger.info(
                "Backfilling newly subscribed target symbols: %s",
                ",".join(sorted(added_map.keys())),
            )
            self.data_backfill.backfill_all(added_map, symbol_meta=self._symbol_meta, intervals=["5m"])
            self.data_writer.flush()

        logger.info(
            "Applied target subscriptions (%s): active=%d added=%d removed=%d",
            reason or "refresh",
            len(conid_map),
            len(added_symbols),
            len(removed_conids),
        )

    def _refresh_target_subscriptions(self, force: bool = False, reason: str = "loop"):
        self._reset_for_new_market_day(force=False)
        refresh_seconds = max(15, self.config.get_int("ibkr_target_refresh_sec", 60))
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
            self._apply_live_subscriptions(target_date, {}, reason=reason)
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

        self._mark_target_statuses(target_date, selected_rows)
        self._apply_live_subscriptions(target_date, conid_map, reason=reason)

    def _subscription_refresh_loop(self):
        logger.info("Target subscription loop started")
        while self._running:
            try:
                self.config.refresh()
                self._reset_for_new_market_day(force=False)
                if self.session_keeper.is_authenticated:
                    self._refresh_target_subscriptions(reason="poll")
                else:
                    logger.info("Skip target refresh while session is unauthenticated")
            except Exception as exc:
                logger.error("Target subscription loop error: %s", exc)

            sleep_seconds = max(15, self.config.get_int("ibkr_target_refresh_sec", 60))
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _watchlist_backfill_candidates(self):
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(1, self.config.get_int("ibkr_watchlist_backfill_batch_size", 12))
        start = self._watchlist_backfill_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_backfill_cursor = (start + batch_size) % max(len(pool), 1)
        return ordered[:batch_size]

    def _watchlist_backfill_loop(self):
        logger.info("Watchlist backfill loop started")
        while self._running:
            try:
                self.config.refresh()
                self._run_watchlist_backfill_cycle()
            except Exception as exc:
                logger.error("Watchlist backfill loop error: %s", exc)

            sleep_seconds = max(300, self.config.get_int("ibkr_watchlist_backfill_interval_min", 30) * 60)
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_watchlist_backfill_cycle(self):
        self._refresh_watchlist_pool()
        candidates = self._watchlist_backfill_candidates()
        if not candidates:
            logger.info("Watchlist backfill skipped: no non-target symbols in pool")
            return

        stale_minutes = max(5, self.config.get_int("ibkr_watchlist_backfill_stale_min", 20))
        now_ms = int(time.time() * 1000)
        stale_ms = stale_minutes * 60 * 1000
        eligible = []
        for symbol in candidates:
            latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            if latest_ms <= 0 or (now_ms - latest_ms) >= stale_ms:
                eligible.append(symbol)

        if not eligible:
            logger.info("Watchlist backfill skipped: batch is fresh enough")
            return

        conid_map = self.conid_resolver.resolve_bulk(eligible)
        if not conid_map:
            logger.warning("Watchlist backfill skipped: no conids resolved")
            return

        symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
        logger.info("Running incremental watchlist backfill for %d symbols", len(conid_map))
        self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
        self.data_writer.flush()
        self._last_backfill_at = time.time()
        self._last_backfill_symbols = sorted(conid_map.keys())

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
            self._signal_wakeup.wait(timeout=SIGNAL_POLL_INTERVAL)
            self._signal_wakeup.clear()

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
        self.data_writer.close()
        self._signal_wakeup.set()
        self._compute_queue.put(None)

        if self._signal_thread:
            self._signal_thread.join(timeout=10)
        if self._subscription_thread:
            self._subscription_thread.join(timeout=10)
        if self._watchlist_backfill_thread:
            self._watchlist_backfill_thread.join(timeout=10)
        if self._compute_thread:
            self._compute_thread.join(timeout=10)

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
                "last_watchlist_refresh": (
                    datetime.fromtimestamp(self._last_watchlist_refresh_at, ET).isoformat()
                    if self._last_watchlist_refresh_at else None
                ),
                "last_target_refresh": (
                    datetime.fromtimestamp(self._last_target_refresh_at, ET).isoformat()
                    if self._last_target_refresh_at else None
                ),
                "last_watchlist_backfill": (
                    datetime.fromtimestamp(self._last_backfill_at, ET).isoformat()
                    if self._last_backfill_at else None
                ),
                "last_watchlist_backfill_symbols": list(self._last_backfill_symbols),
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
