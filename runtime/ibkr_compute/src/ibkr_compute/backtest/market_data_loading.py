from __future__ import annotations

from .runtime_support import *
from .watchlist_universe import merge_trade_watchlist_rows, request_excluded_symbols


class BacktestMarketDataLoadingMixin:
    def _resolve_symbols(self, request: dict) -> list[str]:
        excluded = request_excluded_symbols(request)
        symbols = [str(symbol or "").strip().upper() for symbol in list(request.get("symbols") or [])]
        symbols = [symbol for symbol in symbols if symbol and symbol not in excluded]
        if symbols:
            return symbols[: request["max_symbols"]]

        source = request["symbol_source"]
        if source == "targets":
            rows = self.pb.get_all_records(
                "ibkr_targets",
                filter=f'environment = "{request["source_environment"]}" && status = "active"',
                sort="-date,-score",
                max_pages=20,
            )
            resolved = []
            seen = set()
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol or symbol in seen or symbol in excluded:
                    continue
                seen.add(symbol)
                resolved.append(symbol)
                if len(resolved) >= request["max_symbols"]:
                    break
            return resolved

        if source == "watchlist":
            rows = self.pb.get_all_records(
                "watchlist",
                sort="symbol",
                max_pages=50,
            )
            resolved = []
            for row in merge_trade_watchlist_rows(rows, request):
                symbol = str(row.get("symbol", "")).upper()
                if not symbol or symbol in resolved:
                    continue
                resolved.append(symbol)
                if len(resolved) >= request["max_symbols"]:
                    break
            return resolved

        return []

    def _load_bar_rows_from_sqlite(
        self,
        symbol: str,
        source_environment: str,
        *,
        interval: str = "5m",
        start_ms: int | None = None,
        end_ms: int | None = None,
        before_bar_time_ms: int | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            return []

        conditions = [
            "symbol = ?",
            "interval = ?",
            "environment = ?",
        ]
        params: list[Any] = [symbol, normalize_interval(interval), source_environment]
        if start_ms is not None:
            conditions.append("bar_time_ms >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            conditions.append("bar_time_ms <= ?")
            params.append(int(end_ms))
        if before_bar_time_ms is not None:
            conditions.append("bar_time_ms < ?")
            params.append(int(before_bar_time_ms))

        sql = (
            "SELECT symbol, exchange, interval, open, high, low, close, volume, "
            "session_type, us_time, cn_time, bar_time_ms "
            "FROM ibkr_bars "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY bar_time_ms {'DESC' if descending else 'ASC'}"
        )
        if limit is not None and int(limit or 0) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, params).fetchall()
        except Exception:
            traceback.print_exc()
            return []

        normalized = []
        for row in rows or []:
            normalized.append(
                {
                    "symbol": str(row["symbol"] or ""),
                    "exchange": str(row["exchange"] or ""),
                    "interval": str(row["interval"] or "5m"),
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
            )
        return normalized

    def _load_symbol_bars(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        session_mode: str,
        *,
        allow_backfill: bool = True,
    ) -> list[dict]:
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            start_ms=start_ms,
            end_ms=end_ms,
            descending=False,
        )
        if not rows and self.pb:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{symbol}" && interval = "5m" && environment = "{source_environment}" '
                    f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                ),
                sort="bar_time_ms",
                max_pages=DEFAULT_MAX_PAGES,
            )
        if rows:
            first_bar_ms = int(rows[0].get("bar_time_ms", 0) or 0)
            last_bar_ms = int(rows[-1].get("bar_time_ms", 0) or 0)
        else:
            first_bar_ms = 0
            last_bar_ms = 0
        if allow_backfill and (not rows or first_bar_ms > start_ms or last_bar_ms < end_ms - self._session_edge_grace_ms()):
            warmup_lookback_ms = interval_to_ms("5m") * (BACKTEST_WARMUP_BARS + 20)
            repair = self._backfill_symbol_history(
                symbol,
                source_environment,
                max(0, start_ms - warmup_lookback_ms),
                end_ms,
                interval="5m",
            )
            repair_rows = list((repair or {}).get("rows") or [])
            if repair_rows:
                self._persist_backfill_rows(repair_rows)
            rows = self._merge_backfill_rows(rows, repair_rows)
        if allow_backfill and rows and self._count_internal_5m_gaps(rows) > 0:
            warmup_lookback_ms = interval_to_ms("5m") * (BACKTEST_WARMUP_BARS + 20)
            repair = self._backfill_symbol_history(
                symbol,
                source_environment,
                max(0, start_ms - warmup_lookback_ms),
                end_ms,
                interval="5m",
            )
            repair_rows = list((repair or {}).get("rows") or [])
            if repair_rows:
                self._persist_backfill_rows(repair_rows)
            rows = self._merge_backfill_rows(rows, repair_rows)
        normalized = []
        seen = set()
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms in seen:
                continue
            seen.add(bar_ms)
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms))
            if session_mode == "regular" and session_mode_for_bar_time(bar_ms) != "regular":
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "interval": "5m",
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "session_type": session_type,
                    "us_time": str(row.get("us_time", "") or format_us_time(bar_ms)),
                    "cn_time": str(row.get("cn_time", "") or format_cn_time(bar_ms)),
                    "bar_time_ms": bar_ms,
                }
            )
        normalized.sort(key=lambda item: int(item["bar_time_ms"]))
        return normalized

    def _load_symbol_warmup_bars(
        self,
        symbol: str,
        source_environment: str,
        before_bar_time_ms: int,
        session_mode: str,
        limit: int = BACKTEST_WARMUP_BARS,
    ) -> list[dict]:
        if not self.pb or before_bar_time_ms <= 0 or limit <= 0:
            return []
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            before_bar_time_ms=before_bar_time_ms,
            descending=True,
            limit=max(limit + 20, BACKTEST_WARMUP_BARS + 20),
        )
        if not rows:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{symbol}" && interval = "5m" && environment = "{source_environment}" '
                    f"&& bar_time_ms < {before_bar_time_ms}"
                ),
                sort="-bar_time_ms",
                max_pages=max(4, min(20, math.ceil(limit / 200) + 2)),
            )
        if len(rows) < limit:
            warmup_start_ms = max(0, before_bar_time_ms - (interval_to_ms("5m") * max(limit + 20, BACKTEST_WARMUP_BARS + 20)))
            repair = self._backfill_symbol_history(
                symbol,
                source_environment,
                warmup_start_ms,
                max(0, before_bar_time_ms - interval_to_ms("5m")),
                interval="5m",
            )
            repair_rows = list((repair or {}).get("rows") or [])
            if repair_rows:
                self._persist_backfill_rows(repair_rows)
            rows = self._merge_backfill_rows(rows, repair_rows)
            rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0), reverse=True)
        normalized = []
        seen = set()
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms in seen:
                continue
            seen.add(bar_ms)
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms))
            if session_mode == "regular" and session_mode_for_bar_time(bar_ms) != "regular":
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "interval": "5m",
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "session_type": session_type,
                    "us_time": str(row.get("us_time", "") or format_us_time(bar_ms)),
                    "cn_time": str(row.get("cn_time", "") or format_cn_time(bar_ms)),
                    "bar_time_ms": bar_ms,
                }
            )
            if len(normalized) >= limit:
                break
        normalized.sort(key=lambda item: int(item["bar_time_ms"]))
        return normalized
