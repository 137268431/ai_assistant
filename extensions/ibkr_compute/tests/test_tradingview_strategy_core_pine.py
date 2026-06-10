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
        self.assertIn("bool longRunnerCandidate = setupUsesRunner(longSetup) and not setupIsMr(longSetup)", source)
        self.assertIn("float longRunnerActivationCandidate = longRunnerCandidate ? calcRunnerActivationTarget(longEntryCandidate, longStopCandidate, 1) : na", source)
        self.assertIn("float longTargetCandidate = setupIsMr(longSetup) and longMrMeanTargetAvailable ? longMrMeanTargetRaw : longRunnerCandidate ? calcRunnerSafetyTarget(longEntryCandidate, longStopCandidate, 1) : longHardTargetCandidate", source)
        self.assertIn("bool shortRunnerCandidate = setupUsesRunner(shortSetup) and not setupIsMr(shortSetup)", source)
        self.assertIn("float shortRunnerActivationCandidate = shortRunnerCandidate ? calcRunnerActivationTarget(shortEntryCandidate, shortStopCandidate, -1) : na", source)
        self.assertIn("float shortTargetCandidate = setupIsMr(shortSetup) and shortMrMeanTargetAvailable ? shortMrMeanTargetRaw : shortRunnerCandidate ? calcRunnerSafetyTarget(shortEntryCandidate, shortStopCandidate, -1) : shortHardTargetCandidate", source)
        self.assertIn("float runnerActivationPrice = longRunnerActivationCandidate", source)
        self.assertIn("float runnerActivationPrice = shortRunnerActivationCandidate", source)
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
        self.assertIn('jsonStr("target_role", mrSetup ? "mr_mean_tp" : runnerMode ? "safety_tp" : "hard_tp")', source)
        self.assertIn('jsonStr("mr_target_mode", mrSetup ? "mean_reversion_mixed" : "none")', source)
        self.assertIn('jsonNum("mr_mean_target_price", mrSetup ? targetPrice : na)', source)
        self.assertIn('jsonStr("mr_regime", mrRegimeValue)', source)
        self.assertIn('jsonBool("mr_regime_allowed", mrRegimeAllowedValue)', source)
        self.assertIn('jsonNum("mr_sd_slope_atr", sdSlopeAtr)', source)
        self.assertIn("buildEntryPayload(entryEventId, activeSignalId, activeTradeGroupId, posId, \"long\", longSetup, longReason, qty, longReferenceEntry, entryPrice, submittedLimitPrice", source)
        self.assertIn("buildEntryPayload(entryEventId, activeSignalId, activeTradeGroupId, posId, \"short\", shortSetup, shortReason, qty, shortReferenceEntry, entryPrice, submittedLimitPrice", source)
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
        self.assertIn("color statusColor = activePendingEntry ? color.new(color.orange, 0) : activeFilledPosition ? flowStatusColor", source)
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

    def test_profit_space_filter_blocks_small_net_roi_or_target_space(self):
        source = self.source

        self.assertIn('useProfitSpaceFilter = input.bool(true, "Block insufficient profit space", group="05 Risk")', source)
        self.assertIn('minNetProfitForEntry = input.float(100.0, "Minimum net profit ($)", step=5.0, minval=0.0, group="05 Risk")', source)
        self.assertIn('minNetRoiPctForEntry = input.float(2.0, "Minimum net ROI (%)", step=0.1, minval=0.0, group="05 Risk")', source)
        self.assertIn("float effectiveMinNetProfitForEntry = math.max(minNetProfitForEntry, profitSpaceMinNetProfitFloor)", source)
        self.assertIn("float effectiveMinNetRoiPctForEntry = math.max(minNetRoiPctForEntry, profitSpaceMinNetRoiPctFloor)", source)
        self.assertIn('maxCostPctOfReward = input.float(15.0, "Maximum cost / reward (%)", step=1.0, minval=0.0, group="05 Risk")', source)
        self.assertIn('minTargetAtrMultipleForEntry = input.float(2.0, "Minimum target distance (x ATR)", step=0.25, minval=0.0, group="05 Risk")', source)
        self.assertIn('netProfit < effectiveMinNetProfitForEntry ? "net_profit_below_min"', source)
        self.assertIn('netRoiPct < effectiveMinNetRoiPctForEntry ? "net_roi_below_min"', source)
        self.assertIn("profitSpaceEntryAllowed(float netProfit, float netRoiPct, float costPct, float targetAtrMultiple)", source)
        self.assertIn('blockLongReason := "profit_space_too_small"', source)
        self.assertIn('blockShortReason := "profit_space_too_small"', source)
        self.assertIn('reason == "profit_space_too_small" ? "等更大空间"', source)
        self.assertIn('"预计净利 " + fmtSignedMoney(netProfit) + " / 占用ROI " + fmtSignedPct(netRoiPct) + " / 成本/毛利 "', source)
        self.assertIn('"门槛 净利 >= " + fmtMoney(effectiveMinNetProfitForEntry)', source)
        self.assertIn('" / 占用ROI >= " + str.tostring(effectiveMinNetRoiPctForEntry', source)
        self.assertIn('成本/毛利 <= " + str.tostring(maxCostPctOfReward', source)
        self.assertIn('TP >= " + str.tostring(minTargetAtrMultipleForEntry', source)
        self.assertIn("profitSpaceNetProfitCompareText(float netProfit)", source)
        self.assertIn('"净利 " + fmtSignedMoney(netProfit) + operatorText + fmtMoney(effectiveMinNetProfitForEntry) + gapText', source)
        self.assertIn('netProfit < effectiveMinNetProfitForEntry ? "，差 " + fmtMoney(effectiveMinNetProfitForEntry - netProfit)', source)
        self.assertIn("profitSpaceNetRoiCompareText(float netRoiPct)", source)
        self.assertIn("profitSpaceCostCompareText(float costPct)", source)
        self.assertIn("profitSpaceTargetAtrCompareText(float targetAtrMultiple)", source)
        self.assertIn('"\\n获利空间不足\\n" + profitSpaceNetProfitCompareText(netProfit)', source)
        self.assertIn('profitSpaceBlockedStatusText(float netProfit, float netRoiPct, float costPct, float targetAtrMultiple)', source)
        self.assertIn('profitSpacePayload(direction, qty, entryPrice, targetPrice, targetCheckpoint)', source)
        self.assertIn('jsonNum("entry_notional", entryNotionalValue)', source)
        self.assertIn('jsonNum("expected_net_profit", netProfitValue)', source)
        self.assertIn('jsonNum("expected_net_roi_pct", netRoiPctValue)', source)
        self.assertIn('jsonNum("cost_pct_of_reward", costPctValue)', source)
        self.assertIn('jsonNum("target_distance_atr", targetAtrValue)', source)
        self.assertIn('jsonBool("profit_space_entry_allowed", entryAllowedValue)', source)
        self.assertIn('jsonNum("min_net_profit_for_entry", effectiveMinNetProfitForEntry)', source)
        self.assertIn('jsonNum("min_net_roi_pct_for_entry", effectiveMinNetRoiPctForEntry)', source)

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

    def test_signal_diagnostics_draw_filtered_candidates_without_alerts(self):
        source = self.source
        diagnostic_start = source.index("bool diagnosticLongCandidate")
        diagnostic_end = source.index("if enterLong")
        diagnostic_section = source[diagnostic_start:diagnostic_end]

        self.assertIn('showSignalDiagnostics = input.bool(true, "Show raw/filtered signal diagnostics", group="08 Display")', source)
        self.assertIn("bool diagnosticLongCandidate = canTrade and flatForEntry and longRaw and not enterLong", source)
        self.assertIn("bool diagnosticShortCandidate = canTrade and flatForEntry and shortRaw and not enterShort", source)
        self.assertIn("bool blockedNow = diagnosticLongCandidate or diagnosticShortCandidate", source)
        self.assertIn("bool blockedVisualNow = showFlowLabels and showSignalDiagnostics and blockedNow", source)
        self.assertIn('string blockedReason = blockedReasonRaw == "" ? "candidate_filtered" : blockedReasonRaw', source)
        self.assertIn('"信号过滤\\n" + directionDisplayName(blockedDirection)', source)
        self.assertIn('"\\n原因: " + filterDisplayName(blockedReason)', source)
        self.assertIn('reason == "candidate_filtered" ? "候选未执行"', source)
        self.assertIn('reason == "candidate_filtered" ? "候选未进入下单分支，检查窗口/方向/数量条件"', source)
        self.assertIn("string blockedAnchorText = structuralAnchorMode ?", source)
        self.assertIn("bool blockedProfitSpaceEntryBlocked = blockedDirection == \"long\" ? longProfitSpaceEntryBlocked : shortProfitSpaceEntryBlocked", source)
        self.assertIn("string blockedProfitSpaceFailureReason = blockedDirection == \"long\" ? profitSpaceFailureReason", source)
        self.assertIn("string blockedProfitLabelText = blockedProfitSpaceText == \"\" ? \"\\n\" + blockedProfitSummary : blockedProfitSpaceText", source)
        self.assertIn("string blockedVisualKey = blockedDirection + \":\" + blockedSetup + \":\" + blockedReason + \":\" + blockedComponentText", source)
        self.assertIn("mtfStatusForDirection(blockedDirection)", source)
        self.assertIn("blockedProfitSpaceEntryBlocked ? blockedProfitSpaceFailureReason : \"none\"", source)
        self.assertIn("blockedMrRegimeText + blockedProfitLabelText + blockedAnchorText", source)
        self.assertNotIn("blockedComponentText + \":\" + blockedProfitSpaceText + \":\" + blockedAnchorText", source)
        self.assertNotIn("alert(", diagnostic_section)

    def test_sd_window_resets_only_on_activation_events(self):
        source = self.source

        self.assertIn("bool lowerActivationEvent = canOpenEntry and sdLowerHit and (not lowerWindowActive or lowerWindowUsed or lowerWindowExpired or na(lowerWindowStart))", source)
        self.assertIn("bool upperActivationEvent = canOpenEntry and sdUpperHit and (not upperWindowActive or upperWindowUsed or upperWindowExpired or na(upperWindowStart))", source)
        self.assertIn("if lowerActivationEvent\n    lowerWindowActive := true", source)
        self.assertIn("if upperActivationEvent\n    upperWindowActive := true", source)
        self.assertNotIn("if sdLowerHit\n    lowerWindowActive := true", source)
        self.assertNotIn("if sdUpperHit\n    upperWindowActive := true", source)

    def test_entry_logic_requires_window_and_two_of_three_direction_components(self):
        source = self.source

        self.assertIn("directionComponentCount(bool fractalReady, bool divReady, bool emaCrossReady)", source)
        self.assertIn("directionComponentsReady(bool fractalReady, bool divReady, bool emaCrossReady)", source)
        self.assertIn("int longTrendUpperDirectionComponents = directionComponentCount(upperBullFractalFresh, bullDivFresh, upperBullEmaCrossFresh)", source)
        self.assertIn("int longMrLowerRawDirectionComponents = directionComponentCount(lowerBullFractalSeen, bullDivSeen, lowerBullEmaCrossSeen)", source)
        self.assertIn("int longMrLowerDirectionComponents = directionComponentCount(lowerBullFractalFresh, bullDivFresh, lowerBullEmaCrossFresh)", source)
        self.assertIn("int shortMrUpperRawDirectionComponents = directionComponentCount(upperBearFractalSeen, bearDivSeen, upperBearEmaCrossSeen)", source)
        self.assertIn("int shortMrUpperDirectionComponents = directionComponentCount(upperBearFractalFresh, bearDivFresh, upperBearEmaCrossFresh)", source)
        self.assertIn("int shortTrendLowerDirectionComponents = directionComponentCount(lowerBearFractalFresh, bearDivFresh, lowerBearEmaCrossFresh)", source)
        self.assertIn("pullbackNearResistance(float barHigh, float barClose, float levelPrice, float atrValue)", source)
        self.assertIn("float recentHigh = ta.highest(high[1], entryLevelLookbackBars)", source)
        self.assertIn('lowerBearPullbackLineNow := pullbackNearResistance(high, close, emaFast, atrRaw) ? appendSource(lowerBearPullbackLineNow, "pullback_ema20") : lowerBearPullbackLineNow', source)
        self.assertIn('lowerBearPullbackLineNow := pullbackNearResistance(high, close, recentHigh, atrRaw) ? appendSource(lowerBearPullbackLineNow, "pullback_recent_high") : lowerBearPullbackLineNow', source)
        self.assertNotIn("pullback_signal_mid", source)
        self.assertIn("bool lowerBearPullbackNow = confirmed and lowerWindowValid and lowerBearPullbackLineNow != \"\"", source)
        self.assertIn("bool lowerBearPullbackFresh = lowerBearPullbackSeen and componentFresh(lowerBearPullbackBar)", source)
        self.assertIn("bool lowerBearTrendTriggerFresh = lowerBearTouchFresh or lowerBearPullbackFresh", source)
        self.assertIn("bool lowerBearTrendTriggerSeen = lowerBearTouchSeen or lowerBearPullbackSeen", source)
        self.assertIn("bool setupLongTrendUpper = upperWindowValid and upperBullTouchFresh and longTrendUpperDirectionComponents >= 2", source)
        self.assertIn("bool setupLongMrLower = lowerMrWindowValid and longMrLowerDirectionComponents >= 2", source)
        self.assertIn("bool setupShortMrUpper = upperMrWindowValid and shortMrUpperDirectionComponents >= 2", source)
        self.assertIn("bool setupShortTrendLowerTouch = lowerWindowValid and lowerBearTouchFresh and shortTrendLowerDirectionComponents >= 2", source)
        self.assertIn("bool setupShortTrendLowerPullback = lowerWindowValid and lowerBearPullbackFresh and shortTrendLowerDirectionComponents >= 2", source)
        self.assertIn("bool setupShortTrendLower = setupShortTrendLowerTouch or setupShortTrendLowerPullback", source)
        self.assertIn('setupShortTrendLowerPullback ? "SD lower trend-continuation short: pullback resistance + 2 of 3 bear direction components"', source)
        self.assertIn("int scoreLongTrendUpper = math.min(100,", source)
        self.assertIn("int scoreLongMrLower = math.min(100,", source)
        self.assertIn("int scoreShortMrUpper = math.min(100,", source)
        self.assertIn("int scoreShortTrendLower = math.min(100, (lowerWindowValid ? 25 : 0) + (lowerBearTrendTriggerSeen ? 25 : 0)", source)
        self.assertIn('missingComponents := not lowerBearTrendTriggerSeen ? appendSource(missingComponents, "ema_bear_touch") : missingComponents', source)
        self.assertIn('missingText := str.replace_all(missingText, "ema_bear_touch", "空EMA/回抽")', source)

    def test_mr_regime_filter_anchor_and_mean_target(self):
        source = self.source

        self.assertIn('dtpMrFilterMode = input.string("严格：只放转弱", "DTP MR filter mode", options=["严格：只放转弱", "平衡：只挡初期", "关闭：不挡MR"], group="07 TV Primary Events")', source)
        self.assertIn('mrRangeMaxSdSlopeAtr = input.float(0.10, "MR range max SD slope (x ATR)"', source)
        self.assertIn('dtpMrBlocksPhase(string mode, string phase) =>\n    mode == "关闭：不挡MR" ? false : mode == "严格：只放转弱" ? phase != "weakening" : phase == "early"', source)
        self.assertIn("mrDtpBlocked(string direction, int dtpDirection, string phase, string mode, bool enabled)", source)
        self.assertIn("mrRegimeText(string direction, int dtpDirection, string phase, string mode, bool enabled, float sdSlopeAtrValue)", source)
        self.assertIn('blocked ? "countertrend_blocked" : adverse and phase == "weakening" ? "trend_weakening"', source)
        self.assertIn('mrRegimeDisplayName(string regime)', source)
        self.assertIn("float sdSlopeAtr = atrRaw > 0.0 ? math.abs(sdSlope) / atrRaw : 999999.0", source)
        self.assertIn('string longMrRegime = mrRegimeText("long", dtpDir, dtpPhase, dtpMrFilterMode, useDtpFilter, sdSlopeAtr)', source)
        self.assertIn('string shortMrRegime = mrRegimeText("short", dtpDir, dtpPhase, dtpMrFilterMode, useDtpFilter, sdSlopeAtr)', source)
        self.assertIn('bool longMrDtpBlocked = mrDtpBlocked("long", dtpDir, dtpPhase, dtpMrFilterMode, useDtpFilter)', source)
        self.assertIn('bool shortMrDtpBlocked = mrDtpBlocked("short", dtpDir, dtpPhase, dtpMrFilterMode, useDtpFilter)', source)
        self.assertIn('bool longMrWeakeningReversalOk = longMrRegime != "trend_weakening" or (lowerBullEmaCrossFresh and (lowerBullFractalFresh or bullDivFresh))', source)
        self.assertIn('bool shortMrWeakeningReversalOk = shortMrRegime != "trend_weakening" or (upperBearEmaCrossFresh and (upperBearFractalFresh or bearDivFresh))', source)
        self.assertIn("bool longMrStructuralAnchorQualified = not structuralAnchorMode or anchorNear(longEntryCandidate, lastPivotLow, atrRaw)", source)
        self.assertIn("bool shortMrStructuralAnchorQualified = not structuralAnchorMode or anchorNear(shortEntryCandidate, lastPivotHigh, atrRaw)", source)
        self.assertIn("float longMrMeanTargetRaw = nearestResistanceAbove(longEntryCandidate, sdReg, emaFast, emaSlow", source)
        self.assertIn("float shortMrMeanTargetRaw = nearestSupportBelow(shortEntryCandidate, sdReg, emaFast, emaSlow", source)
        self.assertIn("float longTargetCandidate = setupIsMr(longSetup) and longMrMeanTargetAvailable ? longMrMeanTargetRaw", source)
        self.assertIn("float shortTargetCandidate = setupIsMr(shortSetup) and shortMrMeanTargetAvailable ? shortMrMeanTargetRaw", source)
        self.assertIn('else if longSetup == "mr_sdLower" and longMrDtpBlocked', source)
        self.assertIn('blockLongReason := "mr_dtp_phase_blocked"', source)
        self.assertIn('blockLongReason := "mr_reversal_confirm_missing"', source)
        self.assertIn('blockLongReason := "mr_anchor_unqualified"', source)
        self.assertIn('blockLongReason := "mr_target_space_too_small"', source)
        self.assertIn('else if shortSetup == "mr_sdUpper" and shortMrDtpBlocked', source)
        self.assertIn('blockShortReason := "mr_dtp_phase_blocked"', source)
        self.assertIn('blockShortReason := "mr_reversal_confirm_missing"', source)
        self.assertIn('blockShortReason := "mr_anchor_unqualified"', source)
        self.assertIn('blockShortReason := "mr_target_space_too_small"', source)
        self.assertNotIn("else if useDtpFilter and setupLongMrLower and dtpDir == -1", source)
        self.assertNotIn("else if useDtpFilter and setupShortMrUpper and dtpDir == 1", source)

    def test_post_sd_reversal_trend_setups_do_not_require_active_mr_window(self):
        source = self.source

        self.assertIn('postSdReversalBars = input.int(30, "Post-SD reversal bars"', source)
        self.assertIn('postSdReversalMaxMinutes = input.int(60, "Post-SD reversal max minutes"', source)
        self.assertIn("var int upperSdExtremeBar = na", source)
        self.assertIn("var int lowerSdExtremeBar = na", source)
        self.assertIn("var bool upperPostReversalUsed = false", source)
        self.assertIn("var bool lowerPostReversalUsed = false", source)
        self.assertIn("if canOpenEntry and sdUpperHit\n    upperSdExtremeBar := bar_index", source)
        self.assertIn("if canOpenEntry and sdLowerHit\n    lowerSdExtremeBar := bar_index", source)
        self.assertIn("bool upperSdExtremeFresh = not na(upperSdExtremeBar)", source)
        self.assertIn("bool lowerSdExtremeFresh = not na(lowerSdExtremeBar)", source)

        self.assertIn('setup == "trend_sdUpperBreakdown" ? "SD上轨反转顺势空"', source)
        self.assertIn('setup == "trend_sdLowerBreakout" ? "SD下轨反转顺势多"', source)
        self.assertIn('setup == "trend_sdUpperBreakdown" or setup == "trend_sdLowerBreakout"', source)
        self.assertIn('setup == "trend_sdUpper" or setup == "trend_sdLower" or setup == "trend_sdUpperBreakdown" or setup == "trend_sdLowerBreakout"', source)
        self.assertIn("int longPostLowerBreakoutDirectionComponents = directionComponentCount(lowerBullFractalFresh, bullDivFresh, lowerBullEmaCrossFresh)", source)
        self.assertIn("int shortPostUpperBreakdownDirectionComponents = directionComponentCount(upperBearFractalFresh, bearDivFresh, upperBearEmaCrossFresh)", source)
        self.assertIn("bool setupLongPostLowerBreakout = lowerSdExtremeFresh and not lowerPostReversalUsed and emaFast > emaSlow and close > emaSlow and lowerBullEmaCrossFresh", source)
        self.assertIn("bool setupShortPostUpperBreakdown = upperSdExtremeFresh and not upperPostReversalUsed and emaFast < emaSlow and close < emaSlow and upperBearEmaCrossFresh", source)
        self.assertNotIn("bool setupShortPostUpperBreakdown = upperMrWindowValid", source)
        self.assertNotIn("bool setupLongPostLowerBreakout = lowerMrWindowValid", source)
        self.assertIn('string longSetup = setupLongPostLowerBreakout ? "trend_sdLowerBreakout"', source)
        self.assertIn('string shortSetup = setupShortPostUpperBreakdown ? "trend_sdUpperBreakdown"', source)
        self.assertIn("bool longSignalCandidate = setupLongPostLowerBreakout or setupLongTrendUpper or setupLongMrLower", source)
        self.assertIn("bool shortSignalCandidate = setupShortPostUpperBreakdown or setupShortMrUpper or setupShortTrendLower", source)

        self.assertIn("bool longPostReversalStructuralAnchorQualified = not structuralAnchorMode or anchorNear(longEntryCandidate, lastPivotLow, atrRaw)", source)
        self.assertIn("bool shortPostReversalStructuralAnchorQualified = not structuralAnchorMode or anchorNear(shortEntryCandidate, lastPivotHigh, atrRaw)", source)
        self.assertIn('blockLongReason := "post_reversal_anchor_unqualified"', source)
        self.assertIn('blockShortReason := "post_reversal_anchor_unqualified"', source)
        self.assertIn("float longStructureStopCandidate = setupIsMr(longSetup) or setupIsPostSdReversal(longSetup)", source)
        self.assertIn("float shortStructureStopCandidate = setupIsMr(shortSetup) or setupIsPostSdReversal(shortSetup)", source)

        self.assertIn("int scoreLongPostLowerBreakout = math.min(100, (lowerSdExtremeFresh ? 34 : 0)", source)
        self.assertIn("int scoreShortPostUpperBreakdown = math.min(100, (upperSdExtremeFresh ? 34 : 0)", source)
        self.assertIn('missingComponents := not lowerSdExtremeFresh ? appendSource(missingComponents, "lower_sd_memory")', source)
        self.assertIn('missingComponents := not upperSdExtremeFresh ? appendSource(missingComponents, "upper_sd_memory")', source)
        self.assertIn('missingText := str.replace_all(missingText, "upper_sd_memory", "上轨记忆")', source)
        self.assertIn('missingText := str.replace_all(missingText, "lower_sd_memory", "下轨记忆")', source)
        self.assertIn('bool candidateUpperWindowReady = candidateSetup == "trend_sdUpperBreakdown" ? upperSdExtremeFresh', source)
        self.assertIn('bool candidateLowerWindowReady = candidateSetup == "trend_sdLowerBreakout" ? lowerSdExtremeFresh', source)
        self.assertIn('bool blockedUpperWindowReady = blockedSetup == "trend_sdUpperBreakdown" ? upperSdExtremeFresh', source)
        self.assertIn('bool blockedLowerWindowReady = blockedSetup == "trend_sdLowerBreakout" ? lowerSdExtremeFresh', source)

        self.assertIn('jsonBool("lower_sd_extreme_fresh", lowerSdExtremeFresh)', source)
        self.assertIn('jsonBool("upper_sd_extreme_fresh", upperSdExtremeFresh)', source)
        self.assertIn('activeSetup == "trend_sdLowerBreakout" and not lowerSdExtremeFresh', source)
        self.assertIn('activeSetup == "trend_sdUpperBreakdown" and not upperSdExtremeFresh', source)
        self.assertIn('longSetup == "trend_sdLowerBreakout" ? "post_sd_lower_breakout"', source)
        self.assertIn('shortSetup == "trend_sdUpperBreakdown" ? "post_sd_upper_breakdown"', source)
        self.assertIn("lowerPostReversalUsed := setupLongPostLowerBreakout ? true : lowerPostReversalUsed", source)
        self.assertIn("upperPostReversalUsed := setupShortPostUpperBreakdown ? true : upperPostReversalUsed", source)

    def test_structural_anchor_entry_limit_payload_and_pending_ttl(self):
        source = self.source

        self.assertIn('entryAnchorMode = input.string("setup_structural", "Entry anchor mode", options=["setup_structural", "marketable_cap"], group="05 Risk")', source)
        self.assertIn('entryOrderTtlBars = input.int(1, "Entry order review bars"', source)
        self.assertIn("entryAnchorZoneAtr = input.float(0.15, \"Entry anchor zone (x ATR)\"", source)
        self.assertIn("float longStructuralAnchorLevel = nearestSupportBelow(longReferenceEntry", source)
        self.assertIn("float shortStructuralAnchorLevel = nearestResistanceAbove(shortReferenceEntry", source)
        self.assertIn("float longStructuralEntryRaw = math.min(longReferenceEntry, longStructuralAnchorLevel + atrRaw * entryAnchorZoneAtr)", source)
        self.assertIn("float shortStructuralEntryRaw = math.max(shortReferenceEntry, shortStructuralAnchorLevel - atrRaw * entryAnchorZoneAtr)", source)
        self.assertIn("jsonNum(\"planned_entry_price\", plannedEntry)", source)
        self.assertIn('jsonStr("entry_price_plan", structuralAnchorMode ? "structural_anchor_limit" : "tv_direct_bounded_limit")', source)
        self.assertIn('jsonStr("entry_anchor_reason", entryAnchorReason == "" ? "none" : entryAnchorReason)', source)
        self.assertIn('jsonStr("risk_model", structuralAnchorMode ? "setup_aware_structural_v1" : "tv_direct_bounded_limit_v1")', source)
        self.assertIn("activePendingEntry := true", source)
        self.assertIn('strategy.cancel("TV-L")', source)
        self.assertIn('strategy.cancel("TV-S")', source)
        self.assertIn('"\\nref " + str.tostring(longReferenceEntry', source)
        self.assertIn('"bar\\n锚点: " + activeEntryAnchorReason', source)
        self.assertIn("bool pendingFilledNow = confirmed and activePendingEntry and strategy.position_size != 0", source)
        self.assertIn("bool pendingEntryExpired = confirmed and activePendingEntry and strategy.position_size == 0", source)
        self.assertIn("bool longActionable = longSignalCandidate and not blockLong", source)
        self.assertIn("bool shortActionable = shortSignalCandidate and not blockShort", source)
        self.assertIn("bool pendingOppositeLongActionable = confirmed and activePendingEntry and strategy.position_size == 0 and activeDirection == -1 and longActionable", source)
        self.assertIn("bool pendingOppositeShortActionable = confirmed and activePendingEntry and strategy.position_size == 0 and activeDirection == 1 and shortActionable", source)
        self.assertIn("bool pendingActionableConflict = confirmed and activePendingEntry and strategy.position_size == 0 and longActionable and shortActionable", source)
        self.assertIn("bool pendingOppositeOrConflict = pendingActionableConflict or pendingOppositeLongActionable or pendingOppositeShortActionable", source)
        self.assertNotIn("bool pendingOppositeOrConflict = confirmed and activePendingEntry and strategy.position_size == 0 and (conflict or", source)
        self.assertIn("bool pendingLongEmaHardReversed = emaFast < emaSlow and close < emaSlow", source)
        self.assertIn("bool pendingShortEmaHardReversed = emaFast > emaSlow and close > emaSlow", source)
        self.assertIn("bool pendingTrendInvalidated = confirmed and activePendingEntry and strategy.position_size == 0", source)
        self.assertIn('dtpDir == -1 or mtfLongStatus == "block" or pendingLongEmaHardReversed', source)
        self.assertIn('dtpDir == 1 or mtfShortStatus == "block" or pendingShortEmaHardReversed', source)
        self.assertIn("bool pendingTrendStillValid = confirmed and activePendingEntry and strategy.position_size == 0 and not pendingTrendInvalidated", source)
        self.assertIn("bool pendingEntryRenewNow = pendingEntryExpired and pendingTrendStillValid", source)
        self.assertIn("if pendingEntryRenewNow\n    activeEntryOrderExpiresBar := bar_index + entryOrderTtlBars", source)
        self.assertIn('reason == "trend_invalidated" ? "趋势失效"', source)
        self.assertIn('pendingTrendInvalidated ? "trend_invalidated"', source)
        self.assertIn('pendingEntryExpired and not pendingEntryRenewNow ? "entry_ttl_expired"', source)
        self.assertIn('pendingOppositeOrConflict ? "opposite_signal_or_conflict" : pendingEntryWindowExpired', source)
        self.assertNotIn('pendingEodCancel ? "pending_eod_cancel" : pendingEntryExpired ? "entry_ttl_expired"', source)
        self.assertIn('" / 复查 " + str.tostring(entryOrderTtlBars, "#") + "bar\\n锚点: "', source)
        self.assertNotIn('" / TTL " + str.tostring(entryOrderTtlBars, "#")', source)
        self.assertIn("bool pendingCancelNow = pendingCancelReason != \"\"", source)
        self.assertIn("bool enterLong = canOpenEntry and flatForEntry and allowLong and longSignalCandidate", source)
        self.assertIn("bool enterShort = canOpenEntry and flatForEntry and allowShort and shortSignalCandidate", source)

    def test_mr_window_freshness_and_mr_exit_guards(self):
        source = self.source

        self.assertIn('mrWindowBars = input.int(30, "MR window bars"', source)
        self.assertIn('mrWindowMaxMinutes = input.int(60, "MR window max minutes"', source)
        self.assertIn('mrComponentFreshBars = input.int(30, "MR component freshness bars"', source)
        self.assertIn('mrExpireOnMeanTouch = input.bool(true, "Expire MR window after mean touch"', source)
        self.assertIn("bool lowerWindowTimeExpired = lowerWindowActive and not na(lowerWindowStartTime)", source)
        self.assertIn("bool lowerWindowMeanTouched = lowerWindowActive and mrExpireOnMeanTouch", source)
        self.assertIn("var bool lowerWindowMeanExpired = false", source)
        self.assertIn("var bool upperWindowMeanExpired = false", source)
        self.assertIn("bool lowerWindowHardExpired = lowerWindowBarExpired or lowerWindowTimeExpired", source)
        self.assertIn("bool upperWindowHardExpired = upperWindowBarExpired or upperWindowTimeExpired", source)
        self.assertIn("bool lowerWindowExpired = lowerWindowHardExpired or lowerWindowMeanExpired or lowerWindowMeanTouched", source)
        self.assertIn("bool upperWindowExpired = upperWindowHardExpired or upperWindowMeanExpired or upperWindowMeanTouched", source)
        self.assertIn("if lowerWindowHardExpired and not lowerActivationEvent\n    lowerWindowActive := false", source)
        self.assertIn("if upperWindowHardExpired and not upperActivationEvent\n    upperWindowActive := false", source)
        self.assertIn("bool lowerMrWindowValid = lowerWindowValid and not lowerWindowMeanExpired and not lowerWindowMeanTouched", source)
        self.assertIn("bool upperMrWindowValid = upperWindowValid and not upperWindowMeanExpired and not upperWindowMeanTouched", source)
        self.assertIn("if lowerWindowMeanTouched and not lowerActivationEvent\n    lowerWindowMeanExpired := true", source)
        self.assertIn("if upperWindowMeanTouched and not upperActivationEvent\n    upperWindowMeanExpired := true", source)
        self.assertIn("componentFresh(int componentBar)", source)
        self.assertIn('blockShortReason := "mr_dtp_phase_blocked"', source)
        self.assertIn('blockShortReason := "mr_anchor_unqualified"', source)
        self.assertIn('blockShortReason := "mr_target_space_too_small"', source)
        self.assertIn('blockShortReason := "mr_ema_cross_missing"', source)
        self.assertIn('blockShortReason := "mr_component_stale"', source)
        self.assertIn('activeSetup == "trend_sdLower" and lowerWindowHardExpired', source)
        self.assertIn('activeSetup == "trend_sdUpper" and upperWindowHardExpired', source)
        self.assertIn('pendingExitReason := "mr_structure_invalidated"', source)
        self.assertIn('pendingExitReason := "mr_time_stop"', source)
        self.assertIn('riskUpdateReason := "mr_checkpoint_lock"', source)

    def test_risk_update_sequence_is_incremented_and_serialized(self):
        source = self.source

        self.assertIn("var int activeRiskUpdateSeq = 0", source)
        self.assertIn('jsonInt("risk_update_seq", activeRiskUpdateSeq)', source)
        self.assertNotIn('alert(buildRiskPayload(eventId("risk_update", "initial")', source)
        self.assertNotIn('"initial_protection"', source)
        self.assertIn("activeRiskUpdateSeq := activeRiskUpdateSeq + 1\n        alert(buildRiskPayload(eventId(\"risk_update\", reasonForEvent)", source)

    def test_cancel_alert_payload_and_signal_fill_visuals_are_separate(self):
        source = self.source

        self.assertIn("buildCancelPayload(string eventIdValue, string positionIdValue, string cancelReason)", source)
        self.assertIn('basePayload("cancel", eventIdValue, positionIdValue)', source)
        self.assertIn('jsonStr("origin_signal_id", activeSignalId)', source)
        self.assertIn('jsonStr("cancel_scope", "trade_intent")', source)
        self.assertIn('jsonStr("cancel_policy", "cancel_unfilled_or_close_filled")', source)
        self.assertIn('jsonNum("submitted_entry_limit_price", activeSubmittedEntryLimit)', source)
        self.assertIn('jsonBool("tv_position_filled", strategy.position_size != 0)', source)
        self.assertIn("alert(buildCancelPayload(eventId(\"cancel\", pendingCancelReason), activePositionId, pendingCancelReason), alert.freq_all)", source)
        self.assertIn("var bool activeCancelSent = false", source)
        self.assertIn("activeCancelSent := true", source)
        self.assertIn('"信号已发出\\n提交限价做多\\n"', source)
        self.assertIn('"信号已发出\\n提交限价做空\\n"', source)
        self.assertIn('"TV模拟成交\\n"', source)
        self.assertIn('"信号撤销\\n" + cancelReasonDisplayName(pendingCancelReason)', source)


if __name__ == "__main__":
    unittest.main()
