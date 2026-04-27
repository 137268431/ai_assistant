import importlib.util
import types
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "ops" / "validate" / "run_backtest_chain_coverage.py"


def _load_wrapper_module():
    spec = importlib.util.spec_from_file_location("run_backtest_chain_coverage", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BacktestValidationWrapperTests(unittest.TestCase):
    def test_build_custom_url_uses_api_custom_backtest_route(self):
        module = _load_wrapper_module()

        url = module.build_custom_url(
            "https://quant.lzw-glory.top/",
            "/status",
            {"environment": "live", "unused": ""},
        )

        self.assertEqual(
            url,
            "https://quant.lzw-glory.top/api/custom/ibkr/backtest/status?environment=live",
        )

    def test_daily_scan_replay_preserves_manual_universe_symbols(self):
        module = _load_wrapper_module()
        attempt_cls = types.SimpleNamespace
        coverage_module = types.SimpleNamespace(
            PocketBaseClient=object,
            AttemptPlan=attempt_cls,
            split_csv=lambda raw: [item.strip().upper() for item in raw.split(",") if item.strip()],
        )
        coverage_module.build_attempt_plans = lambda args: [
            attempt_cls(
                label="attempt-1",
                symbol_source="daily_scan_replay",
                symbols=[],
                date_from="2026-03-27",
                date_to="2026-04-24",
                session_mode="extended",
                max_symbols=12,
            )
        ]

        module.install_split_stack_client(coverage_module, "https://quant.lzw-glory.top")

        plans = coverage_module.build_attempt_plans(types.SimpleNamespace(symbols="LMT,NVDA"))

        self.assertEqual(plans[0].symbol_source, "daily_scan_replay")
        self.assertEqual(plans[0].symbols, ["LMT", "NVDA"])


if __name__ == "__main__":
    unittest.main()
