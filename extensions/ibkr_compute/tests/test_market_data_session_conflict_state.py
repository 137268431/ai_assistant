from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.support.market_data_session import (
    MARKET_DATA_SESSION_CONFLICT_STATE_KEY,
    detect_market_data_session_conflict,
    record_market_data_session_conflict_state,
)


class _FakePB:
    def __init__(self):
        self.states: dict[tuple[str, str, str], dict] = {}
        self.upserts: list[tuple[str, str, dict, str]] = []
        self.records: list[tuple[str, dict]] = []

    def get_state(self, state_key, environment, date="global"):
        data = self.states.get((state_key, environment, date))
        return {"id": "state-1", "data": dict(data)} if isinstance(data, dict) else None

    def upsert_state(self, state_key, environment, data, date="global"):
        payload = dict(data or {})
        self.states[(state_key, environment, date)] = payload
        self.upserts.append((state_key, environment, payload, date))
        return {"id": "state-1", "data": payload}

    def create_record(self, collection, payload):
        self.records.append((collection, dict(payload or {})))
        return {"id": f"{collection}-1", **dict(payload or {})}


class _FakeService:
    def __init__(self, pb):
        self.pb = pb


def _runtime_status(error_at_ms: int | None) -> dict:
    broker = {"connected": True, "ready": True}
    if error_at_ms is not None:
        broker.update(
            {
                "last_error_code": 10197,
                "last_error": "No market data during competing live session",
                "last_error_at": datetime.fromtimestamp(error_at_ms / 1000.0, timezone.utc).isoformat(),
            }
        )
    return {
        "environment": "paper",
        "data_environment": "live",
        "gateway": {"running": True, "reachable": True, "broker": broker},
        "session": {"authenticated": True},
        "websocket": {"connected": True, "ready": True, "subscribed_count": 1},
        "realtime_quotes": {
            "total_quotes": 1,
            "stale_quotes": 0,
            "quotes": {"SPY": {"symbol": "SPY", "quote_age_s": 5.0}},
        },
    }


class MarketDataSessionConflictStateTest(unittest.TestCase):
    def test_first_conflict_persists_state_and_records_event(self):
        pb = _FakePB()
        service = _FakeService(pb)
        now_ms = 1_781_535_600_000

        state = record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms - 5_000),
            environment="live",
            now_ms=now_ms,
        )

        self.assertTrue(state["active"])
        self.assertEqual(MARKET_DATA_SESSION_CONFLICT_STATE_KEY, state["code"])
        self.assertEqual(1, state["count"])
        self.assertEqual("live", state["environment"])
        self.assertIn("competing live session", state["message"])
        self.assertEqual(1, len(pb.upserts))
        self.assertEqual(1, len(pb.records))
        self.assertEqual("system_events", pb.records[0][0])
        self.assertEqual(MARKET_DATA_SESSION_CONFLICT_STATE_KEY, pb.records[0][1]["event_type"])

    def test_repeated_same_conflict_does_not_increment_within_cooldown(self):
        pb = _FakePB()
        service = _FakeService(pb)
        now_ms = 1_781_535_600_000

        first = record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms - 5_000),
            environment="live",
            now_ms=now_ms,
        )
        second = record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms - 5_000),
            environment="live",
            now_ms=now_ms + 30_000,
        )

        self.assertEqual(1, first["count"])
        self.assertEqual(1, second["count"])
        self.assertEqual(1, len(pb.upserts))
        self.assertEqual(1, len(pb.records))

    def test_new_error_timestamp_increments_count(self):
        pb = _FakePB()
        service = _FakeService(pb)
        now_ms = 1_781_535_600_000

        record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms - 5_000),
            environment="live",
            now_ms=now_ms,
        )
        state = record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms + 45_000),
            environment="live",
            now_ms=now_ms + 50_000,
        )

        self.assertEqual(2, state["count"])
        self.assertEqual(2, len(pb.upserts))

    def test_conflict_resolution_persists_resolved_state(self):
        pb = _FakePB()
        service = _FakeService(pb)
        now_ms = 1_781_535_600_000

        record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms - 5_000),
            environment="live",
            now_ms=now_ms,
        )
        state = record_market_data_session_conflict_state(
            service,
            _runtime_status(None),
            environment="live",
            now_ms=now_ms + 31 * 60 * 1000,
        )

        self.assertFalse(state["active"])
        self.assertTrue(state["resolved_at"])
        self.assertEqual(2, len(pb.records))
        self.assertEqual("IBKR market data session conflict resolved", pb.records[-1][1]["title"])
        self.assertEqual("pending_resubscribe", state["recovery_action"])
        self.assertTrue(state["recovery_evidence"]["ok"])

    def test_old_10197_evidence_leaves_trace_but_not_active(self):
        now_ms = 1_781_535_600_000

        conflict = detect_market_data_session_conflict(
            _runtime_status(now_ms - 4 * 60 * 1000),
            now_ms=now_ms,
            active_window_sec=180,
        )

        self.assertFalse(conflict["active"])
        self.assertEqual(1, len(conflict["evidence"]))
        self.assertEqual([], conflict["active_evidence"])

    def test_old_10197_waits_for_fresh_quote_before_resolving(self):
        pb = _FakePB()
        service = _FakeService(pb)
        now_ms = 1_781_535_600_000

        record_market_data_session_conflict_state(
            service,
            _runtime_status(now_ms - 5_000),
            environment="live",
            now_ms=now_ms,
        )
        stale_status = _runtime_status(now_ms - 4 * 60 * 1000)
        stale_status["realtime_quotes"] = {
            "total_quotes": 1,
            "stale_quotes": 1,
            "quotes": {"SPY": {"symbol": "SPY", "quote_age_s": 300.0}},
        }
        state = record_market_data_session_conflict_state(
            service,
            stale_status,
            environment="live",
            now_ms=now_ms + 4 * 60 * 1000,
            active_window_sec=180,
            recovery_quote_fresh_sec=120,
        )

        self.assertTrue(state["active"])
        self.assertTrue(state["recovery_pending"])
        self.assertIn("realtime_quotes_stale", state["recovery_evidence"]["blockers"])
        self.assertEqual(1, len(pb.records))


if __name__ == "__main__":
    unittest.main()
