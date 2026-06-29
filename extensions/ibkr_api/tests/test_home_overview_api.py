import re
import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.home.dashboard import (
    _load_execution_fills_for_orders,
    _load_linked_entry_orders,
    build_home_dashboard_response,
)
from ibkr_api.home.current_metrics import publish_current_signal_metrics, summarize_current_signal_counts
from ibkr_api.home.market import build_home_market_response
from ibkr_api.app_core.route_cache import RouteSWRCache, request_cache_bypass, canonical_cache_key


REPO_ROOT = Path(__file__).resolve().parents[3]


class _CountResponse:
    def __init__(self, total):
        self.total = total

    def json(self):
        return {"totalItems": self.total}


class _HomePB:
    base_url = "http://pb.local"

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def _request(self, method, url, params=None, timeout=15):
        collection = re.search(r"/api/collections/([^/]+)/records", url).group(1)
        total = len(self._filtered(collection, (params or {}).get("filter", "")))
        self.calls.append({"type": "count", "collection": collection, "filter": (params or {}).get("filter", "")})
        return _CountResponse(total)

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.calls.append({"type": "records", "collection": collection, "filter": filter or "", "per_page": per_page, "page": page})
        rows = self._filtered(collection, filter or "")
        if sort and "-bar_time_ms" in sort:
            rows = sorted(rows, key=lambda row: (int(row.get("bar_time_ms") or 0), str(row.get("created") or "")), reverse=True)
        elif sort == "-created":
            rows = sorted(rows, key=lambda row: str(row.get("created") or ""), reverse=True)
        start = max(0, int(page - 1) * int(per_page))
        return [dict(row) for row in rows[start:start + int(per_page)]]

    def _filtered(self, collection, filter_text):
        rows = list(self.rows.get(collection, []))
        text = filter_text or ""
        return [row for row in rows if self._matches(row, text)]

    def _matches(self, row, text):
        if not text:
            return True
        env_values = re.findall(r'environment = "([^"]*)"', text)
        if env_values and str(row.get("environment", "")) not in env_values:
            return False
        symbol_values = re.findall(r'symbol = "([^"]*)"', text)
        if symbol_values and str(row.get("symbol", "")).upper() not in {item.upper() for item in symbol_values}:
            return False
        if 'symbol_role = "market_monitor"' in text and row.get("symbol_role") != "market_monitor":
            return False
        if 'interval = "1d"' in text and row.get("interval") != "1d":
            return False
        market_time_match = re.search(
            r'\(\(us_time >= "([^"]*)" && us_time <= "([^"]*)"\) \|\| \(bar_time_ms >= (\d+) && bar_time_ms < (\d+)\)\)',
            text,
        )
        if market_time_match:
            us_time = str(row.get("us_time") or "")
            us_time_matches = bool(us_time) and market_time_match.group(1) <= us_time <= market_time_match.group(2)
            bar_time_ms = int(row.get("bar_time_ms") or 0)
            bar_time_matches = int(market_time_match.group(3)) <= bar_time_ms < int(market_time_match.group(4))
            if not us_time_matches and not bar_time_matches:
                return False
        else:
            start_match = re.search(r"bar_time_ms >= (\d+)", text)
            if start_match and int(row.get("bar_time_ms") or 0) < int(start_match.group(1)):
                return False
            end_match = re.search(r"bar_time_ms < (\d+)", text)
            if end_match and int(row.get("bar_time_ms") or 0) >= int(end_match.group(1)):
                return False
        for field in ("direction", "status", "order_type", "source", "role", "order_id", "unique_id", "trade_group_id", "signal_id"):
            values = re.findall(rf'(?<![A-Za-z0-9_]){field} = "([^"]*)"', text)
            if values and str(row.get(field, "")) not in values:
                return False
        if '(position_side = "long" || direction = "long")' in text:
            if row.get("position_side") != "long" and row.get("direction") != "long":
                return False
        if '(position_side = "short" || direction = "short")' in text:
            if row.get("position_side") != "short" and row.get("direction") != "short":
                return False
        return True


def _runtime_account_request(
    *,
    positions=None,
    live_open_orders=None,
    live_order_groups=None,
    counts=None,
    status_code=200,
    error="",
    payload_extra=None,
    ok=None,
):
    def fake_request(method, base_url, path, params=None, timeout=0, **kwargs):
        position_rows = list(positions or [])
        live_rows = list(live_open_orders or [])
        long_positions = len([item for item in position_rows if float(item.get("quantity", 0) or 0) > 0])
        short_positions = len([item for item in position_rows if float(item.get("quantity", 0) or 0) < 0])
        flat_positions = len([item for item in position_rows if float(item.get("quantity", 0) or 0) == 0])
        extra_counts = (payload_extra or {}).get("counts") if isinstance((payload_extra or {}).get("counts"), dict) else {}
        resolved_counts = {
            "open_positions": len([item for item in position_rows if float(item.get("quantity", 0) or 0) != 0]),
            "long_positions": long_positions,
            "short_positions": short_positions,
            "flat_positions": flat_positions,
            "position_rows": len(position_rows),
            "open_orders": len(live_rows),
            "cancelable_orders": len([item for item in live_rows if item.get("can_cancel")]),
            "editable_orders": len([item for item in live_rows if item.get("can_modify")]),
            **(counts or {}),
            **extra_counts,
        }
        return {
            "ok": (status_code < 400) if ok is None else bool(ok),
            "status_code": status_code,
            "payload": {
                "ok": (status_code < 400) if ok is None else bool(ok),
                "environment": "paper",
                "account_id": "DU123",
                "positions": position_rows,
                "live_open_orders": live_rows,
                "live_order_groups": list(live_order_groups or []),
                "counts": resolved_counts,
                **(payload_extra or {}),
                **({"error": error} if error else {}),
            },
            "error": error,
        }

    return fake_request


def _nested_runtime_account_request(*, positions=None, live_open_orders=None, live_order_groups=None):
    base_request = _runtime_account_request(positions=positions, live_open_orders=live_open_orders, live_order_groups=live_order_groups)

    def fake_request(method, base_url, path, params=None, timeout=0, **kwargs):
        result = base_request(method, base_url, path, params=params, timeout=timeout, **kwargs)
        return {**result, "payload": {"ok": True, "payload": result["payload"]}}

    return fake_request


class HomeOverviewApiTest(unittest.TestCase):
    def test_dashboard_uses_fast_runtime_account_snapshot(self):
        calls = []

        def runtime_request(method, base_url, path, params=None, timeout=0, **kwargs):
            calls.append({"method": method, "base_url": base_url, "path": path, "params": list(params or []), "timeout": timeout})
            return _runtime_account_request(positions=[], live_open_orders=[])(method, base_url, path, params=params, timeout=timeout, **kwargs)

        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=runtime_request,
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual("/ibkr/account", calls[0]["path"])
        params = dict(calls[0]["params"])
        self.assertEqual("0", params["include_pnl"])
        self.assertEqual("1", params["orders_fast"])
        self.assertEqual("1", params["orders_open_only"])
        self.assertEqual("orders_fast", params["snapshot_profile"])

    def test_execution_fills_load_once_by_day_and_filter_locally(self):
        start_ms = 1776916800000
        rows = {
            "ibkr_execution_fills": [
                {"environment": "paper", "exec_id": f"e-{idx}", "order_id": str(idx), "trade_time_ms": start_ms + idx}
                for idx in range(30)
            ]
            + [
                {"environment": "paper", "exec_id": "old", "order_id": "1", "trade_time_ms": start_ms - 1000},
                {"environment": "paper", "exec_id": "other-order", "order_id": "999", "trade_time_ms": start_ms + 10},
            ]
        }
        pb = _HomePB(rows)

        fills = _load_execution_fills_for_orders(
            pb,
            broker_filter="paper",
            orders=[{"order_id": str(idx)} for idx in range(30)],
            start_ms=start_ms,
            end_ms=start_ms + 10_000,
        )

        fill_calls = [call for call in pb.calls if call["type"] == "records" and call["collection"] == "ibkr_execution_fills"]
        self.assertEqual(1, len(fill_calls))
        self.assertIn("trade_time_ms >=", fill_calls[0]["filter"])
        self.assertEqual(30, len(fills))
        self.assertNotIn("old", {row.get("exec_id") for row in fills})
        self.assertNotIn("other-order", {row.get("exec_id") for row in fills})

    def test_linked_entry_orders_dedupes_repeated_lookup_values(self):
        pb = _HomePB(
            {
                "orders": [
                    {
                        "id": "entry-row",
                        "environment": "paper",
                        "role": "entry",
                        "unique_id": "entry-1",
                        "trade_group_id": "tg-1",
                        "signal_id": "sig-1",
                    }
                ]
            }
        )

        entries = _load_linked_entry_orders(
            pb,
            broker_filter="paper",
            orders=[
                {
                    "id": f"exit-{idx}",
                    "environment": "paper",
                    "role": "take_profit",
                    "entry_order_unique_id": "entry-1",
                }
                for idx in range(50)
            ],
        )

        order_calls = [call for call in pb.calls if call["type"] == "records" and call["collection"] == "orders"]
        self.assertEqual(1, len(order_calls))
        self.assertEqual(["entry-1"], [row.get("unique_id") for row in entries])

    def test_current_signal_summary_excludes_terminal_statuses_by_broker_mode(self):
        rows = [
            {"direction": "long", "status": "pending"},
            {"direction": "short", "status": "awaiting_confirm", "extra": {"execution_by_mode": {"paper": {"status": "submitted"}}}},
            {"direction": "long", "status": "closed"},
            {"direction": "short", "status": "entry_missed_limit_cap"},
            {"direction": "long", "status": "pending", "note": "closed_by_manual_close"},
            {"direction": "buy", "status": "protection_incomplete"},
            {"direction": "sell", "status": "rejected"},
        ]

        counts = summarize_current_signal_counts(rows, broker_mode="paper")

        self.assertEqual(counts, {"long": 2, "short": 1})

    def test_publish_current_signal_metrics_counts_today_active_signal_directions(self):
        start_ms = 1776916800000  # 2026-04-23 00:00 ET
        pb = _HomePB(
            {
                "ibkr_signals": [
                    {"environment": "live", "direction": "long", "status": "pending", "bar_time_ms": start_ms + 1, "created": "2026-04-23 09:35:00"},
                    {
                        "environment": "live",
                        "direction": "short",
                        "status": "awaiting_confirm",
                        "extra": {"execution_by_mode": {"paper": {"status": "submitted"}}},
                        "us_time": "2026-04-23 09:40:00",
                        "bar_time_ms": 0,
                        "created": "2026-04-23 09:40:02",
                    },
                    {"environment": "live", "direction": "long", "status": "closed", "bar_time_ms": start_ms + 3, "created": "2026-04-23 09:45:00"},
                    {"environment": "live", "direction": "short", "status": "entry_missed_limit_cap", "bar_time_ms": start_ms + 4, "created": "2026-04-23 09:46:00"},
                    {"environment": "paper", "direction": "long", "status": "pending", "bar_time_ms": start_ms + 5, "created": "2026-04-23 09:47:00"},
                    {"environment": "live", "direction": "long", "status": "pending", "bar_time_ms": start_ms - 1, "created": "2026-04-22 15:55:00"},
                ]
            }
        )

        counts = publish_current_signal_metrics(
            pb,
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
        )

        self.assertEqual(counts, {"long": 1, "short": 1})
        signal_calls = [call for call in pb.calls if call["type"] == "records" and call["collection"] == "ibkr_signals"]
        self.assertTrue(signal_calls)
        self.assertIn('environment = "live"', signal_calls[0]["filter"])
        self.assertIn("bar_time_ms", signal_calls[0]["filter"])

    def test_dashboard_aligns_signal_order_and_gateway_position_counts(self):
        start_ms = 1776916800000  # 2026-04-23 00:00 ET
        rows = {
            "ibkr_signals": [
                {"environment": "live", "direction": "long", "symbol": "AAPL", "status": "pending", "bar_time_ms": start_ms + 1, "created": "2026-04-23 09:35:00"},
                {
                    "environment": "live",
                    "direction": "long",
                    "symbol": "AMD",
                    "status": "awaiting_confirm",
                    "extra": {"execution_by_mode": {"paper": {"status": "submitted"}}},
                    "us_time": "2026-04-23 09:34:00",
                    "bar_time_ms": 0,
                    "created": "2026-04-23 09:34:02",
                },
                {"environment": "live", "direction": "short", "symbol": "MSFT", "status": "rejected", "bar_time_ms": start_ms + 2, "created": "2026-04-23 09:36:00"},
                {"environment": "paper", "direction": "long", "symbol": "TSLA", "bar_time_ms": start_ms + 3, "created": "2026-04-23 09:37:00"},
            ],
            "ibkr_reverse_signals": [
                {"environment": "paper", "source": "tradingview", "symbol": "AAPL", "action_type": "close", "status": "pending", "bar_time_ms": start_ms + 4, "created": "2026-04-23 09:40:00"},
                {"environment": "paper", "source": "tradingview", "symbol": "MSFT", "action_type": "cancel", "status": "confirmed", "bar_time_ms": start_ms + 5, "created": "2026-04-23 09:41:00"},
                {"environment": "paper", "source": "tradingview", "symbol": "TSLA", "action_type": "cancel", "status": "pending", "bar_time_ms": start_ms + 6, "created": "2026-04-23 09:42:00"},
                {"environment": "paper", "source": "tradingview", "symbol": "AMD", "action_type": "adjust_bracket", "status": "pending", "bar_time_ms": start_ms + 7, "created": "2026-04-23 09:43:00"},
                {"environment": "paper", "source": "tradingview", "symbol": "META", "action_type": "legacy_flip", "status": "pending", "bar_time_ms": start_ms + 8, "created": "2026-04-23 09:44:00"},
                {"environment": "paper", "source": "indicator", "symbol": "NVDA", "action_type": "close", "status": "pending", "bar_time_ms": start_ms + 9, "created": "2026-04-23 09:45:00"},
            ],
            "orders": [
                {"environment": "paper", "symbol": "AAPL", "status": "Filled", "order_id": "1001", "broker_order_id": "1001", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "signal_id": "sig-a", "trade_group_id": "g1", "fill_price": 100, "filled_qty": 10, "commission": 1, "bar_time_ms": start_ms + 6, "created": "2026-04-23 09:45:00"},
                {"environment": "paper", "symbol": "AAPL", "status": "Filled", "order_id": "1002", "broker_order_id": "1002", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "signal_id": "sig-a", "trade_group_id": "g1", "fill_price": 100, "filled_qty": 5, "commission": 1, "bar_time_ms": start_ms + 6, "created": "2026-04-23 09:45:10"},
                {"environment": "paper", "symbol": "AAPL", "status": "Filled", "order_id": "2001", "broker_order_id": "2001", "order_type": "TakeProfit", "role": "take_profit", "direction": "long", "position_side": "long", "signal_id": "sig-a", "trade_group_id": "g1", "fill_price": 110, "filled_qty": 10, "commission": 1, "bar_time_ms": start_ms + 7, "created": "2026-04-23 10:10:00"},
                {"environment": "paper", "symbol": "MSFT", "status": "Filled", "order_id": "1003", "broker_order_id": "1003", "order_type": "Entry", "role": "entry", "direction": "short", "position_side": "short", "signal_id": "sig-b", "trade_group_id": "g2", "fill_price": 50, "filled_qty": 2, "bar_time_ms": start_ms + 8, "created": "2026-04-23 09:50:00"},
                {"environment": "paper", "symbol": "NVDA", "status": "Submitted", "order_id": "1004", "broker_order_id": "1004", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "signal_id": "sig-c", "trade_group_id": "g3", "bar_time_ms": start_ms + 8, "created": "2026-04-23 09:50:20"},
                {"environment": "paper", "symbol": "TSLA", "status": "Canceled", "order_id": "1005", "broker_order_id": "1005", "order_type": "Entry", "role": "entry", "direction": "short", "position_side": "short", "signal_id": "sig-d", "trade_group_id": "g4", "bar_time_ms": start_ms + 8, "created": "2026-04-23 09:50:40"},
                {"environment": "paper", "symbol": "ORPHAN", "status": "Submitted", "order_type": "StopLoss", "role": "stop_loss", "direction": "short", "position_side": "short", "signal_id": "orphan", "trade_group_id": "orphan", "bar_time_ms": start_ms + 9, "created": "2026-04-23 09:51:00"},
                {"environment": "paper", "symbol": "OLD", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "bar_time_ms": start_ms - 1, "created": "2026-04-22 09:50:00"},
            ],
            "ibkr_execution_fills": [
                {"environment": "paper", "exec_id": "e-1001", "order_id": "1001", "symbol": "AAPL", "side": "buy", "shares": 10, "price": 100, "commission": 1, "commission_known": True, "currency": "USD", "trade_time_ms": start_ms + 6},
                {"environment": "paper", "exec_id": "e-1002", "order_id": "1002", "symbol": "AAPL", "side": "buy", "shares": 5, "price": 100, "commission": 1, "commission_known": True, "currency": "USD", "trade_time_ms": start_ms + 6},
                {"environment": "paper", "exec_id": "e-2001", "order_id": "2001", "symbol": "AAPL", "side": "sell", "shares": 10, "price": 110, "commission": 1, "commission_known": True, "currency": "USD", "trade_time_ms": start_ms + 7},
            ],
        }
        payload, status = build_home_dashboard_response(
            _HomePB(rows),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[
                    {"symbol": "AAPL", "quantity": 100},
                    {"symbol": "MSFT", "quantity": -20},
                    {"symbol": "FLAT", "quantity": 0},
                ],
                live_open_orders=[
                    {"order_id": "1004", "client_order_id": "coid-g3", "order_type": "LMT", "can_cancel": True, "can_modify": True},
                    {"order_id": "2002", "parent_id": "1001", "client_order_id": "coid-g1-tp", "order_type": "LMT", "can_cancel": True, "can_modify": True},
                    {"order_id": "2003", "parent_id": "1001", "client_order_id": "coid-g1-sl", "order_type": "STP", "can_cancel": True, "can_modify": False},
                    {"order_id": "3001", "client_order_id": "close_msft", "role": "close", "order_type": "MKT", "can_cancel": False, "can_modify": False},
                ],
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["summary"]["signals"],
            {
                "long": 2,
                "short": 1,
                "total": 3,
                "status_counts": {
                    "awaiting_confirm": 0,
                    "pending": 1,
                    "submitted": 1,
                    "protected_active": 0,
                    "protection_incomplete": 0,
                    "executed": 0,
                    "closed": 0,
                    "expired": 0,
                    "cancelled": 0,
                    "rejected": 1,
                },
                "terminal_count": 1,
            },
        )
        expected_action_breakdown = {"close": 1, "cancel": 1, "adjust": 1, "other": 1}
        self.assertEqual(
            payload["summary"]["execution_actions"],
            {"pending": 4, "pending_by_action": expected_action_breakdown, "total": 5},
        )
        self.assertEqual(payload["summary"]["reverse_signals"]["pending"], 4)
        self.assertEqual(payload["summary"]["reverse_signals"]["pending_by_action"], expected_action_breakdown)
        self.assertEqual(
            payload["summary"]["orders"],
            {
                "long": 2,
                "short": 2,
                "total": 4,
                "entry_order_count": 5,
                "take_profit_order_count": 1,
                "stop_loss_order_count": 1,
                "close_order_count": 0,
                "status_counts": {"working": 1, "filled": 3, "cancelled": 1, "closed": 0, "other": 0},
                "group_status_counts": {"open": 1, "filled": 1, "closed": 1, "cancelled": 1, "other": 0},
            },
        )
        positions_summary = payload["summary"]["positions"]
        for key, value in {
            "long": 1,
            "short": 1,
            "total": 2,
            "available": True,
            "detail_available": True,
            "count_available": True,
            "empty_confirmed": False,
            "source": "runtime_account",
            "flat_count": 1,
            "position_rows": 3,
            "account_id": "DU123",
        }.items():
            self.assertEqual(value, positions_summary[key])
        self.assertFalse(positions_summary["positions_stale"])
        self.assertFalse(positions_summary["positions_refresh_required"])
        self.assertEqual(
            payload["summary"]["live_orders"],
            {
                "total": 4,
                "leg_total": 4,
                "total_groups": 3,
                "cancelable": 3,
                "cancelable_groups": 2,
                "editable": 2,
                "editable_groups": 2,
                "available": True,
                "detail_available": True,
                "group_detail_available": True,
                "source": "runtime_account",
                "entry": 1,
                "take_profit": 1,
                "stop_loss": 1,
                "close": 1,
                "other": 0,
            },
        )
        self.assertAlmostEqual(payload["summary"]["pnl"]["total"], 97.67)
        self.assertAlmostEqual(payload["summary"]["pnl"]["realized_gross_pnl"], 100.0)
        self.assertAlmostEqual(payload["summary"]["pnl"]["commission"], 2.33)
        self.assertAlmostEqual(payload["summary"]["pnl"]["entry_commission"], 1.33)
        self.assertAlmostEqual(payload["summary"]["pnl"]["exit_commission"], 1.0)
        self.assertEqual(payload["summary"]["pnl"]["commission_fill_count"], 3)
        self.assertAlmostEqual(payload["summary"]["pnl"]["commission_per_exit"], 2.33)
        self.assertEqual(payload["summary"]["pnl"]["source"], "gateway_execution_fills")
        self.assertEqual(payload["summary"]["pnl"]["commission_source_label"], "IBKR commissionReport")
        self.assertEqual(payload["summary"]["pnl"]["commission_environment_label"], "Paper 模拟")
        self.assertEqual(payload["summary"]["pnl"]["win_count"], 1)
        self.assertEqual(payload["recent_activity"][0]["type"], "order")

    def test_dashboard_does_not_fallback_to_historical_entries_for_positions(self):
        start_ms = 1776916800000
        rows = {
            "ibkr_signals": [],
            "ibkr_reverse_signals": [],
            "orders": [
                {"environment": "paper", "symbol": "OLD1", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "long", "position_side": "long", "bar_time_ms": start_ms - 1, "created": "2026-04-22 09:50:00"},
                {"environment": "paper", "symbol": "OLD2", "status": "Filled", "order_type": "Entry", "role": "entry", "direction": "short", "position_side": "short", "bar_time_ms": start_ms - 2, "created": "2026-04-22 09:55:00"},
            ],
        }

        payload, status = build_home_dashboard_response(
            _HomePB(rows),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(status_code=503, error="runtime offline"),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["positions"]["long"], 0)
        self.assertEqual(payload["summary"]["positions"]["short"], 0)
        self.assertEqual(payload["summary"]["positions"]["total"], 0)
        self.assertFalse(payload["summary"]["positions"]["available"])
        self.assertEqual(payload["summary"]["positions"]["error"], "runtime offline")
        self.assertFalse(payload["summary"]["live_orders"]["available"])
        self.assertEqual(payload["summary"]["live_orders"]["error"], "runtime offline")

    def test_dashboard_distinguishes_empty_positions_from_open_orders(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[
                    {"order_id": "5001", "client_order_id": "entry_empty_case", "order_type": "LMT", "can_cancel": True, "can_modify": True},
                ],
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["summary"]["positions"]["available"])
        self.assertTrue(payload["summary"]["positions"]["empty_confirmed"])
        self.assertEqual(payload["summary"]["positions"]["total"], 0)
        self.assertEqual(payload["summary"]["live_orders"]["total"], 1)
        self.assertEqual(payload["summary"]["live_orders"]["leg_total"], 1)
        self.assertEqual(payload["summary"]["live_orders"]["total_groups"], 1)
        self.assertEqual(payload["summary"]["live_orders"]["entry"], 1)

    def test_dashboard_does_not_treat_omitted_fast_positions_as_confirmed_flat(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[],
                payload_extra={
                    "positions_detail_available": False,
                    "positions_count_available": False,
                    "positions_source": "omitted_open_orders_only",
                    "orders_fast_diagnostics": {"positions_omitted": True},
                    "counts": {"open_orders": 0},
                },
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        positions = payload["summary"]["positions"]
        self.assertFalse(positions["available"])
        self.assertFalse(positions["empty_confirmed"])
        self.assertEqual("runtime_account_positions_omitted", positions["error"])

    def test_dashboard_uses_cached_position_counts_when_fast_positions_are_omitted(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[],
                payload_extra={
                    "positions_detail_available": False,
                    "positions_count_available": True,
                    "positions_source": "omitted_open_orders_only",
                    "positions_age_s": 30.0,
                    "positions_max_stale_s": 300.0,
                    "orders_fast_diagnostics": {"positions_omitted": True},
                    "counts": {
                        "open_positions": 1,
                        "long_positions": 1,
                        "short_positions": 0,
                        "flat_positions": 4,
                        "position_rows": 5,
                        "open_orders": 0,
                    },
                },
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        positions = payload["summary"]["positions"]
        self.assertTrue(positions["available"])
        self.assertFalse(positions["detail_available"])
        self.assertTrue(positions["count_available"])
        self.assertFalse(positions["empty_confirmed"])
        self.assertEqual(1, positions["long"])
        self.assertEqual(0, positions["short"])
        self.assertEqual(1, positions["total"])
        self.assertEqual(5, positions["position_rows"])

    def test_dashboard_marks_stale_position_counts_not_confirmed_flat(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[],
                payload_extra={
                    "positions_detail_available": False,
                    "positions_count_available": True,
                    "positions_source": "omitted_open_orders_only",
                    "positions_age_s": 360.0,
                    "positions_max_stale_s": 300.0,
                    "orders_fast_diagnostics": {"positions_omitted": True},
                    "counts": {
                        "open_positions": 1,
                        "long_positions": 1,
                        "short_positions": 0,
                        "flat_positions": 4,
                        "position_rows": 5,
                        "open_orders": 0,
                    },
                },
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        positions = payload["summary"]["positions"]
        self.assertTrue(positions["available"])
        self.assertTrue(positions["positions_stale"])
        self.assertTrue(positions["positions_refresh_required"])
        self.assertFalse(positions["empty_confirmed"])
        self.assertFalse(positions["count_available"])
        self.assertEqual(0, positions["total"])
        self.assertEqual(0, positions["effective_open_positions"])
        self.assertEqual(1, positions["stale_open_positions_hint"])
        self.assertEqual("positions_snapshot_stale", positions["positions_refresh_block_reason"])

    def test_dashboard_accepts_nested_runtime_account_payload(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_nested_runtime_account_request(
                positions=[{"symbol": "AAPL", "quantity": 10}],
                live_open_orders=[{"order_id": "6001", "client_order_id": "entry_nested", "order_type": "LMT", "can_cancel": True}],
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["summary"]["positions"]["available"])
        self.assertEqual(payload["summary"]["positions"]["long"], 1)
        self.assertTrue(payload["summary"]["live_orders"]["available"])
        self.assertEqual(payload["summary"]["live_orders"]["total"], 1)
        self.assertEqual(payload["summary"]["live_orders"]["total_groups"], 1)

    def test_dashboard_counts_bracket_live_orders_as_one_group(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[
                    {"order_id": "7001", "client_order_id": "entry_combo", "order_type": "LMT", "can_cancel": True, "can_modify": True},
                    {"order_id": "7002", "parent_id": "7001", "client_order_id": "tp_combo", "order_type": "LMT", "can_cancel": True, "can_modify": True},
                    {"order_id": "7003", "parent_id": "7001", "client_order_id": "sl_combo", "order_type": "STP", "can_cancel": True, "can_modify": False},
                ],
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        live_orders = payload["summary"]["live_orders"]
        self.assertEqual(live_orders["total"], 3)
        self.assertEqual(live_orders["leg_total"], 3)
        self.assertEqual(live_orders["total_groups"], 1)
        self.assertEqual(live_orders["cancelable"], 3)
        self.assertEqual(live_orders["cancelable_groups"], 1)
        self.assertEqual(live_orders["editable"], 2)
        self.assertEqual(live_orders["editable_groups"], 1)
        self.assertEqual(live_orders["entry"], 1)
        self.assertEqual(live_orders["take_profit"], 1)
        self.assertEqual(live_orders["stop_loss"], 1)

    def test_dashboard_prefers_runtime_live_order_groups(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[
                    {"order_id": "8001", "client_order_id": "unlinked_a", "order_type": "LMT", "can_cancel": True, "can_modify": False},
                    {"order_id": "8002", "client_order_id": "unlinked_b", "order_type": "STP", "can_cancel": False, "can_modify": True},
                ],
                live_order_groups=[
                    {"group_key": "runtime-group", "live_order_count": 2, "cancelable_orders": 1, "editable_orders": 1},
                ],
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        live_orders = payload["summary"]["live_orders"]
        self.assertEqual(live_orders["total"], 2)
        self.assertEqual(live_orders["leg_total"], 2)
        self.assertEqual(live_orders["total_groups"], 1)
        self.assertEqual(live_orders["cancelable_groups"], 1)
        self.assertEqual(live_orders["editable_groups"], 1)

    def test_dashboard_marks_stale_runtime_account_snapshot_available(self):
        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=_runtime_account_request(
                positions=[],
                live_open_orders=[],
                payload_extra={
                    "stale": True,
                    "cache_state": "stale_after_error",
                    "cache_age_s": 42.0,
                    "refresh_error": "account_snapshot_timeout",
                    "fetched_at": "2026-04-23T13:40:00+00:00",
                },
            ),
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        positions = payload["summary"]["positions"]
        live_orders = payload["summary"]["live_orders"]
        self.assertTrue(positions["available"])
        self.assertTrue(positions["empty_confirmed"])
        self.assertTrue(positions["stale"])
        self.assertEqual("stale_after_error", positions["cache_state"])
        self.assertEqual("account_snapshot_timeout", positions["degraded_reason"])
        self.assertTrue(live_orders["available"])
        self.assertTrue(live_orders["stale"])
        self.assertEqual(0, live_orders["total_groups"])

    def test_dashboard_reports_runtime_account_timeout_explicitly(self):
        def timeout_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {
                "ok": False,
                "status_code": 0,
                "payload": {},
                "error": "HTTPConnectionPool(host='runtime'): Read timed out.",
                "timeout_s": timeout,
            }

        payload, status = build_home_dashboard_response(
            _HomePB({"ibkr_signals": [], "ibkr_reverse_signals": [], "orders": [], "ibkr_execution_fills": []}),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            time_strings=lambda: {"date": "2026-04-23"},
            request_json_request=timeout_request,
            runtime_base_url="http://runtime.local",
        )

        self.assertEqual(status, 200)
        self.assertEqual("runtime_account_timeout", payload["summary"]["positions"]["error"])
        self.assertEqual("runtime_account_timeout", payload["summary"]["live_orders"]["error"])

    def test_market_payload_merges_config_watchlist_quotes_and_daily_fallback(self):
        rows = {
            "watchlist": [
                {"environment": "global", "symbol": "QQQ", "symbol_role": "market_monitor", "updated": "2026-04-23 08:00:00"},
            ],
            "ibkr_bars": [
                {"environment": "live", "symbol": "SPY", "interval": "1d", "close": 500, "us_time": "2026-04-22 16:00:00", "bar_time_ms": 1776902400000},
                {"environment": "live", "symbol": "QQQ", "interval": "1d", "close": 400, "us_time": "2026-04-22 16:00:00", "bar_time_ms": 1776902400000},
                {"environment": "live", "symbol": "QQQ", "interval": "1d", "close": 390, "us_time": "2026-04-21 16:00:00", "bar_time_ms": 1776816000000},
            ],
        }

        def fake_request(method, base_url, path, params=None, timeout=0, **kwargs):
            return {
                "ok": True,
                "status_code": 200,
                "elapsed_ms": 12.3,
                "payload": {
                    "items": [
                        {"symbol": "SPY", "last_price": 505, "day_change_pct": 1.0, "quote_age_s": 1.2, "last_update": "2026-04-23 10:00:00"},
                        {"symbol": "VIX", "last_price": 18.5, "quote_fallback": True, "data_age_s": 300, "us_time": "2026-04-23 09:55:00"},
                    ]
                },
            }

        payload, status = build_home_market_response(
            _HomePB(rows),
            payload={"broker_mode": "paper", "market_data_mode": "live", "market_date": "2026-04-23"},
            config_value=lambda key, default, environment: "SPY,VIX",
            request_json_request=fake_request,
            runtime_base_url="http://runtime.local",
            time_strings=lambda: {"date": "2026-04-23"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["symbols"], ["SPY", "VIX", "QQQ"])
        self.assertEqual(payload["realtime_count"], 1)
        self.assertEqual(payload["fallback_count"], 2)
        by_symbol = {item["symbol"]: item for item in payload["items"]}
        self.assertEqual(by_symbol["SPY"]["source_kind"], "quote")
        self.assertEqual(by_symbol["QQQ"]["display_price"], 400)
        self.assertEqual(by_symbol["VIX"]["data_source_text"], "BAR 5m")

    def test_home_route_cache_hits_and_bypasses(self):
        cache = RouteSWRCache("home-test")
        calls = []

        def builder():
            calls.append(len(calls) + 1)
            return {"ok": True, "value": calls[-1]}, 200

        key = canonical_cache_key("home-dashboard", {"a": "1"})
        first, _ = cache.get(key, builder=builder, ttl_seconds=15, stale_seconds=45)
        second, _ = cache.get(key, builder=builder, ttl_seconds=15, stale_seconds=45)
        bypass, _ = cache.get(
            key,
            builder=builder,
            ttl_seconds=15,
            stale_seconds=45,
            force=request_cache_bypass({"cache_bust": "1780805505054"}),
        )

        self.assertEqual(first["value"], 1)
        self.assertEqual(second["value"], 1)
        self.assertEqual(bypass["value"], 2)
        self.assertEqual(first["_cache"]["state"], "miss")
        self.assertEqual(second["_cache"]["state"], "hit")
        self.assertEqual(bypass["_cache"]["state"], "bypass")

    def test_home_static_uses_aggregated_endpoints_and_static_cache_headers(self):
        index_html = (REPO_ROOT / "runtime/ibkr_console/static/index.html").read_text(encoding="utf-8")
        caddy = (REPO_ROOT / "ops/templates/caddy/quant.lzw-glory.top.caddy").read_text(encoding="utf-8")

        self.assertIn("/api/custom/ibkr/home-dashboard", index_html)
        self.assertIn("/api/custom/ibkr/home-market", index_html)
        self.assertIn('id="todaySignalsStatusSummary"', index_html)
        self.assertIn('id="todayOrdersFoot"', index_html)
        self.assertIn('id="positionsLiveOrdersSummary"', index_html)
        self.assertIn("summary.live_orders", index_html)
        self.assertIn("当前挂单组", index_html)
        self.assertIn("订单腿", index_html)
        self.assertIn("IBKR commissionReport", index_html)
        self.assertNotIn("getFullList", index_html)
        self.assertNotIn("pocketbase.umd.min.js", index_html)
        self.assertIn('Cache-Control "no-cache"', caddy)
        self.assertIn('Cache-Control "public, max-age=300, stale-while-revalidate=86400"', caddy)


if __name__ == "__main__":
    unittest.main()
