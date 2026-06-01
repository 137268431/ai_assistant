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

    def test_fast_in_defaults_volatility_filter_and_marker_toggles(self):
        source = self.source

        self.assertIn('positionAmount = input.float(5000, "Notional per trade ($)", step=500, minval=100, group="05 Risk")', source)
        self.assertIn('sdSignalBand = input.int(4, "MR trigger band", minval=1, maxval=4, group="03 SD Channel")', source)
        self.assertIn('useVolatilityFilter = input.bool(true, "Block low-volatility entries", group="05 Risk")', source)
        self.assertIn('minAtrPctForEntry = input.float(0.08, "Minimum ATR% for entry", step=0.01, minval=0.0, group="05 Risk")', source)
        self.assertIn("bool lowVolatilityEntryBlocked = useVolatilityFilter and atrPct < minAtrPctForEntry", source)
        self.assertIn('blockLongReason := "low_volatility"', source)
        self.assertIn('blockShortReason := "low_volatility"', source)
        self.assertIn('jsonBool("volatility_filter_enabled", useVolatilityFilter)', source)
        self.assertIn('jsonNum("min_atr_pct_for_entry", minAtrPctForEntry)', source)
        self.assertIn('jsonBool("volatility_entry_blocked", lowVolatilityEntryBlocked)', source)

        self.assertIn('showFractalMarkers = input.bool(true, "Show fractal markers", group="08 Display")', source)
        self.assertIn('showEmaCrossMarkers = input.bool(true, "Show EMA20/50 cross markers", group="08 Display")', source)
        self.assertIn('showSdHelperLines = input.bool(false, "Show SD regression/filter lines", group="08 Display")', source)
        self.assertIn("bool showSdHelperBandLines = showSdHelperLines or displayIndicatorMode or displayDebugMode", source)
        self.assertIn('plot(showBandLines ? sdSignalUpper : na, "SD signal upper"', source)
        self.assertIn('plot(showSdHelperBandLines ? sdReg : na, "SD regression"', source)
        self.assertIn('plot(showSdHelperBandLines ? sdFilterUpper : na, "SD filter upper"', source)
        self.assertIn('plotshape(showAnyFractalMarkers and fractalBull, "Fractal bull"', source)
        self.assertIn("offset=-fractalPeriod", source)
        self.assertIn('plotshape(showEmaCrossMarkers and emaGoldenCross, "EMA20/50 golden cross"', source)
        self.assertIn('plotshape(showEmaCrossMarkers and emaDeathCross, "EMA20/50 death cross"', source)
        self.assertIn('text="20/50金叉"', source)
        self.assertIn('text="20/50死叉"', source)

    def test_window_activation_pre_alert_is_non_directional(self):
        source = self.source
        prealert_start = source.index("buildPreAlertPayload")
        prealert_end = source.index("// TV-primary event flow")
        prealert_section = source[prealert_start:prealert_end]

        self.assertIn("buildPreAlertPayload(string eventIdValue, string pendingPositionId, string activationWindow)", source)
        self.assertIn('jsonStr("pre_alert_stage", "window_activation")', prealert_section)
        self.assertIn('jsonStr("activation_window", activationWindow)', prealert_section)
        self.assertIn('jsonNum("activation_window_upper", sdSignalUpper)', prealert_section)
        self.assertIn('jsonNum("activation_window_lower", sdSignalLower)', prealert_section)
        self.assertIn('jsonBool("entry_decides_direction", true)', prealert_section)
        self.assertNotIn("direction_bias", prealert_section)
        self.assertNotIn("candidate_direction", prealert_section)
        self.assertNotIn("position_side", prealert_section)
        self.assertNotIn("mtfPayloadForDirection(candidateDirection)", prealert_section)
        self.assertNotIn("if enableAlerts and preAlertCross", source)
        self.assertIn('alert(buildPreAlertPayload(eventId("pre_alert", "window_lower"), pendingLowerId, "lower"), alert.freq_all)', source)
        self.assertIn('alert(buildPreAlertPayload(eventId("pre_alert", "window_upper"), pendingUpperId, "upper"), alert.freq_all)', source)

    def test_activation_labels_mark_observe_pool_not_entry(self):
        source = self.source
        activation_lines = "\n".join(
            line
            for line in source.splitlines()
            if line.strip().startswith("label.new") and (
                'flowLabelColor("activate_lower"' in line or 'flowLabelColor("activate_upper"' in line
            )
        )

        self.assertIn('"观察池入选\\n" + syminfo.ticker + " · SD下轨观察\\n非开仓点，等组件"', source)
        self.assertIn('"观察池入选\\n" + syminfo.ticker + " · SD上轨观察\\n非开仓点，等组件"', source)
        self.assertNotIn("BUY", activation_lines)
        self.assertNotIn("entry", activation_lines)
        self.assertNotIn("买入做多", activation_lines)
        self.assertNotIn("卖出做空", activation_lines)

    def test_sd_window_resets_only_on_activation_events(self):
        source = self.source

        self.assertIn("bool lowerActivationEvent = sdLowerHit and (not lowerWindowActive or lowerWindowUsed or na(lowerWindowStart) or bar_index - lowerWindowStart > mrWindowBars)", source)
        self.assertIn("bool upperActivationEvent = sdUpperHit and (not upperWindowActive or upperWindowUsed or na(upperWindowStart) or bar_index - upperWindowStart > mrWindowBars)", source)
        self.assertIn("if lowerActivationEvent\n    lowerWindowActive := true", source)
        self.assertIn("if upperActivationEvent\n    upperWindowActive := true", source)
        self.assertNotIn("if sdLowerHit\n    lowerWindowActive := true", source)
        self.assertNotIn("if sdUpperHit\n    upperWindowActive := true", source)

    def test_entry_logic_requires_window_and_two_of_three_direction_components(self):
        source = self.source

        self.assertIn("directionComponentCount(bool fractalReady, bool divReady, bool emaCrossReady)", source)
        self.assertIn("directionComponentsReady(bool fractalReady, bool divReady, bool emaCrossReady)", source)
        self.assertIn("int longTrendUpperDirectionComponents = directionComponentCount(upperBullFractalSeen, bullDivSeen, upperBullEmaCrossSeen)", source)
        self.assertIn("int longMrLowerDirectionComponents = directionComponentCount(lowerBullFractalSeen, bullDivSeen, lowerBullEmaCrossSeen)", source)
        self.assertIn("int shortMrUpperDirectionComponents = directionComponentCount(upperBearFractalSeen, bearDivSeen, upperBearEmaCrossSeen)", source)
        self.assertIn("int shortTrendLowerDirectionComponents = directionComponentCount(lowerBearFractalSeen, bearDivSeen, lowerBearEmaCrossSeen)", source)
        self.assertIn("bool setupLongTrendUpper = upperWindowValid and upperBullTouchSeen and longTrendUpperDirectionComponents >= 2", source)
        self.assertIn("bool setupLongMrLower = lowerWindowValid and longMrLowerDirectionComponents >= 2", source)
        self.assertIn("bool setupShortMrUpper = upperWindowValid and shortMrUpperDirectionComponents >= 2", source)
        self.assertIn("bool setupShortTrendLower = lowerWindowValid and lowerBearTouchSeen and shortTrendLowerDirectionComponents >= 2", source)
        self.assertIn("int scoreLongTrendUpper = math.min(100,", source)
        self.assertIn("int scoreLongMrLower = math.min(100,", source)
        self.assertIn("int scoreShortMrUpper = math.min(100,", source)
        self.assertIn("int scoreShortTrendLower = math.min(100,", source)

    def test_risk_update_sequence_is_incremented_and_serialized(self):
        source = self.source

        self.assertIn("var int activeRiskUpdateSeq = 0", source)
        self.assertIn('jsonInt("risk_update_seq", activeRiskUpdateSeq)', source)
        self.assertIn("activeRiskUpdateSeq := activeRiskUpdateSeq + 1\n            alert(buildRiskPayload(eventId(\"risk_update\", \"initial\")", source)
        self.assertIn("activeRiskUpdateSeq := activeRiskUpdateSeq + 1\n        alert(buildRiskPayload(eventId(\"risk_update\", reasonForEvent)", source)


if __name__ == "__main__":
    unittest.main()
