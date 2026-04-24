import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.orchestration.warmup_cycle import TradingServiceWarmupCycleMixin


class DummyWarmupCycle(TradingServiceWarmupCycleMixin):
    def __init__(self):
        self.complete_calls = []

    def _now_iso(self) -> str:
        return "2026-04-15T19:02:32Z"

    def _format_symbol_list(self, symbols) -> str:
        values = [str(symbol or "").strip().upper() for symbol in (symbols or []) if str(symbol or "").strip()]
        return ",".join(values) if values else "-"

    def _complete_startup_success(self, title: str, detail: dict | None = None) -> bool:
        self.complete_calls.append((title, dict(detail or {})))
        return True

class WarmupCycleStartupReleaseTest(unittest.TestCase):
    def test_release_startup_when_trade_gate_is_open_but_pending_symbols_remain(self):
        cycle = DummyWarmupCycle()
        snapshot = {
            "symbols": ["AAPL", "VIX"],
            "symbols_total": 2,
            "trade_symbols_total": 1,
            "monitor_symbols_total": 1,
        }
        readiness = {
            "ready_symbols": 1,
            "ready_trade_symbols": 1,
            "ready_monitor_symbols": 0,
            "pending_symbols": ["VIX"],
            "integrity_pending_symbols": ["VIX"],
        }
        preflight_result = {
            "attempted_repair_symbols": ["AAPL", "VIX"],
        }
        warmup_timings = {
            "total_elapsed_s": 443.237,
        }

        released = cycle._release_startup_after_trade_gate(
            snapshot,
            readiness,
            preflight_result,
            backfill_written=622,
            started_at="2026-04-15T18:55:09Z",
            warmup_timings=warmup_timings,
        )

        self.assertTrue(released)
        self.assertEqual(len(cycle.complete_calls), 1)
        title, detail = cycle.complete_calls[0]
        self.assertEqual(title, "IBKR Runtime 启动完成（后台继续预热）")
        self.assertEqual(detail["交易门"], "open")
        self.assertEqual(detail["待完成标的"], "VIX")
        self.assertEqual(detail["完整性阻塞"], "VIX")
        self.assertEqual(detail["预热完成"], "2026-04-15T19:02:32Z")
        self.assertEqual(detail["预热耗时"], "443.237s")
        self.assertEqual(
            detail["后续动作"],
            "交易链路已开放，剩余 monitor / integrity repair 在后台继续。",
        )

    def test_local_warmup_materialization_seeds_latest_indicator(self):
        cycle = DummyWarmupCycle()
        fake_server = types.ModuleType("ibkr_compute.api.server")
        call_log = []

        def materialize_engines_from_storage(
            environment,
            symbols,
            interval,
            hydrate_signal_state=True,
            persist_latest_indicator=False,
        ):
            call_log.append(
                (
                    environment,
                    tuple(symbols),
                    interval,
                    hydrate_signal_state,
                    persist_latest_indicator,
                )
            )
            return {"AAPL": {"is_ready": True, "indicator_seeded": True}}

        fake_server.materialize_engines_from_storage = materialize_engines_from_storage

        with mock.patch.dict(sys.modules, {"ibkr_compute.api.server": fake_server}):
            with mock.patch(
                "ibkr_compute.api.service_topology.uses_remote_compute_service",
                return_value=False,
            ):
                with mock.patch(
                    "ibkr_compute.orchestration.warmup_cycle._service_mod",
                    return_value=SimpleNamespace(
                        ENVIRONMENT="live",
                        DEFAULT_WARMUP_REQUIRED_INTERVAL="5m",
                    ),
                ):
                    result = cycle._materialize_warmup_compute_symbols(
                        ["AAPL"],
                        hydrate_signal_state=False,
                    )

        self.assertEqual(result, {"AAPL": {"is_ready": True, "indicator_seeded": True}})
        self.assertEqual(
            call_log,
            [("live", ("AAPL",), "5m", False, True)],
        )


if __name__ == "__main__":
    unittest.main()
