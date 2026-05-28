let currentEnvironment = getSharedDataEnvironment();
let currentBrokerMode = getCurrentBrokerMode();
let latestLogicLoadId = 0;

const LOGIC_SECTION_ORDER = [
  ['system_flow', '系统地图'],
  ['selection', '标的选择'],
  ['indicators', '数据与指标'],
  ['signals', '核心策略'],
  ['execution', '执行校验'],
  ['order_flow', 'Delta / 订单流'],
  ['orders', '订单生命周期'],
  ['broker_mode_switch', '模式切换 / 2FA'],
  ['quality', '数据质量'],
  ['backtest_validation', '回测验证'],
  ['scheduler', '调度时间线'],
];

const SCHEDULER_FAMILY_LABELS = {
  target_universe: '标的与目标池',
  compute_dispatch: '计算与状态摘要',
  trade_maintenance: '信号 / 订单维护',
  data_quality: '数据质量',
  system_monitor: '系统监控 / 报告',
  auth_monitor: '2FA / Session',
  storage_maintenance: '存储治理',
  default: '其他调度',
};

function safeEscape(value) {
  return typeof escapeHtml === 'function'
    ? escapeHtml(value)
    : String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function arrayOrEmpty(value) {
  return Array.isArray(value) ? value : [];
}

function panelSourceRefs(panel = {}) {
  return arrayOrEmpty(panel.source_refs).filter(Boolean);
}

function renderHeroMeta(rulesPayload = {}, cronPayload = {}, errors = []) {
  const computedAt = rulesPayload.computed_at_us || rulesPayload.computed_at || rulesPayload.timestamp || '--';
  const schemaVersion = rulesPayload.schema_version || '--';
  const schedulerItems = arrayOrEmpty(cronPayload.items);
  const enabledJobs = schedulerItems.filter((item) => item && item.effective_enabled !== false).length;
  const errorText = errors.length ? `${errors.length} warning` : 'ok';
  const meta = [
    ['Broker', currentBrokerMode.toUpperCase()],
    ['Data', currentEnvironment.toUpperCase()],
    ['Schema', schemaVersion],
    ['Rules At', computedAt],
    ['Scheduler', `${enabledJobs}/${schedulerItems.length || 0} enabled`],
    ['Source', errorText],
  ];
  document.getElementById('logicHeroMeta').innerHTML = meta.map(([label, value]) => `
    <span class="logic-meta-chip"><span>${safeEscape(label)}</span><strong>${safeEscape(value)}</strong></span>
  `).join('');
}

function renderAnchors() {
  const mount = document.getElementById('logicAnchorStrip');
  mount.innerHTML = LOGIC_SECTION_ORDER.map(([id, label]) => `
    <a class="logic-anchor" href="#logic-${safeEscape(id)}">${safeEscape(label)}</a>
  `).join('');
}

function renderCoverage(coverage = []) {
  const items = arrayOrEmpty(coverage);
  const mount = document.getElementById('logicCoverageGrid');
  if (!items.length) {
    mount.innerHTML = '<div class="logic-empty-state panel">暂无覆盖清单</div>';
    return;
  }
  mount.innerHTML = items.map((item) => {
    const status = item.status === 'covered' ? 'covered' : 'missing';
    return `
      <article class="logic-coverage-card ${status}">
        <span class="logic-coverage-dot"></span>
        <span class="logic-coverage-label">${safeEscape(item.label || item.id || '--')}</span>
        <strong>${status === 'covered' ? 'Covered' : 'Missing'}</strong>
      </article>
    `;
  }).join('');
}

function renderChips(chips = []) {
  const items = arrayOrEmpty(chips);
  if (!items.length) return '';
  return `
    <div class="logic-chip-grid">
      ${items.map((chip) => `
        <article class="logic-chip">
          <div class="logic-chip-label">${safeEscape(chip.label || '--')}</div>
          <div class="logic-chip-value">${safeEscape(chip.value == null ? '--' : String(chip.value))}</div>
          <div class="logic-chip-copy">${safeEscape(chip.copy || chip.note || '--')}</div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderHighlights(highlights = []) {
  const items = arrayOrEmpty(highlights);
  if (!items.length) return '';
  return `
    <div class="logic-highlight-grid">
      ${items.map((item) => `
        <article class="logic-highlight ${safeEscape(item.tone || 'neutral')}">
          <div class="logic-highlight-label">${safeEscape(item.label || item.id || '--')}</div>
          <div class="logic-highlight-value">${safeEscape(item.value == null ? '--' : String(item.value))}</div>
          <div class="logic-highlight-note">${safeEscape(item.note || item.summary || '')}</div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderDetails(details = []) {
  const items = arrayOrEmpty(details);
  if (!items.length) return '';
  return `
    <div class="logic-detail-grid">
      ${items.map((detail) => `
        <article class="logic-detail-card ${safeEscape(detail.tone || 'neutral')}">
          <div class="logic-detail-card-head">
            <div>
              <div class="logic-detail-id">${safeEscape(detail.id || '')}</div>
              <div class="logic-detail-card-title">${safeEscape(detail.title || '--')}</div>
            </div>
            ${detail.summary ? `<span>${safeEscape(detail.summary)}</span>` : ''}
          </div>
          <div class="logic-lines">
            ${arrayOrEmpty(detail.lines).map((line) => `<div class="logic-line">${safeEscape(line)}</div>`).join('')}
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderPolicyCards(panel = {}) {
  const cards = [];
  const retained = arrayOrEmpty(panel.retained_setups);
  const nonCore = arrayOrEmpty(panel.non_core_setups);
  const flow = arrayOrEmpty(panel.setup_flow);
  const delta = panel.delta_policy && typeof panel.delta_policy === 'object' ? panel.delta_policy : null;

  if (panel.strategy_profile || panel.runtime_signal_profile) {
    cards.push({
      tone: 'accent',
      id: 'strategy',
      title: panel.strategy_profile || 'Strategy',
      summary: panel.runtime_signal_profile ? `runtime: ${panel.runtime_signal_profile}` : '',
      lines: [
        panel.strategy_profile ? `core profile: ${panel.strategy_profile}` : '',
        panel.runtime_signal_profile ? `runtime source: ${panel.runtime_signal_profile}` : '',
        retained.length ? `retained setups: ${retained.map((item) => item.id || item.setup || '').filter(Boolean).join(' + ')}` : '',
      ].filter(Boolean),
    });
  }

  if (nonCore.length) {
    cards.push({
      tone: 'warn',
      id: 'non_core',
      title: 'Deleted / Non-Core',
      summary: `${nonCore.length} legacy labels`,
      lines: nonCore.map((item) => `${item.id || '--'} · ${item.status || 'non_core'}`),
    });
  }

  if (flow.length) {
    cards.push({
      tone: 'neutral',
      id: 'flow',
      title: 'Setup Flow',
      summary: 'target -> setup -> execution',
      lines: flow,
    });
  }

  if (delta) {
    cards.push({
      tone: 'warn',
      id: 'delta',
      title: 'Delta Policy',
      summary: delta.summary || delta.mode || 'auxiliary only',
      lines: arrayOrEmpty(delta.lines),
    });
  }

  if (!cards.length) return '';
  return `
    <div class="logic-detail-grid">
      ${cards.map((card) => `
        <article class="logic-detail-card ${safeEscape(card.tone || 'neutral')}">
          <div class="logic-detail-card-head">
            <div>
              <div class="logic-detail-id">${safeEscape(card.id || '')}</div>
              <div class="logic-detail-card-title">${safeEscape(card.title || '--')}</div>
            </div>
            ${card.summary ? `<span>${safeEscape(card.summary)}</span>` : ''}
          </div>
          <div class="logic-lines">
            ${arrayOrEmpty(card.lines).map((line) => `<div class="logic-line">${safeEscape(line)}</div>`).join('')}
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderSourceStack(panel = {}) {
  const refs = panelSourceRefs(panel).slice(0, 8);
  if (!refs.length) return '';
  return `
    <div class="logic-source-stack">
      ${refs.map((ref) => `<span class="logic-source-chip">${safeEscape(ref)}</span>`).join('')}
    </div>
  `;
}

function renderSections(sections = []) {
  const items = arrayOrEmpty(sections);
  if (!items.length) return '';
  return `
    <div class="logic-section-grid">
      ${items.map((section) => `
        <article class="logic-detail-section">
          <div class="logic-detail-title">${safeEscape(section.title || '--')}</div>
          ${section.copy ? `<div class="logic-detail-copy">${safeEscape(section.copy)}</div>` : ''}
          <div class="logic-lines">
            ${arrayOrEmpty(section.lines).map((line) => `<div class="logic-line">${safeEscape(line)}</div>`).join('')}
          </div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderStageLinks(links = []) {
  const items = arrayOrEmpty(links).filter(Boolean);
  if (!items.length) return '';
  return `<div class="logic-stage-links">${items.map((link) => {
    const href = buildPageUrl(String(link || '/'), {}, { environment: currentEnvironment, brokerMode: currentBrokerMode, dataEnvironment: currentEnvironment });
    return `<a href="${safeEscape(href)}">${safeEscape(String(link).split('?')[0])}</a>`;
  }).join('')}</div>`;
}

function renderSystemFlowPanel(panel = {}) {
  const stages = arrayOrEmpty(panel.stages);
  return `
    <section id="logic-system_flow" class="panel logic-panel">
      <div class="logic-panel-head">
        <div>
          <div class="logic-kicker">System Flow</div>
          <div class="logic-title">${safeEscape(panel.title || '系统逻辑地图')}</div>
          <div class="logic-copy">${safeEscape(panel.subtitle || '')}</div>
        </div>
        ${renderSourceStack(panel)}
      </div>
      ${renderChips(panel.chips)}
      ${renderHighlights(panel.highlights)}
      <div class="logic-stage-grid">
        ${stages.map((stage, index) => `
          <article class="logic-stage-card">
            <div class="logic-stage-id">${String(index + 1).padStart(2, '0')} · ${safeEscape(stage.id || '')}</div>
            <div class="logic-stage-title">${safeEscape(stage.label || '--')}</div>
            <div class="logic-stage-copy">${safeEscape(stage.summary || '--')}</div>
            ${renderStageLinks(stage.links)}
          </article>
        `).join('')}
      </div>
      ${renderSections(panel.sections)}
    </section>
  `;
}

function renderGenericPanel(id, panel = {}, index = 0) {
  return `
    <section id="logic-${safeEscape(id)}" class="panel logic-panel">
      <div class="logic-panel-head">
        <div>
          <div class="logic-kicker">${String(index + 1).padStart(2, '0')} · ${safeEscape(id)}</div>
          <div class="logic-title">${safeEscape(panel.title || id)}</div>
          <div class="logic-copy">${safeEscape(panel.subtitle || '')}</div>
        </div>
        ${renderSourceStack(panel)}
      </div>
      ${renderChips(panel.chips)}
      ${renderHighlights(panel.highlights)}
      ${renderPolicyCards(panel)}
      ${renderDetails(panel.details)}
      ${renderSections(panel.sections)}
    </section>
  `;
}

function cronStatusLabel(item = {}) {
  const state = item.job_state && typeof item.job_state === 'object' ? item.job_state : {};
  const status = String(state.status || state.last_status || '').trim();
  if (status) return status.toUpperCase();
  return item.effective_enabled ? 'ENABLED' : 'DISABLED';
}

function cronLastResult(item = {}) {
  const state = item.job_state && typeof item.job_state === 'object' ? item.job_state : {};
  const result = state.last_result && typeof state.last_result === 'object' ? state.last_result : {};
  return result.result || result.status || result.reason || state.last_error || '--';
}

function cronCycleLabel(item = {}) {
  return item.et_cycle_label || item.cycle_label || item.window_label || item.cron_expr || '--';
}

function renderSchedulerPanel(cronPayload = {}) {
  const items = arrayOrEmpty(cronPayload.items);
  const scheduler = cronPayload.scheduler || {};
  const enabledJobs = items.filter((item) => item && item.effective_enabled !== false).length;
  const families = items.reduce((acc, item) => {
    const family = String(item.family || 'default').trim() || 'default';
    if (!acc[family]) acc[family] = [];
    acc[family].push(item);
    return acc;
  }, {});
  const familyOrder = Object.keys(families).sort((a, b) => {
    const first = Math.min(...families[a].map((item) => Number(item.sort_order || 0)));
    const second = Math.min(...families[b].map((item) => Number(item.sort_order || 0)));
    return first - second || a.localeCompare(b);
  });

  return `
    <section id="logic-scheduler" class="panel logic-panel">
      <div class="logic-panel-head">
        <div>
          <div class="logic-kicker">Scheduler</div>
          <div class="logic-title">触发时间与定时调度</div>
          <div class="logic-copy">来自 /api/custom/system/cronz；展示当前环境下所有 scheduler job 的定义、开关、执行器和最近状态。</div>
        </div>
        <div class="logic-source-stack">
          <span class="logic-source-chip">ibkr_scheduler.cron_registry</span>
          <span class="logic-source-chip">ibkr_scheduler.scheduler_app</span>
          <span class="logic-source-chip">ibkr_api.system.jobs</span>
        </div>
      </div>
      <div class="logic-scheduler-summary">
        <article class="logic-scheduler-card"><div class="logic-scheduler-label">Status</div><div class="logic-scheduler-value">${safeEscape(String(scheduler.status || '--').toUpperCase())}</div><div class="logic-scheduler-copy">scheduler service</div></article>
        <article class="logic-scheduler-card"><div class="logic-scheduler-label">Jobs</div><div class="logic-scheduler-value">${safeEscape(`${enabledJobs}/${items.length}`)}</div><div class="logic-scheduler-copy">effective enabled / total</div></article>
        <article class="logic-scheduler-card"><div class="logic-scheduler-label">Mode</div><div class="logic-scheduler-value">${safeEscape(currentBrokerMode)} / ${safeEscape(currentEnvironment)}</div><div class="logic-scheduler-copy">broker / market data</div></article>
      </div>
      ${familyOrder.map((family) => `
        <div class="logic-scheduler-family">
          <div class="logic-scheduler-family-head">
            <div>
              <div class="logic-section-kicker">${safeEscape(family)}</div>
              <div class="logic-section-title">${safeEscape(SCHEDULER_FAMILY_LABELS[family] || family)}</div>
            </div>
            <span class="logic-tag">${safeEscape(String(families[family].filter((item) => item.effective_enabled !== false).length))}/${safeEscape(String(families[family].length))} enabled</span>
          </div>
          <div class="logic-cron-grid">
            ${families[family].map((item) => `
              <article class="logic-cron-card ${item.effective_enabled === false ? 'off' : 'on'}">
                <div class="logic-cron-head">
                  <div>
                    <div class="logic-cron-title">${safeEscape(item.display_name || item.id || '--')}</div>
                    <div class="logic-cron-copy">${safeEscape(item.function_summary || '--')}</div>
                  </div>
                  <span class="logic-state ${item.effective_enabled === false ? '' : 'on'}">${safeEscape(cronStatusLabel(item))}</span>
                </div>
                <div class="logic-cron-meta">
                  <span>id: ${safeEscape(item.id || '--')}</span>
                  <span>runner: ${safeEscape(item.runner_kind || '--')} · scope: ${safeEscape(item.mode_scope || '--')}</span>
                  <span>window: ${safeEscape(item.window_label || cronCycleLabel(item))}</span>
                  <span>cycle: ${safeEscape(cronCycleLabel(item))}</span>
                  <span>cron: ${safeEscape(item.cron_expr || '--')} [${safeEscape(item.cron_timezone || 'UTC')}]</span>
                  <span>result: ${safeEscape(cronLastResult(item))}</span>
                </div>
              </article>
            `).join('')}
          </div>
        </div>
      `).join('')}
    </section>
  `;
}

function renderLogicContent(rulesPayload = {}, cronPayload = {}) {
  const panels = {
    system_flow: rulesPayload.system_flow,
    selection: rulesPayload.selection,
    indicators: rulesPayload.indicators,
    signals: rulesPayload.signals,
    execution: rulesPayload.execution,
    order_flow: rulesPayload.order_flow,
    orders: rulesPayload.orders,
    broker_mode_switch: rulesPayload.broker_mode_switch,
    quality: rulesPayload.quality,
    backtest_validation: rulesPayload.backtest_validation,
  };
  const html = LOGIC_SECTION_ORDER.map(([id], index) => {
    if (id === 'scheduler') return renderSchedulerPanel(cronPayload);
    if (id === 'system_flow') return renderSystemFlowPanel(panels[id] || {});
    return renderGenericPanel(id, panels[id] || { title: id, subtitle: '暂无数据', sections: [] }, index);
  }).join('');
  document.getElementById('logicContent').innerHTML = html;
}

function renderLoadError(errors = []) {
  if (!errors.length) return '';
  return `
    <section class="panel logic-error">
      <div class="logic-section-title">加载警告</div>
      <div class="logic-section-copy">部分数据源加载失败，页面已展示可用数据。</div>
      <div class="logic-lines">${errors.map((error) => `<div class="logic-line">${safeEscape(error)}</div>`).join('')}</div>
    </section>
  `;
}

async function loadSystemLogic(showToastOnSuccess = false) {
  if (!ensureIbkrPageAuth()) return;
  const loadId = ++latestLogicLoadId;
  setIbkrPageLoading(true, '系统逻辑加载中', `正在读取 ${currentBrokerMode}/${currentEnvironment} 的规则与调度。`);
  const errors = [];
  const safeLoad = async (label, loader, fallback) => {
    try {
      return await withTimeout(loader(), 20000, label);
    } catch (error) {
      errors.push(`${label}: ${error.message || error}`);
      return fallback;
    }
  };

  const [rulesPayload, cronPayload] = await Promise.all([
    safeLoad('rules', () => cachedCustomJson('/api/custom/ibkr/rules', currentEnvironment, {}, {
      ttlMs: 60000,
      swrMs: 60000,
      force: Boolean(showToastOnSuccess),
      tags: ['system-logic', 'rules', currentEnvironment],
    }), { ok: false, logic_coverage: [] }),
    safeLoad('cronz', () => cachedCustomJson('/api/custom/system/cronz', currentEnvironment, {}, {
      ttlMs: 30000,
      swrMs: 30000,
      force: Boolean(showToastOnSuccess),
      tags: ['system-logic', 'cronz', currentEnvironment],
    }), { ok: false, items: [], scheduler: {} }),
  ]);

  if (loadId !== latestLogicLoadId) return;
  renderHeroMeta(rulesPayload, cronPayload, errors);
  renderCoverage(rulesPayload.logic_coverage || []);
  renderLogicContent(rulesPayload, cronPayload);
  if (errors.length) {
    document.getElementById('logicContent').insertAdjacentHTML('afterbegin', renderLoadError(errors));
  }
  setPageRefreshTime();
  setPageContextMeta([
    { label: 'Broker', value: currentBrokerMode.toUpperCase(), tone: currentBrokerMode },
    { label: 'Data', value: currentEnvironment.toUpperCase(), tone: currentEnvironment },
    { label: 'Schema', value: rulesPayload.schema_version || '--' },
    { label: 'Coverage', value: `${arrayOrEmpty(rulesPayload.logic_coverage).filter((item) => item.status === 'covered').length}/${arrayOrEmpty(rulesPayload.logic_coverage).length || 0}` },
  ]);
  setIbkrPageLoading(false);
  if (showToastOnSuccess) showToast(errors.length ? '系统逻辑已刷新，存在加载警告' : '系统逻辑已刷新');
}

window.onEnvironmentChange = function onEnvironmentChange(environment) {
  currentEnvironment = environment;
  window.location.href = buildPageUrl('/ibkr_system_logic.html', {}, {
    environment: currentEnvironment,
    brokerMode: currentBrokerMode,
    dataEnvironment: currentEnvironment,
  });
};

window.onRefresh = function onRefresh() {
  loadSystemLogic(true);
};

window.addEventListener('DOMContentLoaded', async () => {
  if (typeof initAuth === 'function' && !initAuth()) return;
  currentEnvironment = getSharedDataEnvironment();
  currentBrokerMode = getCurrentBrokerMode();
  document.getElementById('nav').innerHTML = renderNav('/ibkr_system_logic.html');
  document.getElementById('contextBar').innerHTML = renderPageContextBar('🧭 IBKR 系统逻辑', {
    subtitle: 'core_two_setup_v1 / Delta 辅助策略 / 指标 / 调度',
  });
  document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_system_logic.html');
  renderAnchors();
  await loadSystemLogic(false);
});
