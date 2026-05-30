import copy
import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.system.jobs.tv_pre_alert_target_summary import (  # noqa: E402
    SUMMARY_EVENT_TYPE,
    build_tv_pre_alert_target_summary_response,
)


ET = ZoneInfo("America/New_York")


class _FakePB:
    def __init__(self):
        self.records = {
            "ibkr_targets": [],
            "system_events": [],
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

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        rows = [copy.deepcopy(row) for row in self.records.get(collection, []) if self._matches(row, filter or "")]
        rows = self._sort(rows, sort)
        start = max(0, int(page or 1) - 1) * int(per_page or 200)
        return rows[start : start + int(per_page or 200)]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=20):
        return self.get_records(collection, filter=filter, sort=sort, per_page=500 * int(max_pages or 20), page=1)

    @staticmethod
    def _matches(row, filter_expr):
        text = str(filter_expr or "")
        for field, value in re.findall(r'([A-Za-z0-9_]+)\s*=\s*"([^"]*)"', text):
            if str(row.get(field) or "") != value:
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
    return default


def _et_ms(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp() * 1000)


def _target(
    symbol,
    *,
    status="candidate",
    direction="long",
    score=80,
    first_time="2026-05-29 10:01:00",
    last_time=None,
    mtf_status="pass",
    rank=1,
    source="tradingview",
    event_type="pre_alert",
):
    first_ms = _et_ms(first_time)
    last_ms = _et_ms(last_time or first_time)
    return {
        "id": f"target-{symbol}",
        "symbol": symbol,
        "date": "2026-05-29",
        "environment": "live",
        "status": status,
        "direction_bias": direction,
        "score": score,
        "bar_time_ms": last_ms,
        "extra": {
            "source": source,
            "event_type": event_type,
            "direction_bias": direction,
            "first_tv_event_id": f"tv-{symbol}-first",
            "first_bar_time_ms": first_ms,
            "last_tv_event_id": f"tv-{symbol}-last",
            "last_bar_time_ms": last_ms,
            "mtf_last_status": mtf_status,
            "mtf_last_score": 72,
            "activity_rank": rank,
            "activity_score": score,
        },
    }


def _run_summary(pb, payload=None, *, time_us="2026-05-29 10:15:30", emitted=None):
    emitted = emitted if emitted is not None else []
    return build_tv_pre_alert_target_summary_response(
        pb,
        payload={"broker_mode": "paper", "market_data_mode": "live", **(payload or {})},
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape,
        time_strings=lambda: {"us": time_us, "cn": "2026-05-29 22:15:30", "date": "2026-05-29"},
        emit_system_event=lambda **kwargs: emitted.append(kwargs) or {"ok": True, "notified": True, "message_id": "evt-1"},
        config_value=_config_value,
    )


class TvPreAlertTargetSummaryTests(unittest.TestCase):
    def test_periodic_summary_counts_new_updated_and_emits_event(self):
        pb = _FakePB()
        emitted = []
        pb.records["ibkr_targets"].extend(
            [
                _target("AAPL", status="active", direction="long", first_time="2026-05-29 10:03:00", mtf_status="pass", rank=1),
                _target("MSFT", status="candidate", direction="short", first_time="2026-05-29 09:50:00", last_time="2026-05-29 10:10:00", mtf_status="warn", rank=2),
                _target("NVDA", status="active", direction="short", first_time="2026-05-29 10:13:00", last_time="2026-05-29 10:14:00", mtf_status="block", rank=3),
                _target("OLD", first_time="2026-05-29 09:10:00", last_time="2026-05-29 09:30:00", rank=4),
                _target("BOT", source="scanner", first_time="2026-05-29 10:06:00", rank=5),
            ]
        )

        payload, status_code = _run_summary(pb, emitted=emitted)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["skipped"])
        self.assertEqual(payload["reason"], "periodic")
        self.assertEqual(payload["new_count"], 2)
        self.assertEqual(payload["updated_count"], 1)
        self.assertEqual(payload["status_counts"]["active"], 2)
        self.assertEqual(payload["status_counts"]["candidate"], 1)
        self.assertEqual(payload["direction_counts"]["long"], 1)
        self.assertEqual(payload["direction_counts"]["short"], 2)
        self.assertEqual(payload["mtf_counts"]["pass"], 1)
        self.assertEqual(payload["mtf_counts"]["warn"], 1)
        self.assertEqual(payload["mtf_counts"]["block"], 1)
        self.assertEqual(payload["window_start_ms"], _et_ms("2026-05-29 10:00:00"))
        self.assertEqual(payload["window_end_ms"], _et_ms("2026-05-29 10:15:00"))
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["event_type"], SUMMARY_EVENT_TYPE)
        self.assertEqual(emitted[0]["environment"], "paper")
        self.assertEqual(emitted[0]["detail"]["新增入池"], 2)
        self.assertIn("AAPL", emitted[0]["detail"]["Top symbols"])
        self.assertIn("MSFT", emitted[0]["detail"]["Top symbols"])

    def test_duplicate_window_skips_without_emitting(self):
        pb = _FakePB()
        start_ms = _et_ms("2026-05-29 10:00:00")
        end_ms = _et_ms("2026-05-29 10:15:00")
        window_key = f"2026-05-29:paper:live:{start_ms}:{end_ms}"
        pb.records["system_events"].append(
            {
                "event_type": SUMMARY_EVENT_TYPE,
                "environment": "paper",
                "created": "2026-05-29 10:15:01",
                "detail": {
                    "market_date": "2026-05-29",
                    "data_environment": "live",
                    "summary_window_key": window_key,
                    "window_end_ms": end_ms,
                },
            }
        )
        pb.records["ibkr_targets"].append(_target("AAPL", first_time="2026-05-29 10:03:00"))
        emitted = []

        payload, status_code = _run_summary(pb, emitted=emitted)

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "duplicate_window")
        self.assertEqual(emitted, [])

    def test_previous_summary_end_prevents_periodic_overlap(self):
        pb = _FakePB()
        previous_end_ms = _et_ms("2026-05-29 10:05:00")
        pb.records["system_events"].append(
            {
                "event_type": SUMMARY_EVENT_TYPE,
                "environment": "paper",
                "created": "2026-05-29 10:05:05",
                "detail": {
                    "market_date": "2026-05-29",
                    "data_environment": "live",
                    "summary_window_key": f"2026-05-29:paper:live:{_et_ms('2026-05-29 09:50:00')}:{previous_end_ms}",
                    "window_end_ms": previous_end_ms,
                },
            }
        )
        pb.records["ibkr_targets"].extend(
            [
                _target("AAPL", first_time="2026-05-29 10:03:00"),
                _target("MSFT", first_time="2026-05-29 10:06:00"),
            ]
        )

        payload, status_code = _run_summary(pb)

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["skipped"])
        self.assertEqual(payload["window_start_ms"], previous_end_ms)
        self.assertEqual(payload["window_end_ms"], _et_ms("2026-05-29 10:15:00"))
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(payload["items"][0]["symbol"], "MSFT")

    def test_threshold_summary_sends_before_periodic_boundary(self):
        pb = _FakePB()
        emitted = []
        pb.records["ibkr_targets"].extend(
            [
                _target("AAPL", first_time="2026-05-29 10:01:00", rank=1),
                _target("MSFT", first_time="2026-05-29 10:04:00", rank=2),
            ]
        )

        payload, status_code = _run_summary(
            pb,
            payload={"threshold_count": 2},
            time_us="2026-05-29 10:05:30",
            emitted=emitted,
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["skipped"])
        self.assertEqual(payload["reason"], "threshold")
        self.assertTrue(payload["threshold_hit"])
        self.assertFalse(payload["periodic_due"])
        self.assertEqual(payload["window_start_ms"], _et_ms("2026-05-29 09:50:00"))
        self.assertEqual(payload["window_end_ms"], _et_ms("2026-05-29 10:05:00"))
        self.assertEqual(len(emitted), 1)

    def test_non_periodic_window_below_threshold_skips(self):
        pb = _FakePB()
        pb.records["ibkr_targets"].append(_target("AAPL", first_time="2026-05-29 10:01:00"))
        emitted = []

        payload, status_code = _run_summary(
            pb,
            payload={"threshold_count": 2},
            time_us="2026-05-29 10:05:30",
            emitted=emitted,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "waiting_for_periodic_or_threshold")
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(emitted, [])

    def test_empty_window_skips_unless_forced(self):
        pb = _FakePB()
        emitted = []
        payload, status_code = _run_summary(pb, emitted=emitted)
        self.assertEqual(status_code, 200)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "no_tv_pre_alert_targets")
        self.assertEqual(emitted, [])

        forced_payload, forced_status = _run_summary(pb, payload={"force": True}, emitted=emitted)
        self.assertEqual(forced_status, 200)
        self.assertFalse(forced_payload["skipped"])
        self.assertEqual(forced_payload["reason"], "forced")
        self.assertEqual(forced_payload["total_count"], 0)
        self.assertEqual(len(emitted), 1)

    def test_detail_json_string_duplicate_is_supported(self):
        pb = _FakePB()
        start_ms = _et_ms("2026-05-29 10:00:00")
        end_ms = _et_ms("2026-05-29 10:15:00")
        window_key = f"2026-05-29:paper:live:{start_ms}:{end_ms}"
        pb.records["system_events"].append(
            {
                "event_type": SUMMARY_EVENT_TYPE,
                "environment": "paper",
                "detail": json.dumps(
                    {
                        "market_date": "2026-05-29",
                        "data_environment": "live",
                        "summary_window_key": window_key,
                        "window_end_ms": end_ms,
                    }
                ),
            }
        )
        pb.records["ibkr_targets"].append(_target("AAPL", first_time="2026-05-29 10:03:00"))

        payload, _status_code = _run_summary(pb)

        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "duplicate_window")


if __name__ == "__main__":
    unittest.main()
