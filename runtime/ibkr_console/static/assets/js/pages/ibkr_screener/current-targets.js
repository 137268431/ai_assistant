    function populateSelect(selectId, values) {
      const select = document.getElementById(selectId);
      const currentValue = select.value;
      const options = ['<option value="">全部</option>'].concat(
        (Array.isArray(values) ? values : []).map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
      );
      select.innerHTML = options.join('');
      if (currentValue && Array.from(select.options).some((option) => option.value === currentValue)) {
        select.value = currentValue;
      }
    }

    function inRange(value, min, max) {
      const num = Number(value);
      if (!Number.isFinite(num)) return false;
      if (Number.isFinite(min) && num < min) return false;
      if (Number.isFinite(max) && num > max) return false;
      return true;
    }

    function getRowFilters() {
      return {
        symbol_search: String(document.getElementById('symbolSearch').value || '').trim().toUpperCase(),
        exchange: document.getElementById('exchangeFilter').value || '',
        industry: document.getElementById('industryFilter').value || '',
        target_status: document.getElementById('targetStatusFilter').value || '',
        direction: document.getElementById('directionFilter').value || '',
        price_min: Number(document.getElementById('priceMin').value || NaN),
        price_max: Number(document.getElementById('priceMax').value || NaN),
        day_change_min: Number(document.getElementById('dayChangeMin').value || NaN),
        day_change_max: Number(document.getElementById('dayChangeMax').value || NaN),
        atr_pct_min: Number(document.getElementById('atrPctMin').value || NaN),
        avg_volume_min: Number(document.getElementById('avgVolumeMin').value || NaN),
        premarket_volume_min: Number(document.getElementById('premarketVolumeMin').value || NaN),
        target_score_min: Number(document.getElementById('targetScoreMin').value || NaN),
        freshness_max: Number(document.getElementById('freshnessMax').value || NaN),
        operable_only: Boolean(document.getElementById('operableOnly').checked),
        sort_by: document.getElementById('sortBy').value || 'tradability_desc'
      };
    }

    function sortRows(rows, sortBy) {
      const items = [...rows];
      items.sort((left, right) => {
        if (sortBy === 'symbol_asc') {
          return String(left.symbol || '').localeCompare(String(right.symbol || ''));
        }
        if (sortBy === 'freshness_asc') {
          const l = Number.isFinite(Number(left.freshness_min)) ? Number(left.freshness_min) : Number.MAX_SAFE_INTEGER;
          const r = Number.isFinite(Number(right.freshness_min)) ? Number(right.freshness_min) : Number.MAX_SAFE_INTEGER;
          return l - r || String(left.symbol || '').localeCompare(String(right.symbol || ''));
        }
        if (sortBy === 'premarket_desc') {
          return (Number(right.premarket_volume) || 0) - (Number(left.premarket_volume) || 0)
            || (Number(right.tradability_score) || 0) - (Number(left.tradability_score) || 0);
        }
        if (sortBy === 'target_desc') {
          return (Number(right.target_score) || 0) - (Number(left.target_score) || 0)
            || (Number(right.tradability_score) || 0) - (Number(left.tradability_score) || 0);
        }
        if (sortBy === 'volume_desc') {
          return (Number(right.avg_10d_volume) || 0) - (Number(left.avg_10d_volume) || 0)
            || (Number(right.tradability_score) || 0) - (Number(left.tradability_score) || 0);
        }
        return (Number(right.tradability_score) || 0) - (Number(left.tradability_score) || 0)
          || (Number(right.target_score) || 0) - (Number(left.target_score) || 0)
          || String(left.symbol || '').localeCompare(String(right.symbol || ''));
      });
      return items;
    }

    function buildScorePill(row) {
      const score = Number(row.tradability_score || 0);
      const className = score >= 60 ? 'score-pill good' : 'score-pill';
      return `<span class="${className}">${escapeHtml(formatNumber(score, 0))}</span>`;
    }

    function buildReasonPills(row) {
      const reasons = Array.isArray(row.operable_reasons) ? row.operable_reasons : [];
      if (!reasons.length) return '<span class="muted">--</span>';
      return `<div class="reason-wrap">${reasons.slice(0, 4).map((item) => `<span class="reason-pill">${escapeHtml(item)}</span>`).join('')}</div>`;
    }

    function buildChartUrl(symbol) {
      return buildPageUrl('/ibkr_chart.html', {
        symbol: symbol || '',
        interval: '5m',
      }, { environment: currentEnvironment });
    }

    function buildIndicatorUrl(symbol, marketDate) {
      return buildPageUrl('/ibkr_indicators.html', {
        date: marketDate || '',
        search: symbol || '',
      }, { environment: currentEnvironment });
    }

    function buildSignalUrl(symbol, marketDate) {
      return buildPageUrl('/ibkr_signals.html', {
        date: marketDate || '',
        search: symbol || '',
      }, { environment: currentEnvironment });
    }

    function buildMobileMetricCard(label, valueHtml) {
      return `
        <article class="mobile-metric-card">
          <div class="mobile-metric-label">${escapeHtml(label || '--')}</div>
          <div class="mobile-metric-value">${valueHtml || '--'}</div>
        </article>
      `;
    }

    function buildMobileSection(label, bodyHtml) {
      return `
        <div class="mobile-section">
          <div class="mobile-section-label">${escapeHtml(label || '--')}</div>
          <div class="mobile-section-copy">${bodyHtml || '--'}</div>
        </div>
      `;
    }

    function renderCurrentTargetCards(rows, marketDate) {
      const mount = document.getElementById('currentTargetsCards');
      if (!mount) return;
      if (!Array.isArray(rows) || !rows.length) {
        renderMobileCardState('currentTargetsCards', '当前条件下没有符合的标的。');
        return;
      }

      mount.innerHTML = rows.map((row) => {
        const signalStateKey = formatCurrentSignalState(row);
        const chartUrl = buildChartUrl(row.symbol || '');
        const indicatorUrl = buildIndicatorUrl(row.symbol || '', marketDate);
        const signalUrl = buildSignalUrl(row.symbol || '', marketDate);
        return `
          <article class="mobile-data-card">
            <div class="mobile-data-head">
              <div>
                <a class="mobile-data-symbol" href="${chartUrl}">${escapeHtml(row.symbol || '--')}</a>
                <div class="mobile-data-time">${escapeHtml(row.latest_us_time || '--')}</div>
                <div class="mobile-data-subcopy">${escapeHtml(row.exchange || '--')} / ${escapeHtml(row.industry || '--')}</div>
              </div>
              <div>
                <div class="mobile-data-price">${escapeHtml(formatPrice(row.display_price ?? row.price))}</div>
                <div class="mobile-data-time">${escapeHtml(formatPct(row.display_day_change_pct ?? row.day_change_pct))}</div>
              </div>
            </div>

            <div class="mobile-chip-row">
              ${statusChip(row.target_status || '--', row.target_status || '')}
              ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}
              ${renderTechnicalStateWithTip(row)}
              ${statusChip(formatCurrentStateLabel(signalStateKey), signalStateKey)}
              ${row.has_live_bar ? statusChip(formatFreshness(row.freshness_min), Number(row.freshness_min) <= 30 ? 'active' : 'candidate') : statusChip('无当日bar', 'stale')}
              ${dataQualityChip(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('目标分 / 可操作分', `${escapeHtml(formatNumber(row.target_score || 0, 1))} / ${escapeHtml(formatNumber(row.tradability_score || 0, 0))}`)}
              ${buildMobileMetricCard('信号统计', `${escapeHtml(String(row.signal_count_today || 0))}${row.latest_signal_direction ? ` · ${escapeHtml(String(row.latest_signal_direction || '').toUpperCase())}` : ''}`)}
            </div>

            ${buildMobileSection('筛选理由', escapeHtml(row.scan_reason || row.note || '--'))}
            ${buildMobileSection('当前阶段', `<strong>${escapeHtml(row.workflow_label || formatCurrentStateLabel(row.workflow_stage || row.attention_state || 'watch'))}</strong> · ${escapeHtml(row.workflow_summary || '--')}`)}
            ${buildMobileSection('阶段阻塞', buildFlagPills(row.workflow_blockers, '当前无明显阻塞'))}
            ${buildMobileSection('下一步', escapeHtml(row.workflow_next_action || '--'))}
            ${buildMobileSection('可操作依据', buildReasonPills(row))}

            <div class="mobile-data-actions">
              <a class="mini-link" href="${chartUrl}">Chart</a>
              <a class="mini-link" href="${indicatorUrl}">指标</a>
              <a class="mini-link" href="${signalUrl}">信号</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function coalesceValue(row, keys, fallback = '') {
      for (const key of keys) {
        const value = row?.[key];
        if (value !== undefined && value !== null && value !== '') return value;
      }
      return fallback;
    }

    function normalizeWindowProgressList(value) {
      if (Array.isArray(value)) return value.filter((item) => item !== undefined && item !== null && item !== '');
      if (value === undefined || value === null || value === '') return [];
      if (typeof value === 'string' && /[,，]/.test(value)) {
        return value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
      }
      return [value];
    }

    function getWindowProgressStatus(row) {
      const signalState = row?.signal_state && typeof row.signal_state === 'object' ? row.signal_state : {};
      const windowFlags = row?.window_flags && typeof row.window_flags === 'object' ? row.window_flags : {};
      const explicit = String(coalesceValue({
        ...row,
        signal_state_stage: signalState.stage,
        signal_state_status: signalState.status,
        signal_state_raw: typeof row?.signal_state === 'string' ? row.signal_state : '',
      }, ['status', 'window_status', 'state', 'stage', 'signal_state_stage', 'signal_state_status', 'signal_state_raw'], '') || '').trim().toLowerCase();
      if (explicit) return explicit;
      if (row?.confirmed || row?.is_confirmed) return 'confirmed';
      if (row?.candidate || row?.is_candidate || coalesceValue(row, ['candidate_signal', 'signal_candidate'], '')) return 'candidate';
      if (row?.blocked || row?.is_blocked || normalizeWindowProgressList(coalesceValue(row, ['filter_reasons', 'filter_reason', 'blocked_reasons'], [])).length) return 'blocked';
      if (row?.used || row?.window_used) return 'used';
      if (row?.expired || row?.window_expired) return 'expired';
      if (row?.near_expiry || row?.is_near_expiry) return 'near_expiry';
      const upperActive = Boolean(coalesceValue({
        ...row,
        flag_upper_active: windowFlags.sd_upper_active ?? windowFlags.upper_active,
      }, ['upper_active', 'sd_upper_active', 'upper_window_active', 'flag_upper_active'], false));
      const lowerActive = Boolean(coalesceValue({
        ...row,
        flag_lower_active: windowFlags.sd_lower_active ?? windowFlags.lower_active,
      }, ['lower_active', 'sd_lower_active', 'lower_window_active', 'flag_lower_active'], false));
      if (upperActive && lowerActive) return 'both_active';
      if (upperActive) return 'upper_active';
      if (lowerActive) return 'lower_active';
      return 'no_window';
    }

    function getWindowProgressStatusTabForRow(row) {
      const status = getWindowProgressStatus(row);
      if (['candidate', 'blocked', 'near_expiry', 'no_window'].includes(status)) return status;
      if (['upper_active', 'lower_active', 'both_active'].includes(status)) return 'active';
      return 'other';
    }

    function getWindowProgressStatusCounts(rows = getSortedWindowProgressRows()) {
      const counts = WINDOW_PROGRESS_STATUS_TABS.reduce((acc, tab) => {
        acc[tab.key] = 0;
        return acc;
      }, {});
      counts.all = rows.length;
      rows.forEach((row) => {
        const tab = getWindowProgressStatusTabForRow(row);
        counts[tab] = (counts[tab] || 0) + 1;
      });
      return counts;
    }

    function getFilteredWindowProgressRows(rows = getSortedWindowProgressRows()) {
      const selectedStatus = normalizeWindowProgressStatusTab(activeWindowProgressStatus);
      if (selectedStatus === 'all') return rows;
      return rows.filter((row) => getWindowProgressStatusTabForRow(row) === selectedStatus);
    }

    function renderWindowProgressStatusTabs(rows = getSortedWindowProgressRows()) {
      const root = document.getElementById('windowProgressStatusTabs');
      if (!root) return;
      const counts = getWindowProgressStatusCounts(rows);
      const selectedStatus = normalizeWindowProgressStatusTab(activeWindowProgressStatus);
      root.querySelectorAll('[data-window-status]').forEach((button) => {
        const key = normalizeWindowProgressStatusTab(button.dataset.windowStatus);
        const active = key === selectedStatus;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        const countNode = button.querySelector('.window-progress-status-count');
        if (countNode) countNode.textContent = formatWindowProgressCount(counts[key] || 0);
      });
    }

    function getWindowProgressStatusLabel(value) {
      const labels = {
        no_window: 'no_window',
        upper_active: 'upper_active',
        lower_active: 'lower_active',
        both_active: 'both_active',
        near_expiry: 'near_expiry',
        expired: 'expired',
        used: 'used',
        candidate: 'candidate',
        blocked: 'blocked',
        confirmed: 'confirmed',
      };
      const key = String(value || '').trim().toLowerCase();
      return labels[key] || (key || '--');
    }

    function getWindowProgressPriority(row) {
      const status = getWindowProgressStatus(row);
      const rank = {
        candidate: 0,
        blocked: 1,
        near_expiry: 2,
        confirmed: 3,
        both_active: 4,
        upper_active: 5,
        lower_active: 5,
        no_window: 6,
        expired: 7,
        used: 8,
      };
      return rank[status] ?? 9;
    }

    function getWindowProgressNumber(row, keys, fallback = 0) {
      const value = Number(coalesceValue(row, keys, fallback));
      return Number.isFinite(value) ? value : fallback;
    }

    function getWindowComponentProgress(row) {
      const explicit = Number(coalesceValue(row, ['component_progress', 'components_progress', 'progress'], NaN));
      if (Number.isFinite(explicit)) return explicit;
      const components = row?.components && typeof row.components === 'object' ? row.components : {};
      const collected = normalizeWindowProgressList(coalesceValue({
        ...row,
        nested_collected_components: components.collected ?? components.ready,
      }, ['collected_components', 'components_collected', 'ready_components', 'nested_collected_components'], []));
      const missing = normalizeWindowProgressList(coalesceValue({
        ...row,
        nested_missing_components: components.missing,
      }, ['missing_components', 'components_missing', 'nested_missing_components'], []));
      const total = collected.length + missing.length;
      return total ? collected.length / total : 0;
    }

    function getSortedWindowProgressRows() {
      const rows = Array.isArray(windowProgressPayload.items) ? [...windowProgressPayload.items] : [];
      return rows.sort((left, right) => {
        const priorityDiff = getWindowProgressPriority(left) - getWindowProgressPriority(right);
        if (priorityDiff !== 0) return priorityDiff;
        const barsDiff = getWindowProgressNumber(left, ['bars_remaining', 'remaining_bars'], Number.MAX_SAFE_INTEGER)
          - getWindowProgressNumber(right, ['bars_remaining', 'remaining_bars'], Number.MAX_SAFE_INTEGER);
        if (barsDiff !== 0) return barsDiff;
        const progressDiff = getWindowComponentProgress(right) - getWindowComponentProgress(left);
        if (progressDiff !== 0) return progressDiff;
        const freshnessDiff = getWindowProgressNumber(left, ['freshness_min', 'freshness_minutes'], Number.MAX_SAFE_INTEGER)
          - getWindowProgressNumber(right, ['freshness_min', 'freshness_minutes'], Number.MAX_SAFE_INTEGER);
        if (freshnessDiff !== 0) return freshnessDiff;
        const scoreDiff = getWindowProgressNumber(right, ['target_score', 'score'], 0)
          - getWindowProgressNumber(left, ['target_score', 'score'], 0);
        if (scoreDiff !== 0) return scoreDiff;
        return String(left.symbol || '').localeCompare(String(right.symbol || ''));
      });
    }

    function formatWindowProgressCount(value) {
      const num = Number(value);
      return Number.isFinite(num) ? formatNumber(num, 0) : '--';
    }

    function formatWindowComponentProgress(row) {
      const value = getWindowComponentProgress(row);
      if (!Number.isFinite(value)) return '--';
      const pct = value <= 1 ? value * 100 : value;
      return `${formatNumber(pct, 0)}%`;
    }

    function buildWindowListPills(value, emptyText = '--') {
      const items = normalizeWindowProgressList(value);
      if (!items.length) return `<span class="muted">${escapeHtml(emptyText)}</span>`;
      return `<div class="reason-wrap window-progress-pill-wrap">${items.slice(0, 8).map((item) => `<span class="reason-pill">${escapeHtml(item)}</span>`).join('')}</div>`;
    }

    const WINDOW_COMPONENT_GROUP_ORDER = ['type1_long_trend', 'type3_short_mr', 'type2_long_mr', 'type4_short_trend'];
    const WINDOW_COMPONENT_GROUP_LABELS = {
      type1_long_trend: 'Type1 顺势多',
      type2_long_mr: 'Type2 回归多',
      type3_short_mr: 'Type3 回归空',
      type4_short_trend: 'Type4 顺势空',
    };

    function toFiniteNumber(value, fallback = NaN) {
      if (value === undefined || value === null || value === '') return fallback;
      const numberValue = Number(value);
      return Number.isFinite(numberValue) ? numberValue : fallback;
    }

    function getWindowComponentGroups(row) {
      const source = row?.component_groups && typeof row.component_groups === 'object'
        ? row.component_groups
        : (row?.component_detail && typeof row.component_detail === 'object' ? row.component_detail : {});
      const sourceKeys = Object.keys(source || {});
      if (!sourceKeys.length) return [];
      const orderedKeys = [
        ...WINDOW_COMPONENT_GROUP_ORDER,
        ...sourceKeys.filter((key) => !WINDOW_COMPONENT_GROUP_ORDER.includes(key)),
      ];
      const seen = new Set();
      return orderedKeys.map((key) => {
        if (seen.has(key)) return null;
        seen.add(key);
        const group = source[key];
        if (!group || typeof group !== 'object') return null;
        const active = group.active === true || String(group.active || '').trim().toLowerCase() === 'true';
        if (!active) return null;
        const present = normalizeWindowProgressList(group.present_labels ?? group.collected_labels ?? group.ready_labels ?? group.present ?? group.collected ?? group.ready);
        const missing = normalizeWindowProgressList(group.missing_labels ?? group.missing);
        const completed = toFiniteNumber(group.completed, present.length);
        const total = toFiniteNumber(group.total, Math.max(completed + missing.length, present.length + missing.length));
        const progress = toFiniteNumber(group.progress, total > 0 ? completed / total : NaN);
        return {
          key,
          label: group.label || WINDOW_COMPONENT_GROUP_LABELS[key] || key,
          present,
          missing,
          completed,
          total,
          progress,
          ready: Boolean(group.ready) || (total > 0 && completed >= total),
        };
      }).filter(Boolean);
    }

    function formatWindowComponentGroupProgress(group) {
      if (Number.isFinite(group?.completed) && Number.isFinite(group?.total) && group.total > 0) {
        return `${formatWindowProgressCount(group.completed)}/${formatWindowProgressCount(group.total)}`;
      }
      if (Number.isFinite(group?.progress)) {
        return `${formatNumber((group.progress <= 1 ? group.progress * 100 : group.progress), 0)}%`;
      }
      return '--';
    }

    function buildWindowComponentGroupColumn(row, kind, fallbackValue, emptyText = '--') {
      const groups = getWindowComponentGroups(row);
      if (!groups.length) return buildWindowListPills(fallbackValue, emptyText);
      return `
        <div class="window-component-groups">
          ${groups.map((group) => {
            const items = kind === 'missing' ? group.missing : group.present;
            const groupEmptyText = kind === 'missing' && group.ready ? '已完成' : emptyText;
            return `
              <div class="window-component-group ${group.ready ? 'ready' : ''}" data-component-group="${escapeHtml(group.key)}">
                <div class="window-component-group-head">
                  <span class="window-component-group-title">${escapeHtml(group.label)}</span>
                  <span class="window-component-group-progress">${escapeHtml(formatWindowComponentGroupProgress(group))}</span>
                </div>
                ${buildWindowListPills(items, groupEmptyText)}
              </div>
            `;
          }).join('')}
        </div>
      `;
    }

    function formatWindowSide(row, side) {
      const prefix = side === 'upper' ? 'upper' : 'lower';
      const windowData = row?.[`${prefix}_window`] && typeof row[`${prefix}_window`] === 'object' ? row[`${prefix}_window`] : {};
      const windowFlags = row?.window_flags && typeof row.window_flags === 'object' ? row.window_flags : {};
      const source = {
        ...row,
        window_active: windowData.active,
        window_status: windowData.status,
        window_age_bars: windowData.age_bars ?? windowData.bars_collected,
        window_bars_remaining: windowData.bars_remaining ?? windowData.remaining_bars,
        flag_active: windowFlags[`sd_${prefix}_active`] ?? windowFlags[`${prefix}_active`],
      };
      const active = Boolean(coalesceValue(source, [`${prefix}_active`, `sd_${prefix}_active`, `${prefix}_window_active`, 'window_active', 'flag_active'], false));
      const status = String(coalesceValue(source, [`${prefix}_status`, `${prefix}_window_status`, 'window_status'], active ? `${prefix}_active` : 'inactive'));
      const age = coalesceValue(source, [`${prefix}_age_bars`, `${prefix}_window_age_bars`, `${prefix}_bars_collected`, 'window_age_bars'], '');
      const remaining = coalesceValue(source, [`${prefix}_bars_remaining`, `${prefix}_remaining_bars`, 'window_bars_remaining'], '');
      const parts = [];
      if (age !== '') parts.push(`age ${formatWindowProgressCount(age)}`);
      if (remaining !== '') parts.push(`left ${formatWindowProgressCount(remaining)}`);
      return `
        ${statusChip(status, active ? `${prefix}_active` : status)}
        ${parts.length ? `<div class="muted mono window-progress-subline">${escapeHtml(parts.join(' · '))}</div>` : ''}
      `;
    }

    function getWindowTraceUrl(row, marketDate) {
      const explicit = String(coalesceValue(row, ['trace_url', 'trace_link', 'url'], '') || '').trim();
      if (explicit) return explicit;
      return buildPageUrl('/ibkr_chart.html', {
        symbol: row.symbol || '',
        interval: '5m',
        date: marketDate || '',
        trace: 1,
      }, { environment: currentEnvironment });
    }

    function getWindowCandidateLabel(row) {
      const signalState = row?.signal_state && typeof row.signal_state === 'object' ? row.signal_state : {};
      const candidate = coalesceValue({
        ...row,
        signal_state_label: signalState.label,
        signal_state_reason: signalState.reason,
      }, ['candidate_signal_label', 'signal_label', 'signal_state_label', 'candidate_signal', 'signal_candidate'], '');
      if (candidate && typeof candidate === 'object') {
        return String(candidate.signal || candidate.label || candidate.direction || candidate.status || '--');
      }
      if (candidate) return String(candidate);
      const direction = String(coalesceValue(row, ['candidate_direction', 'signal_direction', 'direction'], '') || '').trim().toUpperCase();
      const status = getWindowProgressStatus(row);
      if (direction) return direction;
      return ['candidate', 'confirmed', 'blocked'].includes(status) ? status : '--';
    }

    function renderWindowProgressCards(rows, marketDate, emptyMessage = '当前没有窗口进度记录。') {
      const mount = document.getElementById('windowProgressCards');
      if (!mount) return;
      if (!Array.isArray(rows) || !rows.length) {
        renderMobileCardState('windowProgressCards', emptyMessage);
        return;
      }

      mount.innerHTML = rows.map((row) => {
        const status = getWindowProgressStatus(row);
        const latestBar = coalesceValue(row, ['latest_5m_bar', 'latest_bar_us', 'latest_us_time', 'bar_time_us', 'latest_bar_time'], '--');
        const components = row?.components && typeof row.components === 'object' ? row.components : {};
        const collected = coalesceValue({
          ...row,
          nested_collected_components: components.collected ?? components.ready,
        }, ['collected_components', 'components_collected', 'ready_components', 'nested_collected_components'], []);
        const missing = coalesceValue({
          ...row,
          nested_missing_components: components.missing,
        }, ['missing_components', 'components_missing', 'nested_missing_components'], []);
        const filterReasons = coalesceValue(row, ['filter_reasons', 'filter_reason', 'blocked_reasons', 'block_reason'], []);
        const traceUrl = getWindowTraceUrl(row, marketDate);
        return `
          <article class="mobile-data-card window-progress-card">
            <div class="mobile-data-head">
              <div>
                <a class="mobile-data-symbol" href="${buildChartUrl(row.symbol || '')}">${escapeHtml(row.symbol || '--')}</a>
                <div class="mobile-data-time">${escapeHtml(latestBar || '--')}</div>
              </div>
              <div class="mobile-chip-row">
                ${statusChip(getWindowProgressStatusLabel(status), status)}
              </div>
            </div>

            <div class="mobile-chip-row">
              ${statusChip(formatFreshness(coalesceValue(row, ['freshness_min', 'freshness_minutes'], NaN)), Number(coalesceValue(row, ['freshness_min', 'freshness_minutes'], NaN)) <= 30 ? 'active' : 'candidate')}
              ${statusChip(`progress ${formatWindowComponentProgress(row)}`, 'config')}
              ${statusChip(`left ${formatWindowProgressCount(coalesceValue(row, ['bars_remaining', 'remaining_bars'], NaN))}`, Number(coalesceValue(row, ['bars_remaining', 'remaining_bars'], NaN)) <= 2 ? 'near_expiry' : 'neutral')}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('上轨窗口', formatWindowSide(row, 'upper'))}
              ${buildMobileMetricCard('下轨窗口', formatWindowSide(row, 'lower'))}
            </div>

            ${buildMobileSection('已收集组件', buildWindowComponentGroupColumn(row, 'present', collected, '暂无'))}
            ${buildMobileSection('缺失组件', buildWindowComponentGroupColumn(row, 'missing', missing, '无缺失'))}
            ${buildMobileSection('候选信号', escapeHtml(getWindowCandidateLabel(row)))}
            ${buildMobileSection('过滤原因', buildWindowListPills(filterReasons, '未触发过滤'))}

            <div class="mobile-data-actions">
              <a class="mini-link" href="${traceUrl}">Trace</a>
              <a class="mini-link" href="${buildChartUrl(row.symbol || '')}">Chart</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function renderWindowProgressTable() {
      const tbody = document.getElementById('windowProgressTable');
      const meta = document.getElementById('windowProgressMeta');
      const metaSecondary = document.getElementById('windowProgressMetaSecondary');
      if (!tbody || !meta || !metaSecondary) return;
      const allRows = getSortedWindowProgressRows();
      const rows = getFilteredWindowProgressRows(allRows);
      const marketDate = windowProgressPayload.market_date || todayTargetsPayload.market_date || document.getElementById('marketDate')?.value || getUsDate();
      const counts = getWindowProgressStatusCounts(allRows);
      const selectedStatus = normalizeWindowProgressStatusTab(activeWindowProgressStatus);
      const selectedLabel = WINDOW_PROGRESS_STATUS_TABS.find((tab) => tab.key === selectedStatus)?.label || '全部';
      renderWindowProgressStatusTabs(allRows);
      meta.textContent = selectedStatus === 'all'
        ? `${allRows.length} 条 · candidate ${counts.candidate || 0} · blocked ${counts.blocked || 0} · near_expiry ${counts.near_expiry || 0} · active ${counts.active || 0}`
        : `${rows.length}/${allRows.length} 条 · 当前 ${selectedLabel} · candidate ${counts.candidate || 0} · blocked ${counts.blocked || 0} · near_expiry ${counts.near_expiry || 0} · active ${counts.active || 0}`;
      metaSecondary.textContent = windowProgressPayload.computed_at_us
        ? `计算时间 ${windowProgressPayload.computed_at_us}。筛选: ${selectedLabel}。排序: candidate/blocked/near_expiry, bars_remaining asc, component_progress desc, freshness_min asc, target_score desc。`
        : `筛选: ${selectedLabel}。排序: candidate/blocked/near_expiry, bars_remaining asc, component_progress desc, freshness_min asc, target_score desc。`;

      if (!allRows.length) {
        tbody.innerHTML = '<tr><td colspan="11" class="empty-state">当前没有窗口进度记录。</td></tr>';
        renderWindowProgressCards([], marketDate);
        return;
      }
      if (!rows.length) {
        const emptyMessage = `当前没有${selectedLabel}状态的窗口进度记录。`;
        tbody.innerHTML = `<tr><td colspan="11" class="empty-state">${escapeHtml(emptyMessage)}</td></tr>`;
        renderWindowProgressCards([], marketDate, emptyMessage);
        return;
      }

      tbody.innerHTML = rows.map((row) => {
        const status = getWindowProgressStatus(row);
        const latestBar = coalesceValue(row, ['latest_5m_bar', 'latest_bar_us', 'latest_us_time', 'bar_time_us', 'latest_bar_time'], '--');
        const freshness = coalesceValue(row, ['freshness_min', 'freshness_minutes'], NaN);
        const barsRemaining = coalesceValue(row, ['bars_remaining', 'remaining_bars'], NaN);
        const components = row?.components && typeof row.components === 'object' ? row.components : {};
        const collected = coalesceValue({
          ...row,
          nested_collected_components: components.collected ?? components.ready,
        }, ['collected_components', 'components_collected', 'ready_components', 'nested_collected_components'], []);
        const missing = coalesceValue({
          ...row,
          nested_missing_components: components.missing,
        }, ['missing_components', 'components_missing', 'nested_missing_components'], []);
        const filterReasons = coalesceValue(row, ['filter_reasons', 'filter_reason', 'blocked_reasons', 'block_reason'], []);
        const traceUrl = getWindowTraceUrl(row, marketDate);
        return `
          <tr>
            <td>
              <a class="symbol-link" href="${buildChartUrl(row.symbol || '')}">${escapeHtml(row.symbol || '--')}</a><br>
              ${statusChip(getWindowProgressStatusLabel(status), status)}<br>
              <span class="muted">target ${escapeHtml(formatNumber(coalesceValue(row, ['target_score', 'score'], 0), 1))}</span>
            </td>
            <td><span class="mono">${escapeHtml(latestBar || '--')}</span></td>
            <td>${statusChip(formatFreshness(freshness), Number(freshness) <= 30 ? 'active' : 'candidate')}</td>
            <td>${formatWindowSide(row, 'upper')}</td>
            <td>${formatWindowSide(row, 'lower')}</td>
            <td>
              <span class="mono">${escapeHtml(formatWindowProgressCount(barsRemaining))}</span><br>
              <span class="muted">progress ${escapeHtml(formatWindowComponentProgress(row))}</span>
            </td>
            <td>${buildWindowComponentGroupColumn(row, 'present', collected, '暂无')}</td>
            <td>${buildWindowComponentGroupColumn(row, 'missing', missing, '无缺失')}</td>
            <td>${escapeHtml(getWindowCandidateLabel(row))}</td>
            <td>${buildWindowListPills(filterReasons, '未触发过滤')}</td>
            <td>
              <div class="row-actions">
                <a class="mini-link" href="${traceUrl}">Trace</a>
              </div>
            </td>
          </tr>
        `;
      }).join('');
      renderWindowProgressCards(rows, marketDate);
    }

    function renderScreenerCards(rows) {
      const mount = document.getElementById('screenerCards');
      if (!mount) return;
      if (!Array.isArray(rows) || !rows.length) {
        renderMobileCardState('screenerCards', '当前条件下没有符合的标的。');
        return;
      }

      mount.innerHTML = rows.map((row) => {
        const symbol = String(row.symbol || '').trim().toUpperCase();
        const checked = selectedSymbols.has(symbol) ? 'checked' : '';
        const chartUrl = buildChartUrl(row.symbol || '');
        return `
          <article class="mobile-data-card">
            <div class="mobile-data-head">
              <div>
                <a class="mobile-data-symbol" href="${chartUrl}">${escapeHtml(row.symbol || '--')}</a>
                <div class="mobile-data-time">${escapeHtml(row.exchange || '--')} / ${escapeHtml(row.industry || '--')}</div>
                <div class="mobile-data-subcopy">${escapeHtml(row.display_price_source || row.price_source || '--')}</div>
              </div>
              <label class="mobile-select-control">
                <input class="row-check" type="checkbox" ${checked} onchange="toggleSelection('${escapeHtml(row.symbol || '')}', this.checked)" />
                <span>选择</span>
              </label>
            </div>

            <div class="mobile-chip-row">
              ${statusChip(row.target_status || 'none', row.target_status || '')}
              ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}
              ${buildScorePill(row)}
              ${statusChip(row.is_operable ? '可操作' : '人工复核', row.is_operable ? 'active' : 'neutral')}
              ${row.has_live_bar ? statusChip(formatFreshness(row.freshness_min), Number(row.freshness_min) <= 30 ? 'active' : 'candidate') : statusChip('无当日bar', 'stale')}
              ${dataQualityChip(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('价格 / 涨跌', `${escapeHtml(formatPrice(row.display_price ?? row.price))}<br><span class="mobile-data-subcopy mono">${escapeHtml(formatPct(row.display_day_change_pct ?? row.day_change_pct))}</span>`)}
              ${buildMobileMetricCard('ATR / 量能', `ATR ${escapeHtml(formatPct(row.atr_pct))}<br><span class="mobile-data-subcopy">10D ${escapeHtml(formatVolume(row.avg_10d_volume))} · PRE ${escapeHtml(formatVolume(row.premarket_volume))}</span>`)}
            </div>

            ${buildMobileSection('筛选理由', escapeHtml(row.scan_reason || row.note || '--'))}
            ${buildMobileSection('可操作依据', buildReasonPills(row))}

            <div class="mobile-data-actions">
              <a class="mini-link" href="${chartUrl}">Chart</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function renderDailyTargetCards(items) {
      const mount = document.getElementById('dailyTargetsCards');
      if (!mount) return;
      if (!Array.isArray(items) || !items.length) {
        renderMobileCardState('dailyTargetsCards', '当前日期没有目标池记录。');
        return;
      }

      mount.innerHTML = items.map((item) => `
        <article class="mobile-data-card">
          <div class="mobile-data-head">
            <div>
              <a class="mobile-data-symbol" href="${buildChartUrl(item.symbol || '')}">${escapeHtml(item.symbol || '--')}</a>
              <div class="mobile-data-time">${escapeHtml(item.exchange || '--')}</div>
              <div class="mobile-data-subcopy">${escapeHtml(item.date || '--')}</div>
            </div>
            <div class="mobile-chip-row">
              ${statusChip(item.status || 'candidate', item.status || 'candidate')}
              ${statusChip(item.direction_bias || 'neutral', item.direction_bias || 'neutral')}
            </div>
          </div>

          <div class="mobile-data-grid">
            ${buildMobileMetricCard('分数', escapeHtml(Number(item.score || 0).toFixed(1)))}
            ${buildMobileMetricCard('更新时间', `${escapeHtml(item.us_time || '--')}<br><span class="mobile-data-subcopy">${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</span>`)}
          </div>

          ${buildMobileSection('理由', escapeHtml(item.scan_reason || '--'))}

          <div class="mobile-data-actions">
            <a class="mini-link" href="${buildChartUrl(item.symbol || '')}">Chart</a>
            <button class="mini-btn" type="button" onclick="editDailyTargetItem('${escapeHtml(item.id || '')}')">编辑</button>
            <button class="mini-btn danger" type="button" onclick="removeDailyTargetItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}')">删除</button>
          </div>
        </article>
      `).join('');
    }

    function renderWatchlistCards(items) {
      const mount = document.getElementById('watchlistCards');
      if (!mount) return;
      if (!Array.isArray(items) || !items.length) {
        renderMobileCardState('watchlistCards', `当前没有符合条件的 ${getWatchlistRoleLabel()} 记录。`);
        return;
      }

      mount.innerHTML = items.map((item) => {
        const configItem = isConfigMonitorItem(item);
        return `
          <article class="mobile-data-card">
            <div class="mobile-data-head">
              <div>
                <a class="mobile-data-symbol" href="${buildChartUrl(item.symbol || '')}">${escapeHtml(item.symbol || '--')}</a>
                <div class="mobile-data-time">${escapeHtml(item.exchange || '--')} / ${escapeHtml(item.industry || '--')}</div>
              </div>
              <div class="mobile-chip-row">
                <span class="env-badge ${resolveRecordEnvClass(item.environment)}">${escapeHtml(formatRecordEnvironment(item.environment))}</span>
                ${configItem ? statusChip('CONFIG', 'config') : statusChip(formatWatchlistRole(item.symbol_role), normalizeWatchlistRole(item.symbol_role))}
              </div>
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('成员属性', escapeHtml(formatWatchlistMember(item)))}
              ${buildMobileMetricCard('更新时间', `${escapeHtml(item.updated_us || item.us_time || '--')}<br><span class="mobile-data-subcopy">${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</span>`)}
            </div>

            ${buildMobileSection('备注', escapeHtml(item.note || '--'))}

            <div class="mobile-data-actions">
              <a class="mini-link" href="${buildChartUrl(item.symbol || '')}">Chart</a>
              ${configItem
                ? `<a class="mini-link" href="${getConfigPageUrl()}">改配置</a>`
                : `<button class="mini-btn" type="button" onclick="editItem('${escapeHtml(item.id || '')}')">编辑</button>
                   <button class="mini-btn danger" type="button" onclick="removeItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}', '${escapeHtml(formatRecordEnvironment(item.environment))}')">删除</button>`
              }
            </div>
          </article>
        `;
      }).join('');
    }

    function mergeTodayTargetRowWithRealtimeQuote(row) {
      const quote = getRealtimeQuote(row?.symbol);
      return {
        ...row,
        display_price: quote?.last_price != null ? quote.last_price : row?.price,
        display_day_change_pct: quote?.day_change_pct != null ? quote.day_change_pct : row?.day_change_pct,
      };
    }

    async function refreshTodayTargetQuotes(items, requestToken) {
      if (!Array.isArray(items) || !items.length) return;
      try {
        await fetchRealtimeQuotesIfNeeded(items.map((row) => row?.symbol).filter(Boolean), {
          reset: false,
          maxAgeMs: 15000,
        });
      } catch (error) {
        console.warn('加载今日标的实时报价失败:', error);
        return;
      }
      if (requestToken !== currentTargetState.requestToken) return;
      todayTargetsPayload = {
        ...todayTargetsPayload,
        items: Array.isArray(todayTargetsPayload.items)
          ? todayTargetsPayload.items.map((row) => mergeTodayTargetRowWithRealtimeQuote(row))
          : [],
      };
      filteredCurrentTargetRows = Array.isArray(todayTargetsPayload.items) ? todayTargetsPayload.items : [];
      renderCurrentTargetTable();
      if (activeTab === 'screener' && activeScreenerView === 'current') updateHero();
    }

    function getCurrentTargetFilters() {
      return {
        search: String(document.getElementById('currentTargetSearch')?.value || '').trim().toUpperCase(),
        technical_state: document.getElementById('currentTechnicalStateFilter')?.value || '',
        signal_state: document.getElementById('currentSignalStateFilter')?.value || '',
        target_status: document.getElementById('currentTargetStatusFilter')?.value || '',
        direction_bias: document.getElementById('currentDirectionBiasFilter')?.value || '',
        ready_only: Boolean(document.getElementById('currentReadyOnly')?.checked),
        signaled_only: Boolean(document.getElementById('currentSignaledOnly')?.checked),
        sort_by: document.getElementById('currentTargetSortBy')?.value || 'attention_asc',
        per_page: Number(document.getElementById('currentTargetPageSize')?.value || currentTargetState.perPage || 10) || 10,
      };
    }

    function getCurrentTargetPageSize() {
      const rawValue = Number(document.getElementById('currentTargetPageSize')?.value || currentTargetState.perPage || 10);
      if (!Number.isFinite(rawValue) || rawValue <= 0) return 10;
      return Math.max(1, Math.min(100, Math.trunc(rawValue)));
    }

    function getCurrentTargetRequestParams(marketDate) {
      const filters = getCurrentTargetFilters();
      currentTargetState.perPage = getCurrentTargetPageSize();
      return {
        environment: currentEnvironment,
        market_date: marketDate,
        search: filters.search,
        technical_state: filters.technical_state,
        signal_state: filters.signal_state,
        target_status: filters.target_status,
        direction_bias: filters.direction_bias,
        ready_only: filters.ready_only,
        signaled_only: filters.signaled_only,
        sort_by: filters.sort_by,
        page: currentTargetState.page,
        per_page: currentTargetState.perPage,
      };
    }

    function getCurrentTargetPageButtons(page, totalPages) {
      const pages = [];
      const pushPage = (value) => {
        if (pages.includes(value)) return;
        pages.push(value);
      };
      pushPage(1);
      for (let index = page - 1; index <= page + 1; index += 1) {
        if (index > 1 && index < totalPages) pushPage(index);
      }
      if (totalPages > 1) pushPage(totalPages);
      return pages.sort((left, right) => left - right);
    }

    function renderCurrentTargetPagination() {
      const page = Math.max(1, Number(todayTargetsPayload.page || currentTargetState.page || 1) || 1);
      const totalPages = Math.max(1, Number(todayTargetsPayload.total_pages || 1) || 1);
      const filteredTotal = Math.max(0, Number(todayTargetsPayload.filtered_total || 0) || 0);
      const returnedCount = Array.isArray(filteredCurrentTargetRows) ? filteredCurrentTargetRows.length : 0;
      const pageButtons = getCurrentTargetPageButtons(page, totalPages);
      const shouldShowPagination = totalPages > 1;
      const controls = [];

      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setCurrentTargetPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>`);
      let lastPage = 0;
      pageButtons.forEach((value) => {
        if (lastPage && value - lastPage > 1) {
          controls.push('<span class="pagination-ellipsis">...</span>');
        }
        controls.push(`<button class="mini-btn pagination-btn ${value === page ? 'active' : ''}" type="button" onclick="setCurrentTargetPage(${value})">${value}</button>`);
        lastPage = value;
      });
      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setCurrentTargetPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`);

      ['currentTargetsPaginationTop', 'currentTargetsPaginationBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (!mount) return;
        const bar = mount.closest('.pagination-bar');
        if (bar) bar.hidden = !shouldShowPagination;
        mount.innerHTML = shouldShowPagination ? controls.join('') : '';
      });

      const statusText = filteredTotal
        ? `第 ${page} / ${totalPages} 页 · 本页 ${returnedCount} 条 · 过滤后 ${filteredTotal} 条`
        : '第 1 / 1 页 · 当前没有结果';
      ['currentTargetsPaginationStatusTop', 'currentTargetsPaginationStatusBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (mount) mount.textContent = shouldShowPagination ? statusText : '';
      });
    }

    function scheduleCurrentTargetReload() {
      window.clearTimeout(currentTargetState.searchDebounceId);
      currentTargetState.searchDebounceId = window.setTimeout(() => {
        applyCurrentTargetFilters({ resetPage: true });
      }, 260);
    }

    function applyCurrentTargetFilters({ resetPage = true } = {}) {
      if (resetPage) currentTargetState.page = 1;
      loadTodayTargets(false);
    }

    window.setCurrentTargetPage = function setCurrentTargetPage(page) {
      const totalPages = Math.max(1, Number(todayTargetsPayload.total_pages || 1) || 1);
      const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
      if (nextPage === currentTargetState.page) return;
      currentTargetState.page = nextPage;
      window.clearTimeout(currentTargetState.searchDebounceId);
      loadTodayTargets(false);
    }

    function formatCurrentSignalState(row) {
      const latestStatus = String(row.latest_signal_status || '').trim().toLowerCase();
      if (!row.has_signal_today) return 'no_signal';
      return latestStatus || 'signaled';
    }

    function formatCurrentStateLabel(value) {
      const key = String(value || '').trim().toLowerCase();
      const labels = {
        ready: 'ready',
        watch: 'watch',
        stale: 'stale',
        signaled: 'signaled',
        no_signal: 'no signal',
        awaiting_confirm: 'awaiting confirm',
        pending: 'pending',
        submitted: 'submitted',
        protected_active: 'protected active',
        protection_incomplete: 'protection incomplete',
        executed: 'executed',
        closed: 'closed',
        expired: 'expired',
        rejected: 'rejected',
        ready_no_signal: 'ready no signal',
      };
      return labels[key] || (key ? key.replace(/_/g, ' ') : '--');
    }

    function buildFlagPills(items, emptyText = '--') {
      const list = Array.isArray(items) ? items.filter(Boolean) : [];
      if (!list.length) return `<span class="muted">${escapeHtml(emptyText)}</span>`;
      return `<div class="reason-wrap">${list.slice(0, 5).map((item) => `<span class="reason-pill">${escapeHtml(item)}</span>`).join('')}</div>`;
    }

    function renderCurrentTargetTable() {
      const tbody = document.getElementById('currentTargetsTable');
      const meta = document.getElementById('currentTargetsMeta');
      const metaSecondary = document.getElementById('currentTargetsMetaSecondary');
      const jumpLink = document.getElementById('todayTargetsJumpLink');
      const marketDate = todayTargetsPayload.market_date || document.getElementById('marketDate').value || getUsDate();
      const workflow = todayTargetsPayload.workflow || {};
      jumpLink.href = workflow.primary_view_url || buildPageUrl('/ibkr_screener.html', {
        tab: 'screener',
        view: 'current',
        date: marketDate,
        market_date: marketDate,
      }, { environment: currentEnvironment });

      const rows = Array.isArray(filteredCurrentTargetRows) ? filteredCurrentTargetRows : [];
      const summary = todayTargetsPayload.summary || {};
      const filteredSummary = todayTargetsPayload.filtered_summary || {};
      const filteredTotal = Math.max(0, Number(todayTargetsPayload.filtered_total || rows.length || 0) || 0);
      const currentPage = Math.max(1, Number(todayTargetsPayload.page || currentTargetState.page || 1) || 1);
      const totalPages = Math.max(1, Number(todayTargetsPayload.total_pages || 1) || 1);
      const readyCount = rows.filter((row) => row.technical_state === 'ready').length;
      const needsActionCount = rows.filter((row) => ['awaiting_confirm', 'pending', 'submitted', 'protected_active', 'protection_incomplete'].includes(String(row.latest_signal_status || ''))).length;
      const signaledCount = rows.filter((row) => row.has_signal_today).length;
      const scanTimeEt = String(workflow.scan_summary_time_et || '09:20');
      const openCheckTimeEt = String(workflow.open_check_time_et || scanTimeEt);
      const workflowTimingCopy = scanTimeEt === openCheckTimeEt
        ? `${scanTimeEt} ET 日筛；${workflow.intraday_refresh_rule || '5m close-driven'}`
        : `${scanTimeEt} ET 日筛；${openCheckTimeEt} ET 检查`;
      meta.textContent = `${currentPage}/${totalPages} 页 · ${rows.length} 条 · ready ${readyCount} · signaled ${signaledCount} · action ${needsActionCount} · ${filteredTotal}/${summary.total || 0}`;
      metaSecondary.textContent = `ready ${filteredSummary.ready_count || 0} · signaled ${filteredSummary.signaled_count || 0} · action ${filteredSummary.needs_action_count || 0}。${workflowTimingCopy}。`;
      renderCurrentTargetPagination();

      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">当前条件下没有符合的标的。</td></tr>';
        renderCurrentTargetCards([], marketDate);
        return;
      }

      tbody.innerHTML = rows.map((row) => {
        const marketDateToken = todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || getUsDate();
        const signalUrl = buildPageUrl('/ibkr_signals.html', {
          date: marketDateToken,
          search: row.symbol || '',
        }, { environment: currentEnvironment });
        const indicatorUrl = buildPageUrl('/ibkr_indicators.html', {
          date: marketDateToken,
          search: row.symbol || '',
        }, { environment: currentEnvironment });
        const chartUrl = buildPageUrl('/ibkr_chart.html', {
          symbol: row.symbol || '',
          interval: '5m',
        }, { environment: currentEnvironment });
        return `
          <tr>
            <td>
              <a class="symbol-link" href="${chartUrl}">${escapeHtml(row.symbol || '--')}</a><br>
              <span class="muted mono">${escapeHtml(row.latest_us_time || '--')}</span><br>
              <span class="muted">${escapeHtml(row.exchange || '--')} / ${escapeHtml(row.industry || '--')}</span>
            </td>
            <td>
              <strong>${escapeHtml(formatPrice(row.display_price ?? row.price))}</strong><br>
              <span class="mono">${escapeHtml(formatPct(row.display_day_change_pct ?? row.day_change_pct))}</span><br>
              ${row.has_live_bar ? statusChip(formatFreshness(row.freshness_min), Number(row.freshness_min) <= 30 ? 'active' : 'candidate') : statusChip('无当日bar', 'stale')}
            </td>
            <td>
              ${statusChip(row.target_status || '--', row.target_status || '')}<br>
              ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}<br>
              <span class="muted">target ${escapeHtml(formatNumber(row.target_score || 0, 1))} · tradability ${escapeHtml(formatNumber(row.tradability_score || 0, 0))}</span>
            </td>
            <td>
              ${renderTechnicalStateWithTip(row)}<br>
              <div style="margin-top:8px;">${buildFlagPills(row.technical_flags, '暂无技术标签')}</div>
            </td>
            <td>
              ${statusChip(formatCurrentStateLabel(formatCurrentSignalState(row)), formatCurrentSignalState(row))}<br>
              <span class="muted">${escapeHtml(row.latest_signal_time || (row.has_signal_today ? '--' : '今日未出信号'))}</span><br>
              <span class="muted">count ${escapeHtml(String(row.signal_count_today || 0))}${row.latest_signal_direction ? ` · ${escapeHtml(String(row.latest_signal_direction || '').toUpperCase())}` : ''}</span>
              ${row.latest_signal_id ? `<br><span class="muted mono">${escapeHtml(row.latest_signal_id)}</span>` : ''}
            </td>
            <td>
              <div class="reason-block">
                <div class="reason-label">筛选理由</div>
                <div class="reason-copy">${escapeHtml(row.scan_reason || row.note || '--')}</div>
              </div>
              <div class="reason-block" style="margin-top:10px;">
                <div class="reason-label">当前阶段</div>
                <div class="reason-copy"><strong>${escapeHtml(row.workflow_label || formatCurrentStateLabel(row.workflow_stage || row.attention_state || 'watch'))}</strong> · ${escapeHtml(row.workflow_summary || '--')}</div>
              </div>
              <div class="reason-block" style="margin-top:10px;">
                <div class="reason-label">阶段阻塞</div>
                ${buildFlagPills(row.workflow_blockers, '当前无明显阻塞')}
              </div>
              <div class="reason-block" style="margin-top:10px;">
                <div class="reason-label">下一步</div>
                <div class="reason-copy">${escapeHtml(row.workflow_next_action || '--')}</div>
              </div>
              <div class="reason-block" style="margin-top:10px;">
                <div class="reason-label">可操作依据</div>
                ${buildReasonPills(row)}
              </div>
              <div class="row-actions" style="margin-top:12px;">
                <a class="mini-link" href="${chartUrl}">Chart</a>
                <a class="mini-link" href="${indicatorUrl}">指标</a>
                <a class="mini-link" href="${signalUrl}">信号</a>
              </div>
            </td>
          </tr>
        `;
      }).join('');
      renderCurrentTargetCards(rows, marketDate);
    }

    async function loadTodayTargets(showToastOnSuccess = false) {
      const marketDate = document.getElementById('marketDate').value || getDailyTargetDate() || getUsDate();
      syncManualDailyScanButton();
      window.clearTimeout(currentTargetState.searchDebounceId);
      const requestToken = ++currentTargetState.requestToken;
      document.getElementById('currentTargetsMeta').textContent = '正在加载当前标的...';
      document.getElementById('currentTargetsMetaSecondary').textContent = '正在计算技术状态与今日信号聚合...';
      document.getElementById('currentTargetsTable').innerHTML = '<tr><td colspan="6" class="empty-state">加载中...</td></tr>';
      renderMobileCardState('currentTargetsCards', '正在加载当前标的...');
      renderCurrentTargetPagination();

      try {
        const payload = await requestJson(`/api/custom/ibkr/today-targets${buildQuery(getCurrentTargetRequestParams(marketDate))}`);
        if (requestToken !== currentTargetState.requestToken) return;
        runtimeCurrentMarketDate = String(payload?.current_market_date || payload?.market_date || runtimeCurrentMarketDate || '').trim();
        const items = Array.isArray(payload?.items) ? payload.items : [];
        todayTargetsPayload = {
          ...(payload || { items: [], summary: {}, market_date: marketDate, filtered_total: 0, total_pages: 1, page: 1 }),
          items: items.map((row) => mergeTodayTargetRowWithRealtimeQuote(row)),
        };
        currentTargetState.page = Math.max(1, Number(todayTargetsPayload.page || currentTargetState.page || 1) || 1);
        currentTargetState.perPage = getCurrentTargetPageSize();
        filteredCurrentTargetRows = Array.isArray(todayTargetsPayload.items) ? todayTargetsPayload.items : [];
        renderRulesBoard();
        renderCurrentTargetTable();
        setPageRefreshTime();
        if (activeTab === 'screener' && activeScreenerView === 'current') updateHero();
        void refreshTodayTargetQuotes(items, requestToken);
        if (showToastOnSuccess) showToast('今日交易标的已刷新');
      } catch (error) {
        if (requestToken !== currentTargetState.requestToken) return;
        todayTargetsPayload = { items: [], summary: {}, market_date: marketDate, filtered_total: 0, total_pages: 1, page: 1, filtered_summary: {} };
        filteredCurrentTargetRows = [];
        document.getElementById('currentTargetsMeta').textContent = `加载失败: ${error.message || error}`;
        document.getElementById('currentTargetsMetaSecondary').textContent = '当前标的榜加载失败。';
        document.getElementById('currentTargetsTable').innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('currentTargetsCards', error.message || error);
        renderCurrentTargetPagination();
        renderRulesBoard();
        if (activeTab === 'screener' && activeScreenerView === 'current') updateHero();
      }
    }

    async function loadWindowProgress(showToastOnSuccess = false, { force = false } = {}) {
      if (!initAuth()) return;
      const marketDate = document.getElementById('marketDate').value || getDailyTargetDate() || getUsDate();
      const loadKey = `${currentEnvironment}::${marketDate}`;
      if (!force && windowProgressState.loadedKey === loadKey && Array.isArray(windowProgressPayload.items)) {
        renderWindowProgressTable();
        setPageRefreshTime();
        if (activeTab === 'screener' && activeScreenerView === 'window-progress') updateHero();
        if (showToastOnSuccess) showToast('窗口进度已刷新');
        return windowProgressPayload;
      }

      const requestToken = ++windowProgressState.requestToken;
      const table = document.getElementById('windowProgressTable');
      const meta = document.getElementById('windowProgressMeta');
      const metaSecondary = document.getElementById('windowProgressMetaSecondary');
      if (meta) meta.textContent = '正在加载窗口进度...';
      if (metaSecondary) metaSecondary.textContent = '正在读取 active window progress...';
      if (table) table.innerHTML = '<tr><td colspan="11" class="empty-state">加载中...</td></tr>';
      renderMobileCardState('windowProgressCards', '正在加载窗口进度...');

      try {
        const payload = await requestJson(`/api/custom/ibkr/active-window-progress${buildQuery({
          environment: currentEnvironment,
          market_date: marketDate
        })}`);
        if (requestToken !== windowProgressState.requestToken) return windowProgressPayload;
        const items = Array.isArray(payload?.items)
          ? payload.items
          : (Array.isArray(payload?.windows) ? payload.windows : []);
        windowProgressPayload = {
          ...(payload || { items: [], summary: {}, market_date: marketDate }),
          items,
          market_date: payload?.market_date || marketDate,
        };
        windowProgressState.loadedKey = loadKey;
        renderWindowProgressTable();
        setPageRefreshTime();
        if (activeTab === 'screener' && activeScreenerView === 'window-progress') updateHero();
        if (showToastOnSuccess) showToast('窗口进度已刷新');
        return windowProgressPayload;
      } catch (error) {
        if (requestToken !== windowProgressState.requestToken) return windowProgressPayload;
        windowProgressPayload = { items: [], summary: {}, market_date: marketDate, computed_at_us: '' };
        windowProgressState.loadedKey = '';
        if (meta) meta.textContent = `加载失败: ${error.message || error}`;
        if (metaSecondary) metaSecondary.textContent = '窗口进度加载失败。';
        if (table) table.innerHTML = `<tr><td colspan="11" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('windowProgressCards', error.message || error);
        if (activeTab === 'screener' && activeScreenerView === 'window-progress') updateHero();
        return windowProgressPayload;
      }
    }

