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
      document.querySelectorAll('#overviewServiceRows .home-stack-service').length >= 5
    );
  }, { timeout: timeoutMs });
}

async function collectHomeOverviewIssues(page, mobile = false) {
  return page.evaluate(({ isMobileViewport, requiredRows }) => {
    const issues = [];
    const tip = document.getElementById('todayTargetsTimeTip');
    const badge = document.querySelector('.home-panel-tip-badge');
    const tipCard = document.querySelector('.home-panel-tip');
    const stackCard = document.getElementById('overviewStackCard');
    const stackSummary = document.getElementById('overviewStackSummary');
    const stackRows = Array.from(document.querySelectorAll('#overviewServiceRows .home-stack-service'));
    const stackLink = document.getElementById('overviewStackLink');

    const tipText = String(tip?.textContent || '').trim();
    const badgeText = String(badge?.textContent || '').trim();
    const tipStyle = tipCard ? window.getComputedStyle(tipCard) : null;
    const stackSummaryText = String(stackSummary?.textContent || '').trim();
    const stackLinkHref = String(stackLink?.getAttribute('href') || '').trim();
    const stackRowNames = stackRows
      .map((row) => String(row.querySelector('.home-stack-service-name')?.textContent || '').trim())
      .filter(Boolean);
    const stackRowStatuses = stackRows
      .map((row) => String(row.querySelector('.home-stack-service-status')?.textContent || '').trim())
      .filter(Boolean);

    if (!tip) issues.push('missing_targets_time_tip');
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
    if (stackLink && !/\/ibkr_system\.html/.test(stackLinkHref)) issues.push(`overview_stack_link_href:${stackLinkHref || 'empty'}`);
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
