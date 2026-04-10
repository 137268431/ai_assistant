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
from ibkr_compute.gateway.cookie_store import clear_cookies
from ibkr_compute.market.conid_resolver import ConidResolver
from ibkr_compute.market.ws_client import IBKRWebSocketClient
from ibkr_compute.market.bar_aggregator import BarAggregator
from ibkr_compute.market.data_writer import DataWriter
from ibkr_compute.market.data_backfill import DataBackfill, _regular_session_gap_summary
from ibkr_compute.market.data_retention import DataRetention
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import HIGHER_INTERVALS, bucket_start_ms, format_us_time, interval_to_ms
from ibkr_compute.core.indicator_engine import indicator_ready_bar_count
from ibkr_compute.order.order_placer import OrderPlacer
from ibkr_compute.order.order_tracker import OrderTracker
from ibkr_compute.order.order_modifier import OrderModifier
from ibkr_compute.order.order_lifecycle import OrderLifecycle
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


def normalize_watchlist_symbol_role(value, default: str = WATCHLIST_SYMBOL_ROLE_TRADE) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_WATCHLIST_SYMBOL_ROLES:
        return normalized
    return default


class IBKRTradingService:
    def __init__(self):
        self.pb = PBClient(base_url=PB_BASE_URL)
        self.config = Config(pb_client=self.pb)
        self.config.refresh()

        self.gateway_manager = GatewayManager()
        self.auth_handler = AuthHandler(gateway_url=GATEWAY_URL, pb_client=self.pb)
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

        self.bar_aggregator = BarAggregator(on_bar_close=self._on_bar_close)
        self.ws_client = IBKRWebSocketClient(
            gateway_url=GATEWAY_URL,
            on_tick=self.bar_aggregator.on_tick,
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
        self._bar_close_thread = None
        self._warmup_thread = None
        self._auth_required_reason = ""
        self._symbol_meta = {}
        self._watchlist_monitor_symbols = []
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
        self._last_realtime_compute_started_at = 0.0
        self._last_realtime_compute_result = {}
        self._last_bar_close_at = 0.0
        self._current_market_date = ""
        self._last_daily_reset_at = 0.0
        self._last_session_authenticated = False
        self._warmup_signature = ()
        self._warmup_state = self._initial_warmup_state()
        self._last_session_issue_kind = ""
        self._last_session_issue_title = ""
        self._last_session_issue_at = 0.0
        self._auth_recovery_lock = threading.RLock()
        self._auth_recovery_state = self._initial_auth_recovery_state()
        self._auth_probe_thread = None
        self._auth_probe_stop = threading.Event()
        self._auth_cycle_seq = 0
        self._auth_restart_thread = None
        self._startup_reason = ""
        self._startup_source = ""
        self._startup_trigger_login = False
        self._startup_status_message_id = ""
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
        self.ws_client.set_order_updates_enabled(self.order_tracker.uses_websocket_updates())

    def _build_2fa_detail(self, reason: str) -> dict:
        return {
            "环境": ENVIRONMENT,
            "原因": reason,
        }

    def _request_manual_2fa(self, reason: str, message: str) -> bool:
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
        return requested

    def _now_iso(self) -> str:
        return datetime.now(ET).isoformat()

    def _now_et(self) -> str:
        return self._now_iso()

    def _runtime_page_url(self) -> str:
        base_url = PB_PUBLIC_URL or ""
        if not base_url:
            return ""
        return f"{base_url}/ibkr_runtime.html?environment={ENVIRONMENT}"

    def _runtime_phase_label(self) -> str:
        if self._starting:
            return "starting"
        if self._running:
            return "running"
        return "stopped"

    def _build_startup_pending_detail(self, reason: str, source: str, trigger_login: bool) -> dict:
        detail = {
            "状态结论": "IBKR Runtime 正在启动，交易链路暂未开放。",
            "系统简介": "负责 Gateway 会话、实时行情、订单链路、信号处理与启动预热。",
            "启动成功条件": (
                "1. Gateway 可用并完成 Session/2FA 认证\n"
                "2. Watchlist/Targets 与订阅装载完成\n"
                "3. WebSocket、订单链路与后台线程已启动\n"
                "4. 活动标的完成 5m warmup，交易门可开放\n"
                "5. 启动前历史回补与完整性预检完成或切入后台继续"
            ),
            "检查时间": self._now_et(),
            "Runtime阶段": "starting",
            "启动原因": reason or "manual_start",
            "启动来源": source or "api_start",
            "触发登录": "yes" if trigger_login else "no",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url
        return detail

    def _record_startup_status_message_id(self, message_id: str):
        normalized = str(message_id or "").strip()
        if not normalized:
            return
        with self._state_lock:
            if self._starting:
                self._startup_status_message_id = normalized

    def _announce_startup_pending(self, reason: str, source: str, trigger_login: bool):
        result = self._emit_system_event(
            "status_change",
            "info",
            "IBKR Runtime 启动中",
            self._build_startup_pending_detail(reason, source, trigger_login),
        )
        if isinstance(result, dict):
            self._record_startup_status_message_id(result.get("message_id", ""))

    def _format_symbol_list(self, symbols: list[str], limit: int = 12) -> str:
        items = [str(symbol or "").upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        if not items:
            return "-"
        if len(items) <= limit:
            return ",".join(items)
        return f"{','.join(items[:limit])} (+{len(items) - limit})"

    def _format_elapsed_seconds(self, elapsed_seconds: float) -> str:
        total_seconds = max(0, int(round(float(elapsed_seconds or 0))))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0 or hours > 0:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)

    def _format_elapsed_between(self, started_at: str, finished_at: str) -> str:
        started_text = str(started_at or "").strip()
        finished_text = str(finished_at or "").strip()
        if not started_text or not finished_text:
            return "-"
        try:
            started_dt = datetime.fromisoformat(started_text)
            finished_dt = datetime.fromisoformat(finished_text)
        except Exception:
            return "-"
        elapsed_seconds = (finished_dt - started_dt).total_seconds()
        if elapsed_seconds < 0:
            return "-"
        return self._format_elapsed_seconds(elapsed_seconds)

    def _get_time_window(self, key: str, default: tuple[int, int]) -> tuple[int, int]:
        raw_value = str(
            self.config.get_for_environment(
                key,
                ENVIRONMENT,
                f"{default[0]:02d}:{default[1]:02d}",
            )
            or ""
        ).strip()
        try:
            hour_text, minute_text = raw_value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return default

    def _trade_window_start(self) -> tuple[int, int]:
        return self._get_time_window("trade_window_start_time", DEFAULT_TRADE_WINDOW_START)

    def _trade_window_end(self) -> tuple[int, int]:
        return self._get_time_window("trade_window_end_time", DEFAULT_TRADE_WINDOW_END)

    def _collect_startup_history_repair_snapshot(self, symbol: str) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        snapshot = {
            "symbol": normalized_symbol,
            "interval": "5m",
            "stored_bar_count": 0,
            "latest_stored_ms": 0,
            "today_regular_count": 0,
            "today_regular_latest_ms": 0,
            "today_gap_count": 0,
            "today_gap_examples": [],
            "needs_history_fetch": False,
            "needs_manual_review": False,
            "needs_pipeline_repair": False,
            "needs_repair": False,
            "safe_repair": False,
            "repair_reason": "",
            "integrity_status": "ok",
        }
        if not normalized_symbol:
            return snapshot

        min_bars = max(60, indicator_ready_bar_count())
        expected_ms = interval_to_ms("5m")
        bars_needed = max(400, min_bars + 20)
        max_pages = max(2, min(8, (bars_needed + 199) // 200))
        et_now = datetime.now(ET)
        market_date = et_now.strftime("%Y-%m-%d")
        now_ms = int(et_now.timestamp() * 1000)
        require_today_regular = (et_now.hour, et_now.minute) >= self._trade_window_start()
        freshness_tolerance_ms = max(expected_ms * 3, 15 * 60 * 1000)

        try:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{normalized_symbol}" && '
                    'interval = "5m" && '
                    f'{self._build_bar_environment_filter()}'
                ),
                sort="-bar_time_ms",
                max_pages=max_pages,
            )
        except Exception as exc:
            snapshot["needs_history_fetch"] = True
            snapshot["needs_pipeline_repair"] = True
            snapshot["needs_repair"] = True
            snapshot["safe_repair"] = True
            snapshot["repair_reason"] = f"startup_snapshot_error:{exc}"
            snapshot["integrity_status"] = "warn"
            return snapshot

        if not rows:
            snapshot["needs_history_fetch"] = True
            snapshot["needs_pipeline_repair"] = True
            snapshot["needs_repair"] = True
            snapshot["safe_repair"] = True
            snapshot["repair_reason"] = f"bars<{min_bars}"
            snapshot["integrity_status"] = "warn"
            return snapshot

        rows = rows[:bars_needed]
        snapshot["stored_bar_count"] = len(rows)
        snapshot["latest_stored_ms"] = int((rows[0] or {}).get("bar_time_ms", 0) or 0)

        today_regular_rows = []
        for row in reversed(rows):
            bar_time_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if bar_time_ms <= 0:
                continue
            row_dt = datetime.fromtimestamp(bar_time_ms / 1000, ET)
            if row_dt.strftime("%Y-%m-%d") != market_date:
                continue
            session_type = str((row or {}).get("session_type", "") or "").strip().lower()
            if session_type != "regular":
                continue
            today_regular_rows.append(row)

        snapshot["today_regular_count"] = len(today_regular_rows)
        if today_regular_rows:
            snapshot["today_regular_latest_ms"] = int(
                (today_regular_rows[-1] or {}).get("bar_time_ms", 0) or 0
            )

        today_gap_summary = _regular_session_gap_summary(
            today_regular_rows,
            "5m",
            same_day_only=False,
            example_limit=4,
        )
        snapshot["today_gap_count"] = int(today_gap_summary.get("gap_count", 0) or 0)
        snapshot["today_gap_examples"] = list(today_gap_summary.get("gap_examples") or [])

        reasons = []
        if snapshot["stored_bar_count"] < min_bars:
            reasons.append(f"bars<{min_bars}")
        if require_today_regular:
            latest_today_regular_ms = int(snapshot.get("today_regular_latest_ms", 0) or 0)
            if latest_today_regular_ms <= 0:
                reasons.append("today_regular_missing")
            elif (now_ms - latest_today_regular_ms) > freshness_tolerance_ms:
                stale_minutes = max(0, int((now_ms - latest_today_regular_ms) // 60000))
                reasons.append(f"today_regular_stale={stale_minutes}m")
        if int(snapshot.get("today_gap_count", 0) or 0) > 0:
            reasons.append(f"today_regular_gaps={int(snapshot.get('today_gap_count', 0) or 0)}")

        needs_repair = bool(reasons)
        snapshot["needs_history_fetch"] = needs_repair
        snapshot["needs_pipeline_repair"] = needs_repair
        snapshot["needs_repair"] = needs_repair
        snapshot["safe_repair"] = needs_repair
        snapshot["repair_reason"] = ",".join(reasons)
        snapshot["integrity_status"] = "warn" if needs_repair else "ok"
        return snapshot

    def _complete_startup_success(self, title: str, detail: dict | None = None) -> bool:
        with self._state_lock:
            if not self._starting:
                return False
            self._starting = False
            startup_reason = self._startup_reason
            startup_source = self._startup_source
            startup_trigger_login = self._startup_trigger_login
            startup_message_id = self._startup_status_message_id
            self._startup_reason = ""
            self._startup_source = ""
            self._startup_trigger_login = False
            self._startup_status_message_id = ""

        payload = {
            "状态结论": "IBKR Runtime 已完成启动前回补与预热，当前服务可用。",
            "检查时间": self._now_et(),
            "Runtime阶段": self._runtime_phase_label(),
            "启动原因": startup_reason or str((self._warmup_state or {}).get("reason") or "startup"),
            "启动来源": startup_source or "api_start",
            "触发登录": "yes" if startup_trigger_login else "no",
        }
        runtime_url = self._runtime_page_url()
        if runtime_url:
            payload["运行页"] = runtime_url
        if title and title != "IBKR Runtime 启动完成":
            payload["启动结果"] = title
        if detail:
            payload.update(detail)
        started_at = str(payload.get("预热开始") or "").strip()
        finished_at = str(payload.get("预热完成") or "").strip()
        if started_at and finished_at and "预热耗时" not in payload:
            payload["预热耗时"] = self._format_elapsed_between(started_at, finished_at)
        self._emit_system_event(
            "status_change",
            "info",
            "IBKR Runtime 启动完成",
            payload,
            message_id=startup_message_id,
        )
        return True

    def _copy_interval_prime_state(self) -> dict:
        with self._interval_prime_lock:
            return dict(self._interval_prime_state)

    def _schedule_interval_prime(self, symbols: list[str], source: str = "startup") -> bool:
        normalized_symbols = [
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        ]
        if not normalized_symbols:
            return False

        with self._interval_prime_lock:
            if self._interval_prime_state.get("running"):
                return False
            self._interval_prime_state = {
                **self._interval_prime_state,
                "running": True,
                "completed_intervals": [],
                "symbol_count": len(normalized_symbols),
                "last_started_at": self._now_iso(),
                "last_finished_at": "",
                "last_duration_s": 0.0,
                "last_error": "",
                "last_source": source,
            }

        def worker():
            started = time.time()
            completed_intervals = []
            last_error = ""
            try:
                from ibkr_compute.api import server as compute_server

                try:
                    with compute_server.compute_lock:
                        compute_server.load_persisted_compute_cursors(ENVIRONMENT)
                except Exception as exc:
                    logger.warning("Interval prime cursor preload failed: %s", exc)

                for interval in STARTUP_BACKGROUND_PRIME_INTERVALS:
                    if not self._running:
                        break
                    for index in range(0, len(normalized_symbols), STARTUP_BACKGROUND_PRIME_CHUNK_SIZE):
                        if not self._running:
                            break
                        chunk = normalized_symbols[index:index + STARTUP_BACKGROUND_PRIME_CHUNK_SIZE]
                        with compute_server.compute_lock:
                            compute_server.materialize_engines_from_storage(
                                ENVIRONMENT,
                                chunk,
                                interval,
                            )
                    completed_intervals.append(interval)
                    logger.info(
                        "Background interval prime finished (%s): interval=%s symbols=%d",
                        source,
                        interval,
                        len(normalized_symbols),
                    )
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Background interval prime failed (%s): %s", source, exc)
            finally:
                finished_at = self._now_iso()
                with self._interval_prime_lock:
                    self._interval_prime_state = {
                        **self._interval_prime_state,
                        "running": False,
                        "completed_intervals": completed_intervals,
                        "last_finished_at": finished_at,
                        "last_duration_s": round(max(0.0, time.time() - started), 3),
                        "last_error": last_error,
                        "last_source": source,
                    }

        self._interval_prime_thread = threading.Thread(
            target=worker,
            daemon=True,
            name="interval-prime",
        )
        self._interval_prime_thread.start()
        return True

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

    def _initial_auth_recovery_state(self) -> dict:
        return {
            "cycle_id": "",
            "recovery_phase": "idle",
            "recovery_reason": "",
            "interruption_kind": "",
            "manual_takeover_active": False,
            "manual_takeover_started_at": "",
            "manual_takeover_until": "",
            "probe_started_at": "",
            "probe_last_checked_at": "",
            "probe_attempts": 0,
            "probe_result": "",
            "auto_restart_scheduled": False,
            "last_runtime_authenticated_at": "",
            "last_gateway_status_code": 0,
            "last_recovery_source": "",
            "lock_owner": "",
            "lock_expires_at": "",
            "updated_at": "",
        }

    def _copy_auth_recovery_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._auth_recovery_state
        return dict(payload or {})

    def _parse_iso_timestamp(self, value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=ET)
            return parsed.astimezone(ET)
        except Exception:
            return None

    def _future_iso(self, offset_seconds: int) -> str:
        return (datetime.now(ET) + timedelta(seconds=max(0, int(offset_seconds or 0)))).isoformat()

    def _next_auth_cycle_id(self) -> str:
        with self._auth_recovery_lock:
            self._auth_cycle_seq += 1
            return f"{int(time.time() * 1000)}-{self._auth_cycle_seq}"

    def _auth_recovery_pb_patch(self, snapshot: dict | None = None) -> dict:
        state = self._copy_auth_recovery_state(snapshot)
        patch = {}
        for key in AUTH_RECOVERY_PB_FIELDS:
            patch[key] = state.get(key)
        return patch

    def _load_global_auth_state(self) -> dict:
        if not self.pb:
            return {}
        try:
            record = self.pb.get_state("ibkr_2fa", ENVIRONMENT, date="global") or {}
            data = record.get("data") if isinstance(record, dict) else {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("Failed to load global ibkr_2fa state: %s", exc)
            return {}

    def _sync_auth_recovery_state_to_pb(self, snapshot: dict | None = None):
        if not self.pb:
            return
        try:
            current = self._load_global_auth_state()
            payload = {
                **current,
                **self._auth_recovery_pb_patch(snapshot),
            }
            if not payload.get("status"):
                payload["status"] = "requested"
            self.pb.upsert_state("ibkr_2fa", ENVIRONMENT, payload, date="global")
        except Exception as exc:
            logger.debug("Failed to sync auth recovery state to PB: %s", exc)

    def _set_auth_recovery_state(self, sync_pb: bool = True, **updates) -> dict:
        with self._auth_recovery_lock:
            next_state = self._copy_auth_recovery_state()
            next_state.update(updates)
            next_state["updated_at"] = self._now_iso()
            if not next_state.get("manual_takeover_active"):
                next_state["manual_takeover_started_at"] = ""
                next_state["manual_takeover_until"] = ""
            self._auth_recovery_state = next_state
            snapshot = self._copy_auth_recovery_state(next_state)
        if sync_pb:
            self._sync_auth_recovery_state_to_pb(snapshot)
        return snapshot

    def _manual_takeover_active(self, snapshot: dict | None = None) -> bool:
        state = self._copy_auth_recovery_state(snapshot)
        if not bool(state.get("manual_takeover_active")):
            return False
        until_dt = self._parse_iso_timestamp(state.get("manual_takeover_until", ""))
        if until_dt and until_dt <= datetime.now(ET):
            self._set_auth_recovery_state(
                manual_takeover_active=False,
                manual_takeover_started_at="",
                manual_takeover_until="",
            )
            return False
        return True

    def _mark_auth_recovered(self, source: str, reason: str = ""):
        stamp = self._now_iso()
        current = self._copy_auth_recovery_state()
        previous_phase = str(current.get("recovery_phase") or "")
        snapshot = self._set_auth_recovery_state(
            cycle_id=current.get("cycle_id") or self._next_auth_cycle_id(),
            recovery_phase="recovered",
            recovery_reason=reason or current.get("recovery_reason") or source,
            interruption_kind="",
            manual_takeover_active=False,
            probe_last_checked_at=stamp,
            probe_result="authenticated",
            auto_restart_scheduled=False,
            last_runtime_authenticated_at=stamp,
            last_recovery_source=source,
            lock_owner="",
            lock_expires_at="",
        )
        self._auth_required_reason = ""
        if previous_phase and previous_phase not in {"idle", "recovered"}:
            try:
                self.auth_handler._report_2fa_status(
                    status="success",
                    reason=reason or "auth_recovered",
                    source=source,
                    detail=self._build_2fa_detail(reason or source),
                    message="IBKR 会话已恢复认证。",
                    last_result="会话恢复成功。",
                    state_patch=self._auth_recovery_pb_patch(snapshot),
                )
            except Exception as exc:
                logger.debug("Failed to report auth recovery success: %s", exc)

    def _ensure_auth_probe(self, cycle_id: str, interruption_kind: str, recovery_reason: str, source: str):
        with self._auth_recovery_lock:
            thread = self._auth_probe_thread
            if thread and thread.is_alive():
                return False
            self._auth_probe_stop.clear()

            def worker():
                started_perf = time.time()
                attempts = 0
                try:
                    while not self._auth_probe_stop.is_set():
                        current = self._copy_auth_recovery_state()
                        if str(current.get("cycle_id") or "") != cycle_id:
                            return
                        attempts += 1
                        authenticated = False
                        gateway_status_code = 0
                        try:
                            auth_payload = self.session_keeper.check_auth_status()
                            authenticated = bool(auth_payload.get("authenticated", False))
                        except Exception as exc:
                            logger.debug("Auth probe auth check failed: %s", exc)
                        try:
                            gateway_status_code = int(self.gateway_manager.status().get("status_code") or 0)
                        except Exception:
                            gateway_status_code = 0
                        phase = "manual_takeover" if self._manual_takeover_active(current) else "silent_probe"
                        snapshot = self._set_auth_recovery_state(
                            cycle_id=cycle_id,
                            recovery_phase=phase,
                            interruption_kind=interruption_kind,
                            recovery_reason=recovery_reason,
                            probe_started_at=current.get("probe_started_at") or self._now_iso(),
                            probe_last_checked_at=self._now_iso(),
                            probe_attempts=attempts,
                            probe_result="authenticated" if authenticated else "pending",
                            last_gateway_status_code=gateway_status_code,
                            last_recovery_source=source,
                            lock_owner="auth_probe",
                            lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
                        )
                        if authenticated:
                            self._mark_auth_recovered(source="auth_probe", reason=recovery_reason or source)
                            return
                        if (time.time() - started_perf) >= AUTH_PROBE_WINDOW_SECONDS:
                            self._set_auth_recovery_state(
                                cycle_id=cycle_id,
                                recovery_phase="auto_restart_2fa",
                                interruption_kind=interruption_kind,
                                recovery_reason=recovery_reason,
                                probe_last_checked_at=self._now_iso(),
                                probe_attempts=attempts,
                                probe_result="timeout",
                                auto_restart_scheduled=True,
                                last_recovery_source=source,
                                lock_owner="auth_restart",
                                lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
                                manual_takeover_active=False,
                            )
                            self._schedule_auth_restart(
                                reason=recovery_reason or "auth_probe_timeout",
                                source="auth_probe",
                                trigger_login=True,
                            )
                            return
                        if self._auth_probe_stop.wait(timeout=AUTH_PROBE_INTERVAL_SECONDS):
                            return
                finally:
                    with self._auth_recovery_lock:
                        if threading.current_thread() is self._auth_probe_thread:
                            self._auth_probe_thread = None

            self._auth_probe_thread = threading.Thread(
                target=worker,
                daemon=True,
                name="auth-probe",
            )
            self._auth_probe_thread.start()
            return True

    def _schedule_auth_restart(self, reason: str, source: str, trigger_login: bool = True):
        with self._auth_recovery_lock:
            thread = self._auth_restart_thread
            if thread and thread.is_alive():
                return False
            current = self._copy_auth_recovery_state()
            current_phase = str(current.get("recovery_phase") or "")
            current_lock_owner = str(current.get("lock_owner") or "")
            if self._manual_takeover_active(current):
                logger.info("Skip auth recovery restart during manual takeover")
                return False
            if current_phase in {"panic_resetting", "starting_runtime"} or current_lock_owner in {"panic_reset", "runtime_start"}:
                logger.info(
                    "Skip auth recovery restart while phase=%s lock_owner=%s",
                    current_phase or "-",
                    current_lock_owner or "-",
                )
                return False

            def worker():
                try:
                    logger.warning("Auth recovery restart scheduled: reason=%s source=%s", reason, source)
                    self.stop()
                    time.sleep(2)
                    self.start(
                        trigger_login=bool(trigger_login),
                        reason=reason or "auth_recovery_auto_restart",
                        source=source or "auth_recovery",
                    )
                except Exception as exc:
                    logger.error("Auth recovery restart failed: %s", exc)
                    self._set_auth_recovery_state(
                        recovery_phase="failed",
                        recovery_reason=reason or "auth_recovery_auto_restart",
                        probe_result="restart_failed",
                        auto_restart_scheduled=False,
                        last_recovery_source=source or "auth_recovery",
                        lock_owner="",
                        lock_expires_at="",
                    )
                finally:
                    with self._auth_recovery_lock:
                        if threading.current_thread() is self._auth_restart_thread:
                            self._auth_restart_thread = None

            self._auth_restart_thread = threading.Thread(
                target=worker,
                daemon=True,
                name="auth-restart",
            )
            self._auth_restart_thread.start()
            return True

    def _start_auth_recovery(self, interruption_kind: str, recovery_reason: str, source: str = "runtime"):
        current = self._copy_auth_recovery_state()
        cycle_id = current.get("cycle_id") or self._next_auth_cycle_id()
        phase = "manual_takeover" if self._manual_takeover_active(current) else "silent_probe"
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase=phase,
            recovery_reason=recovery_reason,
            interruption_kind=interruption_kind,
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_attempts=int(current.get("probe_attempts") or 0),
            probe_result="pending",
            auto_restart_scheduled=False,
            last_gateway_status_code=int(self.gateway_manager.status().get("status_code") or 0),
            last_recovery_source=source,
            lock_owner="auth_probe",
            lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        self._ensure_auth_probe(cycle_id, interruption_kind, recovery_reason, source)
        return snapshot

    def set_manual_takeover(self, enabled: bool, ttl_seconds: int = AUTH_MANUAL_TAKEOVER_TTL_SECONDS, reason: str = "", source: str = "runtime_page") -> dict:
        cycle_id = self._copy_auth_recovery_state().get("cycle_id") or self._next_auth_cycle_id()
        if enabled:
            snapshot = self._set_auth_recovery_state(
                cycle_id=cycle_id,
                recovery_phase="manual_takeover",
                recovery_reason=reason or "manual_takeover",
                interruption_kind=self._copy_auth_recovery_state().get("interruption_kind") or "manual_takeover",
                manual_takeover_active=True,
                manual_takeover_started_at=self._now_iso(),
                manual_takeover_until=self._future_iso(ttl_seconds or AUTH_MANUAL_TAKEOVER_TTL_SECONDS),
                last_recovery_source=source,
                lock_owner="manual_takeover",
                lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
            )
            if not self.session_keeper.is_authenticated:
                self._ensure_auth_probe(cycle_id, str(snapshot.get("interruption_kind") or "manual_takeover"), str(snapshot.get("recovery_reason") or "manual_takeover"), source)
            return snapshot
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            manual_takeover_active=False,
            manual_takeover_started_at="",
            manual_takeover_until="",
            recovery_phase="silent_probe" if not self.session_keeper.is_authenticated else "recovered",
            last_recovery_source=source,
            lock_owner="",
            lock_expires_at="",
        )
        if not self.session_keeper.is_authenticated:
            self._ensure_auth_probe(cycle_id, str(snapshot.get("interruption_kind") or "manual_takeover"), reason or "manual_takeover_released", source)
        return snapshot

    def trigger_auth_probe(self, reason: str = "", source: str = "runtime_page") -> dict:
        current = self._copy_auth_recovery_state()
        cycle_id = current.get("cycle_id") or self._next_auth_cycle_id()
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="manual_takeover" if self._manual_takeover_active(current) else "silent_probe",
            recovery_reason=reason or current.get("recovery_reason") or "manual_probe",
            interruption_kind=current.get("interruption_kind") or "manual_probe",
            probe_started_at=current.get("probe_started_at") or self._now_iso(),
            probe_last_checked_at=self._now_iso(),
            probe_result="pending",
            auto_restart_scheduled=False,
            last_recovery_source=source,
            lock_owner="auth_probe",
            lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        self._ensure_auth_probe(cycle_id, str(snapshot.get("interruption_kind") or "manual_probe"), str(snapshot.get("recovery_reason") or "manual_probe"), source)
        return snapshot

    def panic_reset_auth(self, restart_gateway: bool = True, restart_runtime: bool = True, trigger_login: bool = True, reason: str = "", source: str = "runtime_page") -> dict:
        cycle_id = self._next_auth_cycle_id()
        self._auth_probe_stop.set()
        snapshot = self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="panic_resetting",
            recovery_reason=reason or "panic_reset_2fa",
            interruption_kind="panic_reset",
            manual_takeover_active=False,
            probe_started_at="",
            probe_last_checked_at="",
            probe_attempts=0,
            probe_result="resetting",
            auto_restart_scheduled=bool(restart_runtime and trigger_login),
            last_recovery_source=source,
            lock_owner="panic_reset",
            lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        cookie_result = {"path": "", "existed": False, "removed": False, "error": ""}
        gateway_restarted = False
        runtime_started = False
        self.stop()
        cookie_result = clear_cookies()
        if self.pb:
            try:
                monitor_date = datetime.now(ET).strftime("%Y-%m-%d")
                cleared_auth_state = {
                    **self._load_global_auth_state(),
                    "status": "requested",
                    "message": "已全量清空旧 2FA / Session 状态，准备开启新一轮验证。",
                    "last_result": "旧 2FA / Session 状态已清空。",
                    "last_error": "",
                    "mode": "",
                    "challenge_code": "",
                    "challenge_detected_at": "",
                    "response_code": "",
                    "response_status": "",
                    "response_received_at": "",
                    "response_submitted_at": "",
                    "page_title": "",
                    "page_url": "",
                    "gateway_trace": "",
                    "browser_authenticated": False,
                    "gateway_authenticated": False,
                    "backend_authenticated": False,
                    "runtime_authenticated": False,
                    "runtime_started": False,
                    "message_id": "",
                    "last_delivered_ms": 0,
                    "last_delivered_at": "",
                    "last_delivered_hash": "",
                    "last_delivered_status": "",
                    "last_request_push_ms": 0,
                    "last_request_push_at": "",
                    "requested_at": self._now_iso(),
                    "triggered_at": "",
                    "result_at": "",
                    "next_retry_at": "",
                    **self._auth_recovery_pb_patch(snapshot),
                }
                self.pb.upsert_state("ibkr_2fa", ENVIRONMENT, cleared_auth_state, date="global")
                self.pb.upsert_state("system_auth_edge_monitor", ENVIRONMENT, {}, date=monitor_date)
                self.pb.upsert_state("system_auth_monitor", ENVIRONMENT, {}, date=monitor_date)
            except Exception as exc:
                logger.warning("Failed to clear PB auth state during panic reset: %s", exc)
        self._set_auth_recovery_state(
            cycle_id=cycle_id,
            recovery_phase="panic_resetting",
            recovery_reason=reason or "panic_reset_2fa",
            interruption_kind="panic_reset",
            manual_takeover_active=False,
            manual_takeover_started_at="",
            manual_takeover_until="",
            probe_started_at="",
            probe_last_checked_at=self._now_iso(),
            probe_attempts=0,
            probe_result="cookies_cleared" if cookie_result.get("removed") or not cookie_result.get("existed") else "cookie_clear_failed",
            auto_restart_scheduled=bool(restart_runtime and trigger_login),
            last_recovery_source=source,
            lock_owner="panic_reset",
            lock_expires_at=self._future_iso(AUTH_RECOVERY_LOCK_TTL_SECONDS),
        )
        if restart_gateway:
            gateway_restarted = bool(self.gateway_manager.restart())
        if restart_runtime:
            runtime_started = self._schedule_auth_restart(
                reason=reason or "panic_reset_2fa",
                source=source or "panic_reset",
                trigger_login=bool(trigger_login),
            )
        return {
            "cycle_id": cycle_id,
            "state": self._copy_auth_recovery_state(),
            "cookie_cleared": cookie_result,
            "gateway_restarted": gateway_restarted,
            "runtime_started": runtime_started,
            "restart_gateway": bool(restart_gateway),
            "restart_runtime": bool(restart_runtime),
            "trigger_login": bool(trigger_login),
            "reason": reason or "panic_reset_2fa",
            "source": source,
        }

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
            "integrity_pending_symbols": [],
            "integrity_repair_reasons": {},
            "preflight_repair": {},
            "backfill_written": 0,
            "backfill_result": {},
            "compute_result": {},
            "timings": {},
            "last_duration_s": 0.0,
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
        monitor_symbol_set = set(self._watchlist_monitor_symbols)
        monitor_symbols = [symbol for symbol in symbols if symbol in monitor_symbol_set]
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
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            timings={},
            last_duration_s=0.0,
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
                integrity_pending_symbols=[],
                integrity_repair_reasons={},
                preflight_repair={},
                backfill_written=0,
                backfill_result={},
                compute_result={},
                timings={},
                last_duration_s=0.0,
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
            integrity_pending_symbols=[],
            integrity_repair_reasons={},
            preflight_repair={},
            backfill_written=0,
            backfill_result={},
            compute_result={},
            timings={},
            last_duration_s=0.0,
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
            previous_kind = self._last_session_issue_kind
            self._last_session_authenticated = True
            self._mark_auth_recovered(source="session_transition", reason=previous_kind or "session_restored")
            logger.info("IBKR session restored; scheduling warmup refresh")
            self._notify_session_recovered(previous_kind)
            self._schedule_warmup(reason="session_restored", force=True)
        elif not authenticated and self._last_session_authenticated:
            self._last_session_authenticated = False
            self._close_warmup_gate("session_unauthenticated")
            self._start_auth_recovery(
                interruption_kind="runtime_unauthenticated",
                recovery_reason="session_unauthenticated",
                source="session_transition",
            )
            self._notify_session_issue(
                "runtime_unauthenticated",
                "IBKR Runtime 未认证",
                "检测到运行态已降为未认证，实时行情和交易链路可能不可用。",
                "请立即检查 Gateway 与飞书 2FA 状态，并在需要时重新触发验证。",
            )

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

    def _apply_integrity_readiness(self, readiness: dict, snapshot: dict, repair_plan: dict[str, dict] | None = None) -> dict:
        plan = repair_plan or {}
        if not plan:
            readiness["integrity_pending_symbols"] = []
            readiness["integrity_repair_reasons"] = {}
            for item in readiness.get("symbol_status") or []:
                if isinstance(item, dict):
                    item["integrity_ready"] = True
                    item["integrity_reason"] = ""
            return readiness

        blocking_symbols = sorted(plan.keys())
        repair_reasons = {
            symbol: str((plan.get(symbol) or {}).get("repair_reason") or "history_repair_pending")
            for symbol in blocking_symbols
        }
        ready_set = set(readiness.get("ready_symbols_list") or []) - set(blocking_symbols)
        pending_set = set(readiness.get("pending_symbols") or []) | set(blocking_symbols)
        trade_symbol_set = set(snapshot.get("trade_symbols") or [])
        monitor_symbol_set = set(snapshot.get("monitor_symbols") or [])

        status_map = {
            str((item or {}).get("symbol") or "").upper(): dict(item or {})
            for item in (readiness.get("symbol_status") or [])
            if str((item or {}).get("symbol") or "").strip()
        }
        merged_status = []
        for symbol in snapshot.get("symbols") or []:
            role = "trade" if symbol in trade_symbol_set else "monitor" if symbol in monitor_symbol_set else "active"
            row = dict(status_map.get(symbol) or {})
            row["symbol"] = symbol
            row["role"] = row.get("role") or role
            row["integrity_ready"] = symbol not in repair_reasons
            row["integrity_reason"] = repair_reasons.get(symbol, "")
            if symbol in repair_reasons:
                row["ready"] = False
            merged_status.append(row)

        ready_trade_symbols = len([symbol for symbol in snapshot.get("trade_symbols") or [] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot.get("monitor_symbols") or [] if symbol in ready_set])
        trade_blocked = any(symbol in trade_symbol_set for symbol in blocking_symbols)
        trading_gate_open = (
            bool(snapshot.get("trade_symbols"))
            and ready_trade_symbols == int(snapshot.get("trade_symbols_total", 0) or 0)
            and not trade_blocked
        )

        readiness["phase"] = "ready" if not pending_set else "degraded"
        readiness["ready_symbols"] = len(ready_set)
        readiness["ready_trade_symbols"] = ready_trade_symbols
        readiness["ready_monitor_symbols"] = ready_monitor_symbols
        readiness["ready_symbols_list"] = sorted(ready_set)
        readiness["pending_symbols"] = sorted(pending_set)
        readiness["symbol_status"] = merged_status
        readiness["integrity_pending_symbols"] = blocking_symbols
        readiness["integrity_repair_reasons"] = repair_reasons
        readiness["trading_gate_open"] = trading_gate_open
        if trading_gate_open:
            readiness["trading_gate_reason"] = "ready"
        elif trade_blocked:
            readiness["trading_gate_reason"] = "history_repair_pending"
        elif not snapshot.get("trade_symbols"):
            readiness["trading_gate_reason"] = "no_trade_symbols"
        else:
            readiness["trading_gate_reason"] = "warmup_incomplete"
        return readiness

    def _run_warmup_preflight_repairs(self, snapshot: dict) -> dict:
        repair_plan = self._build_startup_history_repair_plan(snapshot.get("symbols") or [])
        period_overrides = self._build_startup_history_period_overrides(repair_plan)
        if not repair_plan:
            return {
                "initial_repair_symbols": [],
                "attempted_repair_symbols": [],
                "remaining_repair_symbols": [],
                "repair_reasons": {},
                "history_fetch_symbols": [],
                "history_period_overrides": {},
                "history_written_total": 0,
                "repair_result": {},
            }

        logger.info(
            "Warmup preflight history repair started: symbols=%s short_window=%s",
            ",".join(sorted(repair_plan.keys())),
            ",".join(
                f"{symbol}:{(period_overrides.get(symbol) or {}).get('5m')}"
                for symbol in sorted(period_overrides.keys())
            ) or "none",
        )
        repair_result = self._run_bar_integrity_repairs(
            repair_plan,
            source="warmup_preflight",
            allow_defer=False,
            run_pipeline_repair=False,
            history_period_overrides=period_overrides,
        )
        remaining_plan = self._build_startup_history_repair_plan(snapshot.get("symbols") or [])
        history_written_total = 0
        for item in (repair_result.get("per_symbol") or {}).values():
            result = (item or {}).get("result") or {}
            history_written_total += int(result.get("history_written", 0) or 0)
        return {
            "initial_repair_symbols": sorted(repair_plan.keys()),
            "attempted_repair_symbols": sorted(repair_result.get("repair_symbols") or []),
            "remaining_repair_symbols": sorted(remaining_plan.keys()),
            "repair_reasons": {
                symbol: str((data or {}).get("repair_reason") or "history_repair_pending")
                for symbol, data in remaining_plan.items()
            },
            "history_fetch_symbols": sorted(repair_result.get("history_symbols") or []),
            "history_period_overrides": {
                symbol: dict((period_overrides.get(symbol) or {}))
                for symbol in sorted(period_overrides.keys())
            },
            "history_written_total": history_written_total,
            "repair_result": repair_result,
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

        backfill_result = {}
        backfill_written = 0
        compute_result = {}
        last_error = ""
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
        if readiness["trading_gate_open"]:
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
                "交易门": "open",
            }
            if phase != "ready":
                startup_title = "IBKR Runtime 启动完成（后台继续预热）"
                startup_detail["后续动作"] = "交易链路已开放，剩余 monitor/integrity repair 在后台继续。"
                startup_detail["待完成标的"] = self._format_symbol_list(readiness.get("pending_symbols") or [])
                startup_detail["完整性阻塞"] = self._format_symbol_list(readiness.get("integrity_pending_symbols") or [])
            if self._complete_startup_success(startup_title, startup_detail):
                self._schedule_interval_prime(snapshot["symbols"], source="startup_ready")
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
            self._startup_reason = reason
            self._startup_source = source
            self._startup_trigger_login = bool(trigger_login)
            self._startup_status_message_id = ""

        startup_ok = False
        startup_exit_notice = None

        try:
            logger.info("=" * 60)
            logger.info("IBKR Trading Service starting (env=%s)", ENVIRONMENT)
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

            if not self._ensure_gateway():
                logger.error("Gateway setup failed, exiting")
                startup_exit_notice = {
                    "event_type": "alert",
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

            # Do a one-shot auth check WITHOUT starting the keeper loop.
            # Starting session_keeper here would cause it to periodically
            # tickle + check_auth during the 2FA wait, creating a second/third
            # HTTP client hitting the Gateway and interfering with the SSO flow.
            self.session_keeper.check_auth_status()
            time.sleep(1)

            if not self.session_keeper.is_authenticated:
                if not trigger_login:
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
                    startup_exit_notice = {
                        "event_type": "alert",
                        "level": "warning",
                        "title": "IBKR Runtime 等待 2FA",
                        "detail": {
                            "状态结论": "检测到 Gateway 当前未认证，Runtime 尚未完成启动。",
                            "检查时间": self._now_et(),
                            "当前动作": "等待飞书 2FA 审批或人工重新触发启动。",
                            "启动原因": reason or "manual_start",
                            "启动来源": source or "api_start",
                            "触发登录": "no",
                        },
                    }
                    return
                logger.info("Not authenticated, attempting login (session_keeper paused)...")
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
                    startup_exit_notice = {
                        "event_type": "alert",
                        "level": "error",
                        "title": "IBKR Runtime 启动失败",
                        "detail": {
                            "异常结论": "Session/2FA 登录失败，Runtime 未能进入运行态。",
                            "检查时间": self._now_et(),
                            "失败阶段": "session_login",
                            "启动原因": reason or "manual_start",
                            "启动来源": source or "api_start",
                            "触发登录": "yes",
                        },
                    }
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
                    startup_message_id = self._startup_status_message_id
                    self._starting = False
                    self._startup_reason = ""
                    self._startup_source = ""
                    self._startup_trigger_login = False
                    self._startup_status_message_id = ""
                    if startup_exit_notice:
                        exit_notice = dict(startup_exit_notice)
                        exit_notice["message_id"] = startup_message_id
            if exit_notice:
                self._emit_system_event(
                    exit_notice.get("event_type", "alert"),
                    exit_notice.get("level", "error"),
                    exit_notice.get("title", "IBKR Runtime 启动失败"),
                    exit_notice.get("detail", {}),
                    message_id=exit_notice.get("message_id", ""),
                )

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

    def _watchlist_record_role(self, row: dict) -> str:
        return normalize_watchlist_symbol_role((row or {}).get("symbol_role"))

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
        monitor_symbols = []
        for symbol, row in merged.items():
            symbol_role = self._watchlist_record_role(row)
            symbol_meta[symbol] = {
                "exchange": str(row.get("exchange", "") or "").upper(),
                "industry": str(row.get("industry", "") or ""),
                "symbol_role": symbol_role,
            }
            if symbol_role == WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR:
                monitor_symbols.append(symbol)

        self._watchlist_records = merged
        self._watchlist_symbols = sorted(merged.keys())
        self._watchlist_monitor_symbols = sorted(monitor_symbols)
        self._symbol_meta = symbol_meta
        self._last_watchlist_refresh_at = now
        logger.info(
            "Watchlist pool refreshed: %d symbols (%d trade / %d monitor)",
            len(self._watchlist_symbols),
            max(0, len(self._watchlist_symbols) - len(self._watchlist_monitor_symbols)),
            len(self._watchlist_monitor_symbols),
        )

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

        for symbol in self._watchlist_monitor_symbols:
            if symbol in seen:
                continue
            selected_symbols.append(symbol)
            selected_meta[symbol] = {
                "exchange": str(
                    self._symbol_meta.get(symbol, {}).get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    self._symbol_meta.get(symbol, {}).get("industry")
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
                self._refresh_runtime_settings()
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
                self._refresh_runtime_settings()
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

        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if defer_repairs:
            logger.info(
                "Active target repair downgraded to scan-only: reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )

        result = self.scan_bar_integrity(
            list(self._active_subscription_symbols),
            scan_scope="active_target",
            persist=True,
            repair=not defer_repairs,
        )
        summary = result.get("summary") or {}
        attempted_repair_symbols = list(summary.get("attempted_repair_symbols") or [])
        repair_symbols = list(attempted_repair_symbols or summary.get("repair_candidate_symbols") or [])
        if not repair_symbols:
            logger.info("Active target repair skipped: no repair needed")
            return
        if defer_repairs and not attempted_repair_symbols:
            logger.info("Active target repair deferred: pending=%s", ",".join(repair_symbols))
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
                self._refresh_runtime_settings()
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
        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if defer_repairs:
            logger.info(
                "Watchlist maintenance deferred: reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )
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

    def _run_bar_integrity_repairs(
        self,
        snapshots: dict[str, dict],
        source: str,
        allow_defer: bool = True,
        run_pipeline_repair: bool = True,
        history_period_overrides: dict[str, dict] | None = None,
    ) -> dict:
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

        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if allow_defer and defer_repairs:
            for symbol in repair_symbols:
                per_symbol[symbol]["result"]["deferred"] = True
                per_symbol[symbol]["result"]["defer_reason"] = str(defer_snapshot.get("reason") or "realtime_priority_active")
                per_symbol[symbol]["result"]["queue_size"] = int(defer_snapshot.get("queue_size", 0) or 0)
            logger.info(
                "Pipeline repair deferred (%s): symbols=%s reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                source,
                ",".join(sorted(repair_symbols)),
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )
            return {
                "repair_symbols": [],
                "history_symbols": [],
                "deferred_symbols": sorted(repair_symbols),
                "deferred": True,
                "reason": str(defer_snapshot.get("reason") or "realtime_priority_active"),
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
                effective_period_overrides = {
                    symbol: dict((history_period_overrides or {}).get(symbol) or {})
                    for symbol in conid_map.keys()
                    if (history_period_overrides or {}).get(symbol)
                }
                backfill_result = self.data_backfill.backfill_all(
                    conid_map,
                    symbol_meta=symbol_meta,
                    intervals=["5m"],
                    repair_symbols=list(conid_map.keys()),
                    period_overrides=effective_period_overrides,
                )
                self.data_writer.flush()
                self._last_history_repair_at = time.time()
                self._last_history_repair_symbols = sorted(conid_map.keys())
                for symbol in conid_map.keys():
                    per_symbol[symbol]["result"]["history_written"] = int(
                        ((backfill_result.get(symbol) or {}).get("5m", 0) or 0)
                    )
                    history_period = str(
                        ((effective_period_overrides.get(symbol) or {}).get("5m") or "")
                    ).strip()
                    if history_period:
                        per_symbol[symbol]["result"]["history_period"] = history_period

        pipeline_result = {
            "ok": True,
            "symbols": sorted(repair_symbols),
            "compute": {},
            "rollup": {},
            "skipped": not run_pipeline_repair,
        }
        if run_pipeline_repair:
            pipeline_result = self._run_symbol_pipeline_repair(repair_symbols, source=source)
        pipeline_ok = bool(pipeline_result.get("ok", False))
        for symbol in repair_symbols:
            per_symbol[symbol]["result"]["pipeline_ok"] = pipeline_ok
            per_symbol[symbol]["result"]["pipeline"] = {
                "processed": int(((pipeline_result.get("compute") or {}).get("processed", 0) or 0)),
                "errors": int(((pipeline_result.get("compute") or {}).get("errors", 0) or 0)),
                "rollup_written": int(((pipeline_result.get("rollup") or {}).get("written", 0) or 0)),
                "skipped": bool(pipeline_result.get("skipped", False)),
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
        self._refresh_runtime_settings()
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

    def _build_startup_history_repair_plan(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        if not self.config.get_bool_for_environment("ibkr_history_repair_enabled", ENVIRONMENT, True):
            return {}

        plan = {}
        for symbol in sorted({str(item or "").upper() for item in symbols if str(item or "").strip()}):
            snapshot = self._collect_startup_history_repair_snapshot(symbol)
            if bool(snapshot.get("needs_pipeline_repair")):
                plan[symbol] = snapshot
        return plan

    def _startup_history_repair_period(self, snapshot: dict) -> str:
        reasons = [
            str(item or "").strip()
            for item in str((snapshot or {}).get("repair_reason") or "").split(",")
            if str(item or "").strip()
        ]
        if not reasons:
            return ""
        if any(
            reason.startswith("bars<") or reason.startswith("startup_snapshot_error:")
            for reason in reasons
        ):
            return ""
        if all(reason.startswith("today_regular_") for reason in reasons):
            return STARTUP_HISTORY_REPAIR_SHORT_PERIOD
        return ""

    def _build_startup_history_period_overrides(self, repair_plan: dict[str, dict]) -> dict[str, dict]:
        overrides = {}
        for symbol, snapshot in (repair_plan or {}).items():
            period = self._startup_history_repair_period(snapshot)
            if period:
                overrides[str(symbol).upper()] = {"5m": period}
        return overrides

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
        startup_wait_logged_at = 0.0
        while self._running or not self._compute_queue.empty():
            if self._starting:
                queue_size = int(self._compute_queue.qsize())
                now = time.time()
                if queue_size > 0 and (now - startup_wait_logged_at) >= 15:
                    logger.info(
                        "Realtime compute deferred while startup warmup is active: queue=%d",
                        queue_size,
                    )
                    startup_wait_logged_at = now
                time.sleep(1)
                continue

            startup_wait_logged_at = 0.0
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
                self._last_realtime_compute_started_at = time.time()
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
            finally:
                self._last_realtime_compute_started_at = 0.0

    def _bar_close_loop(self):
        logger.info("Bar close guard loop started")
        while self._running:
            try:
                self.bar_aggregator.force_close_due()
            except Exception as exc:
                logger.error("Bar close guard loop error: %s", exc)
            time.sleep(1)

    def _on_bar_close(self, bar_data: dict):
        self._last_bar_close_at = time.time()
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
            self._startup_reason = ""
            self._startup_source = ""
            self._startup_trigger_login = False
        self._running = False
        self._last_session_authenticated = False
        self._auth_probe_stop.set()

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
        return {
            "starting": self._starting,
            "startup_complete": bool(self._running and not self._starting),
            "runtime_phase": self._runtime_phase_label(),
            "environment": ENVIRONMENT,
            "gateway": self.gateway_manager.status(),
            "auth_recovery": self._copy_auth_recovery_state(),
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
                "last_daily_reset": (
                    datetime.fromtimestamp(self._last_daily_reset_at, ET).isoformat()
                    if self._last_daily_reset_at else None
                ),
                "watchlist_pool_count": len(self._watchlist_symbols),
                "active_target_date": self._active_target_date,
                "active_target_count": len(self._active_trade_symbols),
                "active_subscription_count": len(self._active_subscription_symbols),
                "active_target_symbols": list(self._active_trade_symbols),
                "active_subscription_symbols": list(self._active_subscription_symbols),
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
