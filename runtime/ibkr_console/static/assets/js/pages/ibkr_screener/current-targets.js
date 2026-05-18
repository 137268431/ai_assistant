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

    function getLifecycleContextObject(value) {
      if (!value) return {};
      if (typeof value === 'object') return value;
      if (typeof value === 'string') {
        try {
          const parsed = JSON.parse(value);
          return parsed && typeof parsed === 'object' ? parsed : {};
        } catch (_) {
          return {};
        }
      }
      return {};
    }

    function pickLifecycleContextValue(row, keys) {
      const signalState = getLifecycleContextObject(row?.signal_state);
      const candidateSignal = getLifecycleContextObject(row?.candidate_signal || signalState.signal_payload);
      const sources = [
        row || {},
        getLifecycleContextObject(row?.extra),
        signalState,
        candidateSignal,
      ];
      for (const source of sources) {
        for (const key of keys) {
          const value = source?.[key];
          if (value !== undefined && value !== null && value !== '') return value;
        }
      }
      return '';
    }

    function normalizeLifecycleDateToken(value, fallback = '') {
      const text = String(value || '').trim();
      const match = text.match(/^(\d{4}-\d{2}-\d{2})/);
      if (match) return match[1];
      const num = Number(text);
      if (Number.isFinite(num) && num > 0) return new Date(num).toISOString().slice(0, 10);
      return fallback;
    }

    function buildLifecycleFlowUrl(row, marketDate, extraParams = {}) {
      const symbol = String(pickLifecycleContextValue(row, ['symbol', 'ticker']) || '').trim().toUpperCase();
      if (!symbol) return '';
      const signalId = String(pickLifecycleContextValue(row, ['latest_signal_id', 'signal_id', 'origin_signal_id']) || '').trim();
      const tradeGroupId = String(pickLifecycleContextValue(row, ['latest_trade_group_id', 'trade_group_id', 'entry_order_unique_id', 'order_unique_id']) || '').trim();
      const barTimeMs = Number(pickLifecycleContextValue(row, ['latest_signal_time_ms', 'signal_time_ms', 'bar_time_ms', 'latest_bar_time_ms', 'latest_intraday_bar_time_ms']) || 0);
      const date = normalizeLifecycleDateToken(marketDate)
        || normalizeLifecycleDateToken(pickLifecycleContextValue(row, ['date', 'market_date', 'latest_signal_time', 'latest_us_time', 'us_time', 'latest_bar_time_ms']));
      const params = {
        symbol,
        interval: '5m',
        date,
        trace: 1,
        ...extraParams,
      };
      if (signalId) params.signal_id = signalId;
      if (tradeGroupId) params.trade_group_id = tradeGroupId;
      if (Number.isFinite(barTimeMs) && barTimeMs > 0) params.bar_time_ms = Math.round(barTimeMs);
      return buildPageUrl('/ibkr_lifecycle_flow.html', params, { environment: currentEnvironment });
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
        const flowUrl = buildLifecycleFlowUrl(row, marketDate);
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
              ${renderAdmissionScoreChip(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('目标分 / 可操作分', `${escapeHtml(formatNumber(row.target_score || 0, 1))} / ${escapeHtml(formatNumber(row.tradability_score || 0, 0))}`)}
              ${buildMobileMetricCard('信号统计', `${escapeHtml(String(row.signal_count_today || 0))}${row.latest_signal_direction ? ` · ${escapeHtml(String(row.latest_signal_direction || '').toUpperCase())}` : ''}`)}
            </div>

            ${buildMobileSection('筛选理由', escapeHtml(row.scan_reason || row.note || '--'))}
            ${renderSymbolProfileSummary(row) ? buildMobileSection('Symbol profile', renderSymbolProfileSummary(row)) : ''}
            ${buildMobileSection('当前阶段', `<strong>${escapeHtml(row.workflow_label || formatCurrentStateLabel(row.workflow_stage || row.attention_state || 'watch'))}</strong> · ${escapeHtml(row.workflow_summary || '--')}`)}
            ${buildMobileSection('阶段阻塞', buildFlagPills(row.workflow_blockers, '当前无明显阻塞'))}
            ${buildMobileSection('下一步', escapeHtml(row.workflow_next_action || '--'))}
            ${buildMobileSection('可操作依据', buildReasonPills(row))}
            ${(renderAdmissionControlRow(row) || getFailedGates(row).length) ? buildMobileSection('Admission / Gates', `${renderAdmissionControlRow(row)}${renderFailedGatesPills(row, 'failed_gates: none')}`) : ''}

            <div class="mobile-data-actions">
              <a class="mini-link" href="${chartUrl}">Chart</a>
              <a class="mini-link" href="${indicatorUrl}">指标</a>
              <a class="mini-link" href="${signalUrl}">信号</a>
              ${flowUrl ? `<a class="mini-link" href="${flowUrl}">流程图</a>` : ''}
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
    const WINDOW_PROGRESS_STALE_MINUTES = 30;

    function getWindowProgressObject(row, key) {
      const value = row?.[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) return value;
      if (typeof parseMaybeObject === 'function') return parseMaybeObject(value);
      return {};
    }

    function getWindowSignalState(row) {
      return getWindowProgressObject(row, 'signal_state');
    }

    function getWindowProgressTraceStage(row) {
      const signalState = getWindowSignalState(row);
      return String(coalesceValue({
        ...row,
        signal_stage: signalState.stage,
        signal_status: signalState.status,
      }, [
        'trace_stage',
        'signal_stage',
        'signal_state_stage',
        'candidate_signal_stage',
        'stage',
        'signal_status',
      ], '') || '').trim().toLowerCase();
    }

    function getWindowProgressWindowStatus(row) {
      return String(coalesceValue(row, ['window_status', 'status', 'state', 'window_state'], '') || '').trim().toLowerCase();
    }

    function getWindowProgressFreshness(row) {
      const value = Number(coalesceValue(row, ['freshness_min', 'freshness_minutes', 'latest_bar_age_min'], NaN));
      return Number.isFinite(value) ? value : NaN;
    }

    function hasWindowProgressLatestBar(row) {
      return Boolean(coalesceValue(row, [
        'latest_5m_bar',
        'latest_bar_us',
        'latest_us_time',
        'bar_time_us',
        'latest_bar_time',
        'latest_bar_time_ms',
      ], ''));
    }

    function isWindowProgressStale(row) {
      const explicit = String(coalesceValue(row, ['freshness_status', 'data_status', 'timeline_status'], '') || '').trim().toLowerCase();
      if (['stale', 'missing', 'no_bar', 'no_live_bar'].includes(explicit)) return true;
      if (row?.stale || row?.is_stale || row?.needs_backfill) return true;
      if (!hasWindowProgressLatestBar(row)) return true;
      const freshness = getWindowProgressFreshness(row);
      return Number.isFinite(freshness) && freshness > WINDOW_PROGRESS_STALE_MINUTES;
    }

    function getWindowProgressFilterReasons(row) {
      const signalState = getWindowSignalState(row);
      return normalizeWindowProgressList(coalesceValue({
        ...row,
        signal_filter_reason: signalState.filter_reason,
        signal_blockers: signalState.blockers,
        signal_reasons: signalState.filter_reasons,
      }, [
        'filter_reasons',
        'filter_reason',
        'blocked_reasons',
        'block_reason',
        'blocked_reason',
        'signal_filter_reason',
        'signal_blockers',
        'signal_reasons',
      ], []));
    }

    function hasWindowDirectionConflict(row) {
      if (row?.direction_conflict || row?.has_direction_conflict) return true;
      const stage = getWindowProgressTraceStage(row);
      const status = getWindowProgressWindowStatus(row);
      if (stage === 'direction_conflict' || status === 'direction_conflict') return true;
      return getWindowProgressFilterReasons(row).some((item) => {
        const text = String(item || '').trim().toLowerCase();
        return text.includes('direction_conflict')
          || text.includes('target_direction_mismatch')
          || text.includes('direction_mismatch');
      });
    }

    function isWindowProgressConfirmed(row) {
      const stage = getWindowProgressTraceStage(row);
      const status = getWindowProgressWindowStatus(row);
      return row?.confirmed || row?.is_confirmed || stage === 'confirmed' || status === 'confirmed';
    }

    function hasWindowProgressBlockers(row) {
      const stage = getWindowProgressTraceStage(row);
      const status = getWindowProgressWindowStatus(row);
      if (row?.blocked || row?.is_blocked || stage === 'blocked' || status === 'blocked') return true;
      return getWindowProgressFilterReasons(row).length > 0 || getFailedGates(row).length > 0;
    }

    function isWindowProgressNearExpiry(row) {
      const status = getWindowProgressWindowStatus(row);
      if (row?.near_expiry || row?.is_near_expiry || status === 'near_expiry') return true;
      const barsRemaining = Number(coalesceValue(row, ['bars_remaining', 'remaining_bars'], NaN));
      return Number.isFinite(barsRemaining) && barsRemaining >= 0 && barsRemaining <= 2 && (row?.sd_upper_active || row?.sd_lower_active);
    }

    function isWindowProgressTargetCandidate(row) {
      return String(coalesceValue(row, ['target_status', 'status_in_pool', 'pool_status'], '') || '').trim().toLowerCase() === 'candidate';
    }

    function getWindowCandidateSignalObject(row) {
      const candidate = getWindowProgressObject(row, 'candidate_signal');
      if (Object.keys(candidate).length) return candidate;
      return getWindowProgressObject(row, 'signal_candidate');
    }

    function isWindowProgressSignalCandidate(row) {
      const stage = getWindowProgressTraceStage(row);
      const status = getWindowProgressWindowStatus(row);
      const candidateSignal = getWindowCandidateSignalObject(row);
      const rawCandidateSignal = coalesceValue(row, ['candidate_signal', 'signal_candidate'], '');
      const source = getWindowCandidateSignalSource(row).toLowerCase();
      const hasCandidatePayload = Boolean(
        Object.keys(candidateSignal || {}).length
        || coalesceValue(row, ['candidate_signal_label', 'signal_label'], '')
        || (rawCandidateSignal && typeof rawCandidateSignal !== 'object')
      );
      const traceCandidate = ['candidate', 'signal_candidate', 'candidate_signal', 'ready_to_signal'].includes(stage)
        || ['candidate', 'signal_candidate', 'candidate_signal'].includes(status);
      const candidateStage = traceCandidate
        || row?.signal_candidate === true
        || row?.is_signal_candidate === true
        || hasCandidatePayload;
      if (!candidateStage) return false;
      if (source === 'stored_signal' && !traceCandidate) return false;
      return !isWindowProgressConfirmed(row) && !hasWindowProgressBlockers(row) && !hasWindowDirectionConflict(row) && !isWindowProgressStale(row);
    }

    function matchesWindowProgressStatusTab(row, tabKey) {
      const key = normalizeWindowProgressStatusTab(tabKey);
      if (key === 'all') return true;
      if (key === 'signal_candidate') return isWindowProgressSignalCandidate(row);
      if (key === 'confirmed') return isWindowProgressConfirmed(row);
      if (key === 'blocked') return hasWindowProgressBlockers(row) && !hasWindowDirectionConflict(row);
      if (key === 'direction_conflict') return hasWindowDirectionConflict(row);
      if (key === 'near_expiry') return isWindowProgressNearExpiry(row) && !isWindowProgressStale(row);
      if (key === 'stale') return isWindowProgressStale(row);
      if (key === 'target_candidate') return isWindowProgressTargetCandidate(row);
      return false;
    }


    function getWindowProgressStatus(row) {
      const windowFlags = getWindowProgressObject(row, 'window_flags');
      const explicit = getWindowProgressWindowStatus(row);
      if (explicit) return explicit;
      if (isWindowProgressConfirmed(row)) return 'confirmed';
      if (isWindowProgressSignalCandidate(row)) return 'signal_candidate';
      if (hasWindowDirectionConflict(row)) return 'direction_conflict';
      if (hasWindowProgressBlockers(row)) return 'blocked';
      if (row?.used || row?.window_used) return 'used';
      if (row?.expired || row?.window_expired) return 'expired';
      if (isWindowProgressNearExpiry(row)) return 'near_expiry';
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
      return WINDOW_PROGRESS_STATUS_TABS.find((tab) => tab.key !== 'all' && matchesWindowProgressStatusTab(row, tab.key))?.key || 'all';
    }

    function getWindowProgressStatusCounts(rows = getSortedWindowProgressRows()) {
      const counts = WINDOW_PROGRESS_STATUS_TABS.reduce((acc, tab) => {
        acc[tab.key] = tab.key === 'all' ? rows.length : 0;
        return acc;
      }, {});
      rows.forEach((row) => {
        WINDOW_PROGRESS_STATUS_TABS.forEach((tab) => {
          if (tab.key !== 'all' && matchesWindowProgressStatusTab(row, tab.key)) {
            counts[tab.key] = (counts[tab.key] || 0) + 1;
          }
        });
      });
      return counts;
    }

    function getFilteredWindowProgressRows(rows = getSortedWindowProgressRows()) {
      const selectedStatus = normalizeWindowProgressStatusTab(activeWindowProgressStatus);
      if (selectedStatus === 'all') return rows;
      return rows.filter((row) => matchesWindowProgressStatusTab(row, selectedStatus));
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
        signal_candidate: 'signal_candidate',
        target_candidate: 'target_candidate',
        direction_conflict: 'direction_conflict',
        blocked: 'blocked',
        confirmed: 'confirmed',
        stale: 'stale',
      };
      const key = String(value || '').trim().toLowerCase();
      return labels[key] || (key || '--');
    }

    function getWindowProgressPriority(row) {
      if (isWindowProgressSignalCandidate(row)) return 0;
      if (isWindowProgressConfirmed(row)) return 1;
      if (hasWindowDirectionConflict(row)) return 2;
      if (hasWindowProgressBlockers(row)) return 3;
      if (isWindowProgressNearExpiry(row)) return 4;
      if (isWindowProgressStale(row)) return 8;
      const status = getWindowProgressStatus(row);
      const rank = {
        both_active: 5,
        upper_active: 6,
        lower_active: 6,
        no_window: 7,
        expired: 9,
        used: 10,
      };
      return rank[status] ?? 11;
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
      const signalState = getWindowSignalState(row);
      const candidate = coalesceValue({
        ...row,
        signal_state_label: signalState.label,
        signal_state_reason: signalState.reason,
      }, ['candidate_signal_label', 'signal_label', 'signal_state_label', 'candidate_signal', 'signal_candidate'], '');
      if (candidate && typeof candidate === 'object') {
        return String(candidate.signal || candidate.label || candidate.direction || candidate.status || '--');
      }
      if (candidate) return String(candidate);
      const candidatePayload = getWindowCandidateSignalObject(row);
      const payloadLabel = coalesceValue(candidatePayload, ['signal', 'label', 'direction', 'status'], '');
      if (payloadLabel) return String(payloadLabel);
      const direction = String(coalesceValue(row, ['candidate_direction', 'signal_direction', 'direction'], '') || '').trim().toUpperCase();
      const status = getWindowProgressStatus(row);
      if (direction) return direction;
      return ['candidate', 'signal_candidate', 'confirmed', 'blocked'].includes(status) ? status : '--';
    }

    function getWindowSignalStage(row) {
      if (isWindowProgressSignalCandidate(row)) return 'signal_candidate';
      if (isWindowProgressConfirmed(row)) return 'confirmed';
      if (hasWindowDirectionConflict(row)) return 'direction_conflict';
      if (hasWindowProgressBlockers(row)) return 'blocked';
      if (isWindowProgressStale(row)) return 'stale';
      return getWindowProgressTraceStage(row) || getWindowProgressStatus(row) || 'watch';
    }

    function formatWindowSignalStageLabel(row) {
      const labels = {
        signal_candidate: 'signal_candidate',
        candidate: 'signal_candidate',
        confirmed: 'confirmed',
        blocked: 'blocked',
        direction_conflict: 'direction_conflict',
        stale: 'stale',
        no_window: 'watch',
        upper_active: 'watch',
        lower_active: 'watch',
        both_active: 'watch',
      };
      const stage = getWindowSignalStage(row);
      return labels[stage] || stage || '--';
    }

    function getWindowCandidateSignalSource(row) {
      const signalState = getWindowSignalState(row);
      const candidatePayload = getWindowCandidateSignalObject(row);
      return String(coalesceValue({
        ...row,
        state_source: signalState.source,
        payload_source: candidatePayload.source,
      }, ['candidate_signal_source', 'signal_source', 'state_source', 'payload_source'], '') || '').trim();
    }

    function buildWindowSignalCell(row) {
      const stage = getWindowSignalStage(row);
      const candidateLabel = getWindowCandidateLabel(row);
      const source = getWindowCandidateSignalSource(row);
      const candidatePayload = getWindowCandidateSignalObject(row);
      const signalState = getWindowSignalState(row);
      const direction = String(coalesceValue({
        ...row,
        candidate_payload_direction: candidatePayload.direction,
        signal_state_direction: signalState.direction,
      }, ['candidate_direction', 'signal_direction', 'direction', 'candidate_payload_direction', 'signal_state_direction'], '') || '').trim().toUpperCase();
      return `
        <div class="window-signal-stack">
          <div class="pill-row">
            ${statusChip(formatWindowSignalStageLabel(row), stage)}
            ${direction ? statusChip(direction, String(direction).toLowerCase() === 'short' ? 'short' : 'long') : ''}
          </div>
          <div class="muted mono window-progress-subline">${escapeHtml(candidateLabel || '--')}${source ? ` · ${escapeHtml(source)}` : ''}</div>
        </div>
      `;
    }

    function buildWindowLatestCell(row) {
      const latestBar = coalesceValue(row, ['latest_5m_bar', 'latest_bar_us', 'latest_us_time', 'bar_time_us', 'latest_bar_time'], '--');
      const freshness = getWindowProgressFreshness(row);
      const freshnessTone = isWindowProgressStale(row) ? 'stale' : (Number.isFinite(freshness) && freshness <= WINDOW_PROGRESS_STALE_MINUTES ? 'active' : 'candidate');
      const timeline = getWindowProgressObject(row, 'timeline_data');
      const price = coalesceValue({ ...row, timeline_price: timeline.close ?? timeline.price }, ['price', 'display_price', 'timeline_price'], '');
      return `
        <div class="window-latest-stack">
          <span class="mono">${escapeHtml(latestBar || '--')}</span>
          <div class="pill-row">
            ${statusChip(formatFreshness(freshness), freshnessTone)}
            ${price !== '' ? statusChip(formatPrice(price), 'neutral') : ''}
          </div>
        </div>
      `;
    }

    function buildWindowActions(row, marketDate, { compact = false } = {}) {
      const traceUrl = getWindowTraceUrl(row, marketDate);
      const signalUrl = buildSignalUrl(row.symbol || '', marketDate);
      const flowUrl = buildLifecycleFlowUrl(row, marketDate);
      const chartText = compact ? 'Trace' : 'Chart Trace';
      const lifecycleText = compact ? 'Lifecycle' : 'Lifecycle';
      return `
        <div class="row-actions window-row-actions">
          <a class="mini-link" href="${signalUrl}">Signals</a>
          <a class="mini-link" href="${traceUrl}">${chartText}</a>
          ${flowUrl ? `<a class="mini-link" href="${flowUrl}">${lifecycleText}</a>` : ''}
        </div>
      `;
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
        const components = getWindowProgressObject(row, 'components');
        const collected = coalesceValue({
          ...row,
          nested_collected_components: components.collected ?? components.ready,
        }, ['collected_components', 'components_collected', 'ready_components', 'nested_collected_components'], []);
        const missing = coalesceValue({
          ...row,
          nested_missing_components: components.missing,
        }, ['missing_components', 'components_missing', 'nested_missing_components'], []);
        const filterReasons = getWindowProgressFilterReasons(row);
        return `
          <article class="mobile-data-card window-progress-card">
            <div class="mobile-data-head window-progress-mobile-head">
              <div>
                <a class="mobile-data-symbol" href="${buildChartUrl(row.symbol || '')}">${escapeHtml(row.symbol || '--')}</a>
                <div class="mobile-chip-row window-mobile-stage-row">
                  ${statusChip(formatWindowSignalStageLabel(row), getWindowSignalStage(row))}
                  ${isWindowProgressTargetCandidate(row) ? statusChip('target_candidate', 'target_candidate') : statusChip(row.target_status || 'active', row.target_status || 'active')}
                </div>
                <div class="mobile-data-time">Latest 5m · ${escapeHtml(latestBar || '--')}</div>
              </div>
              <div class="window-mobile-actions">
                ${buildWindowActions(row, marketDate, { compact: true })}
              </div>
            </div>

            <div class="mobile-chip-row">
              ${statusChip(getWindowProgressStatusLabel(status), status)}
              ${statusChip(formatFreshness(getWindowProgressFreshness(row)), isWindowProgressStale(row) ? 'stale' : 'active')}
              ${statusChip(`progress ${formatWindowComponentProgress(row)}`, 'config')}
              ${statusChip(`left ${formatWindowProgressCount(coalesceValue(row, ['bars_remaining', 'remaining_bars'], NaN))}`, isWindowProgressNearExpiry(row) ? 'near_expiry' : 'neutral')}
              ${hasWindowDirectionConflict(row) ? statusChip('direction_conflict', 'direction_conflict') : ''}
              ${renderAdmissionScoreChip(row)}
              ${renderNeedsBackfillChip(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('Signal', buildWindowSignalCell(row))}
              ${buildMobileMetricCard('Window', `${formatWindowSide(row, 'upper')}<div class="window-progress-subline"></div>${formatWindowSide(row, 'lower')}`)}
            </div>

            ${buildMobileSection('已收集组件', buildWindowComponentGroupColumn(row, 'present', collected, '暂无'))}
            ${buildMobileSection('缺失组件', buildWindowComponentGroupColumn(row, 'missing', missing, '无缺失'))}
            ${buildMobileSection('Blockers', buildWindowListPills(filterReasons, '未触发过滤'))}
            ${getFailedGates(row).length ? buildMobileSection('Failed gates', renderFailedGatesPills(row)) : ''}
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
      const compactCounts = `signal ${counts.signal_candidate || 0} · confirmed ${counts.confirmed || 0} · blocked ${counts.blocked || 0} · conflict ${counts.direction_conflict || 0} · stale ${counts.stale || 0} · target ${counts.target_candidate || 0}`;
      meta.textContent = selectedStatus === 'all'
        ? `${allRows.length} 条 · ${compactCounts}`
        : `${rows.length}/${allRows.length} 条 · 当前 ${selectedLabel} · ${compactCounts}`;
      metaSecondary.textContent = windowProgressPayload.computed_at_us
        ? `计算时间 ${windowProgressPayload.computed_at_us}。筛选: ${selectedLabel}。排序: signal_candidate/confirmed/blockers/near_expiry, bars_remaining asc, component_progress desc, freshness_min asc, target_score desc。`
        : `筛选: ${selectedLabel}。排序: signal_candidate/confirmed/blockers/near_expiry, bars_remaining asc, component_progress desc, freshness_min asc, target_score desc。`;

      if (!allRows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">当前没有窗口进度记录。</td></tr>';
        renderWindowProgressCards([], marketDate);
        return;
      }
      if (!rows.length) {
        const emptyMessage = `当前没有${selectedLabel}状态的窗口进度记录。`;
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(emptyMessage)}</td></tr>`;
        renderWindowProgressCards([], marketDate, emptyMessage);
        return;
      }

      tbody.innerHTML = rows.map((row) => {
        const status = getWindowProgressStatus(row);
        const barsRemaining = coalesceValue(row, ['bars_remaining', 'remaining_bars'], NaN);
        const components = getWindowProgressObject(row, 'components');
        const collected = coalesceValue({
          ...row,
          nested_collected_components: components.collected ?? components.ready,
        }, ['collected_components', 'components_collected', 'ready_components', 'nested_collected_components'], []);
        const missing = coalesceValue({
          ...row,
          nested_missing_components: components.missing,
        }, ['missing_components', 'components_missing', 'nested_missing_components'], []);
        const filterReasons = getWindowProgressFilterReasons(row);
        return `
          <tr>
            <td>
              <a class="symbol-link" href="${buildChartUrl(row.symbol || '')}">${escapeHtml(row.symbol || '--')}</a><br>
              <div class="pill-row window-symbol-pills">
                ${statusChip(getWindowProgressStatusLabel(status), status)}
                ${isWindowProgressTargetCandidate(row) ? statusChip('target_candidate', 'target_candidate') : statusChip(row.target_status || 'active', row.target_status || 'active')}
                ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}
              </div>
              <span class="muted">target ${escapeHtml(formatNumber(coalesceValue(row, ['target_score', 'score'], 0), 1))}</span>
              ${renderAdmissionControlRow(row)}
            </td>
            <td>${buildWindowLatestCell(row)}</td>
            <td>${buildWindowSignalCell(row)}</td>
            <td>
              <div class="window-side-grid">
                <div>${formatWindowSide(row, 'upper')}</div>
                <div>${formatWindowSide(row, 'lower')}</div>
              </div>
              <div class="window-progress-subline muted mono">left ${escapeHtml(formatWindowProgressCount(barsRemaining))} · progress ${escapeHtml(formatWindowComponentProgress(row))}</div>
              <details class="window-components-details">
                <summary>组件明细</summary>
                <div class="window-components-detail-grid">
                  <div>${buildWindowComponentGroupColumn(row, 'present', collected, '暂无')}</div>
                  <div>${buildWindowComponentGroupColumn(row, 'missing', missing, '无缺失')}</div>
                </div>
              </details>
            </td>
            <td>
              ${buildWindowListPills(filterReasons, '未触发过滤')}
              ${hasWindowDirectionConflict(row) ? `<div style="margin-top:8px;">${statusChip('direction_conflict', 'direction_conflict')}</div>` : ''}
              ${row.trace_error ? `<div class="muted mono window-progress-subline">trace_error: ${escapeHtml(row.trace_error)}</div>` : ''}
              ${getFailedGates(row).length ? `<div style="margin-top:8px;">${renderFailedGatesPills(row)}</div>` : ''}
            </td>
            <td>${buildWindowActions(row, marketDate)}</td>
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
              ${renderAdmissionScoreChip(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('价格 / 涨跌', `${escapeHtml(formatPrice(row.display_price ?? row.price))}<br><span class="mobile-data-subcopy mono">${escapeHtml(formatPct(row.display_day_change_pct ?? row.day_change_pct))}</span>`)}
              ${buildMobileMetricCard('ATR / 量能', `ATR ${escapeHtml(formatPct(row.atr_pct))}<br><span class="mobile-data-subcopy">10D ${escapeHtml(formatVolume(row.avg_10d_volume))} · PRE ${escapeHtml(formatVolume(row.premarket_volume))}</span>`)}
            </div>

            ${buildMobileSection('筛选理由', escapeHtml(row.scan_reason || row.note || '--'))}
            ${renderSymbolProfileSummary(row) ? buildMobileSection('Symbol profile', renderSymbolProfileSummary(row)) : ''}
            ${buildMobileSection('可操作依据', buildReasonPills(row))}
            ${(renderAdmissionControlRow(row) || getFailedGates(row).length) ? buildMobileSection('Admission / Gates', `${renderAdmissionControlRow(row)}${renderFailedGatesPills(row, 'failed_gates: none')}`) : ''}

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
                ${renderAdmissionScoreChip(item)}
                ${renderNeedsBackfillChip(item)}
              </div>
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('成员属性', escapeHtml(formatWatchlistMember(item)))}
              ${buildMobileMetricCard('更新时间', `${escapeHtml(item.updated_us || item.us_time || '--')}<br><span class="mobile-data-subcopy">${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</span>`)}
            </div>

            ${buildMobileSection('备注', escapeHtml(item.note || '--'))}
            ${renderSymbolProfileSummary(item) ? buildMobileSection('Symbol profile', renderSymbolProfileSummary(item)) : ''}
            ${(renderAdmissionControlRow(item) || getFailedGates(item).length) ? buildMobileSection('Admission / Gates', `${renderAdmissionControlRow(item)}${renderFailedGatesPills(item, 'failed_gates: none')}`) : ''}

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
        const flowUrl = buildLifecycleFlowUrl(row, marketDateToken);
        return `
          <tr>
            <td>
              <a class="symbol-link" href="${chartUrl}">${escapeHtml(row.symbol || '--')}</a><br>
              <span class="muted mono">${escapeHtml(row.latest_us_time || '--')}</span><br>
              <span class="muted">${escapeHtml(row.exchange || '--')} / ${escapeHtml(row.industry || '--')}</span>
              ${renderSymbolProfileSummary(row) ? `<div style="margin-top:8px;">${renderSymbolProfileSummary(row)}</div>` : ''}
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
              ${renderAdmissionControlRow(row)}
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
              ${renderAdmissionDiagnosticsBlock(row)}
              <div class="row-actions" style="margin-top:12px;">
                <a class="mini-link" href="${chartUrl}">Chart</a>
                <a class="mini-link" href="${indicatorUrl}">指标</a>
                <a class="mini-link" href="${signalUrl}">信号</a>
                ${flowUrl ? `<a class="mini-link" href="${flowUrl}">流程图</a>` : ''}
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
        const payload = await requestCachedJson(`/api/custom/ibkr/today-targets${buildQuery(getCurrentTargetRequestParams(marketDate))}`, {}, {
          ttlMs: 30000,
          swrMs: 30000,
          force: Boolean(showToastOnSuccess),
          tags: ['screener', 'today-targets', currentEnvironment]
        });
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
      const loadKey = `${currentEnvironment}::${marketDate}::all::200`;
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
      if (metaSecondary) metaSecondary.textContent = '正在读取 all window progress (limit 200)...';
      if (table) table.innerHTML = '<tr><td colspan="6" class="empty-state">加载中...</td></tr>';
      renderMobileCardState('windowProgressCards', '正在加载窗口进度...');

      try {
        const payload = await requestCachedJson(`/api/custom/ibkr/active-window-progress${buildQuery({
          environment: currentEnvironment,
          market_date: marketDate,
          status: 'all',
          limit: 200
        })}`, {}, {
          ttlMs: 30000,
          swrMs: 30000,
          force,
          tags: ['screener', 'active-window-progress', currentEnvironment]
        });
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
        if (table) table.innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('windowProgressCards', error.message || error);
        if (activeTab === 'screener' && activeScreenerView === 'window-progress') updateHero();
        return windowProgressPayload;
      }
    }
