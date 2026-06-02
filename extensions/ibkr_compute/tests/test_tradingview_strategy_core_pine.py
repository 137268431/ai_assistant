import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PINE_PATH = REPO_ROOT / "tradingview" / "Signal_Strategy_Core[Glory].pine"


class TradingViewStrategyCorePineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PINE_PATH.read_text(encoding="utf-8")

    def test_runner_uses_separate_activation_and_safety_tp(self):
        source = self.source

        self.assertIn('enableTpCheckpointRunner = input.bool(true, "Enable runner mode", group="05 Risk")', source)
        self.assertIn('runnerActivationR = input.float(1.0, "Runner activation (R)", step=0.25, minval=0.25, maxval=10.0, group="05 Risk")', source)
        self.assertIn('runnerSafetyR = input.float(4.0, "Runner safety TP (R)", step=0.5, minval=1.0, maxval=20.0, group="05 Risk")', source)
        self.assertIn("setupUsesRunner(string setup)", source)
        self.assertIn("calcRunnerActivationTarget(float entryPrice, float stopPrice, int direction)", source)
        self.assertIn("calcRunnerSafetyTarget(float entryPrice, float stopPrice, int direction)", source)
        self.assertIn("float runnerActivationPrice = runnerMode ? calcRunnerActivationTarget(entryPrice, stopPrice, 1) : na", source)
        self.assertIn("float targetPrice = runnerMode ? calcRunnerSafetyTarget(entryPrice, stopPrice, 1) : hardTargetPrice", source)
        self.assertIn("float runnerActivationPrice = runnerMode ? calcRunnerActivationTarget(entryPrice, stopPrice, -1) : na", source)
        self.assertIn("float targetPrice = runnerMode ? calcRunnerSafetyTarget(entryPrice, stopPrice, -1) : hardTargetPrice", source)
        self.assertIn('strategy.exit("TV-L-RISK", from_entry="TV-L", stop=activeStop, limit=activeTarget', source)
        self.assertIn('comment_profit="平多 · TP止盈"', source)
        self.assertIn("exitTouchesTarget(int direction, float exitPrice, float targetPrice)", source)
        self.assertIn('exitReason := targetExit ? "take_profit" : stopExit ? "stop_loss" : exitReason', source)
        self.assertIn("activeRunnerActive := true", source)
        self.assertIn('reason == "runner_stop" ? "跟踪止盈"', source)
        self.assertIn('if exitReason == "stop_loss" and activeRunnerActive and exitPnlPerShare > 0.0', source)
        self.assertIn('riskUpdateReason := "runner_activation"', source)
        self.assertIn('riskUpdateReason := riskUpdateReason == "" ? "runner_trail_stop" : riskUpdateReason', source)
        self.assertNotIn('riskUpdateReason := "tp_checkpoint_runner"', source)

        activation_index = source.index("if activeRunnerMode and not activeRunnerActive")
        trail_index = source.index("if activeRunnerMode and activeRunnerActive")
        alert_index = source.index("if riskChanged and enableAlerts")
        self.assertLess(activation_index, trail_index)
        self.assertLess(trail_index, alert_index)

    def test_payload_and_plots_distinguish_runner_activation_from_true_tp(self):
        source = self.source

        self.assertIn("jsonNum(\"take_profit\", targetPrice)", source)
        self.assertIn("jsonNum(\"target_checkpoint\", targetCheckpoint)", source)
        self.assertIn("jsonNum(\"safety_take_profit\", targetPrice)", source)
        self.assertIn("jsonNum(\"runner_activation_price\", runnerActivationPrice)", source)
        self.assertIn("jsonNum(\"runner_activation_r\", runnerActivationR)", source)
        self.assertIn("jsonBool(\"runner_enabled\", runnerMode)", source)
        self.assertIn("jsonBool(\"runner_active\", false)", source)
        self.assertIn("jsonBool(\"tp_checkpoint_runner\", runnerMode)", source)
        self.assertIn("jsonBool(\"target_is_hard\", not runnerMode)", source)
        self.assertIn("jsonBool(\"target_checkpoint_is_exit\", not runnerMode)", source)
        self.assertIn('jsonStr("target_role", runnerMode ? "safety_tp" : "hard_tp")', source)
        self.assertIn("buildEntryPayload(entryEventId, posId, \"long\", longSetup, longReason, qty, entryPrice, stopPrice, targetPrice, runnerActivationPrice, runnerMode", source)
        self.assertIn("buildEntryPayload(entryEventId, posId, \"short\", shortSetup, shortReason, qty, entryPrice, stopPrice, targetPrice, runnerActivationPrice, runnerMode", source)
        self.assertIn("jsonRequestedSides(string requestedSides)", source)
        self.assertIn('string requestedSides = stopChanged and targetChanged ? "stop_loss,take_profit" : stopChanged ? "stop_loss" : targetChanged ? "take_profit" : ""', source)
        self.assertIn("float eventNewTarget = targetChanged ? activeTarget : na", source)
        self.assertIn('plot(visibleHardTarget, "TP 真实止盈"', source)
        self.assertIn('plot(visibleSafetyTarget, "Safety TP 真实止盈"', source)
        self.assertIn('plot(visibleRunnerActivation, "Runner Activation"', source)
        self.assertNotIn('plot(visibleTarget, "TP 目标/检查点"', source)

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

    def test_flow_label_backgrounds_are_configurably_transparent(self):
        source = self.source
        label_lines = [line for line in source.splitlines() if "label.new(" in line]

        self.assertIn(
            'flowLabelTransparency = input.int(30, "Flow label background transparency", minval=0, maxval=100, group="08 Display")',
            source,
        )
        self.assertIn("flowBaseColor(string state, string direction) =>", source)
        self.assertIn("color.new(flowBaseColor(state, direction), flowLabelTransparency)", source)
        self.assertIn("flowStatusColor(string state, string direction) =>", source)
        self.assertIn("color.new(flowBaseColor(state, direction), 0)", source)
        self.assertIn("color=color.new(color.orange, flowLabelTransparency)", source)
        self.assertIn("color statusColor = activePositionId != \"\" ? flowStatusColor", source)
        self.assertIn("color.new(color.green, flowLabelTransparency)", source)
        self.assertIn("color.new(color.red, flowLabelTransparency)", source)
        self.assertFalse(
            [line for line in label_lines if "color.new(" in line and ", 0)" in line],
            "flow labels should not use opaque inline label backgrounds",
        )

    def test_fast_in_defaults_volatility_filter_and_marker_toggles(self):
        source = self.source

        self.assertIn('positionAmount = input.float(5000, "Notional per trade ($)", step=500, minval=100, group="05 Risk")', source)
        self.assertIn('sdSignalBand = input.int(3, "MR trigger band", minval=1, maxval=4, group="03 SD Channel")', source)
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
        self.assertIn("var bool candidateNoticeSentToday = false", source)
        self.assertIn("if isNewTradingDay\n    candidateNoticeSentToday := false", source)
        self.assertIn("bool lowerCandidateNotice = lowerActivationEvent and not candidateNoticeSentToday", source)
        self.assertIn("bool upperCandidateNotice = upperActivationEvent and not candidateNoticeSentToday and not lowerCandidateNotice", source)
        self.assertIn("if lowerCandidateNotice or upperCandidateNotice\n    candidateNoticeSentToday := true", source)
        self.assertNotIn("if enableAlerts and canTrade and flatForEntry and lowerActivationEvent", source)
        self.assertNotIn("if enableAlerts and canTrade and flatForEntry and upperActivationEvent", source)
        self.assertNotIn("pendingObservationWindow", source)
        self.assertNotIn("观察池入选", source)
        self.assertNotIn("PRE_ALERT 入池候选", source)
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

        self.assertIn('"已入选候选池\\n" + syminfo.ticker + " · SD窗口开启\\nSD下轨候选\\n非开仓点，等组件"', source)
        self.assertIn('"已入选候选池\\n" + syminfo.ticker + " · SD窗口开启\\nSD上轨候选\\n非开仓点，等组件"', source)
        self.assertIn("if showFlowLabels and lowerCandidateNotice", source)
        self.assertIn("if showFlowLabels and upperCandidateNotice", source)
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
