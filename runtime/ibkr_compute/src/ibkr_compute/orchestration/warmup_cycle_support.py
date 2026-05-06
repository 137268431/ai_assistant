from __future__ import annotations


def _service_mod():
    import sys

    facade = sys.modules.get("ibkr_compute.orchestration.warmup_cycle")
    legacy = getattr(facade, "_service_mod", None) if facade is not None else None
    if callable(legacy) and legacy is not _service_mod:
        return legacy()

    from . import trading_service as service_mod

    return service_mod


class WarmupCycleSupportMixin:
    def _is_local_pb_unavailable_error(self, error) -> bool:
        text = str(error or "").strip().lower()
        if not text:
            return False
        if "8090" not in text:
            return False
        if "127.0.0.1" not in text and "localhost" not in text:
            return False
        return any(
            marker in text
            for marker in (
                "connection refused",
                "failed to establish a new connection",
                "max retries exceeded",
            )
        )

    def _release_startup_after_trade_gate(
        self,
        snapshot: dict,
        readiness: dict,
        preflight_result: dict,
        backfill_written: int,
        started_at: str,
        warmup_timings: dict,
    ) -> bool:
        finished_at = self._now_iso()
        startup_detail = {
            "Warmup结果": f"{readiness['ready_symbols']}/{snapshot['symbols_total']} ready",
            "交易标的": f"{readiness['ready_trade_symbols']}/{snapshot['trade_symbols_total']} ready",
            "监控标的": f"{readiness['ready_monitor_symbols']}/{snapshot['monitor_symbols_total']} ready",
            "预检修复标的": self._format_symbol_list(
                preflight_result.get("attempted_repair_symbols") or []
            ),
            "回补写入Bars": backfill_written,
            "预热开始": started_at,
            "预热完成": finished_at,
            "预热耗时": f"{float(warmup_timings.get('total_elapsed_s', 0.0) or 0.0):.3f}s",
            "交易门": "open",
            "后续动作": "交易链路已开放，剩余 monitor / integrity repair 在后台继续。",
            "待完成标的": self._format_symbol_list(
                readiness.get("pending_symbols") or []
            ),
            "完整性阻塞": self._format_symbol_list(
                readiness.get("integrity_pending_symbols") or []
            ),
        }
        if not self._complete_startup_success(
            "IBKR Runtime 启动完成（后台继续预热）",
            startup_detail,
        ):
            return False
        return True

