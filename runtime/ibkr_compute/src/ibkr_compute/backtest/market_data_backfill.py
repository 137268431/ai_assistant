from __future__ import annotations

from .runtime_support import *


class BacktestMarketDataBackfillMixin:
    def _preflight_backfill_symbols(
        self,
        symbols: list[str],
        request: dict,
        progress_context: dict | None = None,
    ) -> dict:
        if not bool(request.get("preflight_backfill", True)):
            return {
                "enabled": False,
                "symbols": list(symbols or []),
                "needed_symbols": [],
                "initial": [],
                "results": {},
                "final": [],
            }
        if not symbols:
            return {"enabled": True, "symbols": [], "needed_symbols": [], "initial": [], "results": {}, "final": []}

        self._set_progress_context("running", "preflight", "checking bar coverage", 4, progress_context)
        initial = [
            self._symbol_range_coverage_summary(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                warmup_bars=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            )
            for symbol in symbols
        ]
        needed_symbols = [item["symbol"] for item in initial if item.get("needs_backfill")]
        if not needed_symbols:
            return {
                "enabled": True,
                "symbols": list(symbols),
                "needed_symbols": [],
                "initial": initial,
                "results": {},
                "final": initial,
            }

        start_ms, end_ms = self._date_to_ms_range(request["date_from"], request["date_to"])
        warmup_lookback_ms = interval_to_ms("5m") * (
            max(
                BACKTEST_WARMUP_BARS,
                int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            )
            + 20
        )
        backfill_start_ms = max(0, start_ms - warmup_lookback_ms)
        max_workers = min(
            max(1, int(request.get("backfill_concurrency", DEFAULT_BACKTEST_BACKFILL_CONCURRENCY) or DEFAULT_BACKTEST_BACKFILL_CONCURRENCY)),
            MAX_BACKTEST_BACKFILL_CONCURRENCY,
            len(needed_symbols),
        )
        symbol_timeout_s = min(
            MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
            max(
                30,
                int(
                    request.get("backfill_symbol_timeout_s", DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)
                    or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS
                ),
            ),
        )
        max_batches = min(
            MAX_BACKTEST_BACKFILL_MAX_BATCHES,
            max(
                1,
                int(request.get("backfill_max_batches", DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES) or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES),
            ),
        )
        history_timeout_s = min(
            MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
            max(
                5,
                int(
                    request.get("backfill_history_timeout_s", DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)
                    or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS
                ),
            ),
        )
        history_max_retries = min(
            MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
            max(
                0,
                int(
                    request.get("backfill_history_max_retries")
                    if request.get("backfill_history_max_retries") is not None
                    else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
                ),
            ),
        )
        self._set_progress_context(
            "running",
            "preflight_backfill",
            f"backfilling {len(needed_symbols)} symbols with {max_workers} workers",
            5,
            progress_context,
        )

        results: dict[str, dict] = {}
        completed = 0
        repair_windows_by_symbol = {
            str(item.get("symbol") or "").upper(): list(item.get("repair_windows") or [])
            for item in initial
            if item.get("needs_backfill")
        }

        def run_symbol(symbol: str) -> dict:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            windows = repair_windows_by_symbol.get(symbol) or [
                {
                    "start_ms": backfill_start_ms,
                    "end_ms": end_ms,
                    "reason": "legacy_full_range",
                }
            ]
            repair_rows = []
            repair_results = []
            remaining_batches = max_batches
            window_started_at = time.monotonic()
            for window in windows:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                if remaining_batches <= 0:
                    repair_results.append(
                        {
                            "ok": bool(repair_rows),
                            "reason": "max_batches_reached",
                            "fetched_rows": 0,
                            "batches": 0,
                            "window": window,
                        }
                    )
                    break
                elapsed_s = time.monotonic() - window_started_at
                remaining_elapsed_s = max(30, int(symbol_timeout_s - elapsed_s))
                if elapsed_s >= symbol_timeout_s:
                    repair_results.append(
                        {
                            "ok": bool(repair_rows),
                            "reason": "symbol_timeout",
                            "fetched_rows": 0,
                            "batches": 0,
                            "window": window,
                        }
                    )
                    break
                repair = self._backfill_symbol_history(
                    symbol,
                    request["source_environment"],
                    int(window.get("start_ms", backfill_start_ms) or backfill_start_ms),
                    int(window.get("end_ms", end_ms) or end_ms),
                    interval="5m",
                    max_elapsed_s=remaining_elapsed_s,
                    max_batches=remaining_batches,
                    history_timeout_s=history_timeout_s,
                    history_max_retries=history_max_retries,
                    cancel_check=self._cancel_event.is_set,
                )
                repair_result = {
                    key: value
                    for key, value in (repair or {}).items()
                    if key not in {"rows"}
                }
                repair_result["window"] = window
                repair_results.append(repair_result)
                repair_rows.extend(list((repair or {}).get("rows") or []))
                remaining_batches -= int((repair or {}).get("batches", 0) or 0)
                if str((repair or {}).get("reason") or "") in {"symbol_timeout", "cancelled", "partial_cancelled", "max_batches_reached"}:
                    break

            repair_rows = self._dedupe_backfill_rows(repair_rows)
            persisted_rows = self._persist_backfill_rows(repair_rows) if repair_rows else 0
            rolled_rows = self._rollup_symbol_history(symbol, request["source_environment"]) if persisted_rows > 0 else 0
            ok = bool(repair_rows) and persisted_rows > 0
            if not repair_rows and all(str(item.get("reason") or "") == "no_rows_fetched" for item in repair_results):
                ok = False
            reason = "ok" if ok else str((repair_results[-1] if repair_results else {}).get("reason") or "no_rows_fetched")
            return {
                "symbol": symbol,
                "ok": ok,
                "reason": reason,
                "fetched_rows": len(repair_rows),
                "batches": int(sum(int(item.get("batches", 0) or 0) for item in repair_results)),
                "elapsed_s": round(time.monotonic() - window_started_at, 3),
                "persisted_rows": int(persisted_rows or 0),
                "rolled_rows": int(rolled_rows or 0),
                "repair_window_count": len(windows),
                "repair_windows": windows[:20],
                "repair_results": repair_results[:20],
            }

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ibkr-backtest-preflight") as executor:
            pending = {executor.submit(run_symbol, symbol): symbol for symbol in needed_symbols}
            while pending:
                if self._cancel_event.is_set():
                    for pending_future in pending:
                        pending_future.cancel()
                    raise BacktestCancelled()
                done, pending_futures = wait(
                    pending,
                    timeout=BACKTEST_PREFLIGHT_HEARTBEAT_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    inflight_symbols = [pending[future] for future in pending]
                    progress_value = 5 + int((completed / max(1, len(needed_symbols))) * 10)
                    inflight_text = ",".join(inflight_symbols[:6]) or "--"
                    if len(inflight_symbols) > 6:
                        inflight_text += f"+{len(inflight_symbols) - 6}"
                    self._set_progress_context(
                        "running",
                        "preflight_backfill",
                        f"backfilled {completed}/{len(needed_symbols)} symbols; inflight {inflight_text}",
                        progress_value,
                        progress_context,
                    )
                    continue

                symbol_by_future = dict(pending)
                pending = {future: symbol_by_future[future] for future in pending_futures}
                for future in done:
                    symbol = symbol_by_future.get(future, "")
                    try:
                        results[symbol] = future.result()
                    except BacktestCancelled:
                        for pending_future in pending:
                            pending_future.cancel()
                        raise
                    except Exception as exc:
                        traceback.print_exc()
                        results[symbol] = {"symbol": symbol, "ok": False, "error": str(exc)[:500]}
                    completed += 1
                    progress_value = 5 + int((completed / max(1, len(needed_symbols))) * 10)
                    self._set_progress_context(
                        "running",
                        "preflight_backfill",
                        f"backfilled {completed}/{len(needed_symbols)} symbols",
                        progress_value,
                        progress_context,
                    )

        self._set_progress_context("running", "preflight", "rechecking bar coverage", 15, progress_context)
        final = [
            self._symbol_range_coverage_summary(
                symbol,
                request["source_environment"],
                request["date_from"],
                request["date_to"],
                warmup_bars=int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            )
            for symbol in symbols
        ]
        return {
            "enabled": True,
            "symbols": list(symbols),
            "needed_symbols": needed_symbols,
            "initial": initial,
            "results": results,
            "final": final,
            "backfill_start_us": format_us_time(backfill_start_ms) if backfill_start_ms > 0 else "",
            "backfill_end_us": format_us_time(end_ms) if end_ms > 0 else "",
            "concurrency": max_workers,
            "symbol_timeout_s": symbol_timeout_s,
            "max_batches": max_batches,
            "history_timeout_s": history_timeout_s,
            "history_max_retries": history_max_retries,
        }

    def _format_backfill_start_time(self, anchor_ms: int) -> str:
        # IB API's dash format is interpreted as UTC; keep the anchor instant exact.
        return datetime.fromtimestamp(int(anchor_ms) / 1000, timezone.utc).strftime("%Y%m%d-%H:%M:%S")

    def _backfill_symbol_history(
        self,
        symbol: str,
        source_environment: str,
        start_ms: int,
        end_ms: int,
        interval: str = "5m",
        *,
        max_elapsed_s: int | None = None,
        max_batches: int | None = None,
        history_timeout_s: int | None = None,
        history_max_retries: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict:
        if (
            not self.pb
            or self.data_backfill is None
            or self.conid_resolver is None
            or start_ms <= 0
            or end_ms <= 0
            or end_ms < start_ms
        ):
            return {"ok": False, "reason": "backfill_unavailable"}

        normalized_interval = normalize_interval(interval)
        if normalized_interval != "5m":
            return {"ok": False, "reason": "unsupported_interval"}

        try:
            conid = int(self.conid_resolver.resolve(symbol) or 0)
        except Exception:
            conid = 0
        if conid <= 0:
            return {"ok": False, "reason": "conid_unresolved"}

        interval_ms = interval_to_ms(normalized_interval)
        period = "4d"
        bar_size = "5min"
        # Historical requests use an exclusive end boundary, so ask one bar past
        # the final missing bar and then filter back to the requested window.
        anchor_ms = int(end_ms) + interval_ms
        earliest_needed_ms = int(start_ms)
        batches = 0
        fetched_rows = []
        seen_bar_ms = set()
        started_at = time.monotonic()
        raw_points = 0
        raw_first_ms = 0
        raw_last_ms = 0
        request_start_times: list[str] = []
        batch_limit = min(
            MAX_BACKTEST_BACKFILL_MAX_BATCHES,
            max(1, int(max_batches or DEFAULT_BACKTEST_BACKFILL_MAX_BATCHES)),
        )
        elapsed_limit_s = min(
            MAX_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS,
            max(30, int(max_elapsed_s or DEFAULT_BACKTEST_BACKFILL_SYMBOL_TIMEOUT_SECONDS)),
        )
        request_timeout_s = min(
            MAX_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS,
            max(5, int(history_timeout_s or DEFAULT_BACKTEST_BACKFILL_HISTORY_TIMEOUT_SECONDS)),
        )
        request_max_retries = min(
            MAX_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES,
            max(
                0,
                int(
                    history_max_retries
                    if history_max_retries is not None
                    else DEFAULT_BACKTEST_BACKFILL_HISTORY_MAX_RETRIES
                ),
            ),
        )

        stop_reason = ""
        while anchor_ms >= earliest_needed_ms and batches < batch_limit:
            if cancel_check and cancel_check():
                fetched_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                return {
                    "ok": bool(fetched_rows),
                    "reason": "cancelled" if not fetched_rows else "partial_cancelled",
                    "fetched_rows": len(fetched_rows),
                    "batches": batches,
                    "rows": fetched_rows,
                    "elapsed_s": round(time.monotonic() - started_at, 3),
                }
            if time.monotonic() - started_at >= elapsed_limit_s:
                stop_reason = "symbol_timeout"
                break
            start_time = self._format_backfill_start_time(anchor_ms)
            request_start_times.append(start_time)
            try:
                payload = self.data_backfill._request_history_json(
                    conid,
                    symbol,
                    normalized_interval,
                    period,
                    bar_size,
                    start_time=start_time,
                    timeout=request_timeout_s,
                    max_retries=request_max_retries,
                )
            except Exception as exc:
                fetched_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
                return {
                    "ok": bool(fetched_rows),
                    "reason": "partial_history_fetch_failed" if fetched_rows else "history_fetch_failed",
                    "error": str(exc)[:1000],
                    "fetched_rows": len(fetched_rows),
                    "batches": batches,
                    "rows": fetched_rows,
                    "elapsed_s": round(time.monotonic() - started_at, 3),
                }
            bars = list(payload.get("data") or [])
            if not bars:
                break

            batches += 1
            raw_points += len(bars)
            oldest_batch_ms = 0
            for bar in bars:
                raw_bar_time = int(bar.get("t", 0) or 0)
                bar_time_ms = raw_bar_time if raw_bar_time > 1_000_000_000_000 else raw_bar_time * 1000
                if bar_time_ms <= 0:
                    continue
                if raw_first_ms <= 0 or bar_time_ms < raw_first_ms:
                    raw_first_ms = bar_time_ms
                if raw_last_ms <= 0 or bar_time_ms > raw_last_ms:
                    raw_last_ms = bar_time_ms
                if oldest_batch_ms <= 0 or bar_time_ms < oldest_batch_ms:
                    oldest_batch_ms = bar_time_ms
                if bar_time_ms in seen_bar_ms:
                    continue
                seen_bar_ms.add(bar_time_ms)
                if bar_time_ms < earliest_needed_ms or bar_time_ms > end_ms:
                    continue
                fetched_rows.append({
                    "symbol": symbol,
                    "environment": source_environment,
                    "exchange": "",
                    "interval": normalized_interval,
                    "open": float(bar.get("o", 0) or 0),
                    "high": float(bar.get("h", 0) or 0),
                    "low": float(bar.get("l", 0) or 0),
                    "close": float(bar.get("c", 0) or 0),
                    "volume": float(bar.get("v", 0) or 0),
                    "bar_time_ms": bar_time_ms,
                    "us_time": format_us_time(bar_time_ms),
                    "cn_time": format_cn_time(bar_time_ms),
                    "session_type": classify_session(bar_time_ms=bar_time_ms),
                    "source": "backfill",
                    "extra": {
                        "source": "ibkr_history_backfill",
                        "canonical": True,
                        "conid": conid,
                        "interval": normalized_interval,
                        "outside_rth": True,
                        "request_period": period,
                        "request_bar": bar_size,
                        "request_start_time": start_time,
                        "backfill_scope": "backtest_range",
                        **build_runtime_timestamps(),
                    },
                })

            if oldest_batch_ms <= 0 or oldest_batch_ms <= earliest_needed_ms:
                break
            next_anchor_ms = oldest_batch_ms - interval_ms
            if next_anchor_ms >= anchor_ms:
                break
            anchor_ms = next_anchor_ms

        if not stop_reason and anchor_ms >= earliest_needed_ms and batches >= batch_limit:
            stop_reason = "max_batches_reached"

        fetched_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
        return {
            "ok": bool(fetched_rows),
            "reason": stop_reason or ("ok" if fetched_rows else "no_rows_fetched"),
            "fetched_rows": len(fetched_rows),
            "raw_points": raw_points,
            "raw_first_us": format_us_time(raw_first_ms) if raw_first_ms > 0 else "",
            "raw_last_us": format_us_time(raw_last_ms) if raw_last_ms > 0 else "",
            "request_start_times": request_start_times[:10],
            "batches": batches,
            "rows": fetched_rows,
            "elapsed_s": round(time.monotonic() - started_at, 3),
        }

    def _merge_backfill_rows(self, rows: list[dict], fetched_rows: list[dict]) -> list[dict]:
        merged = {}
        for row in rows or []:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms > 0:
                merged[bar_ms] = row
        for row in fetched_rows or []:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms > 0 and bar_ms not in merged:
                merged[bar_ms] = row
        return [merged[key] for key in sorted(merged)]

    def _dedupe_backfill_rows(self, rows: list[dict]) -> list[dict]:
        merged = {}
        for row in rows or []:
            bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if bar_ms > 0:
                merged[bar_ms] = row
        return [merged[key] for key in sorted(merged)]

    def _persist_backfill_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        try:
            with open_pb_sqlite(readonly=False, timeout=30.0) as conn:
                with conn:
                    return upsert_bars(conn, rows)
        except Exception:
            traceback.print_exc()

        if not self.pb:
            return 0

        written = 0
        for index in range(0, len(rows), 80):
            chunk = rows[index : index + 80]
            try:
                result = self.pb.upsert_bars(chunk)
            except Exception:
                traceback.print_exc()
                continue
            if result.get("ok", False):
                written += int(result.get("created", 0) or 0) + int(result.get("updated", 0) or 0)
        return written

    def _rollup_symbol_history(self, symbol: str, source_environment: str) -> int:
        try:
            with open_pb_sqlite(readonly=False, timeout=30.0) as conn:
                rows = conn.execute(
                    """
                    SELECT symbol, exchange, open, high, low, close, volume, session_type, us_time, cn_time, bar_time_ms
                    FROM ibkr_bars
                    WHERE symbol = ? AND interval = '5m' AND environment = ?
                    ORDER BY bar_time_ms ASC
                    """,
                    (symbol, source_environment),
                ).fetchall()
                if not rows:
                    return 0

                builder = TimeframeBarBuilder(target_intervals=HIGHER_INTERVALS)
                pending = []
                written = 0
                for row in rows:
                    base_bar = {
                        "symbol": str(row["symbol"] or "").upper(),
                        "exchange": str(row["exchange"] or "").upper(),
                        "environment": source_environment,
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
                    }
                    for derived in builder.consume(base_bar):
                        derived_extra = dict(derived.get("extra") or {})
                        derived_extra["canonical"] = True
                        derived["extra"] = derived_extra
                        pending.append(derived)
                        if len(pending) >= 400:
                            with conn:
                                written += upsert_bars(conn, pending)
                            pending = []
                if pending:
                    with conn:
                        written += upsert_bars(conn, pending)
                return written
        except Exception:
            traceback.print_exc()
            return 0

    def _ensure_scan_history_available(self, symbol: str, request: dict, cutoff_ms: int) -> dict:
        environment = request["source_environment"]
        lookback_limit = int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS)
        coverage_bars = max(BACKTEST_WARMUP_BARS + 20, lookback_limit + 20)
        coverage_start_ms = max(0, cutoff_ms - (interval_to_ms("5m") * coverage_bars))
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            environment,
            start_ms=coverage_start_ms,
            end_ms=cutoff_ms,
            descending=False,
        )
        need_backfill = (
            not rows
            or int(rows[0].get("bar_time_ms", 0) or 0) > coverage_start_ms
            or int(rows[-1].get("bar_time_ms", 0) or 0) < max(0, cutoff_ms - interval_to_ms("5m"))
            or self._count_internal_5m_gaps(rows) > 0
        )
        if not need_backfill:
            return {"ok": True, "needed": False, "persisted_rows": 0, "rolled_rows": 0}

        repair = self._backfill_symbol_history(
            symbol,
            environment,
            coverage_start_ms,
            cutoff_ms,
            interval="5m",
        )
        repair_rows = list((repair or {}).get("rows") or [])
        if not repair_rows:
            return {"ok": False, "needed": True, "persisted_rows": 0, "rolled_rows": 0}

        persisted_rows = self._persist_backfill_rows(repair_rows)
        rolled_rows = self._rollup_symbol_history(symbol, environment) if persisted_rows > 0 else 0
        return {
            "ok": persisted_rows > 0,
            "needed": True,
            "persisted_rows": persisted_rows,
            "rolled_rows": rolled_rows,
        }
