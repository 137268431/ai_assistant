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
        avg_volume_min: Number(document.getElementById('avgVolumeMin').value || NaN),
        premarket_volume_min: Number(document.getElementById('premarketVolumeMin').value || NaN),
        target_score_min: Number(document.getElementById('targetScoreMin').value || NaN),
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

    function getTvChartUrl(row) {
      return String(row?.tv_chart_url || row?.chart_url || row?.extra?.tv_chart_url || '').trim();
    }

    function buildTvChartLink(row, label = 'TV') {
      const url = getTvChartUrl(row);
      return url ? `<a class="mini-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : '';
    }

    function getTargetExtra(row) {
      return typeof parseMaybeObject === 'function' ? parseMaybeObject(row?.extra) : getLifecycleContextObject(row?.extra);
    }

    function getTargetMtfObject(row) {
      const extra = getTargetExtra(row);
      const direct = typeof parseMaybeObject === 'function' ? parseMaybeObject(row?.mtf) : getLifecycleContextObject(row?.mtf);
      const nested = typeof parseMaybeObject === 'function' ? parseMaybeObject(extra.mtf) : getLifecycleContextObject(extra.mtf);
      return Object.keys(direct || {}).length ? direct : nested;
    }

    function pickTargetContextValue(row, keys) {
      const extra = getTargetExtra(row);
      const mtf = getTargetMtfObject(row);
      const sources = [row || {}, extra, mtf];
      for (const source of sources) {
        for (const key of keys) {
          const value = source?.[key];
          if (value !== undefined && value !== null && value !== '') return value;
        }
      }
      return '';
    }

    function formatTargetListText(value) {
      if (Array.isArray(value)) return value.filter(Boolean).join(',');
      return String(value || '').trim();
    }

    function formatTargetContextTime(value) {
      const text = String(value || '').trim();
      if (!text) return '';
      const numeric = Number(text);
      if (Number.isFinite(numeric) && numeric > 0) {
        const ms = numeric > 1000000000000 ? numeric : numeric * 1000;
        const date = new Date(ms);
        if (!Number.isNaN(date.getTime())) {
          return new Intl.DateTimeFormat('en-US', {
            timeZone: 'America/New_York',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
          }).format(date);
        }
      }
      const parsed = new Date(text);
      if (!Number.isNaN(parsed.getTime()) && typeof formatMarketTime === 'function') {
        return formatMarketTime(text, 'short');
      }
      return text;
    }

    function getMtfTone(status) {
      const key = String(status || '').trim().toLowerCase();
      if (key === 'pass') return 'active';
      if (key === 'warn') return 'candidate';
      if (key === 'block' || key === 'fail') return 'stale';
      return 'neutral';
    }

    function buildTargetMtfContextHtml(row, { compact = false } = {}) {
      const mtf = getTargetMtfObject(row);
      const status = String(pickTargetContextValue(row, ['mtf_last_status', 'mtf_status']) || mtf.status || '').trim();
      const pickedScore = pickTargetContextValue(row, ['mtf_last_score', 'mtf_score']);
      const scoreValue = pickedScore !== '' ? pickedScore : mtf.score;
      const entryTf = String(pickTargetContextValue(row, ['entry_tf', 'chart_tf']) || mtf.entry_tf || '').trim();
      const confirmTfs = formatTargetListText(pickTargetContextValue(row, ['confirm_tfs']) || mtf.confirm_tfs);
      const blockReason = String(pickTargetContextValue(row, ['mtf_last_block_reason', 'mtf_block_reason', 'block_reason']) || mtf.block_reason || '').trim();
      const stack = String(pickTargetContextValue(row, ['timeframe_stack']) || mtf.timeframe_stack || '').trim();
      const hasScore = scoreValue !== undefined && scoreValue !== null && scoreValue !== '';
      if (!status && !hasScore && !entryTf && !confirmTfs && !stack) return '';
      const chips = [
        status ? statusChip(`MTF ${status}`, getMtfTone(status)) : '',
        hasScore ? statusChip(`score ${formatNumber(scoreValue, 0)}`, getMtfTone(status)) : '',
        entryTf ? statusChip(`entry ${entryTf}`, 'config') : '',
        confirmTfs ? statusChip(`confirm ${confirmTfs}`, 'neutral') : '',
        blockReason && blockReason !== 'none' ? statusChip(blockReason, 'stale') : '',
      ].filter(Boolean).join('');
      const stackLine = stack ? `<div class="muted mono">${escapeHtml(stack)}</div>` : '';
      if (compact) return `<div class="pill-row">${chips}</div>${stackLine}`;
      return `
        <div class="reason-block" style="margin-top:8px;">
          <div class="reason-label">MTF 多时间维度</div>
          <div class="pill-row">${chips}</div>
          ${stackLine}
        </div>
      `;
    }

    function buildTargetActivationHtml(row, { compact = false } = {}) {
      const source = String(pickTargetContextValue(row, ['activation_source', 'source']) || '').trim();
      const firstEvent = String(pickTargetContextValue(row, ['first_tv_event_id']) || '').trim();
      const lastEvent = String(pickTargetContextValue(row, ['last_tv_event_id', 'tv_event_id']) || '').trim();
      const firstBar = formatTargetContextTime(pickTargetContextValue(row, ['first_bar_time_ms']));
      const lastBar = formatTargetContextTime(pickTargetContextValue(row, ['last_bar_time_ms', 'bar_time_ms']));
      const firstCreated = formatTargetContextTime(pickTargetContextValue(row, ['first_created']));
      const rank = pickTargetContextValue(row, ['activity_rank']);
      const rankReason = String(pickTargetContextValue(row, ['rank_reason']) || '').trim();
      const subscriptionRank = pickTargetContextValue(row, ['subscription_rank']);
      const subscriptionSelected = pickTargetContextValue(row, ['subscription_selected']);
      const withinBudget = pickTargetContextValue(row, ['within_subscription_budget']);
      const subscriptionSelectedBool = typeof normalizeTruth === 'function' ? normalizeTruth(subscriptionSelected) : Boolean(subscriptionSelected);
      const withinBudgetBool = typeof normalizeTruth === 'function' ? normalizeTruth(withinBudget) : Boolean(withinBudget);
      const chips = [
        source ? statusChip(`src ${source}`, 'config') : '',
        rank ? statusChip(`rank ${rank}`, 'active') : '',
        subscriptionRank ? statusChip(`sub ${subscriptionRank}`, subscriptionSelectedBool ? 'active' : 'candidate') : '',
        withinBudget !== '' ? statusChip(withinBudgetBool ? 'budget yes' : 'budget no', withinBudgetBool ? 'active' : 'stale') : '',
      ].filter(Boolean).join('');
      const lines = [
        firstEvent ? `first ${firstEvent}` : '',
        firstBar ? `first bar ${firstBar}` : '',
        firstCreated ? `created ${firstCreated}` : '',
        lastEvent ? `last ${lastEvent}` : '',
        lastBar ? `last bar ${lastBar}` : '',
        rankReason ? `rank ${rankReason}` : '',
      ].filter(Boolean);
      if (!chips && !lines.length) return '';
      const details = lines.length ? `<div class="muted mono">${escapeHtml(lines.join(' · '))}</div>` : '';
      if (compact) return `<div class="pill-row">${chips}</div>${details}`;
      return `
        <div class="reason-block" style="margin-top:8px;">
          <div class="reason-label">激活来源 / 排名</div>
          <div class="pill-row">${chips}</div>
          ${details}
        </div>
      `;
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
      if (Number.isFinite(num) && num > 0) return getEtDateStringFromMs(num);
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
        const tvChartUrl = getTvChartUrl(row);
        const symbolHtml = tvChartUrl
          ? `<a class="mobile-data-symbol" href="${escapeHtml(tvChartUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.symbol || '--')}</a>`
          : `<span class="mobile-data-symbol">${escapeHtml(row.symbol || '--')}</span>`;
        const signalUrl = buildSignalUrl(row.symbol || '', marketDate);
        const flowUrl = buildLifecycleFlowUrl(row, marketDate);
        return `
          <article class="mobile-data-card">
            <div class="mobile-data-head">
              <div>
                ${symbolHtml}
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
              ${statusChip(formatCurrentStateLabel(signalStateKey), signalStateKey)}
              ${dataQualityChip(row)}
              ${renderAdmissionScoreChip(row)}
              ${renderExecutionLayerPills(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('目标分 / 可操作分', `${escapeHtml(formatNumber(row.target_score || 0, 1))} / ${escapeHtml(formatNumber(row.tradability_score || 0, 0))}`)}
              ${buildMobileMetricCard('信号统计', `${escapeHtml(String(row.signal_count_today || 0))}${row.latest_signal_direction ? ` · ${escapeHtml(String(row.latest_signal_direction || '').toUpperCase())}` : ''}`)}
            </div>

            ${renderCurrentSignalReason(row) ? buildMobileSection('信号拒绝/终止原因', renderCurrentSignalReason(row)) : ''}
            ${buildMobileSection('筛选理由', escapeHtml(row.scan_reason || row.note || '--'))}
            ${(buildTargetActivationHtml(row, { compact: true }) || buildTargetMtfContextHtml(row, { compact: true })) ? buildMobileSection('TV 激活 / MTF', `${buildTargetActivationHtml(row, { compact: true })}${buildTargetMtfContextHtml(row, { compact: true })}`) : ''}
            ${renderSymbolProfileSummary(row) ? buildMobileSection('Symbol profile', renderSymbolProfileSummary(row)) : ''}
            ${buildMobileSection('当前阶段', `<strong>${escapeHtml(row.workflow_label || formatCurrentStateLabel(row.workflow_stage || row.attention_state || 'watch'))}</strong> · ${escapeHtml(row.workflow_summary || '--')}`)}
            ${renderExecutionLayerBlock(row)}
            ${buildMobileSection('阶段阻塞', buildFlagPills(row.workflow_blockers, '当前无明显阻塞'))}
            ${buildMobileSection('下一步', escapeHtml(row.workflow_next_action || '--'))}
            ${buildMobileSection('可操作依据', buildReasonPills(row))}
            ${(renderAdmissionControlRow(row) || getFailedGates(row).length) ? buildMobileSection('Admission / Gates', `${renderAdmissionControlRow(row)}${renderFailedGatesPills(row, 'failed_gates: none')}`) : ''}

            <div class="mobile-data-actions">
              ${buildTvChartLink(row, 'TV')}
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
        const progressDiff = getWindowComponentProgress(right) - getWindowComponentProgress(left);
        if (progressDiff !== 0) return progressDiff;
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
      const traceUrl = getTvChartUrl(row);
      const signalUrl = buildSignalUrl(row.symbol || '', marketDate);
      const flowUrl = buildLifecycleFlowUrl(row, marketDate);
      const chartText = compact ? 'Trace' : 'Chart Trace';
      const lifecycleText = compact ? 'Lifecycle' : 'Lifecycle';
      return `
        <div class="row-actions window-row-actions">
          <a class="mini-link" href="${signalUrl}">Signals</a>
          ${traceUrl ? `<a class="mini-link" href="${escapeHtml(traceUrl)}" target="_blank" rel="noopener noreferrer">${chartText}</a>` : ''}
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
                ${getTvChartUrl(row) ? `<a class="mobile-data-symbol" href="${escapeHtml(getTvChartUrl(row))}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.symbol || '--')}</a>` : `<span class="mobile-data-symbol">${escapeHtml(row.symbol || '--')}</span>`}
                <div class="mobile-chip-row window-mobile-stage-row">
                  ${statusChip(formatWindowSignalStageLabel(row), getWindowSignalStage(row))}
                  ${isWindowProgressTargetCandidate(row) ? statusChip('target_candidate', 'target_candidate') : statusChip(row.target_status || 'active', row.target_status || 'active')}
                </div>
              </div>
              <div class="window-mobile-actions">
                ${buildWindowActions(row, marketDate, { compact: true })}
              </div>
            </div>

            <div class="mobile-chip-row">
              ${statusChip(getWindowProgressStatusLabel(status), status)}
              ${statusChip(`progress ${formatWindowComponentProgress(row)}`, 'config')}
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
        ? `计算时间 ${windowProgressPayload.computed_at_us}。筛选: ${selectedLabel}。排序: signal_candidate/confirmed/blockers/near_expiry, component_progress desc, target_score desc。`
        : `筛选: ${selectedLabel}。排序: signal_candidate/confirmed/blockers/near_expiry, component_progress desc, target_score desc。`;

      if (!allRows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">当前没有窗口进度记录。</td></tr>';
        renderWindowProgressCards([], marketDate);
        return;
      }
      if (!rows.length) {
        const emptyMessage = `当前没有${selectedLabel}状态的窗口进度记录。`;
        tbody.innerHTML = `<tr><td colspan="5" class="empty-state">${escapeHtml(emptyMessage)}</td></tr>`;
        renderWindowProgressCards([], marketDate, emptyMessage);
        return;
      }

      tbody.innerHTML = rows.map((row) => {
        const status = getWindowProgressStatus(row);
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
              ${getTvChartUrl(row) ? `<a class="symbol-link" href="${escapeHtml(getTvChartUrl(row))}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.symbol || '--')}</a>` : `<span class="symbol-link">${escapeHtml(row.symbol || '--')}</span>`}<br>
              <div class="pill-row window-symbol-pills">
                ${statusChip(getWindowProgressStatusLabel(status), status)}
                ${isWindowProgressTargetCandidate(row) ? statusChip('target_candidate', 'target_candidate') : statusChip(row.target_status || 'active', row.target_status || 'active')}
                ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}
              </div>
              <span class="muted">target ${escapeHtml(formatNumber(coalesceValue(row, ['target_score', 'score'], 0), 1))}</span>
              ${renderAdmissionControlRow(row)}
            </td>
            <td>${buildWindowSignalCell(row)}</td>
            <td>
              <div class="window-side-grid">
                <div>${formatWindowSide(row, 'upper')}</div>
                <div>${formatWindowSide(row, 'lower')}</div>
              </div>
              <div class="window-progress-subline muted mono">progress ${escapeHtml(formatWindowComponentProgress(row))}</div>
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
        const tvChartUrl = getTvChartUrl(row);
        const symbolHtml = tvChartUrl
          ? `<a class="mobile-data-symbol" href="${escapeHtml(tvChartUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.symbol || '--')}</a>`
          : `<span class="mobile-data-symbol">${escapeHtml(row.symbol || '--')}</span>`;
        return `
          <article class="mobile-data-card">
            <div class="mobile-data-head">
              <div>
                ${symbolHtml}
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
              ${dataQualityChip(row)}
              ${renderAdmissionScoreChip(row)}
            </div>

            <div class="mobile-data-grid">
              ${buildMobileMetricCard('价格', escapeHtml(formatPrice(row.display_price ?? row.price)))}
              ${buildMobileMetricCard('量能', `<span class="mobile-data-subcopy">10D ${escapeHtml(formatVolume(row.avg_10d_volume))} · PRE ${escapeHtml(formatVolume(row.premarket_volume))}</span>`)}
            </div>

            ${buildMobileSection('筛选理由', escapeHtml(row.scan_reason || row.note || '--'))}
            ${renderSymbolProfileSummary(row) ? buildMobileSection('Symbol profile', renderSymbolProfileSummary(row)) : ''}
            ${buildMobileSection('可操作依据', buildReasonPills(row))}
            ${(renderAdmissionControlRow(row) || getFailedGates(row).length) ? buildMobileSection('Admission / Gates', `${renderAdmissionControlRow(row)}${renderFailedGatesPills(row, 'failed_gates: none')}`) : ''}

            <div class="mobile-data-actions">
              ${buildTvChartLink(row, 'TV')}
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
              ${buildTvChartLink(item, item.symbol || '--') || `<span class="mobile-data-symbol">${escapeHtml(item.symbol || '--')}</span>`}
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
            ${buildMobileMetricCard('更新时间', `${escapeHtml(item.us_time || '--')}<br><span class="mobile-data-subcopy">${item.updated ? escapeHtml(formatMarketTime(item.updated, 'short')) : '--'}</span>`)}
          </div>

          ${buildMobileSection('理由', escapeHtml(item.scan_reason || '--'))}
          ${(buildTargetActivationHtml(item, { compact: true }) || buildTargetMtfContextHtml(item, { compact: true })) ? buildMobileSection('TV 激活 / MTF', `${buildTargetActivationHtml(item, { compact: true })}${buildTargetMtfContextHtml(item, { compact: true })}`) : ''}

          <div class="mobile-data-actions">
            ${buildTvChartLink(item, 'TV')}
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
                ${buildTvChartLink(item, item.symbol || '--') || `<span class="mobile-data-symbol">${escapeHtml(item.symbol || '--')}</span>`}
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
              ${buildMobileMetricCard('更新时间', `${escapeHtml(item.updated_us || item.us_time || '--')}<br><span class="mobile-data-subcopy">${item.updated ? escapeHtml(formatMarketTime(item.updated, 'short')) : '--'}</span>`)}
            </div>

            ${buildMobileSection('备注', escapeHtml(item.note || '--'))}
            ${renderSymbolProfileSummary(item) ? buildMobileSection('Symbol profile', renderSymbolProfileSummary(item)) : ''}
            ${(renderAdmissionControlRow(item) || getFailedGates(item).length) ? buildMobileSection('Admission / Gates', `${renderAdmissionControlRow(item)}${renderFailedGatesPills(item, 'failed_gates: none')}`) : ''}

            <div class="mobile-data-actions">
              ${buildTvChartLink(item, 'TV')}
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
        signal_state: document.getElementById('currentSignalStateFilter')?.value || '',
        target_status: document.getElementById('currentTargetStatusFilter')?.value || '',
        execution_layer: document.getElementById('currentExecutionLayerFilter')?.value || '',
        direction_bias: document.getElementById('currentDirectionBiasFilter')?.value || '',
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
        broker_mode: currentBrokerMode,
        market_data_mode: currentEnvironment,
        data_environment: currentEnvironment,
        market_date: marketDate,
        search: filters.search,
        signal_state: filters.signal_state,
        target_status: filters.target_status,
        execution_layer: filters.execution_layer,
        direction_bias: filters.direction_bias,
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

    function humanizeCurrentSignalReason(reason) {
      const code = String(reason || '').trim();
      const map = {
        target_direction_mismatch: '方向不匹配：信号方向与当日 active target 方向不一致',
        target_direction_missing: '缺少目标方向：当日 active target 未提供 long/short direction_bias',
        target_direction_provider_error: '目标方向读取失败',
        entry_guard_no_fresh_quote: '下单前没有新鲜报价',
        entry_guard_stop_already_crossed: '下单前已穿过止损位',
        entry_guard_price_drift: '下单前价格漂移过大',
        buying_power_blocked: '购买力阈值拦截',
        submit_failed: '订单提交失败',
        signal_expired: '信号已过有效期',
      };
      if (!code) return '';
      return map[code] || code.replace(/_/g, ' ');
    }

    function getCurrentSignalReason(row) {
      return String(
        row.latest_signal_status_reason_human
        || humanizeCurrentSignalReason(row.latest_signal_status_reason)
        || row.latest_signal_note
        || ''
      ).trim();
    }

    function renderCurrentSignalReason(row) {
      const status = formatCurrentSignalState(row);
      if (!['rejected', 'expired', 'protection_incomplete'].includes(status)) return '';
      const reason = getCurrentSignalReason(row);
      if (!reason) return '';
      const code = String(row.latest_signal_status_reason || '').trim();
      const brokerMode = String(row.latest_signal_effective_broker_mode || currentBrokerMode || '').trim();
      return `
        <div class="reason-copy current-signal-reason">
          ${escapeHtml(reason)}
          ${code ? `<span class="muted mono"> · ${escapeHtml(code)}</span>` : ''}
          ${brokerMode ? `<span class="muted mono"> · ${escapeHtml(brokerMode)}</span>` : ''}
        </div>
      `;
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
      const needsActionCount = rows.filter((row) => ['awaiting_confirm', 'pending', 'submitted', 'protected_active', 'protection_incomplete'].includes(String(row.latest_signal_status || ''))).length;
      const signaledCount = rows.filter((row) => row.has_signal_today).length;
      const executionEligibleCount = Number(summary.execution_eligible_count || 0) || 0;
      const observeOnlyCount = Number(summary.observe_only_count || 0) || 0;
      const watchOnlyCount = Number(summary.watch_only_count || 0) || 0;
      const scanTimeEt = String(workflow.scan_summary_time_et || '08:20-09:20');
      const openCheckTimeEt = String(workflow.open_check_time_et || scanTimeEt);
      const topupWindowEt = String(workflow.topup_window_et || '09:25-11:00');
      const workflowTimingCopy = scanTimeEt === openCheckTimeEt
        ? `${scanTimeEt} ET 覆盖预筛；${topupWindowEt} ET 增量入池；${workflow.intraday_refresh_rule || '5m close-driven'}`
        : `${scanTimeEt} ET 覆盖预筛；${topupWindowEt} ET 增量入池；${openCheckTimeEt} ET 检查`;
      meta.textContent = `${currentPage}/${totalPages} 页 · ${rows.length} 条 · exec ${executionEligibleCount} · observe ${observeOnlyCount} · signaled ${signaledCount} · action ${needsActionCount} · ${filteredTotal}/${summary.total || 0}`;
      metaSecondary.textContent = `signaled ${filteredSummary.signaled_count || 0} · action ${filteredSummary.needs_action_count || 0} · watch_only ${watchOnlyCount}。${workflowTimingCopy}。`;
      renderCurrentTargetPagination();

      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-state">当前条件下没有符合的标的。</td></tr>';
        renderCurrentTargetCards([], marketDate);
        return;
      }

      tbody.innerHTML = rows.map((row) => {
        const marketDateToken = todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || getUsDate();
        const signalUrl = buildPageUrl('/ibkr_signals.html', {
          date: marketDateToken,
          search: row.symbol || '',
        }, { environment: currentEnvironment });
        const tvChartUrl = getTvChartUrl(row);
        const flowUrl = buildLifecycleFlowUrl(row, marketDateToken);
        return `
          <tr>
            <td>
              ${tvChartUrl ? `<a class="symbol-link" href="${escapeHtml(tvChartUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.symbol || '--')}</a>` : `<span class="symbol-link">${escapeHtml(row.symbol || '--')}</span>`}<br>
              <span class="muted mono">${escapeHtml(row.latest_us_time || '--')}</span><br>
              <span class="muted">${escapeHtml(row.exchange || '--')} / ${escapeHtml(row.industry || '--')}</span>
              ${renderSymbolProfileSummary(row) ? `<div style="margin-top:8px;">${renderSymbolProfileSummary(row)}</div>` : ''}
              ${buildTargetActivationHtml(row)}
            </td>
            <td>
              ${statusChip(row.target_status || '--', row.target_status || '')}<br>
              ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}<br>
              <span class="muted">target ${escapeHtml(formatNumber(row.target_score || 0, 1))} · tradability ${escapeHtml(formatNumber(row.tradability_score || 0, 0))}</span>
              ${renderExecutionLayerPills(row)}
              ${renderAdmissionControlRow(row)}
              ${buildTargetMtfContextHtml(row)}
            </td>
            <td>
              ${statusChip(formatCurrentStateLabel(formatCurrentSignalState(row)), formatCurrentSignalState(row))}<br>
              <span class="muted">${escapeHtml(row.latest_signal_time || (row.has_signal_today ? '--' : '今日未出信号'))}</span><br>
              <span class="muted">count ${escapeHtml(String(row.signal_count_today || 0))}${row.latest_signal_direction ? ` · ${escapeHtml(String(row.latest_signal_direction || '').toUpperCase())}` : ''}</span>
              ${row.latest_signal_id ? `<br><span class="muted mono">${escapeHtml(row.latest_signal_id)}</span>` : ''}
              ${renderCurrentSignalReason(row)}
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
              ${renderExecutionLayerBlock(row)}
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
                ${buildTvChartLink(row, 'TV')}
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
      document.getElementById('currentTargetsMetaSecondary').textContent = '正在汇总目标状态与今日信号...';
      document.getElementById('currentTargetsTable').innerHTML = '<tr><td colspan="4" class="empty-state">加载中...</td></tr>';
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
          broker_mode: currentBrokerMode,
          market_data_mode: currentEnvironment,
          data_environment: currentEnvironment,
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
