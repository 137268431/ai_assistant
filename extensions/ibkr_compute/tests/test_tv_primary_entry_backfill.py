import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
API_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
for path in (SRC_ROOT, API_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ibkr_api.tradingview.tv_primary import _route_entry  # noqa: E402


def _escape(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _config_value(key, default, environment):
    del environment
    values = {
        "tv_primary_trade_universe_symbols": "WPM",
        "tv_entry_requires_authorized_symbol": "TRUE",
        "tv_entry_window_enforce_enabled": "TRUE",
    }
    return values.get(key, default)


def _entry_payload(symbol="WPM"):
    return {
        "symbol": symbol,
        "exchange": "NYSE",
        "date": "2026-06-01",
        "us_time": "2026-06-01 10:00:00",
        "cn_time": "2026-06-01 22:00:00",
        "bar_time_ms": 1_780_000_000_000,
        "direction": "long",
        "position_side": "long",
        "signal_id": f"{symbol}_entry_1",
        "setup": "mr_sdLower",
        "reason": "SD下轨回归多",
        "entry": 130.0,
        "stop_loss": 128.0,
        "take_profit": 134.0,
        "shares": 10,
        "activity_score": 92,
        "quality_score": 92,
        "script_tag": "Signal_Strategy_Core[Glory]",
    }


class DummyPocketBase:
    def __init__(self, targets=None):
        self.targets = [dict(row) for row in (targets or [])]
        self.next_id = len(self.targets) + 1

    def _match_target(self, filter_expr):
        for row in self.targets:
            if all(token in filter_expr for token in (
                f'symbol = "{row.get("symbol")}"',
                f'date = "{row.get("date")}"',
                f'environment = "{row.get("environment")}"',
            )):
                return row
        return None

    def get_first_record(self, collection, filter=""):
        if collection != "ibkr_targets":
            return None
        row = self._match_target(filter)
        return dict(row) if row else None

    def create_record(self, collection, data):
        if collection != "ibkr_targets":
            raise AssertionError(f"unexpected create collection: {collection}")
        record = {**dict(data), "id": f"target-{self.next_id}"}
        self.next_id += 1
        self.targets.append(record)
        return dict(record)

    def update_record(self, collection, record_id, data):
        if collection != "ibkr_targets":
            raise AssertionError(f"unexpected update collection: {collection}")
        for index, row in enumerate(self.targets):
            if str(row.get("id")) == str(record_id):
                self.targets[index] = {**row, **dict(data), "id": row.get("id")}
                return dict(self.targets[index])
        raise AssertionError(f"target not found: {record_id}")


def _route_with_dummy_signal(pb, payload):
    captured = {}

    def build_signal_ingest_response(_pb, *, payload, **kwargs):
        del _pb, kwargs
        captured["payload"] = payload
        return {"ok": True, "target": "ibkr_signals", "id": "signal-1", "signal_id": payload["signal_id"]}, 200

    result, status = _route_entry(
        pb,
        payload,
        event_id=payload["signal_id"],
        event_type="entry",
        environment="live",
        broker_mode="live",
        config_value=_config_value,
        escape_filter=_escape,
        build_signal_ingest_response=build_signal_ingest_response,
        normalize_environment=lambda value, default: value or default,
        send_interactive=None,
        update_interactive=None,
        signal_chat_id_fn=None,
        console_base_url="",
    )
    return result, status, captured["payload"]


class TvPrimaryEntryBackfillTests(unittest.TestCase):
    def test_authorized_entry_creates_active_target_when_pre_alert_was_missing(self):
        pb = DummyPocketBase()

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(pb.targets))
        target = pb.targets[0]
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertEqual(target["extra"]["source"], "tradingview")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_backfill_reason"], "missing_pre_alert_or_candidate")
        self.assertEqual(target["extra"]["target_admission_reason"], "entry_signal_backfill")
        self.assertEqual(signal_payload["extra"]["target_id"], target["id"])
        self.assertTrue(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "created")

    def test_authorized_entry_upgrades_existing_candidate_target_to_active(self):
        pb = DummyPocketBase([
            {
                "id": "target-existing",
                "symbol": "WPM",
                "exchange": "NYSE",
                "date": "2026-06-01",
                "environment": "live",
                "direction_bias": "neutral",
                "score": 40,
                "status": "candidate",
                "extra": {"source": "tradingview", "event_type": "pre_alert"},
            }
        ])

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(pb.targets))
        target = pb.targets[0]
        self.assertEqual(target["id"], "target-existing")
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_backfill_reason"], "candidate_entry_upgrade")
        self.assertEqual(target["extra"]["entry_signal_id"], "WPM_entry_1")
        self.assertEqual(signal_payload["extra"]["target_id"], "target-existing")
        self.assertTrue(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "updated")

    def test_authorized_entry_adds_activation_metadata_to_legacy_active_target(self):
        pb = DummyPocketBase([
            {
                "id": "target-active-prealert",
                "symbol": "WPM",
                "exchange": "NYSE",
                "date": "2026-06-01",
                "environment": "live",
                "direction_bias": "neutral",
                "score": 40,
                "status": "active",
                "extra": {"source": "tradingview", "event_type": "pre_alert"},
            }
        ])

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        target = pb.targets[0]
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_backfill_reason"], "active_entry_upgrade")
        self.assertEqual(target["extra"]["entry_signal_id"], "WPM_entry_1")
        self.assertTrue(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "updated")

    def test_authorized_entry_does_not_reactivate_removed_target(self):
        pb = DummyPocketBase([
            {
                "id": "target-removed",
                "symbol": "WPM",
                "exchange": "NYSE",
                "date": "2026-06-01",
                "environment": "live",
                "direction_bias": "long",
                "score": 40,
                "status": "removed",
                "extra": {"source": "tradingview", "event_type": "pre_alert"},
            }
        ])

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual("removed", pb.targets[0]["status"])
        self.assertFalse(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "skipped")
        self.assertEqual(signal_payload["extra"]["target_backfill"]["reason"], "target_status_not_backfilled")


if __name__ == "__main__":
    unittest.main()
