from __future__ import annotations


class WarmupCycleReadinessModelMixin:
    def _build_warmup_readiness(
        self,
        snapshot: dict,
        status_by_symbol: dict[str, dict] | None,
        *,
        required_interval: str,
    ) -> dict:
        ready_symbols = []
        pending_symbols = []
        symbol_status = []
        ready_set = set()
        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(snapshot["trade_symbols"])
        monitor_symbol_set = set(snapshot["monitor_symbols"])

        for symbol in snapshot["symbols"]:
            status = dict((status_by_symbol or {}).get(symbol) or {})
            is_ready = bool(status.get("ready"))
            bar_count = int(status.get("bar_count", 0) or 0)
            last_bar_time_ms = int(status.get("last_bar_time_ms", 0) or 0)
            if symbol in trade_symbol_set:
                role = "trade"
            elif symbol in monitor_symbol_set:
                role = "monitor"
            elif symbol in scan_symbol_set:
                role = "scan"
            elif symbol in subscription_symbol_set:
                role = "subscription"
            else:
                role = "data"
            row = {
                "symbol": symbol,
                "role": role,
                "ready": is_ready,
                "bar_count": bar_count,
                "last_bar_time_ms": last_bar_time_ms,
            }
            source = str(status.get("source") or "").strip()
            if source:
                row["source"] = source
            symbol_status.append(row)
            if is_ready:
                ready_symbols.append(symbol)
                ready_set.add(symbol)
            else:
                pending_symbols.append(symbol)

        ready_scan_symbols = len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set])
        ready_subscription_symbols = len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set])
        ready_trade_symbols = len([symbol for symbol in snapshot["trade_symbols"] if symbol in ready_set])
        ready_monitor_symbols = len([symbol for symbol in snapshot["monitor_symbols"] if symbol in ready_set])
        trading_gate_open = bool(snapshot["trade_symbols"]) and ready_trade_symbols == snapshot["trade_symbols_total"]
        data_ready = bool(snapshot["symbols"]) and not pending_symbols
        return {
            "phase": "ready" if data_ready else "degraded",
            "data_ready": data_ready,
            "trade_allowed": trading_gate_open,
            "required_interval": required_interval,
            "ready_symbols": len(ready_symbols),
            "ready_scan_symbols": ready_scan_symbols,
            "ready_subscription_symbols": ready_subscription_symbols,
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
            readiness["data_ready"] = bool(snapshot.get("symbols")) and not (readiness.get("pending_symbols") or [])
            readiness["trade_allowed"] = bool(readiness.get("trading_gate_open"))
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
        scan_symbol_set = set(snapshot.get("scan_symbols") or [])
        subscription_symbol_set = set(snapshot.get("subscription_symbols") or [])
        trade_symbol_set = set(snapshot.get("trade_symbols") or [])
        monitor_symbol_set = set(snapshot.get("monitor_symbols") or [])

        status_map = {
            str((item or {}).get("symbol") or "").upper(): dict(item or {})
            for item in (readiness.get("symbol_status") or [])
            if str((item or {}).get("symbol") or "").strip()
        }
        merged_status = []
        for symbol in snapshot.get("symbols") or []:
            if symbol in trade_symbol_set:
                role = "trade"
            elif symbol in monitor_symbol_set:
                role = "monitor"
            elif symbol in scan_symbol_set:
                role = "scan"
            elif symbol in subscription_symbol_set:
                role = "subscription"
            else:
                role = "data"
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
        data_ready = bool(snapshot.get("symbols")) and not pending_set
        readiness["phase"] = "ready" if data_ready else "degraded"
        readiness["data_ready"] = data_ready
        readiness["trade_allowed"] = trading_gate_open
        readiness["ready_symbols"] = len(ready_set)
        readiness["ready_scan_symbols"] = len([symbol for symbol in snapshot.get("scan_symbols") or [] if symbol in ready_set])
        readiness["ready_subscription_symbols"] = len([symbol for symbol in snapshot.get("subscription_symbols") or [] if symbol in ready_set])
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

