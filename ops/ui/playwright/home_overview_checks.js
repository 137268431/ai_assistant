const HOME_STACK_REQUIRED_ROWS = ['Runtime', 'Compute', 'API', 'Scheduler', 'PocketBase'];

async function waitForHomeOverviewReady(page, timeoutMs) {
  await page.waitForFunction(() => {
    const readyStates = {
      overview: document.getElementById('homeOverview')?.dataset.ready || '',
      targets: document.getElementById('homeTargets')?.dataset.ready || '',
      market: document.getElementById('homeMarket')?.dataset.ready || '',
      activity: document.getElementById('homeActivity')?.dataset.ready || '',
      stack: document.getElementById('overviewStackCard')?.dataset.ready || '',
    };
    return (
      readyStates.overview === 'ready' &&
      ['ready', 'empty'].includes(readyStates.targets) &&
      ['ready', 'empty'].includes(readyStates.market) &&
      ['ready', 'empty'].includes(readyStates.activity) &&
      readyStates.stack === 'ready' &&
      document.querySelectorAll('#homeOverview .home-stat-card').length >= 6 &&
      document.querySelectorAll('#homeQuickLinks .home-quick-link').length >= 6 &&
      document.querySelectorAll('#homeQuickLinks .home-tool-chip').length >= 5 &&
      document.querySelectorAll('#overviewServiceRows .home-stack-service').length >= 5
    );
  }, { timeout: timeoutMs });
}

async function collectHomeOverviewIssues(page, mobile = false) {
  return page.evaluate(({ isMobileViewport, requiredRows }) => {
    const issues = [];
    const tip = document.getElementById('todayTargetsTimeTip');
    const stageSummary = document.getElementById('todayTargetsStageSummary');
    const admittedObserve = document.getElementById('todayTargetsAdmittedObserve');
    const signalCandidate = document.getElementById('todayTargetsSignalCandidate');
    const executionEligible = document.getElementById('todayTargetsExecutionEligible');
    const sourceSummary = document.getElementById('todayTargetsSourceSummary');
    const sourcePrimary = document.getElementById('todayTargetsSourcePrimary');
    const sdSummary = document.getElementById('todayTargetsSdSummary');
    const sdBreakdown = document.getElementById('todayTargetsSdBreakdown');
    const sdTotal = document.getElementById('todayTargetsSdTotal');
    const sdUpper = document.getElementById('todayTargetsSdUpper');
    const sdLower = document.getElementById('todayTargetsSdLower');
    const badge = document.querySelector('.home-panel-tip-badge');
    const tipCard = document.querySelector('.home-panel-tip');
    const stackCard = document.getElementById('overviewStackCard');
    const stackSummary = document.getElementById('overviewStackSummary');
    const stackRows = Array.from(document.querySelectorAll('#overviewServiceRows .home-stack-service'));
    const stackLink = document.getElementById('overviewStackLink');
    const configLink = document.getElementById('actionConfigLink');
    const toolChips = Array.from(document.querySelectorAll('#homeQuickLinks .home-tool-chip'));
    const signalsStatus = document.getElementById('todaySignalsStatusSummary');
    const signalsFoot = document.getElementById('todaySignalsFoot');
    const ordersStatus = document.getElementById('todayOrdersStatusSummary');
    const ordersGroup = document.getElementById('todayOrdersGroupSummary');
    const ordersFoot = document.getElementById('todayOrdersFoot');
    const positionsLabel = document.getElementById('positionsStateLabel');
    const positionsLiveOrders = document.getElementById('positionsLiveOrdersSummary');
    const positionsFoot = document.getElementById('positionsFoot');

    const tipText = String(tip?.textContent || '').trim();
    const stageSummaryText = String(stageSummary?.textContent || '').trim();
    const sourceSummaryText = String(sourceSummary?.textContent || '').trim();
    const sdSummaryText = String(sdSummary?.textContent || '').trim();
    const sdBreakdownText = String(sdBreakdown?.textContent || '').trim();
    const badgeText = String(badge?.textContent || '').trim();
    const tipStyle = tipCard ? window.getComputedStyle(tipCard) : null;
    const stackSummaryText = String(stackSummary?.textContent || '').trim();
    const stackLinkHref = String(stackLink?.getAttribute('href') || '').trim();
    const configLinkHref = String(configLink?.getAttribute('href') || '').trim();
    const toolChipTexts = toolChips.map((chip) => String(chip.textContent || '').trim()).filter(Boolean);
    const stackRowNames = stackRows
      .map((row) => String(row.querySelector('.home-stack-service-name')?.textContent || '').trim())
      .filter(Boolean);
    const stackRowStatuses = stackRows
      .map((row) => String(row.querySelector('.home-stack-service-status')?.textContent || '').trim())
      .filter(Boolean);
    const signalsStatusText = String(signalsStatus?.textContent || '').trim();
    const signalsFootText = String(signalsFoot?.textContent || '').trim();
    const ordersStatusText = String(ordersStatus?.textContent || '').trim();
    const ordersGroupText = String(ordersGroup?.textContent || '').trim();
    const ordersFootText = String(ordersFoot?.textContent || '').trim();
    const positionsLabelText = String(positionsLabel?.textContent || '').trim();
    const positionsLiveOrdersText = String(positionsLiveOrders?.textContent || '').trim();
    const positionsFootText = String(positionsFoot?.textContent || '').trim();

    if (!tip) issues.push('missing_targets_time_tip');
    if (!stageSummary) issues.push('missing_targets_stage_summary');
    if (!admittedObserve) issues.push('missing_targets_admitted_observe');
    if (!signalCandidate) issues.push('missing_targets_signal_candidate');
    if (!executionEligible) issues.push('missing_targets_execution_eligible');
    if (!sourceSummary) issues.push('missing_targets_source_summary');
    if (!sourcePrimary) issues.push('missing_targets_source_primary');
    if (!sdSummary) issues.push('missing_targets_sd_summary');
    if (!sdBreakdown) issues.push('missing_targets_sd_breakdown');
    if (!sdTotal) issues.push('missing_targets_sd_total');
    if (!sdUpper) issues.push('missing_targets_sd_upper');
    if (!sdLower) issues.push('missing_targets_sd_lower');
    if (stageSummary && (!stageSummaryText.includes('入选观察') || !stageSummaryText.includes('交易候选') || !stageSummaryText.includes('可执行'))) {
      issues.push(`targets_stage_summary_text:${stageSummaryText || 'empty'}`);
    }
    if (sourceSummary && !sourceSummaryText.includes('来源')) {
      issues.push(`targets_source_summary_text:${sourceSummaryText || 'empty'}`);
    }
    if (sdBreakdown && !sdBreakdownText.includes('SD窗口')) {
      issues.push(`targets_sd_breakdown_text:${sdBreakdownText || 'empty'}`);
    }
    if (sdSummary && (!sdSummaryText.includes('SD上轨') || !sdSummaryText.includes('SD下轨'))) {
      issues.push(`targets_sd_summary_text:${sdSummaryText || 'empty'}`);
    }
    const targetsFoot = document.getElementById('todayTargetsFoot');
    const targetsFootText = String(targetsFoot?.textContent || '').trim();
    if (!targetsFoot) issues.push('missing_targets_execution_foot');
    if (targetsFoot && (!targetsFootText.includes('执行中') || !targetsFootText.includes('目标ACTIVE'))) {
      issues.push(`targets_execution_foot_text:${targetsFootText || 'empty'}`);
    }
    if (!badge) issues.push('missing_targets_tip_badge');
    if (!tipCard) issues.push('missing_targets_tip_card');
    if (tip && !tipText.includes('美东交易日')) issues.push('targets_time_tip_missing_market_date_copy');
    if (tip && !tipText.includes('ET')) issues.push('targets_time_tip_missing_et_copy');
    if (tip && !tipText.includes('当前标的榜')) issues.push('targets_time_tip_missing_jump_hint');
    if (badge && badgeText !== 'Tips') issues.push(`targets_tip_badge_text:${badgeText || 'empty'}`);
    if (tipCard && tipStyle?.display !== 'flex') issues.push(`targets_tip_display:${tipStyle?.display || 'missing'}`);
    if (tipCard && isMobileViewport && tipStyle?.flexDirection !== 'column') {
      issues.push(`targets_tip_mobile_direction:${tipStyle?.flexDirection || 'missing'}`);
    }
    if (!stackCard) issues.push('missing_overview_stack_card');
    if (!stackSummary) issues.push('missing_overview_stack_summary');
    if (!stackLink) issues.push('missing_overview_stack_link');
    if (!configLink) issues.push('missing_action_config_link');
    if (stackLink && !/\/ibkr_system\.html/.test(stackLinkHref)) issues.push(`overview_stack_link_href:${stackLinkHref || 'empty'}`);
    if (configLink && !/\/ibkr_config\.html/.test(configLinkHref)) issues.push(`action_config_link_href:${configLinkHref || 'empty'}`);
    if (!toolChipTexts.some((text) => text.includes('配置'))) issues.push('missing_config_tool_chip');
    if (stackSummary && !stackSummaryText.includes('Runtime / Compute / API / Scheduler 已拆分')) {
      issues.push(`overview_stack_summary_text:${stackSummaryText || 'empty'}`);
    }
    if (stackRows.length < 5) issues.push(`overview_stack_row_count:${stackRows.length}`);
    requiredRows.forEach((label) => {
      if (!stackRowNames.includes(label)) {
        issues.push(`overview_stack_missing_row:${label}`);
      }
    });
    if (stackRowStatuses.length < 5) issues.push(`overview_stack_status_count:${stackRowStatuses.length}`);
    if (!signalsStatus) issues.push('missing_home_signals_status_summary');
    if (!signalsFoot) issues.push('missing_home_signals_foot');
    if (!ordersStatus) issues.push('missing_home_orders_status_summary');
    if (!ordersGroup) issues.push('missing_home_orders_group_summary');
    if (!ordersFoot) issues.push('missing_home_orders_foot');
    if (!positionsLabel) issues.push('missing_home_positions_state_label');
    if (!positionsLiveOrders) issues.push('missing_home_positions_live_orders_summary');
    if (!positionsFoot) issues.push('missing_home_positions_foot');
    if (signalsStatus && (!signalsStatusText.includes('待确认') || !signalsStatusText.includes('待执行') || !signalsStatusText.includes('挂单'))) {
      issues.push(`signals_status_summary_text:${signalsStatusText || 'empty'}`);
    }
    if (signalsFoot && (!signalsFootText.includes('成交保护') || !signalsFootText.includes('已平仓') || !signalsFootText.includes('已取消') || !signalsFootText.includes('终止'))) {
      issues.push(`signals_foot_text:${signalsFootText || 'empty'}`);
    }
    if (ordersStatus && (!ordersStatusText.includes('已成交') || !ordersStatusText.includes('挂单') || !ordersStatusText.includes('已取消'))) {
      issues.push(`orders_status_summary_text:${ordersStatusText || 'empty'}`);
    }
    if (ordersGroup && (!ordersGroupText.includes('交易组(去重)') || !ordersGroupText.includes('Entry单'))) {
      issues.push(`orders_group_summary_text:${ordersGroupText || 'empty'}`);
    }
    if (ordersFoot && (!ordersFootText.includes('当前挂单组') || !ordersFootText.includes('订单腿') || !ordersFootText.includes('Close') || !ordersFootText.includes('可取消组') || !ordersFootText.includes('可改单组'))) {
      issues.push(`orders_foot_missing_live_open_order_groups:${ordersFootText || 'empty'}`);
    }
    if (positionsLabel && !/(账户持仓|推断持仓|无实际持仓|持仓未确认|持仓接口不可用)/.test(positionsLabelText)) {
      issues.push(`positions_state_label_text:${positionsLabelText || 'empty'}`);
    }
    if (positionsLiveOrders && (!positionsLiveOrdersText.includes('当前挂单组') || !positionsLiveOrdersText.includes('订单腿'))) {
      issues.push(`positions_live_orders_text:${positionsLiveOrdersText || 'empty'}`);
    }
    if (positionsFoot && !/Gateway/.test(positionsFootText)) {
      issues.push(`positions_foot_text:${positionsFootText || 'empty'}`);
    }

    return issues;
  }, {
    isMobileViewport: mobile,
    requiredRows: HOME_STACK_REQUIRED_ROWS,
  });
}

module.exports = {
  HOME_STACK_REQUIRED_ROWS,
  waitForHomeOverviewReady,
  collectHomeOverviewIssues,
};
