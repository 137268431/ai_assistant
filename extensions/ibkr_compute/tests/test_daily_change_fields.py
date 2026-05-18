import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute.runtime_state import caches


class _FakePB:
    def __init__(self, rows=None, fail_on_read=False):
        self.rows = list(rows or [])
        self.fail_on_read = fail_on_read
        self.calls = []

    def get_all_records(self, collection, filter=None, sort=None, max_pages=None):
        self.calls.append(
            {
                "collection": collection,
                "filter": str(filter or ""),
                "sort": sort,
                "max_pages": max_pages,
            }
        )
        if self.fail_on_read:
            raise AssertionError("fallback query should not run")
        return [dict(row) for row in self.rows]


def _fake_app(pb, daily_rows):
    return SimpleNamespace(
        pb=pb,
        daily_close_cache={"live": {"PANW": [dict(row) for row in daily_rows]}},
        build_bar_environment_filter=lambda environment, include_legacy_empty=True: '(environment = "live" || environment = "")',
    )


class DailyChangeFieldsTest(unittest.TestCase):
    def test_uses_recent_regular_5m_close_when_daily_cache_is_stale(self):
        daily_rows = [
            {"bar_time_ms": 1778212800000, "date": "2026-05-08", "close": 207.00},
            {"bar_time_ms": 1778472000000, "date": "2026-05-11", "close": 213.51},
            {"bar_time_ms": 1778558400000, "date": "2026-05-12", "close": 214.46},
            {"bar_time_ms": 1778644800000, "date": "2026-05-13", "close": 227.07},
        ]
        pb = _FakePB(
            [
                {"bar_time_ms": 1778788500000, "session_type": "regular", "close": 235.00},
                {"bar_time_ms": 1778874900000, "session_type": "regular", "close": 242.84},
                {"bar_time_ms": 1778875200000, "session_type": "afterhours", "close": 241.36},
            ]
        )
        app = _fake_app(pb, daily_rows)

        with mock.patch("ibkr_compute.api.compute.runtime_state.caches._api_app", return_value=app):
            fields = caches.get_daily_change_fields("live", "PANW", 246.13, 1779115200000)

        self.assertEqual(fields["day_change_pct"], 1.35)
        self.assertEqual(fields["prev_close_change_pct"], 3.34)
        self.assertEqual(fields["change_7d"], 15.28)
        self.assertEqual(len(pb.calls), 1)
        self.assertIn('session_type = "regular"', pb.calls[0]["filter"])
        self.assertEqual(app.daily_close_cache["live"]["PANW"][-1]["date"], "2026-05-15")
        self.assertEqual(app.daily_close_cache["live"]["PANW"][-1]["close"], 242.84)

    def test_keeps_friday_daily_close_for_monday_without_fallback_query(self):
        daily_rows = [
            {"bar_time_ms": 1778644800000, "date": "2026-05-13", "close": 227.07},
            {"bar_time_ms": 1778874900000, "date": "2026-05-15", "close": 242.84},
        ]
        pb = _FakePB(fail_on_read=True)
        app = _fake_app(pb, daily_rows)

        with mock.patch("ibkr_compute.api.compute.runtime_state.caches._api_app", return_value=app):
            fields = caches.get_daily_change_fields("live", "PANW", 246.13, 1779115200000)

        self.assertEqual(fields["day_change_pct"], 1.35)
        self.assertEqual(pb.calls, [])


if __name__ == "__main__":
    unittest.main()
