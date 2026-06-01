import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PINE_PATH = REPO_ROOT / "tradingview" / "Signal_Strategy_Core[Glory].pine"


class TradingViewStrategyCorePineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PINE_PATH.read_text(encoding="utf-8")

    def test_tp_checkpoint_runner_uses_safety_target_and_trailing_stop(self):
        source = self.source

        self.assertIn('enableTpCheckpointRunner = input.bool(true, "TP checkpoint runner", group="05 Risk")', source)
        self.assertIn("setupUsesRunner(string setup)", source)
        self.assertIn("calcRunnerSafetyTarget(float entryPrice, float stopPrice, int direction)", source)
        self.assertIn("activeTargetCheckpointHit := true", source)
        self.assertIn('reason == "runner_stop" ? "跟踪止盈"', source)
        self.assertIn('if exitReason == "stop_loss" and activeTargetCheckpointHit and exitPnlPerShare > 0.0', source)
        self.assertIn('riskUpdateReason := "tp_checkpoint_runner"', source)
        self.assertIn('riskUpdateReason := riskUpdateReason == "" ? "runner_trail_stop" : riskUpdateReason', source)

        trail_index = source.index("if activeRunnerMode and activeTargetCheckpointHit")
        alert_index = source.index("if riskChanged and enableAlerts")
        self.assertLess(trail_index, alert_index)

    def test_entry_payload_marks_runner_checkpoint_and_hard_target_state(self):
        source = self.source

        self.assertIn("jsonNum(\"target_checkpoint\", checkpointPrice)", source)
        self.assertIn("jsonNum(\"safety_take_profit\", targetPrice)", source)
        self.assertIn("jsonBool(\"tp_checkpoint_runner\", runnerMode)", source)
        self.assertIn("jsonBool(\"target_is_hard\", not runnerMode)", source)
        self.assertIn("buildEntryPayload(entryEventId, posId, \"long\", longSetup, longReason, qty, entryPrice, stopPrice, targetPrice, checkpointPrice, runnerMode", source)
        self.assertIn("buildEntryPayload(entryEventId, posId, \"short\", shortSetup, shortReason, qty, entryPrice, stopPrice, targetPrice, checkpointPrice, runnerMode", source)

    def test_exit_labels_show_pnl_amount_and_percent(self):
        source = self.source

        self.assertIn("exitPnlLabelText(string reason, string timeText, float price, float pnl, float pnlPct)", source)
        self.assertIn("PnL \" + fmtSignedMoney(pnl) + \" (\" + fmtSignedPct(pnlPct) + \")", source)
        self.assertIn(
            "label.new(bar_index, exitY, exitPnlLabelText(exitReason, lastExitTimeText, exitPrice, lastExitPnl, lastExitPnlPct)",
            source,
        )
        self.assertIn(
            'label.new(bar_index, eodY, exitPnlLabelText("force_flat_eod", lastExitTimeText, eodExitPrice, lastExitPnl, lastExitPnlPct)',
            source,
        )


if __name__ == "__main__":
    unittest.main()
