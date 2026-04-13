from __future__ import annotations

import json
import math
import threading
import time
import traceback
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable, Optional

from ibkr_compute.core.config import Config
from ibkr_compute.integrations.pb_client import PBClient
from ibkr_compute.market.data_backfill import DataBackfill
from ibkr_compute.market.pocketbase_sqlite import (
    count_rows,
    count_rows_by_environment,
    delete_rows_by_environment,
    delete_state_rows,
    fetch_state_payload,
    open_pb_sqlite,
    upsert_bars,
    upsert_state_payload,
)
from ibkr_compute.market.timeframe_builder import TimeframeBarBuilder
from ibkr_compute.market.timeframe_utils import (
    HIGHER_INTERVALS,
    bucket_start_ms,
    build_runtime_timestamps,
    classify_session,
    format_cn_time,
    format_us_time,
    interval_to_ms,
    ms_to_et,
)
from ibkr_compute.market.conid_resolver import ConidResolver

JOB_STATE_KEY = "ibkr_history_rebuild_job"
JOB_STATE_DATE = "global"
DEFAULT_LOOKBACK_DAYS = 14
MAX_LOOKBACK_DAYS = 365
DEFAULT_COMPUTE_BATCH_SIZE = 4
MIN_COMPUTE_BATCH_SIZE = 1
MAX_COMPUTE_BATCH_SIZE = 12
FETCH_INTERVAL = "5m"
FETCH_PERIOD = "4d"
FETCH_BAR_SIZE = "5min"
WATCHLIST_SYMBOL_ROLES = {"", "trade", "market_monitor"}
DEFAULT_MARKET_WS_SYMBOLS = ("SPY", "QQQ", "VIX")
PURGE_TABLES = (
    "ibkr_bars",
    "ibkr_indicators",
    "ibkr_signals",
    "ibkr_reverse_signals",
    "ibkr_targets",
    "ibkr_bar_integrity",
)
PURGE_STATE_KEYS = (
    "compute_cursors",
    "ibkr_bar_integrity_cursor",
    "ibkr_signals",
    "ibkr_daily_scan_state",
    JOB_STATE_KEY,
)


def _normalize_symbols(values: Any) -> list[str]:
    if isinstance(values, str):
        raw_values = values.replace("\n", ",").split(",")
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    else:
        raw_values = []

    normalized = []
    seen = set()
    for raw in raw_values:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _normalize_symbol_csv(value: Any) -> list[str]:
    if isinstance(value, str):
        return _normalize_symbols(value.replace("\n", ",").split(","))
    return _normalize_symbols(value)


class HistoryRebuildManager:
    def __init__(
        self,
        pb_client: PBClient,
        config: Config | None,
        *,
        compute_runner: Callable[[dict], dict],
        scan_runner: Callable[[dict], dict],
        current_market_date_resolver: Callable[[], str],
        runtime_status_resolver: Callable[[str], dict] | None = None,
    ):
        self.pb = pb_client
        self.config = config
        self.compute_runner = compute_runner
        self.scan_runner = scan_runner
        self.current_market_date_resolver = current_market_date_resolver
        self.runtime_status_resolver = runtime_status_resolver
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._status = self._load_persisted_status("live")
        self._cancel_event = threading.Event()
        self._conid_resolver = ConidResolver(pb_client=pb_client) if pb_client else None
        if self._conid_resolver is not None:
            try:
                self._conid_resolver.load_cache_from_pb()
            except Exception:
                traceback.print_exc()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, payload: dict | None) -> dict:
        request = self._normalize_request(payload or {})
        with self._lock:
            if self.is_running():
                return {
                    "ok": False,
                    "error": "history_rebuild_already_running",
                    **deepcopy(self._status),
                }

            runtime_check = self._runtime_check(request["environment"])
            if runtime_check.get("busy"):
                return {
                    **deepcopy(self._status),
                    "ok": False,
                    "error": "runtime_running",
                    "message": "历史重建前请先停掉当前 runtime，避免实时写入和全量清理互相冲突。",
                    "runtime_check": runtime_check,
                }

            now_ms = int(time.time() * 1000)
            job_id = f"history_rebuild_{now_ms}"
            self._cancel_event.clear()
            self._status = {
                "ok": True,
                "running": True,
                "status": "queued",
                "stage": "queued",
                "progress": 0,
                "job_id": job_id,
                "mode": request["mode"],
                "environment": request["environment"],
                "lookback_days": request["lookback_days"],
                "compute_batch_size": request["compute_batch_size"],
                "scan_after_rebuild": request["scan_after_rebuild"],
                "requested_symbols": list(request["symbols"]),
                "pipeline_stages": [
                    "resolve_universe",
                    "purge",
                    "backfill_bars",
                    "rollup_intervals",
                    "materialize_indicators",
                    "run_daily_scan",
                    "verify",
                ],
                "symbol_total": 0,
                "processed_symbols": 0,
                "fetched_5m_bars": 0,
                "rolled_bars": 0,
                "compute_processed": 0,
                "compute_batches_total": 0,
                "compute_batches_completed": 0,
                "counts_before": {},
                "counts_after": {},
                "purged": {},
                "scan_result": {},
                "verification": {},
                "runtime_check": runtime_check,
                "logs": [],
                "last_error": "",
                "started_at": datetime.utcnow().isoformat() + "Z",
                "finished_at": "",
                "updated_at_ms": now_ms,
            }
            self._append_log_locked(
                f"job queued: env={request['environment']} lookback_days={request['lookback_days']}"
            )
            self._persist_status_locked()
            self._thread = threading.Thread(
                target=self._run_job,
                args=(request,),
                daemon=True,
                name=f"ibkr-history-rebuild-{job_id[-8:]}",
            )
            self._thread.start()
            return deepcopy(self._status)

    def status(self, environment: str | None = None) -> dict:
        requested_environment = str(environment or self._status.get("environment") or "live").strip().lower() or "live"
        with self._lock:
            if not self.is_running():
                persisted = self._load_persisted_status(requested_environment)
                if persisted.get("updated_at_ms", 0) >= self._status.get("updated_at_ms", 0):
                    self._status = persisted
            payload = deepcopy(self._status)
            payload.setdefault("ok", True)
            payload["running"] = self.is_running()
            payload["runtime_check"] = self._runtime_check(requested_environment)
            return payload

    def _normalize_request(self, payload: dict) -> dict:
        environment = str(payload.get("environment") or "live").strip().lower() or "live"
        try:
            lookback_days = int(payload.get("lookback_days") or DEFAULT_LOOKBACK_DAYS)
        except (TypeError, ValueError):
            lookback_days = DEFAULT_LOOKBACK_DAYS
        lookback_days = max(1, min(MAX_LOOKBACK_DAYS, lookback_days))

        try:
            compute_batch_size = int(payload.get("compute_batch_size") or DEFAULT_COMPUTE_BATCH_SIZE)
        except (TypeError, ValueError):
            compute_batch_size = DEFAULT_COMPUTE_BATCH_SIZE
        compute_batch_size = max(MIN_COMPUTE_BATCH_SIZE, min(MAX_COMPUTE_BATCH_SIZE, compute_batch_size))

        scan_after_rebuild = str(payload.get("scan_after_rebuild", True)).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        mode = str(payload.get("mode") or "full_reset_rebuild").strip().lower() or "full_reset_rebuild"
        symbols = _normalize_symbols(payload.get("symbols") or payload.get("symbol"))
        return {
            "mode": mode,
            "environment": environment,
            "lookback_days": lookback_days,
            "compute_batch_size": compute_batch_size,
            "scan_after_rebuild": scan_after_rebuild,
            "symbols": symbols,
        }

    def _run_job(self, request: dict) -> None:
        with self._lock:
            self._set_status_locked("running", "resolve_universe", "准备执行全量历史重建", 2)

        conn = None
        try:
            conn = open_pb_sqlite(readonly=False, timeout=60.0)
            environment = request["environment"]
            symbols = self._load_symbol_universe(environment, request.get("symbols") or [])
            end_ms = max(0, bucket_start_ms(int(time.time() * 1000), FETCH_INTERVAL) - interval_to_ms(FETCH_INTERVAL))
            start_ms = max(0, end_ms - request["lookback_days"] * 24 * 60 * 60 * 1000)

            counts_before = self._count_chain_rows(conn, environment)
            with self._lock:
                self._status["counts_before"] = counts_before
                self._status["symbol_total"] = len(symbols)
                self._status["symbols"] = [item["symbol"] for item in symbols]
                self._status["start_ms"] = start_ms
                self._status["end_ms"] = end_ms
                self._status["start_us_time"] = format_us_time(start_ms) if start_ms > 0 else ""
                self._status["end_us_time"] = format_us_time(end_ms) if end_ms > 0 else ""
                self._append_log_locked(
                    f"loaded universe: {len(symbols)} symbols, range {self._status['start_us_time']} -> {self._status['end_us_time']}"
                )
                self._persist_status_locked()

            self._purge_live_chain(conn, environment)
            self._refill_5m_bars(conn, environment, symbols, start_ms, end_ms, request)
            self._rollup_higher_intervals(conn, environment, symbols)
            self._recompute_indicators(environment, symbols, request)
            if request["scan_after_rebuild"]:
                self._scan_targets(environment)
            verification = self._verify(conn, environment)

            with self._lock:
                self._status["counts_after"] = self._count_chain_rows(conn, environment)
                self._status["verification"] = verification
                self._status["running"] = False
                self._status["status"] = "completed"
                self._status["stage"] = "completed"
                self._status["progress"] = 100
                self._status["finished_at"] = datetime.utcnow().isoformat() + "Z"
                self._append_log_locked("history rebuild completed")
                self._persist_status_locked()
        except Exception as exc:
            traceback.print_exc()
            with self._lock:
                self._status["running"] = False
                self._status["status"] = "failed"
                self._status["stage"] = "failed"
                self._status["last_error"] = str(exc)
                self._status["finished_at"] = datetime.utcnow().isoformat() + "Z"
                self._append_log_locked(f"failed: {exc}")
                self._persist_status_locked()
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self._thread = None

    def _runtime_check(self, environment: str) -> dict:
        if not self.runtime_status_resolver:
            return {"busy": False}
        try:
            payload = self.runtime_status_resolver(environment) or {}
        except Exception as exc:
            return {"busy": False, "error": str(exc)}

        gateway = payload.get("gateway") or {}
        websocket = payload.get("websocket") or {}
        actual_environment = str(payload.get("environment") or environment).strip().lower() or environment
        same_environment = actual_environment == environment
        busy = same_environment and (bool(payload.get("starting")) or bool(payload.get("startup_complete")))
        busy = busy or bool(gateway.get("running")) or bool(websocket.get("running"))
        busy = same_environment and busy
        return {
            "busy": busy,
            "environment": environment,
            "actual_environment": actual_environment,
            "environment_mismatch": actual_environment != environment,
            "starting": bool(payload.get("starting")),
            "startup_complete": bool(payload.get("startup_complete")),
            "gateway_running": bool(gateway.get("running")),
            "websocket_running": bool(websocket.get("running")),
        }

    def _load_symbol_universe(self, environment: str, explicit_symbols: list[str]) -> list[dict]:
        if explicit_symbols:
            return [{"symbol": symbol, "exchange": "", "symbol_role": "manual"} for symbol in explicit_symbols]

        rows = self.pb.get_all_records(
            "watchlist",
            filter=(
                f'(environment = "{environment}" || environment = "global" || environment = "") '
                '&& (symbol_role = "trade" || symbol_role = "market_monitor" || symbol_role = "")'
            ),
            sort="symbol",
            max_pages=20,
        )
        merged = {}
        applied = {}
        priority = {"": 0, "global": 1, environment: 2}
        for row in rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if not symbol:
                continue
            role = str(row.get("symbol_role", "") or "").strip().lower()
            if role not in WATCHLIST_SYMBOL_ROLES:
                continue
            row_env = str(row.get("environment", "") or "").strip().lower()
            rank = priority.get(row_env, -1)
            if rank < 0:
                continue
            if symbol in applied and applied[symbol] > rank:
                continue
            applied[symbol] = rank
            merged[symbol] = {
                "symbol": symbol,
                "exchange": str(row.get("exchange", "") or "").strip().upper(),
                "symbol_role": role or "trade",
            }
        configured_market_ws_symbols = []
        if self.config and self.config.get_bool_for_environment("ibkr_market_ws_enabled", environment, True):
            configured_market_ws_symbols = _normalize_symbol_csv(
                self.config.get_for_environment(
                    "ibkr_market_ws_symbols",
                    environment,
                    ",".join(DEFAULT_MARKET_WS_SYMBOLS),
                )
            )
        for symbol in configured_market_ws_symbols:
            merged.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "exchange": "",
                    "symbol_role": "market_monitor",
                },
            )
        return list(merged.values())

    def _count_chain_rows(self, conn, environment: str) -> dict:
        counts = {table: count_rows_by_environment(conn, table, environment) for table in PURGE_TABLES}
        counts["ibkr_state_live"] = count_rows(
            conn,
            "ibkr_state",
            where="environment = ?",
            params=(environment,),
        )
        counts["ibkr_state_rebuild_job"] = count_rows(
            conn,
            "ibkr_state",
            where="environment = ? AND state_key = ?",
            params=(environment, JOB_STATE_KEY),
        )
        return counts

    def _purge_live_chain(self, conn, environment: str) -> None:
        with self._lock:
            self._set_status_locked("running", "purge", "清理旧的 bars / indicators / signals / targets / integrity", 8)

        purged = {}
        with conn:
            for table in PURGE_TABLES:
                purged[table] = delete_rows_by_environment(conn, table, environment)
            purged["ibkr_state"] = delete_state_rows(conn, environment, PURGE_STATE_KEYS)

        with self._lock:
            self._status["purged"] = purged
            self._append_log_locked(
                "purged chain: "
                + ", ".join(f"{key}={value}" for key, value in purged.items())
            )
            self._persist_status_locked()

    def _build_data_backfill(self, environment: str) -> DataBackfill:
        return DataBackfill(config=self.config, environment=environment)

    def _refill_5m_bars(
        self,
        conn,
        environment: str,
        symbols: list[dict],
        start_ms: int,
        end_ms: int,
        request: dict,
    ) -> None:
        data_backfill = self._build_data_backfill(environment)
        total_symbols = max(1, len(symbols))
        for index, item in enumerate(symbols, start=1):
            symbol = item["symbol"]
            with self._lock:
                progress = 12 + int((index - 1) / total_symbols * 46)
                self._set_status_locked(
                    "running",
                    "backfill_bars",
                    f"回填 5m 权威 bars: {symbol} ({index}/{total_symbols})",
                    progress,
                )
            result = self._backfill_symbol_range(
                conn,
                data_backfill,
                environment,
                symbol,
                item.get("exchange", ""),
                start_ms,
                end_ms,
                request["lookback_days"],
            )
            with self._lock:
                self._status["processed_symbols"] = index
                self._status["fetched_5m_bars"] = int(self._status.get("fetched_5m_bars", 0) or 0) + int(
                    result.get("written_rows", 0) or 0
                )
                self._append_log_locked(
                    f"refilled {symbol}: rows={result.get('written_rows', 0)} batches={result.get('batches', 0)}"
                )
                self._persist_status_locked()

    def _backfill_symbol_range(
        self,
        conn,
        data_backfill: DataBackfill,
        environment: str,
        symbol: str,
        exchange: str,
        start_ms: int,
        end_ms: int,
        lookback_days: int,
    ) -> dict:
        if self._conid_resolver is None:
            raise RuntimeError("conid_resolver_unavailable")

        try:
            conid = int(self._conid_resolver.resolve(symbol) or 0)
        except Exception:
            conid = 0
        if conid <= 0:
            raise RuntimeError(f"conid_unresolved:{symbol}")

        interval_ms = interval_to_ms(FETCH_INTERVAL)
        anchor_ms = int(end_ms)
        earliest_needed_ms = int(start_ms)
        seen_bar_ms = set()
        written_rows = 0
        batches = 0
        max_batches = max(8, math.ceil(max(1, end_ms - start_ms) / (4 * 24 * 60 * 60 * 1000)) + 8)

        while anchor_ms >= earliest_needed_ms and batches < max_batches:
            request_start_time = ms_to_et(anchor_ms).strftime("%Y%m%d-%H:%M:%S")
            payload = data_backfill._request_history_json(
                conid,
                symbol,
                FETCH_INTERVAL,
                FETCH_PERIOD,
                FETCH_BAR_SIZE,
                start_time=request_start_time,
            )
            bars = list(payload.get("data") or [])
            if not bars:
                break

            batches += 1
            oldest_batch_ms = 0
            prepared = []
            for bar in bars:
                raw_bar_time = int(bar.get("t", 0) or 0)
                bar_time_ms = raw_bar_time if raw_bar_time > 1_000_000_000_000 else raw_bar_time * 1000
                if bar_time_ms <= 0:
                    continue
                if oldest_batch_ms <= 0 or bar_time_ms < oldest_batch_ms:
                    oldest_batch_ms = bar_time_ms
                if bar_time_ms in seen_bar_ms:
                    continue
                seen_bar_ms.add(bar_time_ms)
                if bar_time_ms < earliest_needed_ms or bar_time_ms > end_ms:
                    continue
                prepared.append(
                    {
                        "symbol": symbol,
                        "environment": environment,
                        "exchange": exchange,
                        "interval": FETCH_INTERVAL,
                        "open": float(bar.get("o", 0) or 0),
                        "high": float(bar.get("h", 0) or 0),
                        "low": float(bar.get("l", 0) or 0),
                        "close": float(bar.get("c", 0) or 0),
                        "volume": float(bar.get("v", 0) or 0),
                        "bar_time_ms": bar_time_ms,
                        "us_time": format_us_time(bar_time_ms),
                        "cn_time": format_cn_time(bar_time_ms),
                        "session_type": classify_session(bar_time_ms=bar_time_ms),
                        "extra": {
                            "source": "ibkr_history_rebuild",
                            "canonical": True,
                            "conid": conid,
                            "interval": FETCH_INTERVAL,
                            "outside_rth": True,
                            "request_period": FETCH_PERIOD,
                            "request_bar": FETCH_BAR_SIZE,
                            "request_start_time": request_start_time,
                            "history_rebuild_lookback_days": lookback_days,
                            **build_runtime_timestamps(),
                        },
                    }
                )
            if prepared:
                with conn:
                    written_rows += upsert_bars(conn, prepared)

            if oldest_batch_ms <= 0 or oldest_batch_ms <= earliest_needed_ms:
                break
            next_anchor_ms = oldest_batch_ms - interval_ms
            if next_anchor_ms >= anchor_ms:
                break
            anchor_ms = next_anchor_ms

        return {
            "symbol": symbol,
            "written_rows": written_rows,
            "batches": batches,
        }

    def _rollup_higher_intervals(self, conn, environment: str, symbols: list[dict]) -> None:
        total_symbols = max(1, len(symbols))
        for index, item in enumerate(symbols, start=1):
            symbol = item["symbol"]
            with self._lock:
                progress = 60 + int((index - 1) / total_symbols * 14)
                self._set_status_locked(
                    "running",
                    "rollup_intervals",
                    f"重建 15m/30m/1h/4h/1d: {symbol} ({index}/{total_symbols})",
                    progress,
                )
            rolled = self._rollup_symbol(conn, symbol, environment)
            with self._lock:
                self._status["rolled_bars"] = int(self._status.get("rolled_bars", 0) or 0) + rolled
                self._append_log_locked(f"rolled {symbol}: derived={rolled}")
                self._persist_status_locked()

    def _rollup_symbol(self, conn, symbol: str, environment: str) -> int:
        rows = conn.execute(
            """
            SELECT symbol, exchange, interval, open, high, low, close, volume, session_type, us_time, cn_time, bar_time_ms, extra
            FROM ibkr_bars
            WHERE symbol = ? AND interval = '5m' AND environment = ?
            ORDER BY bar_time_ms ASC
            """,
            (symbol, environment),
        ).fetchall()
        if not rows:
            return 0

        builder = TimeframeBarBuilder(target_intervals=HIGHER_INTERVALS)
        pending = []
        written = 0
        for row in rows:
            extra = {}
            raw_extra = row["extra"]
            if isinstance(raw_extra, str) and raw_extra.strip():
                try:
                    extra = json.loads(raw_extra)
                except Exception:
                    extra = {}
            base_bar = {
                "symbol": str(row["symbol"] or "").upper(),
                "environment": environment,
                "exchange": str(row["exchange"] or "").upper(),
                "interval": "5m",
                "open": float(row["open"] or 0),
                "high": float(row["high"] or 0),
                "low": float(row["low"] or 0),
                "close": float(row["close"] or 0),
                "volume": float(row["volume"] or 0),
                "session_type": str(row["session_type"] or ""),
                "us_time": str(row["us_time"] or ""),
                "cn_time": str(row["cn_time"] or ""),
                "bar_time_ms": int(row["bar_time_ms"] or 0),
                "extra": extra,
            }
            for derived in builder.consume(base_bar):
                derived_extra = dict(derived.get("extra") or {})
                derived_extra["canonical"] = True
                derived["extra"] = derived_extra
                pending.append(derived)
                if len(pending) >= 500:
                    with conn:
                        written += upsert_bars(conn, pending)
                    pending = []
        if pending:
            with conn:
                written += upsert_bars(conn, pending)
        return written

    def _recompute_indicators(self, environment: str, symbols: list[dict], request: dict) -> None:
        symbol_list = [item["symbol"] for item in symbols]
        if not symbol_list:
            return

        batches = [
            symbol_list[index : index + request["compute_batch_size"]]
            for index in range(0, len(symbol_list), request["compute_batch_size"])
        ]
        total_batches = len(batches)
        with self._lock:
            self._status["compute_batches_total"] = total_batches
            self._persist_status_locked()

        for index, batch in enumerate(batches, start=1):
            with self._lock:
                progress = 76 + int((index - 1) / max(1, total_batches) * 16)
                self._set_status_locked(
                    "running",
                    "materialize_indicators",
                    f"重算 indicators/signals: {', '.join(batch[:3])}{' ...' if len(batch) > 3 else ''}",
                    progress,
                )
            result = self.compute_runner(
                {
                    "source": "history_rebuild",
                    "environment": environment,
                    "symbols": batch,
                    "persist_signals": False,
                    "force_rollup": False,
                }
            ) or {}
            with self._lock:
                self._status["compute_batches_completed"] = index
                self._status["compute_processed"] = int(self._status.get("compute_processed", 0) or 0) + int(
                    result.get("processed", 0) or 0
                )
                self._append_log_locked(
                    f"compute batch {index}/{total_batches}: processed={result.get('processed', 0)} errors={result.get('errors', 0)}"
                )
                self._persist_status_locked()
            if result.get("ok") is False or int(result.get("errors", 0) or 0) > 0:
                raise RuntimeError(
                    f"history_rebuild_compute_failed: batch={index} errors={result.get('errors', 0)}"
                )

    def _scan_targets(self, environment: str) -> None:
        with self._lock:
            self._set_status_locked("running", "run_daily_scan", "回放盘前筛选，重建当日 targets", 94)
        result = self.scan_runner({"environment": environment}) or {}
        with self._lock:
            self._status["scan_result"] = result
            self._append_log_locked(
                f"scan completed: scanned={result.get('scanned', 0)} candidates={result.get('candidates', 0)}"
            )
            self._persist_status_locked()
        if result.get("ok") is False:
            raise RuntimeError("history_rebuild_scan_failed")

    def _verify(self, conn, environment: str) -> dict:
        with self._lock:
            self._set_status_locked("running", "verify", "校验重建结果", 98)
        interval_rows = conn.execute(
            """
            SELECT interval, COUNT(*) AS total
            FROM ibkr_bars
            WHERE environment = ?
            GROUP BY interval
            ORDER BY interval
            """,
            (environment,),
        ).fetchall()
        intervals = {str(row["interval"] or ""): int(row["total"] or 0) for row in interval_rows}
        total_5m = int(intervals.get("5m", 0) or 0)
        canonical_5m = 0
        try:
            canonical_5m = count_rows(
                conn,
                "ibkr_bars",
                where="environment = ? AND interval = '5m' AND json_extract(extra, '$.canonical') = 1",
                params=(environment,),
            )
        except Exception:
            canonical_5m = count_rows(
                conn,
                "ibkr_bars",
                where="environment = ? AND interval = '5m' AND extra LIKE '%\"canonical\":true%'",
                params=(environment,),
            )
        return {
            "market_date": self.current_market_date_resolver(),
            "interval_counts": intervals,
            "total_5m": total_5m,
            "canonical_5m": canonical_5m,
            "noncanonical_5m": max(0, total_5m - canonical_5m),
            "targets": count_rows_by_environment(conn, "ibkr_targets", environment),
            "indicators": count_rows_by_environment(conn, "ibkr_indicators", environment),
            "signals": count_rows_by_environment(conn, "ibkr_signals", environment),
        }

    def _load_persisted_status(self, environment: str) -> dict:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        try:
            with open_pb_sqlite(readonly=True, timeout=5.0) as conn:
                record = fetch_state_payload(conn, JOB_STATE_KEY, runtime_environment, date=JOB_STATE_DATE)
                if record and isinstance(record.get("data"), dict):
                    payload = dict(record["data"])
                    payload["running"] = False
                    payload.setdefault("ok", True)
                    return payload
        except Exception:
            pass
        return {
            "ok": True,
            "running": False,
            "status": "idle",
            "stage": "idle",
            "progress": 0,
            "job_id": "",
            "environment": runtime_environment,
            "logs": [],
            "last_error": "",
            "updated_at_ms": 0,
        }

    def _persist_status_locked(self) -> None:
        self._status["updated_at_ms"] = int(time.time() * 1000)
        try:
            with open_pb_sqlite(readonly=False, timeout=10.0) as conn:
                with conn:
                    upsert_state_payload(
                        conn,
                        JOB_STATE_KEY,
                        str(self._status.get("environment") or "live"),
                        self._status,
                        date=JOB_STATE_DATE,
                    )
        except Exception:
            if self.pb:
                self.pb.upsert_state(
                    JOB_STATE_KEY,
                    str(self._status.get("environment") or "live"),
                    self._status,
                    date=JOB_STATE_DATE,
                )

    def _set_status_locked(self, status: str, stage: str, message: str, progress: int) -> None:
        self._status["status"] = status
        self._status["stage"] = stage
        self._status["message"] = message
        self._status["progress"] = max(0, min(100, int(progress or 0)))
        self._status["running"] = status not in {"completed", "failed", "idle"}
        self._append_log_locked(message, dedupe=True)
        self._persist_status_locked()

    def _append_log_locked(self, message: str, *, dedupe: bool = False) -> None:
        text = str(message or "").strip()
        if not text:
            return
        logs = list(self._status.get("logs") or [])
        if dedupe and logs and str(logs[-1].get("message") or "") == text:
            return
        logs.append(
            {
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "message": text,
            }
        )
        self._status["logs"] = logs[-40:]
