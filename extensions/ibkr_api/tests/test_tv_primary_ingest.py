import copy
import re
import sys
import unittest
from pathlib import Path


SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
COMPUTE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
for src_root in (SERVICE_SRC_ROOT, COMPUTE_SRC_ROOT):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.tradingview.tv_primary import TV_EVENT_COLLECTION, process_tv_primary_event


class _FakePB:
    def __init__(self):
        self.records = {
            TV_EVENT_COLLECTION: [],
            "ibkr_targets": [],
            "ibkr_signals": [],
            "ibkr_reverse_signals": [],
            "orders": [],
        }
        self._counters = {}

    def _next_id(self, collection):
        self._counters[collection] = self._counters.get(collection, 0) + 1
        return f"{collection}-{self._counters[collection]}"

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row.setdefault("id", self._next_id(collection))
        self.records.setdefault(collection, []).append(row)
        return copy.deepcopy(row)

    def update_record(self, collection, record_id, patch):
        for row in self.records.setdefault(collection, []):
            if str(row.get("id")) == str(record_id):
                row.update(copy.deepcopy(patch))
                return copy.deepcopy(row)
        raise KeyError(record_id)

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return copy.deepcopy(rows[0]) if rows else None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        rows = [copy.deepcopy(row) for row in self.records.get(collection, []) if self._matches(row, filter or "")]
        rows = self._sort(rows, sort)
        start = max(0, int(page or 1) - 1) * int(per_page or 200)
        return rows[start : start + int(per_page or 200)]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=20):
        return self.get_records(collection, filter=filter, sort=sort, per_page=200 * int(max_pages or 20), page=1)

    @staticmethod
    def _matches(row, filter_expr):
        text = str(filter_expr or "")
        for field, value in re.findall(r'([A-Za-z0-9_]+)\\s*=\\s*"([^"]*)"', text):
            if str(row.get(field) or "") != value:
                return False
        for field, value in re.findall(r'([A-Za-z0-9_]+)\\s*=\\s*(\\d+)', text):
            if str(row.get(field) or "0") != value:
                return False
        return True

    @staticmethod
    def _sort(rows, sort):
        if not sort:
            return rows
        for key in reversed([part.strip() for part in str(sort).split(",") if part.strip()]):
            reverse = key.startswith("-")
            field = key[1:] if reverse else key
            rows.sort(key=lambda row: row.get(field) or "", reverse=reverse)
        return rows


def _escape(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _normalize_environment(value, default):
    return str(value or default).strip().lower() or default


def _config_value(key, default, environment):
    overrides = {
        "tv_entry_requires_active_target": "TRUE",
        "tv_entry_allow_self_activate": "FALSE",
        "tv_entry_window_enforce_enabled": "TRUE",
        "tv_max_active_targets": "10",
    }
    return overrides.get(key, default)


def _build_signal_ingest_response(pb, *, payload, **_kwargs):
    row = pb.create_record(
        "ibkr_signals",
        {
            "symbol": payload["symbol"],
            "environment": payload["market_data_mode"],
            "direction": payload["direction"],
            "signal_id": payload["signal_id"],
            "entry": payload["entry"],
            "stop_loss": payload["stop_loss"],
            "take_profit": payload["take_profit"],
            "shares": payload["shares"],
            "status": "pending",
            "extra": payload["extra"],
        },
    )
    return {
        "ok": True,
        "target": "ibkr_signals",
        "id": row["id"],
        "signal_id": row["signal_id"],
        "action": "created",
        "status": "pending",
    }, 200


def _process(pb, payload):
    return process_tv_primary_event(
        pb,
        payload,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape,
        build_signal_ingest_response=_build_signal_ingest_response,
        config_value=_config_value,
    )


class TvPrimaryIngestTests(unittest.TestCase):
    def test_pre_alert_upserts_target_and_dedupes_event(self):
        pb = _FakePB()
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-1",
            "symbol": "aapl",
            "direction_bias": "long",
            "activity_score": 88,
            "quality_score": 82,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 09:36:00",
        }

        response, status = _process(pb, payload)

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["target"], "ibkr_targets")
        self.assertEqual(pb.records["ibkr_targets"][0]["status"], "active")
        self.assertEqual(pb.records["ibkr_targets"][0]["direction_bias"], "long")
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["status"], "routed")

        duplicate, duplicate_status = _process(pb, payload)
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["skipped"])
        self.assertEqual(len(pb.records[TV_EVENT_COLLECTION]), 1)

    def test_entry_routes_to_ibkr_signals_with_tv_payload_aliases(self):
        pb = _FakePB()
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-1",
                "signal_id": "tv-entry-1",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 193.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["target"], "ibkr_signals")
        saved = pb.records["ibkr_signals"][0]
        self.assertEqual(saved["environment"], "live")
        self.assertEqual(saved["entry"], 188.25)
        self.assertEqual(saved["shares"], 12)
        self.assertEqual(saved["extra"]["source"], "tradingview")
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["broker_mode"], "paper")

    def test_risk_update_routes_to_adjust_bracket_reverse_signal(self):
        pb = _FakePB()
        pb.create_record(
            "orders",
            {
                "signal_id": "tv-entry-1",
                "environment": "paper",
                "role": "stop_loss",
                "status": "Submitted",
                "broker_order_id": "sl-100",
            },
        )
        pb.create_record(
            "orders",
            {
                "signal_id": "tv-entry-1",
                "environment": "paper",
                "role": "take_profit",
                "status": "Submitted",
                "broker_order_id": "tp-100",
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "risk_update",
                "event_id": "tv-risk-1",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "new_stop_loss": 187.10,
                "new_take_profit": 194.40,
                "risk_update_reason": "breakeven_trail",
                "environment": "paper",
                "market_data_mode": "live",
                "bar_time_ms": 1770001200000,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["source"], "tradingview")
        self.assertEqual(reverse["action_type"], "adjust_bracket")
        self.assertEqual(reverse["environment"], "paper")
        self.assertEqual(reverse["extra"]["sl_order_id"], "sl-100")
        self.assertEqual(reverse["extra"]["tp_order_id"], "tp-100")
        self.assertEqual(reverse["extra"]["new_sl"], 187.10)

    def test_exit_routes_to_close_reverse_signal(self):
        pb = _FakePB()
        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "exit",
                "event_id": "tv-exit-1",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "exit_reason": "take_profit",
                "environment": "paper",
                "market_data_mode": "live",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["action_type"], "close")
        self.assertEqual(reverse["extra"]["reverse_kind"], "tv_exit")


if __name__ == "__main__":
    unittest.main()
