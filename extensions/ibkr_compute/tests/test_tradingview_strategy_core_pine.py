import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PINE_PATH = REPO_ROOT / "tradingview" / "Signal_Strategy_Core[Glory].pine"
DISPLAY_PINE_PATH = REPO_ROOT / "tradingview" / "Signal_Strategy_Display[Glory].pine"
V1_CORE_PINE_PATH = REPO_ROOT / "tradingview" / "v1" / "Signal_Strategy_Core[Glory].pine"
V1_DISPLAY_PINE_PATH = REPO_ROOT / "tradingview" / "v1" / "Signal_Strategy_Display[Glory].pine"


class TradingViewStrategyCorePineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CORE_PINE_PATH.read_text(encoding="utf-8")
        cls.display_source = DISPLAY_PINE_PATH.read_text(encoding="utf-8")

    def test_core_and_display_scripts_have_separate_responsibilities(self):
        core = self.source
        display = self.display_source

        self.assertIn('strategy(title="Signal Strategy Core[Glory]"', core)
        self.assertIn('indicator(title="Signal Strategy Display[Glory]"', display)
        self.assertIn("alert(", core)
        self.assertIn("strategy.entry", core)
        self.assertIn("strategy.exit", core)
        self.assertNotIn("label.new(", core)
        self.assertNotIn("plot(", core)
        self.assertNotIn("plotshape(", core)
        self.assertNotIn("table.", core)
        self.assertNotIn("line.", core)
        self.assertIn("label.new(", display)
        self.assertIn("plotshape(", display)
        self.assertIn("table.cell", display)
        self.assertNotIn("strategy.", display)
        self.assertNotIn("strategy(", display)
        self.assertNotIn("alert(", display)
        self.assertNotIn("basePayload", display)
        self.assertNotIn("jsonStr(", display)
        self.assertNotIn("jsonNum(", display)
        self.assertNotIn("jsonBool(", display)

    def test_v1_archive_is_preserved(self):
        self.assertTrue(V1_CORE_PINE_PATH.exists())
        self.assertTrue(V1_DISPLAY_PINE_PATH.exists())
        self.assertIn('string STRATEGY_VERSION = "SSC_v1_20260613_alert_slim"', V1_CORE_PINE_PATH.read_text(encoding="utf-8"))
        self.assertIn('indicator(title="Signal Strategy Display[Glory]"', V1_DISPLAY_PINE_PATH.read_text(encoding="utf-8"))

    def test_v3_model_identity_and_scope_are_explicit(self):
        source = self.source
        display = self.display_source

        self.assertIn('string STRATEGY_VERSION = "SSC_v3_20260616_clear_structure_small_stop"', source)
        self.assertIn('string ENTRY_WINDOW_MODEL = "clear_structure_small_stop_v3"', source)
        self.assertIn('string TP_SL_MODEL = "clear_structure_invalidation_v3"', source)
        self.assertIn('string RISK_MODEL = "clear_structure_small_stop_asymmetric_risk_v3"', source)
        self.assertIn('string TRADE_MODEL = "clear_structure_small_stop_v3"', source)
        self.assertIn('string STRATEGY_VERSION = "SSD_v3_20260616_clear_structure_small_stop"', display)
        self.assertIn('string ENTRY_WINDOW_MODEL = "clear_structure_small_stop_v3"', display)
        self.assertIn('Display shadow，不代表 TV strategy 已成交', display)

    def test_universal_small_stop_gate_is_hard_filter(self):
        source = self.source
        display = self.display_source

        for text in (source, display):
            self.assertIn('predictiveMaxRiskDollars = input.float(37.5, "Predictive max trial risk ($)"', text)
            self.assertIn('confirmedMaxRiskDollars = input.float(75.0, "Confirmed max trial risk ($)"', text)
            self.assertIn('predictiveMaxStopAtr = input.float(0.70, "Predictive max stop distance (x ATR)"', text)
            self.assertIn('confirmedMaxStopAtr = input.float(0.90, "Confirmed max stop distance (x ATR)"', text)
            self.assertIn('predictiveMaxStopBps = input.float(60.0, "Predictive max stop distance (bps)"', text)
            self.assertIn('confirmedMaxStopBps = input.float(80.0, "Confirmed max stop distance (bps)"', text)
            self.assertIn('smallStopTrialAllowed(float entryPrice, float stopPrice, float atrValue, float maxRiskDollars, float maxAtrMultiple, float maxBps) =>', text)
            self.assertIn('rps <= maxRiskDollars and atrDistance <= maxAtrMultiple and bpsDistance <= maxBps', text)
            self.assertIn('calcTrialQty(float entryPrice, float stopPrice, float maxRiskDollars) =>', text)
            self.assertIn('minExpectedR = input.float(2.50, "Minimum expected reward (R)"', text)
            self.assertNotIn('minStopRiskFloorDollars', text)
            self.assertNotIn('Minimum stop breathing room', text)

    def test_only_current_setup_families_are_used_in_current_files(self):
        for text in (self.source, self.display_source):
            self.assertIn('"trend_support_long"', text)
            self.assertIn('"trend_resistance_short"', text)
            self.assertIn('"probe_2b_top_short"', text)
            self.assertIn('"probe_2b_bottom_long"', text)
            self.assertNotIn('"mr_sdLower"', text)
            self.assertNotIn('"mr_sdUpper"', text)
            self.assertNotIn('"trend_sdUpper"', text)
            self.assertNotIn('"trend_sdLower"', text)
            self.assertNotIn('"trend_emaDeathContinuation"', text)

    def test_trend_entries_require_pullback_to_strong_structure(self):
        source = self.source

        self.assertIn('referenceSession = input.session("0930-1600", "Reference day structure session"', source)
        self.assertIn('bool referenceDayReset = timeframe.change("D") or referenceSessionStarted', source)
        self.assertIn('trendHasHH', source)
        self.assertIn('trendHasHL', source)
        self.assertIn('trendHasLL', source)
        self.assertIn('trendHasLH', source)
        self.assertIn('bool strongStructTrendLong = dtpBull and emaFast > emaMid and emaMid > emaSlow and close >= sdMid and trendHasHH and trendHasHL', source)
        self.assertIn('bool strongStructTrendShort = dtpBear and emaFast < emaMid and emaMid < emaSlow and close <= sdMid and trendHasLL and trendHasLH', source)
        self.assertIn('float trendSupportBasis = strongStructTrendLong ? todayStructLow : na', source)
        self.assertIn('float trendResistanceBasis = strongStructTrendShort ? todayStructHigh : na', source)
        self.assertIn('bool trendLongPredictContext = trendLongContext and strongTrendSupport', source)
        self.assertIn('bool trendShortPredictContext = trendShortContext and strongTrendResistance', source)
        self.assertIn('trendLongPredictStop = strongTrendSupport ? trendSupportZoneLow - invalidationBuffer', source)
        self.assertIn('trendShortPredictStop = strongTrendResistance ? trendResistanceZoneHigh + invalidationBuffer', source)

    def test_probe_entries_support_predictive_and_confirmed_2b_orders(self):
        source = self.source

        self.assertIn('topPredictAllowed = topPredictContext and smallStopTrialAllowed', source)
        self.assertIn('bottomPredictAllowed = bottomPredictContext and smallStopTrialAllowed', source)
        self.assertIn('float topBasis = todayStructHigh', source)
        self.assertIn('float bottomBasis = todayStructLow', source)
        self.assertIn('topSweepFailed = confirmed and topRightShoulder and high > todayStructHigh + tickBuffer and close < todayStructHigh', source)
        self.assertIn('bottomSweepFailed = confirmed and bottomRightShoulder and low < todayStructLow - tickBuffer and close > todayStructLow', source)
        self.assertIn('probeMinAnchorAgeBars = input.int(6, "Probe min anchor age bars"', source)
        self.assertIn('probeMinPullbackAtr = input.float(0.80, "Probe min pullback (x ATR)"', source)
        self.assertIn('probeRetestBandAtr = input.float(0.35, "Probe retest band (x ATR)"', source)
        self.assertIn('topConfirmAllowed = topConfirmContext and smallStopTrialAllowed', source)
        self.assertIn('bottomConfirmAllowed = bottomConfirmContext and smallStopTrialAllowed', source)
        self.assertIn('topSignalGapOk', source)
        self.assertIn('bottomSignalGapOk', source)

    def test_entry_window_uses_0910_to_1530(self):
        source = self.source
        display = self.display_source

        self.assertIn('tradingSession = input.session("0910-1530", "Trading session"', source)
        self.assertIn('entrySession = input.session("0910-1530", "Entry window"', source)
        self.assertIn('entrySession = input.session("0910-1530", "Entry window"', display)
        self.assertIn('jsonStr("entry_cutoff_time", "15:30")', source)

    def test_core_serializes_predictive_metadata_and_latency_fields(self):
        source = self.source

        for field in (
            'jsonStr("entry_intent", entryIntent)',
            'jsonStr("trial_risk_mode", trialRiskMode)',
            'jsonStr("trade_model", TRADE_MODEL)',
            'jsonBool("structure_clear", selectedStructureClear)',
            'jsonNum("structure_anchor_price", selectedStructureAnchor)',
            'jsonNum("expected_r", selectedExpectedR)',
            'jsonStr("trend_struct_dir", trendStructDir)',
            'jsonStr("trend_struct_pattern", selectedTrendStructPattern)',
            'jsonStr("probe_pattern", selectedProbePattern)',
            'jsonStr("probe_filter_reason", selectedProbeFilterReason)',
            'jsonNum("today_struct_high", todayStructHigh)',
            'jsonNum("today_struct_low", todayStructLow)',
            'jsonBool("small_stop_gate_passed", selectedSmallStopGatePassed)',
            'jsonStr("small_stop_gate_reason", selectedSmallStopGateReason)',
            'jsonNum("stop_distance_atr", selectedStopDistanceAtr)',
            'jsonNum("stop_distance_bps", selectedStopDistanceBps)',
            'jsonNum("max_trial_risk_dollars", selectedRiskBudget)',
            'jsonNum("prediction_level", selectedPredictionLevel)',
            'jsonNum("prediction_zone_low", selectedZoneLow)',
            'jsonNum("prediction_zone_high", selectedZoneHigh)',
            'jsonNum("invalidation_price", selectedInvalidation)',
            'jsonStr("main_reason", selectedMainReason)',
            'jsonStr("secondary_reason", selectedSecondaryReason == "" ? "none" : selectedSecondaryReason)',
            'jsonStr("plan_replace_reason", planReplaced ? replaceReason : "none")',
            'jsonNum("bar_open_ms", time)',
            'jsonNum("bar_close_ms", time_close)',
            'jsonNum("pine_eval_ms", timenow)',
        ):
            self.assertIn(field, source)

    def test_pending_lifecycle_replaces_or_cancels_unfilled_predictions(self):
        source = self.source

        self.assertIn('entryOrderTtlBars = input.int(2, "Entry order TTL bars"', source)
        self.assertIn('pendingExpired = pendingEntry and not na(activeExpiresBar) and bar_index > activeExpiresBar', source)
        self.assertIn('pendingInvalidated = pendingEntry and (activeDirection == 1 ? close < activeStop : close > activeStop)', source)
        self.assertIn('pendingOppositeSelected = pendingEntry and selectedDirection != 0 and selectedDirection != activeDirection', source)
        self.assertIn('activeEntryIntent == "predictive_limit" and selectedEntryIntent == "confirmed_limit"', source)
        self.assertIn('planReplaceMinMoveAtr = input.float(0.25, "Plan replace min move (x ATR)"', source)
        self.assertIn('bool meaningfulPlanMove = math.abs(nz(activeEntry, selectedEntry) - selectedEntry) >= atr * planReplaceMinMoveAtr', source)
        self.assertIn('strategy.cancel(orderIdForDirection(activeDirection))', source)
        self.assertIn('"same_direction_plan_replaced"', source)

    def test_runner_keeps_far_safety_tp_and_updates_stop(self):
        source = self.source

        self.assertIn('predictiveRunnerActivationR = input.float(0.60, "Predictive breakeven/runner trigger (R)"', source)
        self.assertIn('confirmedRunnerActivationR = input.float(0.80, "Confirmed breakeven/runner trigger (R)"', source)
        self.assertIn('runnerSafetyR = input.float(6.0, "Safety take profit (R)"', source)
        self.assertIn('calcTargetAtR(selectedEntry, selectedStop, selectedDirection, runnerSafetyR)', source)
        self.assertIn('riskReason := "runner_activation"', source)
        self.assertIn('riskReason := riskReason == "" ? "runner_trail_stop" : riskReason', source)
        self.assertIn('riskReason := "no_follow_through_compress_stop"', source)
        self.assertIn('jsonRequestedSides(requestedSides)', source)
        self.assertIn('strategy.exit("TV-L-RISK", from_entry="TV-L", stop=activeStop, limit=activeTarget', source)
        self.assertIn('strategy.exit("TV-S-RISK", from_entry="TV-S", stop=activeStop, limit=activeTarget', source)

    def test_display_explains_event_flow_and_four_plan_lines(self):
        display = self.display_source

        self.assertIn('displayPreset = input.string("核心交易流程", "Display preset"', display)
        self.assertIn('labelDetailMode = input.string("compact_tooltip", "Flow label detail mode"', display)
        self.assertIn('labelTextMode = input.string("auto", "Label text color"', display)
        self.assertIn('flowLabelText(string compactText, string detailText) =>', display)
        self.assertIn('flowLabelTooltip(string detailText) =>', display)
        self.assertIn('flowLabelSize() =>', display)
        self.assertIn('labelTextColor(string state, int direction) =>', display)
        self.assertIn('tableBgColor(int row) =>', display)
        self.assertIn('tableTextColor(int row) =>', display)
        for line in display.splitlines():
            if "label.new(" in line:
                self.assertIn("flowLabelText(", line)
                self.assertIn("textcolor=labelTextColor(", line)
                self.assertIn("size=flowLabelSize()", line)
                self.assertIn("tooltip=flowLabelTooltip(", line)
                self.assertNotIn("textcolor=color.white", line)
                self.assertNotIn("size=size.normal", line)
                self.assertNotIn("size=size.small", line)
            if "table.cell(" in line:
                self.assertIn("text_color=tableTextColor(", line)
                self.assertIn("bgcolor=tableBgColor(", line)
                self.assertNotIn("text_color=color.white", line)
        self.assertIn('"F" + str.tostring(flowId) + " ②" + intentName(selectedEntryIntent)', display)
        self.assertIn('主因: " + selectedMainReason', display)
        self.assertIn('次因: " + (selectedSecondaryReason == "" ? "none" : selectedSecondaryReason)', display)
        self.assertIn('结构锚点 " + fmt(selectedStructureAnchor)', display)
        self.assertIn('plot(showEmaFastSlowLines ? emaFast : na, "EMA 20 快线"', display)
        self.assertIn('plot(showDtpTrendLine ? dtpAvg : na, "DTP 趋势线", color=dtpLineColor, linewidth=2)', display)
        self.assertIn('plot(showBandLines ? sdUpper : na, "SD signal upper"', display)
        self.assertIn('plot(showStructureGuideLines ? todayStructHigh : na, "Today Struct High"', display)
        self.assertIn('plot(showStructureGuideLines ? todayStructLow : na, "Today Struct Low"', display)
        self.assertIn('plotshape(showComponentMarkers and sdLowerHit, "SD lower hit"', display)
        self.assertIn('plotshape(showAnyFractalMarkers and fractalBull, "Fractal bull"', display)
        self.assertIn('plotshape(showEmaCrossMarkers and emaGoldenCross, "EMA20/50 golden cross"', display)
        self.assertIn('plotshape(showComponentMarkers and bullDivNow, "Bull divergence"', display)
        self.assertIn('currentEntryLine := line.new(bar_index, shadowEntry', display)
        self.assertIn('color=color.new(color.yellow, 0)', display)
        self.assertIn('currentStopLine := line.new(bar_index, shadowStop', display)
        self.assertIn('color=color.new(color.red, 0)', display)
        self.assertIn('currentRunnerLine := line.new(bar_index, shadowRunner', display)
        self.assertIn('color=color.new(color.blue, 0)', display)
        self.assertIn('currentTargetLine := line.new(bar_index, shadowTarget', display)
        self.assertIn('color=color.new(color.green, 0)', display)
        self.assertIn('"黄/红/蓝/绿"', display)
        self.assertIn('"Entry / SL / Runner / SafetyTP"', display)


if __name__ == "__main__":
    unittest.main()
