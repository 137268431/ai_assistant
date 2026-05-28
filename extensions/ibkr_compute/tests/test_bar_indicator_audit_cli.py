import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "ops" / "validate" / "run_bar_indicator_audit.py"
SPEC = importlib.util.spec_from_file_location("run_bar_indicator_audit", SCRIPT_PATH)
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, path, payload, *, timeout):
        self.calls.append({"path": path, "payload": payload, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def config(**overrides):
    base = {
        "base_url": "https://quant.example",
        "environment": "live",
        "interval": "5m",
        "market_date": "2026-05-27",
        "symbols": ("AAPL", "MSFT"),
        "persist_truth": False,
        "chunk_size": 2,
        "scan_scope": "test_audit",
        "skip_indicator_compare": False,
        "compare_indicators_on_bar_fail": False,
        "include_signals": True,
        "timeout_seconds": 5.0,
    }
    base.update(overrides)
    return audit.AuditConfig(**base)


def truth_response(*, ok=True, rows=None):
    rows = rows or [
        {
            "symbol": "AAPL",
            "status": "ok",
            "sampled_bar_count": 78,
            "matched_bar_count": 78,
            "missing_stored_bar_count": 0,
            "missing_ibkr_bar_count": 0,
            "bar_mismatch_count": 0,
            "window_start_ms": 100,
            "window_end_ms": 200,
        },
        {
            "symbol": "MSFT",
            "status": "ok",
            "sampled_bar_count": 78,
            "matched_bar_count": 78,
            "missing_stored_bar_count": 0,
            "missing_ibkr_bar_count": 0,
            "bar_mismatch_count": 0,
            "window_start_ms": 100,
            "window_end_ms": 200,
        },
    ]
    return {
        "ok": ok,
        "rows": rows,
        "summary": {
            "coverage_complete": True,
            "audited_symbols_total": len(rows),
            "window_start_ms": 100,
            "window_end_ms": 200,
            "status_counts": {"ok": len(rows), "error": 0, "unavailable": 0},
            "error_count": 0,
        },
        "errors": [],
        "_http_status": 200,
    }


def compare_response(symbol, *, indicator_mismatch=0, signal_mismatch=0, indicator_status="match"):
    return {
        "ok": True,
        "comparison": {
            "summary": {
                "stored_visible_bars": 78,
                "ibkr_visible_bars": 78,
                "matched_bar_count": 78,
                "missing_stored_bar_count": 0,
                "missing_ibkr_bar_count": 0,
                "bar_mismatch_count": 0,
                "indicator_mismatch_count": indicator_mismatch,
                "signal_mismatch_count": signal_mismatch,
            },
            "timeline": [
                {
                    "bar_time_ms": 100,
                    "status": {
                        "bar": "match",
                        "indicator": indicator_status,
                        "signal": "absent",
                    },
                }
            ],
            "mismatch_examples": [],
        },
        "meta": {"symbol": symbol},
        "_http_status": 200,
    }


class BarIndicatorAuditCliTests(unittest.TestCase):
    def test_build_url_normalizes_base_and_path(self):
        self.assertEqual(
            audit.build_url("https://quant.example/", "api/custom/ibkr/proxy"),
            "https://quant.example/api/custom/ibkr/proxy",
        )

    def test_split_symbols_dedupes_and_uppercases(self):
        self.assertEqual(audit.split_symbols("aapl, MSFT; aapl\nnvda"), ["AAPL", "MSFT", "NVDA"])

    def test_latest_complete_market_date_uses_previous_day_before_et_close(self):
        now = datetime(2026, 5, 27, 20, 30, tzinfo=timezone.utc)  # 16:30 ET
        self.assertEqual(audit.latest_complete_market_date(now), "2026-05-26")

    def test_build_payloads_match_split_stack_proxy_contract(self):
        cfg = config(symbols=("AAPL",), persist_truth=False)
        self.assertEqual(
            audit.build_truth_audit_payload(cfg),
            {
                "environment": "live",
                "data_environment": "live",
                "market_data_mode": "live",
                "symbols": ["AAPL"],
                "market_date": "2026-05-27",
                "persist": False,
                "chunk_size": 2,
                "scan_scope": "test_audit",
            },
        )
        self.assertEqual(
            audit.build_chart_compare_proxy_payload(cfg, symbol="aapl", start_ms=100, end_ms=200),
            {
                "action": "chart/compare",
                "environment": "live",
                "data_environment": "live",
                "market_data_mode": "live",
                "symbol": "AAPL",
                "interval": "5m",
                "start_ms": 100,
                "end_ms": 200,
                "include_signals": True,
            },
        )

    def test_run_audit_stops_indicator_compare_when_bar_truth_fails(self):
        bad_truth = truth_response(
            rows=[
                {
                    "symbol": "AAPL",
                    "status": "error",
                    "sampled_bar_count": 77,
                    "matched_bar_count": 77,
                    "missing_stored_bar_count": 1,
                    "missing_ibkr_bar_count": 0,
                    "bar_mismatch_count": 0,
                    "window_start_ms": 100,
                    "window_end_ms": 200,
                },
                {
                    "symbol": "MSFT",
                    "status": "ok",
                    "sampled_bar_count": 78,
                    "matched_bar_count": 78,
                    "missing_stored_bar_count": 0,
                    "missing_ibkr_bar_count": 0,
                    "bar_mismatch_count": 0,
                    "window_start_ms": 100,
                    "window_end_ms": 200,
                },
            ]
        )
        bad_truth["summary"]["status_counts"] = {"ok": 1, "error": 1, "unavailable": 0}
        client = FakeClient([bad_truth])

        result = audit.run_audit(config(), client=client)

        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"], "bar_truth_failed")
        self.assertEqual(result["indicator_compare"]["skipped_reason"], "bar_truth_failed")
        self.assertEqual([call["path"] for call in client.calls], ["/api/custom/ibkr/data_quality/truth_audit"])

    def test_run_audit_compares_indicators_after_clean_bar_truth(self):
        client = FakeClient([truth_response(), compare_response("AAPL"), compare_response("MSFT")])

        result = audit.run_audit(config(), client=client)

        self.assertTrue(result["ok"])
        self.assertEqual(result["verdict"], "bar_truth_ok_indicator_ok")
        self.assertEqual(
            [call["path"] for call in client.calls],
            [
                "/api/custom/ibkr/data_quality/truth_audit",
                "/api/custom/ibkr/proxy",
                "/api/custom/ibkr/proxy",
            ],
        )
        self.assertEqual(client.calls[1]["payload"]["symbol"], "AAPL")
        self.assertEqual(client.calls[2]["payload"]["symbol"], "MSFT")

    def test_run_audit_reports_indicator_mismatch_after_clean_bars(self):
        client = FakeClient([truth_response(), compare_response("AAPL", indicator_mismatch=2), compare_response("MSFT")])

        result = audit.run_audit(config(), client=client)

        self.assertFalse(result["ok"])
        self.assertEqual(result["verdict"], "indicator_compare_failed")
        self.assertEqual(result["indicator_compare"]["summary"]["indicator_mismatch_symbols"], ["AAPL"])

    def test_run_audit_reports_missing_indicator_status_as_indicator_mismatch(self):
        client = FakeClient([truth_response(), compare_response("AAPL", indicator_status="missing_ibkr"), compare_response("MSFT")])

        result = audit.run_audit(config(), client=client)

        self.assertFalse(result["ok"])
        self.assertEqual(result["indicator_compare"]["summary"]["indicator_mismatch_symbols"], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
