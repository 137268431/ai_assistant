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
        self.assertIn('bool dayRangeClear = dayRangeFormed and dayRangeAtr >= minClearDayRangeAtr', source)
        self.assertIn('bool dominantTrendLong = dayRangeClear and dtpBull and close >= sdMid', source)
        self.assertIn('bool dominantTrendShort = dayRangeClear and dtpBear and close <= sdMid', source)
        self.assertIn('bool trendLongContext = strongStructTrendLong or dominantTrendLong', source)
        self.assertIn('bool trendShortContext = strongStructTrendShort or dominantTrendShort', source)
        self.assertIn('float trendSupportBasis = trendLongContext ? todayStructLow : na', source)
        self.assertIn('float trendResistanceBasis = trendShortContext ? todayStructHigh : na', source)
        self.assertIn('strongTrendSupport = trendLongContext and dayRangeClear and trendSupportAgeBars >= minTrendSupportAgeBars', source)
        self.assertIn('strongTrendResistance = trendShortContext and dayRangeClear and trendResistanceAgeBars >= minTrendSupportAgeBars', source)
        self.assertIn('bool trendLongPredictContext = trendLongContext and strongTrendSupport and not volumeAttackDown', source)
        self.assertIn('bool trendShortPredictContext = trendShortContext and strongTrendResistance and not volumeAttackUp', source)
        self.assertIn('trendLongPredictStop = strongTrendSupport ? trendSupportZoneLow - invalidationBuffer', source)
        self.assertIn('trendShortPredictStop = strongTrendResistance ? trendResistanceZoneHigh + invalidationBuffer', source)

    def test_probe_entries_support_predictive_and_confirmed_2b_orders(self):
        source = self.source
        display = self.display_source

        for text in (source, display):
            self.assertIn('structureClusterBandAtr = input.float(0.40, "Structure cluster band (x ATR)"', text)
            self.assertIn('minStructureSeparationAtr = input.float(1.00, "Independent structure separation (x ATR)"', text)
            self.assertIn('minMiddleSwingAtr = input.float(1.00, "Middle swing for second test (x ATR)"', text)
            self.assertIn('maxRetestAfterMiddleSwingBars = input.int(12, "Max bars after middle swing retest"', text)
            self.assertIn('minVisualMiddleRetraceRatio = input.float(0.35, "Visual middle retrace ratio"', text)
            self.assertIn('minVisualRightRetestRatio = input.float(0.65, "Visual right retest ratio"', text)
            self.assertIn('minVisualLegBps = input.float(12.0, "Visual min leg (bps)"', text)
            self.assertIn('minClearDayRangeAtr = input.float(2.20, "Clear day range (x ATR)"', text)
            self.assertIn('minTrendSupportAgeBars = input.int(4, "Trend support min age bars"', text)
            self.assertIn('exhaustionVolumeMaxRatio = input.float(1.05, "Exhaustion max volume (x MA)"', text)
            self.assertIn('momentumDecayMaxRatio = input.float(0.65, "Momentum decay max ratio"', text)
            self.assertIn('exhaustionDecayConfirmBars = input.int(2, "Momentum decay confirm bars"', text)
            self.assertIn('exhaustionClosePosShortMax = input.float(0.45, "Short exhaustion close position max"', text)
            self.assertIn('exhaustionClosePosLongMin = input.float(0.55, "Long exhaustion close position min"', text)
            self.assertIn('minAnyPlanGapBars = input.int(4, "Any-plan min gap bars"', text)
            self.assertIn('minAnyPlanDistanceAtr = input.float(0.80, "Any-plan min distance (x ATR)"', text)
            self.assertIn('volumeMaLen = input.int(20, "Volume MA length"', text)
            self.assertIn('highAttackVolumeMult = input.float(1.35, "High attack volume (x MA)"', text)
            self.assertIn('lowRetestVolumeMult = input.float(0.95, "Low retest volume (x MA)"', text)
            self.assertIn('attackClosePosThreshold = input.float(0.65, "Attack close position"', text)
            self.assertIn('attackVolumeCooldownBars = input.int(4, "Attack volume cooldown bars"', text)
            self.assertIn('minSameSideSignalSeparationAtr = input.float(1.20, "Same-side signal separation (x ATR)"', text)
            self.assertIn('failedBreakCloseBackAtr = input.float(0.10, "Failed break close-back (x ATR)"', text)
            self.assertIn('structureBreakAcceptBars = input.int(3, "Structure break accept bars"', text)
            self.assertIn('structureBreakAcceptCloseAtr = input.float(0.15, "Structure break accept close (x ATR)"', text)
            self.assertIn('todayStructHighZoneLow', text)
            self.assertIn('todayStructLowZoneHigh', text)
            self.assertIn('lowBeforeStructHigh', text)
            self.assertIn('highBeforeStructLow', text)
            self.assertIn('lowSinceStructHighBar', text)
            self.assertIn('highSinceStructLowBar', text)
            self.assertIn('visualLegBps(float legValue, float basisPrice) =>', text)
            self.assertIn('visualLegRatio(float numerator, float denominator) =>', text)
            self.assertIn('volumeRatio = not na(volumeMa) and volumeMa > 0.0 ? barVolume / volumeMa : 1.0', text)
            self.assertIn('volumeAttackUp = volumeExpanded and close > open and closePosition >= attackClosePosThreshold', text)
            self.assertIn('volumeAttackDown = volumeExpanded and close < open and closePosition <= 1.0 - attackClosePosThreshold', text)
            self.assertIn('"left_leg_too_small"', text)
            self.assertIn('"middle_leg_too_small"', text)
            self.assertIn('"right_retest_too_small"', text)
            self.assertIn('"volume_attack_up_no_short"', text)
            self.assertIn('"volume_attack_down_no_long"', text)
            self.assertIn('"low_volume_retest_ok"', text)
            self.assertIn('"high_volume_failed_break_ok"', text)
            self.assertIn('"low_volume_momentum_decay_ok"', text)
            self.assertIn('"day_range_not_formed"', text)
            self.assertIn('"momentum_not_decayed"', text)
            self.assertIn('"momentum_decay_not_continuous"', text)
            self.assertIn('"attack_volume_cooling"', text)
            self.assertIn('"no_weak_close_confirm"', text)
            self.assertIn('"retest_too_slow"', text)
            self.assertIn('"micro_structure_too_close"', text)

        self.assertIn('topPredictAllowed = topPredictContext and smallStopTrialAllowed', source)
        self.assertIn('bottomPredictAllowed = bottomPredictContext and smallStopTrialAllowed', source)
        self.assertIn('float topBasis = todayStructHigh', source)
        self.assertIn('float bottomBasis = todayStructLow', source)
        self.assertIn('topSweepFailed = confirmed and topRightShoulder and high > topZoneHigh + tickBuffer and close < topZoneHigh - atr * failedBreakCloseBackAtr', source)
        self.assertIn('bottomSweepFailed = confirmed and bottomRightShoulder and low < bottomZoneLow - tickBuffer and close > bottomZoneLow + atr * failedBreakCloseBackAtr', source)
        self.assertIn('topLeftLegBps = visualLegBps(topLeftLeg, todayStructHigh)', source)
        self.assertIn('topMiddleRetraceRatio = visualLegRatio(topMiddleLeg, topLeftLeg)', source)
        self.assertIn('topRightRetestRatio = visualLegRatio(topRightLeg, topMiddleLeg)', source)
        self.assertIn('topMiddleSwingReady = not na(lowSinceStructHighBar) and topVisualLeftOk and topMiddleRetraceRatio >= minVisualMiddleRetraceRatio', source)
        self.assertIn('topRightRetestReady = topMiddleSwingReady and topRightRetestRatio >= minVisualRightRetestRatio', source)
        self.assertIn('topRetestFastEnough = topRightRetestReady and bar_index > lowSinceStructHighBar and bar_index - lowSinceStructHighBar <= maxRetestAfterMiddleSwingBars', source)
        self.assertIn('topAnchorReady = dayRangeClear and topAnchorAgeBars >= probeMinAnchorAgeBars and topRetestFastEnough', source)
        self.assertIn('topMomentumDecayRatio = not na(topMiddleSpeed) and topMiddleSpeed > 0.0 ? topRightSpeed / topMiddleSpeed : na', source)
        self.assertIn('topDecayStreak := topRightShoulder and topRetestBand and upperNoVolumeExpansion and topMomentumDecayed ? topDecayStreak + 1 : 0', source)
        self.assertIn('bool topMomentumDecayReady = topDecayStreak >= exhaustionDecayConfirmBars', source)
        self.assertIn('upperExhaustionZone = dayRangeClear and topRightShoulder and topRetestBand and upperNoVolumeExpansion and topMomentumDecayReady', source)
        self.assertIn('upperExhaustionConfirmedShort = confirmed and upperExhaustionZone and upperWeakClose', source)
        self.assertIn('topHighVolumeFailedBreak = topSweepFailed and volumeExpanded', source)
        self.assertIn('topPredictVolumeOk = not attackUpCooling', source)
        self.assertIn('topConfirmVolumeOk = topHighVolumeFailedBreak or not attackUpCooling or upperExhaustionConfirmedShort', source)
        self.assertIn('topPredictContext = (topFirstExhaustion or topRightShoulder) and topCounterTrendAllowed and topPredictEntry >= close and topPredictEntry - close <= atr * predictionArmDistanceAtr and topSignalGapOk and topPredictVolumeOk', source)
        self.assertIn('topConfirmContext = topConfirmedShort and (topCounterTrendAllowed or upperExhaustionConfirmedShort) and topSignalGapOk and topConfirmVolumeOk', source)
        self.assertIn('bottomLeftLegBps = visualLegBps(bottomLeftLeg, todayStructLow)', source)
        self.assertIn('bottomMiddleRetraceRatio = visualLegRatio(bottomMiddleLeg, bottomLeftLeg)', source)
        self.assertIn('bottomRightRetestRatio = visualLegRatio(bottomRightLeg, bottomMiddleLeg)', source)
        self.assertIn('bottomMiddleSwingReady = not na(highSinceStructLowBar) and bottomVisualLeftOk and bottomMiddleRetraceRatio >= minVisualMiddleRetraceRatio', source)
        self.assertIn('bottomRightRetestReady = bottomMiddleSwingReady and bottomRightRetestRatio >= minVisualRightRetestRatio', source)
        self.assertIn('bottomRetestFastEnough = bottomRightRetestReady and bar_index > highSinceStructLowBar and bar_index - highSinceStructLowBar <= maxRetestAfterMiddleSwingBars', source)
        self.assertIn('bottomAnchorReady = dayRangeClear and bottomAnchorAgeBars >= probeMinAnchorAgeBars and bottomRetestFastEnough', source)
        self.assertIn('bottomMomentumDecayRatio = not na(bottomMiddleSpeed) and bottomMiddleSpeed > 0.0 ? bottomRightSpeed / bottomMiddleSpeed : na', source)
        self.assertIn('bottomDecayStreak := bottomRightShoulder and bottomRetestBand and lowerNoVolumeExpansion and bottomMomentumDecayed ? bottomDecayStreak + 1 : 0', source)
        self.assertIn('bool bottomMomentumDecayReady = bottomDecayStreak >= exhaustionDecayConfirmBars', source)
        self.assertIn('lowerExhaustionZone = dayRangeClear and bottomRightShoulder and bottomRetestBand and lowerNoVolumeExpansion and bottomMomentumDecayReady', source)
        self.assertIn('lowerExhaustionConfirmedLong = confirmed and lowerExhaustionZone and lowerStrongClose', source)
        self.assertIn('bottomHighVolumeFailedBreak = bottomSweepFailed and volumeExpanded', source)
        self.assertIn('bottomPredictVolumeOk = not attackDownCooling', source)
        self.assertIn('bottomConfirmVolumeOk = bottomHighVolumeFailedBreak or not attackDownCooling or lowerExhaustionConfirmedLong', source)
        self.assertIn('bottomPredictContext = (bottomFirstExhaustion or bottomRightShoulder) and bottomCounterTrendAllowed and bottomPredictEntry <= close and close - bottomPredictEntry <= atr * predictionArmDistanceAtr and bottomSignalGapOk and bottomPredictVolumeOk', source)
        self.assertIn('bottomConfirmContext = bottomConfirmedLong and (bottomCounterTrendAllowed or lowerExhaustionConfirmedLong) and bottomSignalGapOk and bottomConfirmVolumeOk', source)
        self.assertIn('lastVolumeAttackUpBar := bar_index', source)
        self.assertIn('lastVolumeAttackDownBar := bar_index', source)
        self.assertIn('attackUpCooling = not na(barsSinceAttackUp) and barsSinceAttackUp <= attackVolumeCooldownBars', source)
        self.assertIn('upperBreakAccepting = not na(todayStructHighZoneHigh) and not na(todayStructLow) and close > todayStructHighZoneHigh + atr * structureBreakAcceptCloseAtr', source)
        self.assertIn('lowerBreakAccepting = not na(todayStructLowZoneLow) and not na(todayStructHigh) and close < todayStructLowZoneLow - atr * structureBreakAcceptCloseAtr', source)
        self.assertIn('upperStructureBreakAccepted := true', source)
        self.assertIn('lowerStructureBreakAccepted := true', source)
        self.assertIn('selectedSameSideSignalFarEnough', source)
        self.assertIn('selectedAnyPlanFarEnough', source)
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
            'jsonStr("trend_regime", trendRegime)',
            'jsonStr("dominant_trend_side", dominantTrendSide)',
            'jsonBool("day_range_formed", dayRangeFormed)',
            'jsonNum("day_range_atr", dayRangeAtr)',
            'jsonStr("level_role", selectedLevelRole)',
            'jsonStr("exhaustion_side", selectedExhaustionSide)',
            'jsonBool("exhaustion_confirmed", selectedExhaustionConfirmed)',
            'jsonStr("exhaustion_reason", selectedExhaustionReason)',
            'jsonNum("momentum_decay_ratio", selectedMomentumDecayRatio)',
            'jsonNum("momentum_decay_streak", selectedMomentumDecayStreak)',
            'jsonNum("momentum_decay_confirm_bars", selectedMomentumDecayConfirmBars)',
            'jsonBool("attack_volume_cooling", selectedAttackVolumeCooling)',
            'jsonNum("attack_volume_cooldown_bars", selectedAttackVolumeCooldownBars)',
            'jsonBool("structure_break_accepted", selectedStructureBreakAccepted)',
            'jsonNum("structure_break_accept_bars", selectedStructureBreakAcceptBars)',
            'jsonNum("prior_level_distance_atr", selectedPriorLevelDistanceAtr)',
            'jsonNum("middle_retrace_ratio", selectedProbeMiddleRatio)',
            'jsonNum("right_retest_ratio", selectedProbeRightRatio)',
            'jsonNum("any_plan_gap_bars", selectedAnyPlanGapBars)',
            'jsonNum("any_plan_distance_atr", selectedAnyPlanDistanceAtr)',
            'jsonStr("probe_pattern", selectedProbePattern)',
            'jsonStr("probe_filter_reason", selectedProbeFilterReason)',
            'jsonNum("probe_retest_gap_bars", selectedProbeRetestGapBars)',
            'jsonNum("visual_left_leg_bps", selectedProbeLeftLegBps)',
            'jsonNum("visual_middle_retrace_ratio", selectedProbeMiddleRatio)',
            'jsonNum("visual_right_retest_ratio", selectedProbeRightRatio)',
            'jsonNum("volume_ratio", selectedVolumeRatio)',
            'jsonStr("volume_reason", selectedVolumeReason)',
            'jsonNum("close_position", selectedClosePosition)',
            'jsonBool("volume_attack_up", volumeAttackUp)',
            'jsonBool("volume_attack_down", volumeAttackDown)',
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
        self.assertIn('planReplaceMinMoveAtr = input.float(0.60, "Plan replace min move (x ATR)"', source)
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
        self.assertIn('exhaustionProtectMinMfeR = input.float(1.20, "Exhaustion protect min MFE (R)"', source)
        self.assertIn('exhaustionExitNow = activeMfeR >= exhaustionProtectMinMfeR', source)
        self.assertIn('activeManualExitReason := "exhaustion_take_profit"', source)
        self.assertIn('"upper_exhaustion_profit_protect"', source)
        self.assertIn('"lower_exhaustion_profit_protect"', source)
        self.assertIn('jsonStr("profit_protect_reason", reason)', source)
        self.assertIn('jsonRequestedSides(requestedSides)', source)
        self.assertIn('fmtSignedMoneyCore(float value) =>', source)
        self.assertIn('fmtSignedRCore(float value) =>', source)
        self.assertIn('fmtPrice(float value) =>', source)
        self.assertIn('str.tostring(value, format.mintick)', source)
        self.assertIn('exitReasonDisplayNameCore(string reason) =>', source)
        self.assertIn('entryOrderComment(int direction, float entryPrice, float qty, float stopPrice, float targetPrice, float runnerPrice) =>', source)
        self.assertIn('exitOrderComment(string reason, float exitPrice) =>', source)
        self.assertIn('(direction == 1 ? "L " : "S ") + fmtPrice(entryPrice) + " Q"', source)
        self.assertNotIn('" SL" + fmt(stopPrice) + " TP" + fmt(targetPrice) + " R" + fmt(runnerPrice)', source)
        self.assertIn('reason == "runner_stop" ? "RUN"', source)
        self.assertIn('exitReasonDisplayNameCore(reason) + " " + fmtSignedMoneyCore(pnlValue) + " X" + fmtPrice(exitPrice)', source)
        self.assertIn('strategy.entry("TV-L", strategy.long, qty=selectedQty, limit=selectedEntry, comment=entryOrderComment', source)
        self.assertIn('strategy.entry("TV-S", strategy.short, qty=selectedQty, limit=selectedEntry, comment=entryOrderComment', source)
        self.assertIn('comment_profit=exitOrderComment("take_profit", activeTarget)', source)
        self.assertIn('comment_loss=exitOrderComment("stop_loss", activeStop)', source)
        self.assertIn('comment_loss=exitOrderComment(activeRunnerActive and activeStop > activeEntry ? "runner_stop" : "stop_loss", activeStop)', source)
        self.assertIn('comment_loss=exitOrderComment(activeRunnerActive and activeStop < activeEntry ? "runner_stop" : "stop_loss", activeStop)', source)
        self.assertIn('strategy.close(orderIdForDirection(activeDirection), comment=exitOrderComment("force_flat_eod", close))', source)
        self.assertNotIn('comment_profit="take_profit"', source)
        self.assertNotIn('comment_loss="stop_loss"', source)
        self.assertIn('strategy.exit("TV-L-RISK", from_entry="TV-L", stop=activeStop, limit=activeTarget', source)
        self.assertIn('strategy.exit("TV-S-RISK", from_entry="TV-S", stop=activeStop, limit=activeTarget', source)

    def test_display_explains_event_flow_and_four_plan_lines(self):
        display = self.display_source

        self.assertIn('displayPreset = input.string("核心交易流程", "Display preset"', display)
        self.assertIn('showStatusTable = input.bool(false, "Show status table"', display)
        self.assertIn('showFractalMarkers = input.bool(false, "Show fractal markers"', display)
        self.assertIn('showEmaCrossMarkers = input.bool(false, "Show EMA20/50 cross markers"', display)
        self.assertIn('labelDetailMode = input.string("compact_tooltip", "Flow label detail mode"', display)
        self.assertIn('labelTextMode = input.string("auto", "Label text color"', display)
        self.assertIn('flowLabelText(string compactText, string detailText) =>', display)
        self.assertIn('flowLabelTooltip(string detailText) =>', display)
        self.assertIn('flowLabelSize() =>', display)
        self.assertIn('labelTextColor(string state, int direction) =>', display)
        self.assertIn('tableBgColor(int row) =>', display)
        self.assertIn('tableTextColor(int row) =>', display)
        self.assertIn('riskDollars(float entryPrice, float stopPrice, float qty) =>', display)
        self.assertIn('pnlDollars(float entryPrice, float levelPrice, int direction, float qty) =>', display)
        self.assertIn('moneyText(float value) =>', display)
        self.assertIn('planMoneyText(float entryPrice, float stopPrice, float runnerPrice, float targetPrice, int direction, float qty) =>', display)
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
        self.assertIn('plot(showStructureGuideLines ? todayStructHighZoneHigh : na, "Struct High Zone High"', display)
        self.assertIn('plot(showStructureGuideLines ? todayStructLowZoneLow : na, "Struct Low Zone Low"', display)
        self.assertIn('selectedProbeRetestGapBars', display)
        self.assertIn('selectedProbeLeftLegBps', display)
        self.assertIn('selectedProbeMiddleRatio', display)
        self.assertIn('selectedProbeRightRatio', display)
        self.assertIn('selectedVolumeReason', display)
        self.assertIn('selectedVolumeRatio', display)
        self.assertIn('量能 vol/MA " + fmt(selectedVolumeRatio)', display)
        self.assertIn('selectedLevelRole', display)
        self.assertIn('selectedMomentumDecayRatio', display)
        self.assertIn('selectedMomentumDecayStreak', display)
        self.assertIn('selectedAttackVolumeCooling', display)
        self.assertIn('位置角色 " + selectedLevelRole', display)
        self.assertIn('exhaustionWatchTop', display)
        self.assertIn('前高二测：多单止盈区，等待确认做空', display)
        self.assertIn('衰竭保护', display)
        self.assertIn('structureBreakAcceptedEvent', display)
        self.assertIn('站稳前高：更新今日结构高', display)
        self.assertIn('volumeBlockedTop', display)
        self.assertIn('volumeBlockedBottom', display)
        self.assertIn('放量攻击后仍在冷却，不做预测逆向', display)
        self.assertIn('连续衰减 " + fmt(exhaustionWatchTop ? topDecayStreak * 1.0 : bottomDecayStreak * 1.0)', display)
        self.assertIn('"\\n三段结构 L" + fmt(selectedProbeLeftLegBps)', display)
        self.assertIn('str.tostring(maxRetestAfterMiddleSwingBars)', display)
        self.assertIn('同向距离 " + fmt(selectedSameSideSignalDistanceAtr)', display)
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
        self.assertIn('var label currentEntryLabel = na', display)
        self.assertIn('var label currentStopLabel = na', display)
        self.assertIn('var label currentRunnerLabel = na', display)
        self.assertIn('var label currentTargetLabel = na', display)
        self.assertIn('currentStopLabel := label.new(lineEnd, shadowStop', display)
        self.assertIn('label.set_text(currentStopLabel', display)
        self.assertIn('"金额"', display)
        self.assertIn('"SL " + moneyText(', display)
        self.assertIn('"黄/红/蓝/绿"', display)
        self.assertIn('"Entry / SL / Runner / SafetyTP"', display)


if __name__ == "__main__":
    unittest.main()
