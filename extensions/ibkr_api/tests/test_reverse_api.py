import sys
import unittest
from pathlib import Path

SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
COMPUTE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
for src_root in (SERVICE_SRC_ROOT, COMPUTE_SRC_ROOT):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.reverse_actions import build_reverse_ack_response, build_reverse_dispatch_response
from ibkr_api.reverse_common import normalize_reverse_record
from ibkr_api.reverse_queries import build_reverse_list_response, build_reverse_pending_response


class _FakeRecord:
    def __init__(self, data):
        self.data = dict(data)

    def get(self, field_name):
        return self.data.get(field_name)

    def as_dict(self):
        return dict(self.data)


class _FakePB:
    def __init__(self, records):
        self.records = {
            "ibkr_reverse_signals": [_FakeRecord(record) for record in records],
        }
        self.get_records_calls = []
        self.get_first_record_calls = []
        self.update_calls = []

    def _coerce(self, record):
        return record if isinstance(record, _FakeRecord) else _FakeRecord(record)

    def _iter_records(self, collection):
        return list(self.records.get(collection, []))

    def _parse_literal(self, token):
        text = token.strip()
        if text.startswith('"') and text.endswith('"'):
            return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if "." in text:
            return float(text)
        return int(text)

    def _matches_filter(self, record, filter_expr):
        if not filter_expr:
            return True
        for part in [item.strip() for item in filter_expr.split("&&") if item.strip()]:
            if ">=" in part:
                field, value = [item.strip() for item in part.split(">=", 1)]
                if float(record.get(field) or 0) < float(self._parse_literal(value)):
                    return False
                continue
            if "<=" in part:
                field, value = [item.strip() for item in part.split("<=", 1)]
                if float(record.get(field) or 0) > float(self._parse_literal(value)):
                    return False
                continue
            if "<" in part:
                field, value = [item.strip() for item in part.split("<", 1)]
                if float(record.get(field) or 0) >= float(self._parse_literal(value)):
                    return False
                continue
            field, value = [item.strip() for item in part.split("=", 1)]
            if str(record.get(field) or "") != str(self._parse_literal(value)):
                return False
        return True

    def _sort_records(self, records, sort_expr):
        items = list(records)
        if not sort_expr:
            return items
        for key in reversed([item.strip() for item in sort_expr.split(",") if item.strip()]):
            reverse = key.startswith("-")
            field = key[1:] if reverse else key
            items.sort(key=lambda record: record.get(field) or "", reverse=reverse)
        return items

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        self.get_records_calls.append(
            {
                "collection": collection,
                "filter": filter,
                "sort": sort,
                "per_page": per_page,
                "page": page,
            }
        )
        records = [record for record in self._iter_records(collection) if self._matches_filter(record, filter)]
        records = self._sort_records(records, sort)
        start = max(0, (int(page) - 1) * int(per_page))
        end = start + int(per_page)
        return records[start:end]

    def get_first_record(self, collection, filter=None, sort=None):
        self.get_first_record_calls.append(
            {
                "collection": collection,
                "filter": filter,
                "sort": sort,
            }
        )
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return rows[0] if rows else None

    def update_record(self, collection, record_id, data):
        self.update_calls.append(
            {
                "collection": collection,
                "record_id": record_id,
                "data": dict(data),
            }
        )
        for index, record in enumerate(self._iter_records(collection)):
            if str(record.get("id")) != str(record_id):
                continue
            merged = record.as_dict()
            merged.update(data)
            updated = _FakeRecord(merged)
            self.records[collection][index] = updated
            return updated
        raise KeyError(record_id)


def _reverse_record(
    record_id,
    *,
    symbol="AAPL",
    status="pending",
    priority=5,
    environment="live",
    bar_time_ms=0,
    created="2026-04-22T12:00:00Z",
    extra=None,
    source="tradingview",
):
    payload = {
        "id": record_id,
        "symbol": symbol,
        "direction": "long",
        "strength": "strong",
        "score": 8,
        "action_type": "close",
        "status": status,
        "reason": "",
        "processed_time": "",
        "bar_time_ms": bar_time_ms,
        "us_time": "2026-04-22 08:00:00",
        "cn_time": "2026-04-22 20:00:00",
        "source": source,
        "priority": priority,
        "environment": environment,
        "created": created,
        "updated": created,
        "extra": {
            "environment": environment,
            "signal_id": "sig-origin",
            "origin_signal_id": "sig-trigger",
            "trade_group_id": "grp-1",
            "entry_order_unique_id": "entry-1",
            "order_unique_id": "entry-1",
            "broker_order_id": "1001",
            "order_status": "Filled",
            "relation_status": "active",
            "position_side": "long",
            "current_direction": "long",
            "new_direction": "short",
            "entry_price": 100,
            "quantity": 10,
            "take_profit": 110,
            "stop_loss": 95,
            "manual_requested": False,
            "triggered_signals": ["crsi", "vwap"],
        },
    }
    payload["extra"].update(extra or {})
    return payload


class ReverseQueryTests(unittest.TestCase):
    def test_reverse_list_filters_by_date_symbol_and_status(self):
        target_day_ms = 1776744000000
        next_day_ms = 1776830400000
        pb = _FakePB(
            [
                _reverse_record("rev-1", bar_time_ms=target_day_ms + 1000),
                _reverse_record("rev-2", status="cancelled", bar_time_ms=target_day_ms + 2000),
                _reverse_record("rev-3", symbol="MSFT", bar_time_ms=target_day_ms + 3000),
                _reverse_record("rev-4", environment="paper", bar_time_ms=target_day_ms + 4000),
                _reverse_record("rev-5", bar_time_ms=next_day_ms),
            ]
        )

        payload, status_code = build_reverse_list_response(
            pb,
            environment="live",
            date_str="2026-04-21",
            symbol="aapl",
            statuses="pending",
            limit=50,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual([item["id"] for item in payload["ibkr_signals"]], ["rev-1"])
        self.assertEqual(len(pb.get_records_calls), 1)
        self.assertIn('environment = "live"', pb.get_records_calls[0]["filter"])
        self.assertIn("bar_time_ms >=", pb.get_records_calls[0]["filter"])
        self.assertIn("bar_time_ms <", pb.get_records_calls[0]["filter"])

    def test_reverse_list_uses_et_market_date_bounds(self):
        pb = _FakePB(
            [
                _reverse_record("rev-1", bar_time_ms=1776816000000),
            ]
        )

        payload, status_code = build_reverse_list_response(
            pb,
            environment="live",
            date_str="2026-04-21",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual([item["id"] for item in payload["ibkr_signals"]], ["rev-1"])

    def test_reverse_list_returns_empty_for_valid_date_without_rows(self):
        pb = _FakePB(
            [
                _reverse_record("rev-1", bar_time_ms=1776902400000),
            ]
        )

        payload, status_code = build_reverse_list_response(
            pb,
            environment="live",
            date_str="2026-04-21",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["ibkr_signals"], [])
        self.assertEqual(len(pb.get_records_calls), 1)
        self.assertIn("bar_time_ms >=", pb.get_records_calls[0]["filter"])

    def test_reverse_list_falls_back_when_date_is_missing(self):
        pb = _FakePB(
            [
                _reverse_record("rev-1", bar_time_ms=1776902400000),
            ]
        )

        payload, status_code = build_reverse_list_response(
            pb,
            environment="live",
            date_str="",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual([item["id"] for item in payload["ibkr_signals"]], ["rev-1"])
        self.assertEqual(len(pb.get_records_calls), 1)
        self.assertEqual(pb.get_records_calls[0]["filter"], 'environment = "live" && source = "tradingview"')

    def test_reverse_list_falls_back_when_date_is_invalid(self):
        pb = _FakePB(
            [
                _reverse_record("rev-1", bar_time_ms=1776902400000),
            ]
        )

        payload, status_code = build_reverse_list_response(
            pb,
            environment="live",
            date_str="not-a-date",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual([item["id"] for item in payload["ibkr_signals"]], ["rev-1"])
        self.assertEqual(len(pb.get_records_calls), 1)
        self.assertEqual(pb.get_records_calls[0]["filter"], 'environment = "live" && source = "tradingview"')

    def test_reverse_pending_returns_only_pending_rows(self):
        pb = _FakePB(
            [
                _reverse_record("rev-1", priority=2, bar_time_ms=200),
                _reverse_record("rev-2", priority=9, bar_time_ms=100),
                _reverse_record("rev-3", status="confirmed", priority=10, bar_time_ms=300),
            ]
        )

        payload, status_code = build_reverse_pending_response(pb, environment="live")

        self.assertEqual(status_code, 200)
        self.assertEqual([item["id"] for item in payload["ibkr_signals"]], ["rev-2", "rev-1"])
        self.assertEqual(pb.get_records_calls[0]["sort"], "-priority,-bar_time_ms")


class ReverseActionTests(unittest.TestCase):
    def test_reverse_dispatch_execute_marks_manual_request(self):
        pb = _FakePB([_reverse_record("rev-1", priority=5)])
        notifications = []

        payload, status_code = build_reverse_dispatch_response(
            pb,
            payload={"reverse_id": "rev-1", "action": "execute", "reason": "rush"},
            clock=lambda: "2026-04-22T12:00:00Z",
            notify_status=lambda event, record, context: notifications.append((event, normalize_reverse_record(record), context)),
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["signal"]["manual_requested"])
        self.assertEqual(payload["signal"]["manual_requested_at"], "2026-04-22T12:00:00Z")
        self.assertEqual(payload["signal"]["priority"], 1)
        self.assertEqual(payload["signal"]["reason"], "rush")
        self.assertEqual(pb.update_calls[0]["record_id"], "rev-1")
        self.assertEqual(notifications[0][0], "execute_request")
        self.assertEqual(notifications[0][2]["message"], "rush")

    def test_reverse_dispatch_cancel_marks_cancelled(self):
        pb = _FakePB([_reverse_record("rev-1")])
        notifications = []

        payload, status_code = build_reverse_dispatch_response(
            pb,
            payload={"reverse_id": "rev-1", "action": "cancel"},
            clock=lambda: "2026-04-22T12:00:00Z",
            notify_status=lambda event, record, context: notifications.append((event, normalize_reverse_record(record), context)),
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["signal"]["status"], "cancelled")
        self.assertEqual(payload["signal"]["reason"], "页面取消执行动作")
        self.assertEqual(payload["signal"]["result_status"], "cancelled_by_page")
        self.assertEqual(notifications[0][0], "cancel")
        self.assertEqual(notifications[0][2]["message"], "页面已取消该执行动作")


    def test_reverse_dispatch_rejects_non_tv_execution_action(self):
        pb = _FakePB([_reverse_record("rev-legacy", source="indicator")])

        payload, status_code = build_reverse_dispatch_response(
            pb,
            payload={"reverse_id": "rev-legacy", "action": "execute"},
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["reason"], "non_tv_action_disabled")
        self.assertEqual(pb.update_calls, [])

    def test_reverse_dispatch_skips_non_pending_records(self):
        pb = _FakePB([_reverse_record("rev-1", status="confirmed")])

        payload, status_code = build_reverse_dispatch_response(
            pb,
            payload={"reverse_id": "rev-1", "action": "execute"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["signal"]["status"], "confirmed")
        self.assertEqual(pb.update_calls, [])

    def test_reverse_ack_merges_execution_result_fields(self):
        pb = _FakePB(
            [
                _reverse_record(
                    "rev-1",
                    extra={
                        "manual_requested": True,
                        "manual_requested_at": "2026-04-22T11:50:00Z",
                        "signal_id": "sig-base",
                    },
                )
            ]
        )
        notifications = []

        payload, status_code = build_reverse_ack_response(
            pb,
            payload={
                "reverse_id": "rev-1",
                "status": "confirmed",
                "reason": "filled",
                "broker_order_id": "2002",
                "order_unique_id": "entry-2",
                "entry_order_unique_id": "entry-2",
                "trade_group_id": "grp-2",
                "origin_signal_id": "sig-ack",
                "current_direction": "long",
                "new_direction": "short",
                "executed_action": "close",
                "result_status": "filled",
                "entry_price": "123.4",
                "quantity": "50",
                "take_profit": "130",
                "stop_loss": "120",
                "target_state": "filled_position",
                "order_status": "Filled",
                "relation_status": "closed",
                "position_side": "flat",
            },
            clock=lambda: "2026-04-22T12:10:00Z",
            notify_status=lambda event, record, context: notifications.append((event, normalize_reverse_record(record), context)),
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["signal"]["status"], "confirmed")
        self.assertEqual(payload["signal"]["reason"], "filled")
        self.assertEqual(payload["signal"]["order_id"], "2002")
        self.assertEqual(payload["signal"]["signal_id"], "sig-ack")
        self.assertEqual(payload["signal"]["manual_requested"], False)
        self.assertEqual(payload["signal"]["manual_requested_at"], "2026-04-22T11:50:00Z")
        self.assertEqual(payload["signal"]["entry_price"], 123.4)
        self.assertEqual(payload["signal"]["quantity"], 50.0)
        self.assertEqual(payload["signal"]["take_profit"], 130.0)
        self.assertEqual(payload["signal"]["stop_loss"], 120.0)
        self.assertEqual(payload["signal"]["target_state"], "filled_position")
        self.assertEqual(notifications[0][0], "ack")
        self.assertEqual(notifications[0][2]["message"], "filled")

    def test_reverse_ack_accepts_signal_id_as_lookup_id(self):
        pb = _FakePB([_reverse_record("rev-1")])

        payload, status_code = build_reverse_ack_response(
            pb,
            payload={"signal_id": "rev-1", "status": "failed"},
            clock=lambda: "2026-04-22T12:10:00Z",
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["signal"]["status"], "failed")
        self.assertEqual(pb.get_first_record_calls[0]["filter"], 'id = "rev-1"')


if __name__ == "__main__":
    unittest.main()
