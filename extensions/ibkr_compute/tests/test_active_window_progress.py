import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.core.signal_generator import SignalGenerator


def base_snapshot(**overrides):
    snapshot = {
        "close": 100.0,
        "atr": 2.0,
        "atr_raw": 2.0,
        "sd_zone": 0,
        "sd_trend": 0,
        "dtp_dir": 0,
        "dtp_phase": "neutral",
        "dtp_phase_bars": 99,
    }
    snapshot.update(overrides)
    return snapshot


class ActiveWindowProgressTest(unittest.TestCase):
    def test_active_window_age_progress_boundaries_and_expiry(self):
        gen = SignalGenerator("SPY", "5m", {"signal_window_max_bars": 12})

        gen.update(base_snapshot(sd_upper=True))
        trace = gen.get_trace_snapshot()
        self.assertTrue(trace["window_flags"]["sd_upper_active"])
        self.assertEqual(trace["window_flags"]["sd_upper_age_bars"], 0)

        for _ in range(10):
            gen.update(base_snapshot())
        trace = gen.get_trace_snapshot()
        self.assertTrue(trace["window_flags"]["sd_upper_active"])
        self.assertEqual(trace["window_flags"]["sd_upper_age_bars"], 10)
        self.assertLessEqual(12 - trace["window_flags"]["sd_upper_age_bars"], 2)

        for _ in range(2):
            gen.update(base_snapshot())
        trace = gen.get_trace_snapshot()
        self.assertTrue(trace["window_flags"]["sd_upper_active"])
        self.assertEqual(trace["window_flags"]["sd_upper_age_bars"], 12)
        self.assertEqual(12 - trace["window_flags"]["sd_upper_age_bars"], 0)

        gen.update(base_snapshot())
        trace = gen.get_trace_snapshot()
        self.assertFalse(trace["window_flags"]["sd_upper_active"])
        self.assertEqual(trace["window_flags"]["sd_upper_age_bars"], 0)
        self.assertTrue(any("窗口超过12根K线" in item for item in trace["events"]))

    def test_trace_schema_exposes_active_window_and_component_flags(self):
        gen = SignalGenerator("SPY", "5m", {"signal_window_max_bars": 12})
        gen.update(base_snapshot(sd_lower=True))
        trace = gen.get_trace_snapshot()

        self.assertEqual(
            set(trace),
            {"signal_state", "events", "filters", "window_flags", "component_flags", "setup_state"},
        )
        self.assertGreaterEqual(
            set(trace["signal_state"]),
            {
                "stage",
                "direction",
                "signal",
                "label",
                "reason",
                "filter_reason",
                "signal_window",
                "signal_mode",
                "ema_touch_line",
                "div_source",
                "setup",
                "entry_order_type",
                "technical_description",
                "trigger_checks",
                "filter_checks",
                "signal_payload",
            },
        )
        self.assertGreaterEqual(
            set(trace["window_flags"]),
            {
                "sd_upper_valid",
                "sd_lower_valid",
                "sd_upper_active",
                "sd_lower_active",
                "sd_upper_used",
                "sd_lower_used",
                "sd_upper_age_bars",
                "sd_lower_age_bars",
            },
        )
        self.assertGreaterEqual(
            set(trace["component_flags"]),
            {
                "sd_upper_bull_touch_seen",
                "sd_upper_bull_fractal_seen",
                "sd_upper_bear_fractal_seen",
                "sd_lower_bull_fractal_seen",
                "sd_lower_bear_touch_seen",
                "sd_lower_bear_fractal_seen",
                "bull_crsi_div_seen",
                "bear_crsi_div_seen",
                "bull_obv_div_seen",
                "bear_obv_div_seen",
                "buy_raw",
                "sell_raw",
            },
        )

    def test_type1_requires_sd_upper_ema_bull_fractal_and_bull_divergence(self):
        self.assertEqual(
            self._run_signal_sequence("upper", ["ema_bull_touch", "fractal_bull", "crsi_bull_div"])["signal"],
            "trend_sdUpper",
        )
        for missing in ("ema_bull_touch", "fractal_bull", "crsi_bull_div"):
            with self.subTest(missing=missing):
                signal = self._run_signal_sequence(
                    "upper",
                    [item for item in ("ema_bull_touch", "fractal_bull", "crsi_bull_div") if item != missing],
                )
                self.assertIsNone(signal)

    def test_type2_requires_sd_lower_bull_fractal_and_bull_divergence(self):
        self.assertEqual(
            self._run_signal_sequence("lower", ["fractal_bull", "crsi_bull_div"])["signal"],
            "mr_sdLower",
        )
        for missing in ("fractal_bull", "crsi_bull_div"):
            with self.subTest(missing=missing):
                signal = self._run_signal_sequence(
                    "lower",
                    [item for item in ("fractal_bull", "crsi_bull_div") if item != missing],
                )
                self.assertIsNone(signal)

    def test_type3_requires_sd_upper_bear_fractal_and_bear_divergence(self):
        self.assertEqual(
            self._run_signal_sequence("upper", ["fractal_bear", "crsi_bear_div"])["signal"],
            "mr_sdUpper",
        )
        for missing in ("fractal_bear", "crsi_bear_div"):
            with self.subTest(missing=missing):
                signal = self._run_signal_sequence(
                    "upper",
                    [item for item in ("fractal_bear", "crsi_bear_div") if item != missing],
                )
                self.assertIsNone(signal)

    def test_type4_requires_sd_lower_ema_bear_fractal_and_bear_divergence(self):
        self.assertEqual(
            self._run_signal_sequence("lower", ["ema_bear_touch", "fractal_bear", "crsi_bear_div"])["signal"],
            "trend_sdLower",
        )
        for missing in ("ema_bear_touch", "fractal_bear", "crsi_bear_div"):
            with self.subTest(missing=missing):
                signal = self._run_signal_sequence(
                    "lower",
                    [item for item in ("ema_bear_touch", "fractal_bear", "crsi_bear_div") if item != missing],
                )
                self.assertIsNone(signal)

    def _run_signal_sequence(self, side, component_keys):
        gen = SignalGenerator("SPY", "5m", {"signal_window_max_bars": 12})
        gen.update(base_snapshot(sd_upper=side == "upper", sd_lower=side == "lower"))
        signal = None
        for key in component_keys:
            signal = gen.update(base_snapshot(**{key: True}))
        return signal


if __name__ == "__main__":
    unittest.main()
