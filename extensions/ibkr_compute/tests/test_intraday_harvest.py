import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.intraday_harvest import (
    ACTION_FULL_EXIT,
    ACTION_PARTIAL_EXIT,
    ACTION_REENTRY,
    split_core_tactical_quantity,
    evaluate_intraday_harvest,
    suggested_stop_price,
)


class IntradayHarvestDecisionTests(unittest.TestCase):
    def test_split_core_tactical_quantity_defaults_to_seventy_thirty(self):
        split = split_core_tactical_quantity(100)

        self.assertTrue(split["split"])
        self.assertEqual(70, split["core"])
        self.assertEqual(30, split["tactical"])

    def test_overbought_reversal_and_dtp_weakening_triggers_partial_exit(self):
        position = {"direction": "long", "entry_price": 100.0, "stop_price": 98.0, "shares": 30}
        state = {"saw_overheated": True}
        snapshot = {
            "close": 101.4,
            "high": 102.1,
            "low": 101.0,
            "bar_time_ms": 1,
            "crsi": 68.0,
            "crsi_ub": 70.0,
            "dtp_phase": "weakening",
            "dtp_dir": 1,
            "ema_fast": 101.0,
            "vwap": 101.0,
        }

        decision = evaluate_intraday_harvest(position, snapshot, state=state)

        self.assertEqual(ACTION_PARTIAL_EXIT, decision["action"])
        self.assertTrue(decision["state"]["partial_exited"])
        self.assertIn("dtp_phase_weakening", decision["reasons"])

    def test_severe_decay_triggers_full_exit(self):
        position = {"direction": "long", "entry_price": 100.0, "stop_price": 98.0, "shares": 70}
        snapshot = {
            "close": 100.1,
            "high": 101.0,
            "low": 99.8,
            "bar_time_ms": 2,
            "crsi": 65.0,
            "crsi_ub": 70.0,
            "dtp_phase": "weakening",
            "dtp_dir": -1,
            "ema_weak": True,
            "ema_fast": 100.5,
            "vwap": 100.4,
            "any_bear_div": True,
        }

        decision = evaluate_intraday_harvest(position, snapshot, state={"saw_overheated": True})

        self.assertEqual(ACTION_FULL_EXIT, decision["action"])
        self.assertGreaterEqual(decision["score"], 4)

    def test_partial_state_near_support_and_recovery_triggers_reentry(self):
        position = {"direction": "long", "entry_price": 100.0, "stop_price": 98.0, "shares": 30}
        snapshot = {
            "close": 101.0,
            "high": 101.2,
            "low": 100.8,
            "bar_time_ms": 3,
            "vwap": 101.02,
            "ema_fast": 101.01,
            "sd_mid": 101.03,
            "dtp_phase": "confirmed",
            "dtp_dir": 1,
            "crsi": 52.0,
            "crsi_db": 30.0,
            "crsi_ub": 70.0,
            "ema_strong_bull": True,
            "atr": 0.5,
        }

        decision = evaluate_intraday_harvest(
            position,
            snapshot,
            state={"partial_exited": True, "cycles": 1},
            allow_reentry=True,
        )

        self.assertEqual(ACTION_REENTRY, decision["action"])
        self.assertGreater(decision["reentry_prices"]["stop_loss"], 0)
        self.assertGreater(decision["reentry_prices"]["take_profit"], decision["reentry_prices"]["entry"])

    def test_suggested_stop_does_not_widen_existing_risk(self):
        long_stop = suggested_stop_price(
            {"direction": "long", "entry_price": 100.0, "stop_price": 99.0, "shares": 10},
            {"close": 100.8},
        )
        short_stop = suggested_stop_price(
            {"direction": "short", "entry_price": 100.0, "stop_price": 101.0, "shares": 10},
            {"close": 99.2},
        )

        self.assertGreaterEqual(long_stop, 99.0)
        self.assertLessEqual(short_stop, 101.0)


if __name__ == "__main__":
    unittest.main()
