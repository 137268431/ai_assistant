(function tradeReviewPage() {
  'use strict';

  const PAGE_PATH = '/ibkr_trade_review.html';
  const ENDPOINT = '/api/custom/ibkr/analytics/daily-trade-review';
  const state = {
    payload: null,
    selectedSymbol: '',
    symbolTab: 'all',
    search: '',
    requestSeq: 0,
    abortController: null
  };
  const SYMBOL_TABS = [
    { id: 'all', label: '全部' },
    { id: 'active', label: '激活' },
    { id: 'selected', label: '已选' },
    { id: 'traded', label: '已交易' },
    { id: 'not_selected', label: '未选/拒绝' },
    { id: 'problem', label: '问题' }
  ];

  function $(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatNumber(value, digits = 0) {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return '--';
    return number.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
  }

  function todayEt() {
    if (typeof getCurrentEtDateString === 'function') return getCurrentEtDateString();
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  }

  function pageUrl(path, params = {}) {
    if (typeof buildPageUrl === 'function') return buildPageUrl(path, params);
    const query = new URLSearchParams(params);
    return `${path}${query.toString() ? `?${query}` : ''}`;
  }

  function linkedPageUrl(path, params = {}) {
    const text = String(path || '').trim();
    if (text.includes('?') && !Object.keys(params || {}).length) return text;
    return pageUrl(text || '/ibkr_lifecycle_flow.html', params);
  }

  function apiUrl(path, params = {}) {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, value);
    });
    return `${path}${query.toString() ? `?${query.toString()}` : ''}`;
  }

  function syncUrlFromForm() {
    const next = new URL(window.location.href);
    const params = getReviewParams();
    Object.entries(params).forEach(([key, value]) => {
      if (key === 'limit') return;
      if (value !== undefined && value !== null && value !== '') next.searchParams.set(key, value);
      else next.searchParams.delete(key);
    });
    if (!$('symbolInput').value) next.searchParams.delete('symbol');
    next.searchParams.delete('status');
    if (state.symbolTab && state.symbolTab !== 'all') next.searchParams.set('tab', state.symbolTab);
    else next.searchParams.delete('tab');
    window.history.replaceState({}, '', `${next.pathname}${next.search}`);
  }

  function authHeaders(extra = {}) {
    if (typeof getAuthHeaders === 'function') return getAuthHeaders(extra);
    const token = localStorage.getItem('pb_token') || '';
    return token ? { ...extra, Authorization: token } : extra;
  }

  function showMessage(message) {
    if (typeof showToast === 'function') showToast(message);
    else console.log(message);
  }

  function asObject(value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value;
    if (typeof value === 'string' && value.trim().startsWith('{')) {
      try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
      } catch (_) {
        return {};
      }
    }
    return {};
  }

  function asList(value) {
    if (Array.isArray(value)) return value.filter((item) => item !== undefined && item !== null && item !== '');
    if (value && typeof value === 'object') {
      return Object.entries(value).map(([key, item]) => (
        item && typeof item === 'object' ? { code: key, ...item } : { code: key, text: item }
      ));
    }
    if (typeof value === 'string' && value.trim()) {
      return value.split(/\n|;|\|/).map((item) => item.trim()).filter(Boolean);
    }
    return [];
  }

  function firstText(...values) {
    for (const value of values) {
      if (value === undefined || value === null) continue;
      if (Array.isArray(value)) {
        const joined = value.map((item) => firstText(item)).filter(Boolean).join(' / ');
        if (joined) return joined;
        continue;
      }
      const text = String(value).trim();
      if (text) return text;
    }
    return '';
  }

  function lowerText(value) {
    return firstText(value).toLowerCase();
  }

  function truthyValue(value) {
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return Number.isFinite(value) && value !== 0;
    const text = lowerText(value);
    return ['1', 'true', 'yes', 'y', 'on', 'passed', 'pass'].includes(text);
  }

  function normalizeSymbolTab(value) {
    const key = lowerText(value).replace(/[\s-]+/g, '_');
    if (SYMBOL_TABS.some((tab) => tab.id === key)) return key;
    return '';
  }

  function tabFromLegacyStatus(value) {
    const key = lowerText(value).replace(/[\s-]+/g, '_');
    if (['open', 'closed', 'traded'].includes(key)) return 'traded';
    if (['selected', 'signaled'].includes(key)) return 'selected';
    if (key === 'not_selected') return 'not_selected';
    if (key === 'problem') return 'problem';
    return '';
  }

  function classToken(value, fallback = 'item') {
    return lowerText(value).replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || fallback;
  }

  function reasonText(reason, fallback = '') {
    if (typeof reason === 'string' || typeof reason === 'number') return firstText(reason);
    const row = asObject(reason);
    return firstText(
      row.text,
      row.reason_text,
      row.reason,
      row.message,
      row.status_reason_human,
      row.status_reason,
      row.rejection_reason_human,
      row.rejection_reason_code,
      row.blocked_reason,
      row.expired_reason,
      row.code,
      row.reason_code,
      fallback
    );
  }

  function reasonLabel(reason, fallback = '原因') {
    if (typeof reason === 'string' || typeof reason === 'number') return fallback;
    const row = asObject(reason);
    return firstText(row.decision, row.stage, row.source, row.code, row.reason_code, fallback);
  }

  function formatTimeValue(...values) {
    for (const value of values) {
      if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
        if (typeof formatMarketTime === 'function') return formatMarketTime(value, 'datetime');
        return new Date(value).toISOString();
      }
      const text = firstText(value);
      if (!text || text === '0') continue;
      if (/^\d{12,}$/.test(text)) {
        const ms = Number(text);
        if (Number.isFinite(ms) && ms > 0) {
          if (typeof formatMarketTime === 'function') return formatMarketTime(ms, 'datetime');
          return new Date(ms).toISOString();
        }
      }
      return text;
    }
    return '--';
  }

  function reviewContext() {
    const payload = state.payload || {};
    const params = getReviewParams();
    return {
      market_date: firstText(payload.market_date, payload.date, params.market_date, todayEt()),
      broker_mode: lowerText(payload.broker_mode || params.broker_mode || 'paper') || 'paper',
      data_environment: lowerText(payload.data_environment || payload.market_data_mode || params.data_environment || 'live') || 'live'
    };
  }

  function marketSessionLabel(payload = state.payload) {
    const source = payload || {};
    const session = asObject(source.session || source.market_session || source.session_info || source.time_window);
    const summary = asObject(source.summary);
    return firstText(
      source.session_label,
      source.market_session_label,
      source.time_window_label,
      source.market_time_window,
      session.label,
      session.name,
      session.phase,
      summary.session_label,
      summary.time_window_label,
      '时段待确认'
    );
  }

  function renderReviewContextBar() {
    const node = $('contextBar');
    if (!node) return;
    const context = reviewContext();
    if (typeof setBrokerModeContext === 'function') {
      setBrokerModeContext({
        broker_mode: context.broker_mode,
        data_environment: context.data_environment,
        market_data_environment: context.data_environment
      });
    }
    if (typeof renderPageContextBar === 'function') {
      node.innerHTML = renderPageContextBar('🧾 IBKR 每日复盘', {
        subtitle: 'Broker / Data / Market Date / 时段 · forensic console',
        metaItems: [
          { label: 'Market Date', value: context.market_date },
        ]
      });
      return;
    }
    const sessionLabel = marketSessionLabel();
    node.innerHTML = `
      <div class="review-context-fallback">
        <strong>IBKR 每日复盘</strong>
        <span>Broker ${escapeHtml(context.broker_mode)}</span>
        <span>Data ${escapeHtml(context.data_environment)}</span>
        <span>Market Date ${escapeHtml(context.market_date)}</span>
        <span>时段 ${escapeHtml(sessionLabel)}</span>
      </div>
    `;
  }

  function getReviewParams() {
    const params = {
      market_date: $('reviewDateInput').value || todayEt(),
      broker_mode: $('brokerModeInput').value || 'paper',
      data_environment: $('dataModeInput').value || 'live',
      market_data_mode: $('dataModeInput').value || 'live',
      include_events: $('includeEventsInput').checked ? '1' : '0',
      limit: '500'
    };
    const symbol = String($('symbolInput').value || '').trim().toUpperCase();
    if (symbol) params.symbol = symbol;
    return params;
  }

  async function loadReview() {
    const seq = state.requestSeq + 1;
    state.requestSeq = seq;
    if (state.abortController) state.abortController.abort();
    state.abortController = new AbortController();
    renderLoading();
    const params = getReviewParams();
    const url = apiUrl(ENDPOINT, params);
    try {
      const response = await fetch(url, { headers: authHeaders(), signal: state.abortController.signal });
      const text = await response.text();
      const payload = text ? JSON.parse(text) : {};
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `Request failed (${response.status})`);
      if (seq !== state.requestSeq) return;
      state.payload = payload;
      if (typeof syncBrokerModeFromPayload === 'function') syncBrokerModeFromPayload(payload);
      const first = (payload.items || [])[0];
      state.selectedSymbol = first ? first.symbol : '';
      initChrome();
      renderAll();
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (seq !== state.requestSeq) return;
      state.payload = null;
      state.selectedSymbol = '';
      renderError(error.message || String(error));
    }
  }

  function renderLoading() {
    $('summaryCards').innerHTML = '<article class="metric-card loading-card">正在聚合 daily-trade-review...</article>';
    $('symbolStatusTabs').innerHTML = '';
    $('symbolList').innerHTML = '<div class="empty-state">加载中...</div>';
    $('symbolDetail').innerHTML = '<div class="empty-state">加载中...</div>';
    $('issueList').innerHTML = '<div class="empty-state">加载中...</div>';
  }

  function renderError(message) {
    $('summaryCards').innerHTML = `<article class="metric-card error-card">加载失败：${escapeHtml(message)}</article>`;
    $('symbolStatusTabs').innerHTML = '';
    $('symbolList').innerHTML = '<div class="empty-state">没有可展示数据。</div>';
    $('symbolDetail').innerHTML = '<div class="empty-state">请检查 API、权限或 PocketBase schema。</div>';
    $('issueList').innerHTML = '<div class="empty-state">加载失败。</div>';
  }

  function renderAll() {
    renderSummary();
    renderSymbolTabs();
    renderSymbols();
    renderDetail();
    renderIssues();
    renderIntegrations();
  }

  function renderSummary() {
    const payload = state.payload || {};
    const summary = payload.summary || {};
    const daily = summary.daily_signals || {};
    const realized = daily.realized || {};
    const filterCopy = `${payload.market_date || '--'} · ${payload.broker_mode || '--'} / ${payload.data_environment || '--'}`;
    const cards = [
      ['日期', payload.market_date || '--', filterCopy],
      ['返回数', summary.returned, summary.truncated ? `已截断 / total ${summary.symbols || 0}` : `total ${summary.symbols || 0}`],
      ['已选标', summary.selected_count, 'active / candidate target'],
      ['未选/拒绝', summary.not_selected_count, '完整账本来自 ibkr_target_decisions'],
      ['已成交', summary.traded_count, 'entry filled symbols'],
      ['问题数', summary.issue_count, '逻辑异常 / 缺失链路'],
      ['净 PnL', formatNumber(realized.net_pnl || 0, 2), `closed ${daily.closed_trades || 0}`],
      ['信号', summary.signal_count, `orders ${summary.order_count || 0}`],
      ['决策记录', summary.decision_rows, payload.warnings?.length ? '含兼容警告' : 'ledger rows']
    ];
    $('summaryCards').innerHTML = cards.map(([label, value, copy]) => `
      <article class="metric-card ${label === '问题数' && Number(value) > 0 ? 'is-hot' : ''}">
        <div class="metric-label">${escapeHtml(label)}</div>
        <div class="metric-value">${escapeHtml(value)}</div>
        <div class="metric-copy">${escapeHtml(copy)}</div>
      </article>
    `).join('');
  }

  function allReviewItems() {
    return Array.isArray(state.payload?.items) ? state.payload.items : [];
  }

  function isProblemItem(item) {
    return Boolean((item.issue_flags || []).length);
  }

  function orderSummary(item) {
    return asObject(item?.order_summary);
  }

  function isClosedItem(item) {
    const order = orderSummary(item);
    const status = lowerText(item?.review_status);
    return status === 'closed' || Number(order.exit_filled || 0) > 0;
  }

  function isTradedItem(item) {
    const order = orderSummary(item);
    const status = lowerText(item?.review_status);
    return isClosedItem(item)
      || ['open', 'traded'].includes(status)
      || Number(order.entry_filled || 0) > 0;
  }

  function tradeSummary(item) {
    const order = orderSummary(item);
    const realized = asObject(item?.realized);
    if (!isTradedItem(item)) return '';
    return `entry ${Number(order.entry_filled || 0)} · exit ${Number(order.exit_filled || 0)} · PnL ${formatNumber(realized.net_pnl || 0, 2)}`;
  }

  function isActiveTarget(item) {
    if (isProblemItem(item) || isTradedItem(item)) return false;
    const target = asObject(item.target);
    const targetExtra = asObject(target.extra);
    const selection = asObject(item.selection_summary);
    const targetStatus = lowerText(target.status || selection.target_status || item.target_status || item.status);
    return targetStatus === 'active' || truthyValue(selection.active_gate_passed) || truthyValue(targetExtra.active_gate_passed);
  }

  function isSelectedItem(item) {
    if (isProblemItem(item) || isTradedItem(item)) return false;
    const selection = asObject(item.selection_summary);
    const target = asObject(item.target);
    const targetStatus = lowerText(target.status || selection.target_status || item.target_status || item.status);
    const decision = lowerText(item.selection_decision || item.decision || selection.decision);
    const status = lowerText(item.review_status);
    return Boolean(item.target)
      || truthyValue(selection.selected)
      || isActiveTarget(item)
      || ['active', 'candidate', 'selected'].includes(targetStatus)
      || ['selected', 'active', 'candidate', 'accepted'].includes(decision)
      || ['selected', 'signaled', 'open', 'closed'].includes(status);
  }

  function isNotSelectedItem(item) {
    if (isProblemItem(item) || isTradedItem(item) || isSelectedItem(item)) return false;
    const selection = asObject(item.selection_summary);
    const decision = lowerText(item.selection_decision || item.decision || selection.decision || selection.latest_decision);
    const status = lowerText(item.review_status);
    if (status === 'not_selected') return true;
    if (['rejected', 'not_selected', 'deferred', 'error', 'blocked'].includes(decision)) return true;
    return collectNotSelectedReasons(item).length > 0;
  }

  function matchesSymbolTab(item, tab = state.symbolTab) {
    if (tab === 'active') return isActiveTarget(item);
    if (tab === 'selected') return isSelectedItem(item);
    if (tab === 'traded') return isTradedItem(item) && !isProblemItem(item);
    if (tab === 'not_selected') return isNotSelectedItem(item);
    if (tab === 'problem') return isProblemItem(item);
    return true;
  }

  function renderSymbolTabs() {
    const node = $('symbolStatusTabs');
    if (!node) return;
    const rows = allReviewItems();
    const counts = {
      all: rows.length,
      active: rows.filter((item) => matchesSymbolTab(item, 'active')).length,
      selected: rows.filter((item) => matchesSymbolTab(item, 'selected')).length,
      traded: rows.filter((item) => matchesSymbolTab(item, 'traded')).length,
      not_selected: rows.filter((item) => matchesSymbolTab(item, 'not_selected')).length,
      problem: rows.filter((item) => matchesSymbolTab(item, 'problem')).length
    };
    node.innerHTML = SYMBOL_TABS.map((tab) => `
      <button
        class="symbol-tab ${tab.id === state.symbolTab ? 'is-active' : ''}"
        type="button"
        role="tab"
        aria-selected="${tab.id === state.symbolTab ? 'true' : 'false'}"
        data-tab="${escapeHtml(tab.id)}"
      >
        <span>${escapeHtml(tab.label)}</span>
        <strong>${escapeHtml(counts[tab.id] || 0)}</strong>
      </button>
    `).join('');
    node.querySelectorAll('.symbol-tab').forEach((button) => {
      button.addEventListener('click', () => {
        const nextTab = normalizeSymbolTab(button.dataset.tab) || 'all';
        if (nextTab === state.symbolTab) return;
        state.symbolTab = nextTab;
        syncUrlFromForm();
        renderSymbolTabs();
        renderSymbols();
        renderDetail();
        renderIssues();
      });
    });
  }

  function filteredItems() {
    const rows = allReviewItems().filter((item) => matchesSymbolTab(item));
    const needle = state.search.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((item) => {
      const haystack = [
        item.symbol,
        item.review_status,
        item.selection_reason,
        item.selection_summary?.reason_text,
        item.selection_summary?.reason_code,
        item.signal_summary?.status_explanation,
        ...(item.not_selected_reasons || []).map((reason) => `${reason.code} ${reason.text} ${reason.reason_code || ''}`),
        ...(item.issue_flags || []).map((flag) => `${flag.code} ${flag.message}`)
      ].join(' ').toLowerCase();
      return haystack.includes(needle);
    });
  }

  function reviewStatusLabel(item) {
    const key = lowerText(item?.review_status);
    if (isProblemItem(item)) return '链路异常';
    if (isClosedItem(item)) return '已闭环';
    if (isTradedItem(item)) return '已开仓';
    if (key === 'not_selected') return '未选/拒绝';
    if (key === 'selected') return '已选标';
    if (key === 'signaled') return '已出信号';
    return firstText(item?.review_status, 'unknown');
  }

  function toneForStatus(status, item = null) {
    if (item && isProblemItem(item)) return 'danger';
    if (item && isClosedItem(item)) return 'closed';
    if (item && isTradedItem(item)) return 'traded';
    const key = String(status || '').toLowerCase();
    if (key === 'problem') return 'danger';
    if (key === 'not_selected') return 'muted';
    if (key === 'closed') return 'closed';
    if (key === 'open') return 'traded';
    if (key === 'open' || key === 'signaled') return 'warn';
    return 'info';
  }

  function rowStateClasses(item) {
    return [
      item.symbol === state.selectedSymbol ? 'active' : '',
      isProblemItem(item) ? 'is-problem' : '',
      isClosedItem(item) && !isProblemItem(item) ? 'is-closed' : '',
      isTradedItem(item) && !isClosedItem(item) && !isProblemItem(item) ? 'is-traded' : ''
    ].filter(Boolean).join(' ');
  }

  function renderSymbols() {
    const rows = filteredItems();
    if (!rows.length) {
      $('symbolList').innerHTML = '<div class="empty-state">没有匹配标的。</div>';
      state.selectedSymbol = '';
      renderDetail();
      return;
    }
    if (!rows.some((row) => row.symbol === state.selectedSymbol)) state.selectedSymbol = rows[0].symbol;
    $('symbolList').innerHTML = rows.map((item) => {
      const issues = (item.issue_flags || []).length;
      const reasons = collectNotSelectedReasons(item);
      const signal = signalSummary(item);
      const reasonText = selectedReason(item) || reasons[0]?.text || signal.reason || '--';
      const tradeLine = tradeSummary(item);
      return `
        <button class="symbol-row ${rowStateClasses(item)}" type="button" data-symbol="${escapeHtml(item.symbol)}">
          <span class="symbol-main">
            <strong>${escapeHtml(item.symbol)}</strong>
            <em>${escapeHtml(reasonText)}</em>
            ${tradeLine ? `<small class="symbol-trade-line">${escapeHtml(tradeLine)}</small>` : ''}
          </span>
          <span class="status-pill ${toneForStatus(item.review_status, item)}">${escapeHtml(reviewStatusLabel(item))}</span>
          ${issues ? `<span class="issue-count">${issues}</span>` : ''}
        </button>
      `;
    }).join('');
    document.querySelectorAll('.symbol-row').forEach((node) => {
      node.addEventListener('click', () => {
        state.selectedSymbol = node.dataset.symbol || '';
        renderSymbols();
        renderDetail();
      });
    });
  }

  function selectedItem() {
    return (state.payload?.items || []).find((item) => item.symbol === state.selectedSymbol) || null;
  }

  function collectNotSelectedReasons(item) {
    const target = asObject(item.target);
    const targetExtra = asObject(target.extra);
    const rawReasons = [
      ...asList(item.not_selected_reasons),
      ...asList(item.rejection_reasons),
      ...asList(item.rejected_reasons),
      ...asList(item.blockers),
      ...asList(item.blocked_reasons),
      ...asList(item.execution_blockers || target.execution_blockers || targetExtra.execution_blockers)
    ];
    const seen = new Set();
    return rawReasons.map((reason) => {
      const label = reasonLabel(reason, '未选');
      const text = reasonText(reason);
      return {
        label,
        text,
        tone: reasonTone(label, text)
      };
    }).filter((reason) => {
      const key = `${reason.label}:${reason.text}`;
      if (!reason.text || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function selectedReason(item) {
    const target = asObject(item.target);
    const extra = asObject(target.extra);
    const activeSummary = asObject(extra.active_reason_summary);
    const selection = asObject(item.selection_summary);
    const selectedDecision = [
      ...asList(item.decisions),
      ...asList(item.target_decisions),
      ...asList(item.selection_decisions)
    ]
      .find((decision) => ['selected', 'active', 'accepted'].includes(lowerText(asObject(decision).decision)));
    return firstText(
      item.selection_reason,
      item.selected_reason,
      item.why_selected,
      selection.reason_text,
      target.scan_reason,
      activeSummary.scan_reason,
      reasonText(selectedDecision)
    );
  }

  function reasonTone(label, text) {
    const value = `${label || ''} ${text || ''}`.toLowerCase();
    if (value.includes('error') || value.includes('blocked') || value.includes('blocker')) return 'blocked';
    if (value.includes('deferred') || value.includes('skip') || value.includes('already exists') || value.includes('已存在')) return 'deferred';
    if (value.includes('rejected') || value.includes('not_selected') || value.includes('拒绝') || value.includes('未选')) return 'rejected';
    return 'muted';
  }

  function splitReasonTokens(value) {
    return firstText(value)
      .split(/[,，;；\n|]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function shortReasonText(value, maxLength = 120) {
    const text = firstText(value);
    if (text.length <= maxLength) return text;
    return `${text.slice(0, maxLength - 1)}…`;
  }

  function renderSelectedReasonList(reason) {
    const text = firstText(reason);
    const tokens = splitReasonTokens(text);
    if (!tokens.length) {
      return '<span class="reason-chip muted reason-token"><span>没有 selected target / selected decision 原因字段。</span></span>';
    }
    const limit = 8;
    const visible = tokens.slice(0, limit).map((token) => (
      `<span class="reason-chip reason-token selected" title="${escapeHtml(token)}"><span>${escapeHtml(token)}</span></span>`
    )).join('');
    const overflow = tokens.length > limit
      ? `<span class="reason-chip muted reason-token" title="${escapeHtml(text)}"><span>+${tokens.length - limit} more</span></span>`
      : '';
    return `<div class="reason-chip-list selected-reason-list" title="${escapeHtml(text)}">${visible}${overflow}</div>`;
  }

  function renderNotSelectedReasonList(reasons, { selectedContext = false } = {}) {
    const emptyCopy = selectedContext
      ? '没有拒绝/跳过记录；最终状态由选标、信号和订单链路确认。'
      : '无 rejected/not_selected/blocker 字段；旧 payload 只能说明“没有入选证据”。';
    if (!reasons.length) {
      return `<span class="reason-chip muted"><b>${selectedContext ? '无记录' : '未选'}</b><span>${escapeHtml(emptyCopy)}</span></span>`;
    }
    return reasons.slice(0, 8).map((reason) => (
      `<span class="reason-chip ${escapeHtml(reason.tone || 'muted')}"><b>${escapeHtml(reason.label)}</b><span>${escapeHtml(reason.text)}</span></span>`
    )).join('');
  }

  function selectionSummary(item) {
    const status = lowerText(item.review_status);
    const selection = lowerText(item.selection_decision || item.decision);
    const target = asObject(item.target);
    const targetStatus = lowerText(target.status || item.target_status || item.status);
    const hasTarget = Boolean(item.target) || ['active', 'candidate', 'selected'].includes(targetStatus);
    const selected = hasTarget || ['selected', 'active', 'candidate', 'accepted'].includes(selection) || ['selected', 'signaled', 'open', 'closed'].includes(status);
    const rejected = ['rejected', 'not_selected', 'deferred', 'error', 'blocked'].includes(selection) || status === 'not_selected';
    if (status === 'problem') {
      return {
        label: hasTarget ? '已选但异常' : '链路异常',
        tone: 'danger',
        copy: (item.issue_flags || [])[0]?.message || '执行链路存在 issue_flags'
      };
    }
    if (isClosedItem(item)) {
      return {
        label: '已闭环',
        tone: 'closed',
        copy: `${tradeSummary(item) || '已交易并结束闭环'}；${selectedReason(item) || '来自今日信号/订单链路'}`
      };
    }
    if (isTradedItem(item)) {
      return {
        label: '已交易',
        tone: 'traded',
        copy: `${tradeSummary(item) || '已有 entry 成交'}；${selectedReason(item) || '来自今日信号/订单链路'}`
      };
    }
    if (selected) {
      return { label: '已选标', tone: 'done', copy: selectedReason(item) || '进入今日候选/active 标的池' };
    }
    if (rejected) {
      const reasons = collectNotSelectedReasons(item);
      return { label: '未选/拒绝', tone: 'muted', copy: reasons[0]?.text || '选标账本标记为 rejected/not_selected' };
    }
    return { label: '无明确结论', tone: 'warn', copy: '旧 payload 未提供 selection_decision / target 记录' };
  }

  function signalSummary(item) {
    const signal = asObject(item.signal_summary);
    const signalEvent = collectEvents(item).filter((event) => lowerText(event.type).includes('signal')).pop() || {};
    const total = Number(signal.total || item.signal_count_today || item.signal_count || 0);
    const status = firstText(
      signal.latest_status,
      item.latest_signal_status,
      signalEvent.status,
      total ? 'generated' : ''
    );
    const reason = firstText(
      signal.status_explanation,
      signal.status_reason,
      signal.latest_reason,
      item.latest_signal_status_reason_human,
      item.latest_signal_status_reason,
      item.latest_signal_note,
      signalEvent.reason
    );
    const time = formatTimeValue(signal.latest_time, item.latest_signal_time, signalEvent.time, signal.bar_time_ms, signal.latest_time_ms, item.latest_signal_time_ms, signalEvent.ts_ms);
    const expiredAt = formatTimeValue(signal.expired_at, signal.expired_at_ms);
    return {
      total,
      status,
      reason,
      time,
      signal: firstText(signal.signal, item.latest_signal_name, signalEvent.signal),
      statusExplanation: firstText(signal.status_explanation, reason),
      expiredAt: expiredAt === '--' ? '' : expiredAt,
      blockedReason: firstText(signal.blocked_reason, signal.filter_reason)
    };
  }

  function blockedOrExpiredReason(item) {
    const signal = signalSummary(item);
    const status = lowerText(signal.status);
    const terminal = ['expired', 'blocked', 'rejected', 'cancelled', 'canceled'].some((key) => status.includes(key));
    const explicit = firstText(
      item.expired_reason,
      item.blocked_reason,
      item.filter_reason,
      item.status_reason,
      signal.blockedReason
    );
    if (explicit) return explicit;
    if (!terminal) return '';
    return firstText(
      signal.statusExplanation,
      signal.reason,
      terminal && signal.expiredAt ? `过期时间 ${signal.expiredAt}` : '',
      terminal ? `${signal.status}（旧 payload 未提供详细原因）` : ''
    );
  }

  function collectEvents(item) {
    return [
      ...asList(item.events),
      ...asList(item.timeline),
      ...asList(item.lifecycle_events),
      ...asList(item.event_chain)
    ].map((event) => {
      if (typeof event === 'string' || typeof event === 'number') return { type: event, lane: 'event' };
      return asObject(event);
    }).filter((event) => Object.keys(event).length);
  }

  function renderReasonChips(item) {
    const selection = selectionSummary(item);
    const selectedCopy = selectedReason(item);
    const notSelected = collectNotSelectedReasons(item);
    const signal = signalSummary(item);
    const blockedReason = blockedOrExpiredReason(item);
    const selectedContext = isSelectedItem(item) || isTradedItem(item);
    const selectedFallback = selectedCopy || (selectedContext ? '已进入今日目标 / 信号 / 订单链路。' : '');
    const notSelectedLabel = selectedContext ? '拒绝/跳过记录' : '为什么没选';
    const notSelectedHelp = selectedContext
      ? '这些记录可能来自盘中补池、重复入池或风控拒绝；最终结论以上方“选标结论”为准。'
      : '来自 rejected / not_selected / deferred 决策账本。';
    const blockedCard = blockedReason ? `
        <article class="explain-card warn">
          <span>为什么过期/阻塞</span>
          <strong>${escapeHtml(shortReasonText(blockedReason))}</strong>
          <p>${escapeHtml('来自 signal status_reason / blocker / rejected ledger。')}</p>
        </article>
    ` : '';
    return `
      <div class="forensic-explain-grid">
        <article class="explain-card ${classToken(selection.tone)}">
          <span>选标结论</span>
          <strong>${escapeHtml(selection.label)}</strong>
          <p title="${escapeHtml(selection.copy)}">${escapeHtml(shortReasonText(selection.copy))}</p>
        </article>
        <article class="explain-card ${selectedContext ? 'done' : 'secondary'}">
          <span>为什么选</span>
          ${renderSelectedReasonList(selectedFallback)}
          <p>${escapeHtml(selectedCopy ? '来自 selected target / selected decision / scan_reason。' : '没有入选原因字段。')}</p>
        </article>
        <article class="explain-card ${selectedContext ? 'secondary' : 'warn'}">
          <span>${escapeHtml(notSelectedLabel)}</span>
          <div class="reason-chip-list">${renderNotSelectedReasonList(notSelected, { selectedContext })}</div>
          <p>${escapeHtml(notSelectedHelp)}</p>
        </article>
        <article class="explain-card">
          <span>信号什么时候产生</span>
          <strong>${escapeHtml(signal.time)}</strong>
          <p>${escapeHtml(signal.total ? `${signal.signal || signal.status || 'generated'} · ${signal.reason || '无原因字段'}` : '当天没有同标的信号记录。')}</p>
        </article>
        ${blockedCard}
      </div>
    `;
  }

  function emptyChainExplanation(item) {
    const filters = asObject(state.payload?.filters);
    const signal = signalSummary(item);
    const order = asObject(item.order_summary);
    const hasSummaryEvidence = Boolean(item.target || item.decision_count || signal.total || order.total || collectNotSelectedReasons(item).length);
    if (String(filters.include_events) === 'false' || String(filters.include_events) === '0') {
      return '事件摘要未请求；上方链路由摘要字段重建。勾选“显示事件摘要”可拉取 target/signal/order 事件。';
    }
    if (hasSummaryEvidence) {
      return 'API 没有返回 events 数组；已用旧 payload 的摘要字段重建执行链路，缺少逐笔时间线。';
    }
    return '空链路：当天没有 target_decisions、selected target、same-day signal 或 order；该标的没有可追踪执行事件。';
  }

  function renderEvents(item) {
    const events = collectEvents(item);
    if (!events.length) {
      return `<div class="empty-state compact">${escapeHtml(emptyChainExplanation(item))}</div>`;
    }
    return `<div class="event-timeline">${events.map((event) => {
      const meta = [
        formatTimeValue(event.time, event.us_time, event.ts_ms),
        event.source,
        event.order_id ? `order ${event.order_id}` : '',
        event.signal_id ? `signal ${event.signal_id}` : '',
        event.direction
      ].filter(Boolean).join(' · ');
      return `
        <article class="event-item lane-${classToken(event.lane || event.type || 'event')}">
          <div class="event-dot"></div>
          <div class="event-body">
            <div class="event-title">${escapeHtml(event.type || event.event_type || 'event')} <span>${escapeHtml(firstText(event.status, event.role))}</span></div>
            <div class="event-copy">${escapeHtml(firstText(event.reason, event.message, event.role, event.signal_id, '--'))}</div>
            <div class="event-meta">${escapeHtml(meta || '--')}</div>
          </div>
        </article>
      `;
    }).join('')}</div>`;
  }

  function renderExecutionChain(item) {
    const selection = selectionSummary(item);
    const signal = signalSummary(item);
    const order = asObject(item.order_summary);
    const realized = asObject(item.realized);
    const steps = [
      { label: '选标', value: selection.label, tone: selection.tone, copy: selection.copy },
      { label: '信号', value: signal.total ? (signal.status || 'generated') : '无信号', tone: signal.total ? 'info' : 'muted', copy: signal.total ? `${signal.time} · ${signal.reason || '无原因字段'}` : '没有 same-day signal' },
      { label: '入场', value: Number(order.entry_filled || 0) ? 'filled' : (Number(order.total || 0) ? 'order seen' : '无订单'), tone: Number(order.entry_filled || 0) ? 'done' : 'muted', copy: `entry ${order.entry_filled || 0} / orders ${order.total || 0}` },
      { label: '保护', value: Number(order.protection_orders || 0) ? 'TP/SL seen' : '无保护单', tone: Number(order.protection_orders || 0) ? 'warn' : 'muted', copy: `protection ${order.protection_orders || 0}` },
      { label: '退出', value: Number(order.exit_filled || 0) ? 'closed' : '未平仓/无退出', tone: Number(order.exit_filled || 0) ? 'done' : 'muted', copy: `exit ${order.exit_filled || 0} · PnL ${formatNumber(realized.net_pnl || 0, 2)}` }
    ];
    return `
      <div class="execution-chain">
        ${steps.map((step) => `
          <article class="chain-step ${classToken(step.tone)}">
            <span>${escapeHtml(step.label)}</span>
            <strong>${escapeHtml(step.value)}</strong>
            <em>${escapeHtml(step.copy)}</em>
          </article>
        `).join('')}
      </div>
      <div class="chain-events">${renderEvents(item)}</div>
    `;
  }

  function renderDetail() {
    const item = selectedItem();
    if (!item) {
      $('detailTitle').textContent = '选择一个标的';
      $('detailCopy').textContent = '查看这个标的从选标到平仓的所有事件和原因。';
      $('symbolDetail').innerHTML = '<div class="empty-state">没有详情。</div>';
      return;
    }
    $('detailTitle').textContent = `${item.symbol} · ${reviewStatusLabel(item)}`;
    const selection = selectionSummary(item);
    const signal = signalSummary(item);
    const notSelected = collectNotSelectedReasons(item);
    $('detailCopy').textContent = tradeSummary(item) || selectedReason(item) || notSelected[0]?.text || signal.reason || selection.copy || '暂无摘要原因。';
    $('lifecycleLink').href = linkedPageUrl(item.lifecycle_url || '/ibkr_lifecycle_flow.html');
    const order = item.order_summary || {};
    const realized = item.realized || {};
    $('symbolDetail').innerHTML = `
      <div class="detail-cards">
        <article><span>结论</span><strong>${escapeHtml(selection.label)}</strong><em title="${escapeHtml(selection.copy || '--')}">${escapeHtml(shortReasonText(selection.copy || '--'))}</em></article>
        <article><span>信号时间</span><strong>${escapeHtml(signal.time)}</strong><em>${escapeHtml(signal.status || '--')}</em></article>
        <article><span>开仓成交</span><strong>${escapeHtml(order.entry_filled || 0)}</strong><em>orders ${escapeHtml(order.total || 0)}</em></article>
        <article><span>平仓</span><strong>${escapeHtml(order.exit_filled || 0)}</strong><em>PnL ${escapeHtml(formatNumber(realized.net_pnl || 0, 2))}</em></article>
      </div>
      <section class="detail-block">
        <h3>选标 / 信号解释</h3>
        ${renderReasonChips(item)}
      </section>
      <section class="detail-block">
        <h3>执行链路</h3>
        ${renderExecutionChain(item)}
      </section>
    `;
  }

  function renderIssues() {
    const rows = filteredItems().filter((item) => (item.issue_flags || []).length);
    const warnings = state.payload?.warnings || [];
    const warningHtml = warnings.length ? warnings.map((warning) => `
      <article class="issue-card warn"><strong>${escapeHtml(warning.code || 'warning')}</strong><p>${escapeHtml(warning.message || '')}</p></article>
    `).join('') : '';
    if (!rows.length && !warnings.length) {
      $('issueList').innerHTML = '<div class="empty-state">当前筛选范围没有发现执行逻辑问题。</div>';
      return;
    }
    $('issueList').innerHTML = `${warningHtml}${rows.flatMap((item) => (item.issue_flags || []).map((flag) => `
      <article class="issue-card ${escapeHtml(flag.severity || 'medium')}">
        <strong>${escapeHtml(item.symbol)} · ${escapeHtml(flag.code)}</strong>
        <p>${escapeHtml(flag.message || '')}</p>
        <a href="${linkedPageUrl(item.lifecycle_url || '/ibkr_lifecycle_flow.html')}">打开生命周期</a>
      </article>
    `)).join('')}`;
  }

  function renderIntegrations() {
    const node = $('integrationList');
    if (!node) return;
    const rows = state.payload?.integrations || [];
    node.innerHTML = rows.length ? rows.map((item) => `
      <article class="integration-card">
        <strong>${escapeHtml(item.id)}</strong>
        <span>${escapeHtml(item.endpoint || item.collection || '')}</span>
        <p>${escapeHtml(item.role || '')}</p>
      </article>
    `).join('') : '<div class="empty-state compact">接口来源暂无返回；不影响复盘结果。</div>';
  }

  function initChrome() {
    renderReviewContextBar();
    const nav = $('nav');
    if (nav && typeof renderNav === 'function') nav.innerHTML = renderNav(PAGE_PATH);
    const bridge = $('pageBridge');
    if (bridge && typeof renderExecutionBridge === 'function') bridge.innerHTML = renderExecutionBridge(PAGE_PATH, getReviewParams());
  }

  function initFromQuery() {
    const query = new URLSearchParams(window.location.search);
    $('reviewDateInput').value = query.get('market_date') || query.get('date') || todayEt();
    $('brokerModeInput').value = query.get('broker_mode') || 'paper';
    $('dataModeInput').value = query.get('data_environment') || query.get('market_data_mode') || 'live';
    state.symbolTab = normalizeSymbolTab(query.get('tab')) || tabFromLegacyStatus(query.get('status')) || 'all';
    $('symbolInput').value = (query.get('symbol') || '').toUpperCase();
    if (query.has('include_events')) $('includeEventsInput').checked = ['1', 'true', 'yes', 'on'].includes(String(query.get('include_events') || '').toLowerCase());
  }

  document.addEventListener('DOMContentLoaded', () => {
    initFromQuery();
    initChrome();
    $('reviewFilterForm').addEventListener('submit', (event) => {
      event.preventDefault();
      syncUrlFromForm();
      initChrome();
      loadReview();
    });
    $('resetButton').addEventListener('click', () => {
      $('reviewDateInput').value = todayEt();
      $('brokerModeInput').value = 'paper';
      $('dataModeInput').value = 'live';
      state.symbolTab = 'all';
      $('symbolInput').value = '';
      $('localSearchInput').value = '';
      state.search = '';
      $('includeEventsInput').checked = true;
      syncUrlFromForm();
      initChrome();
      loadReview();
    });
    $('localSearchInput').addEventListener('input', (event) => {
      state.search = event.target.value || '';
      renderSymbols();
      renderDetail();
      renderIssues();
    });
    loadReview().catch((error) => showMessage(error.message || String(error)));
  });
})();
