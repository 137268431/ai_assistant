(function tradeReviewPage() {
  'use strict';

  const PAGE_PATH = '/ibkr_trade_review.html';
  const ENDPOINT = '/api/custom/ibkr/analytics/daily-trade-review';
  const state = { payload: null, selectedSymbol: '', search: '' };

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

  function authHeaders(extra = {}) {
    if (typeof getAuthHeaders === 'function') return getAuthHeaders(extra);
    const token = localStorage.getItem('pb_token') || '';
    return token ? { ...extra, Authorization: token } : extra;
  }

  function showMessage(message) {
    if (typeof showToast === 'function') showToast(message);
    else console.log(message);
  }

  function getReviewParams() {
    const params = {
      market_date: $('reviewDateInput').value || todayEt(),
      broker_mode: $('brokerModeInput').value || 'paper',
      data_environment: $('dataModeInput').value || 'live',
      market_data_mode: $('dataModeInput').value || 'live',
      status: $('statusInput').value || 'all',
      include_events: $('includeEventsInput').checked ? '1' : '0',
      limit: '500'
    };
    const symbol = String($('symbolInput').value || '').trim().toUpperCase();
    if (symbol) params.symbol = symbol;
    return params;
  }

  async function loadReview() {
    renderLoading();
    const params = getReviewParams();
    const query = new URLSearchParams(params).toString();
    const url = pageUrl(`${ENDPOINT}?${query}`);
    try {
      const response = await fetch(url, { headers: authHeaders() });
      const text = await response.text();
      const payload = text ? JSON.parse(text) : {};
      if (!response.ok || payload.ok === false) throw new Error(payload.error || `Request failed (${response.status})`);
      state.payload = payload;
      const first = (payload.items || [])[0];
      state.selectedSymbol = first ? first.symbol : '';
      renderAll();
    } catch (error) {
      renderError(error.message || String(error));
    }
  }

  function renderLoading() {
    $('summaryCards').innerHTML = '<article class="metric-card loading-card">正在聚合 daily-trade-review...</article>';
    $('symbolList').innerHTML = '<div class="empty-state">加载中...</div>';
    $('symbolDetail').innerHTML = '<div class="empty-state">加载中...</div>';
    $('issueList').innerHTML = '<div class="empty-state">加载中...</div>';
  }

  function renderError(message) {
    $('summaryCards').innerHTML = `<article class="metric-card error-card">加载失败：${escapeHtml(message)}</article>`;
    $('symbolList').innerHTML = '<div class="empty-state">没有可展示数据。</div>';
    $('symbolDetail').innerHTML = '<div class="empty-state">请检查 API、权限或 PocketBase schema。</div>';
    $('issueList').innerHTML = '<div class="empty-state">加载失败。</div>';
  }

  function renderAll() {
    renderSummary();
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
    const cards = [
      ['标的数', summary.symbols, 'selected + unselected + traded'],
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

  function filteredItems() {
    const rows = Array.isArray(state.payload?.items) ? state.payload.items : [];
    const needle = state.search.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((item) => {
      const haystack = [
        item.symbol,
        item.review_status,
        item.selection_reason,
        ...(item.not_selected_reasons || []).map((reason) => `${reason.code} ${reason.text}`),
        ...(item.issue_flags || []).map((flag) => `${flag.code} ${flag.message}`)
      ].join(' ').toLowerCase();
      return haystack.includes(needle);
    });
  }

  function toneForStatus(status) {
    const key = String(status || '').toLowerCase();
    if (key === 'problem') return 'danger';
    if (key === 'not_selected') return 'muted';
    if (key === 'closed') return 'done';
    if (key === 'open' || key === 'signaled') return 'warn';
    return 'info';
  }

  function renderSymbols() {
    const rows = filteredItems();
    if (!rows.length) {
      $('symbolList').innerHTML = '<div class="empty-state">没有匹配标的。</div>';
      return;
    }
    if (!rows.some((row) => row.symbol === state.selectedSymbol)) state.selectedSymbol = rows[0].symbol;
    $('symbolList').innerHTML = rows.map((item) => {
      const issues = (item.issue_flags || []).length;
      const reasons = item.not_selected_reasons || [];
      const reasonText = item.selection_reason || reasons[0]?.text || item.signal_summary?.latest_reason || '--';
      return `
        <button class="symbol-row ${item.symbol === state.selectedSymbol ? 'active' : ''}" type="button" data-symbol="${escapeHtml(item.symbol)}">
          <span class="symbol-main"><strong>${escapeHtml(item.symbol)}</strong><em>${escapeHtml(reasonText)}</em></span>
          <span class="status-pill ${toneForStatus(item.review_status)}">${escapeHtml(item.review_status || 'unknown')}</span>
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

  function renderReasonChips(item) {
    const chips = [];
    if (item.selection_reason) chips.push(['选标', item.selection_reason]);
    (item.not_selected_reasons || []).slice(0, 6).forEach((reason) => chips.push([reason.decision || '未选', reason.text || reason.code]));
    if (item.signal_summary?.latest_reason) chips.push(['信号', item.signal_summary.latest_reason]);
    if (!chips.length) return '<div class="empty-state compact">没有原因字段。</div>';
    return `<div class="reason-chip-list">${chips.map(([label, text]) => `<span class="reason-chip"><b>${escapeHtml(label)}</b>${escapeHtml(text)}</span>`).join('')}</div>`;
  }

  function renderEvents(item) {
    const events = Array.isArray(item.events) ? item.events : [];
    if (!events.length) {
      return '<div class="empty-state compact">未请求事件摘要；可勾选“显示事件摘要”重新加载，或打开生命周期图查看完整 DAG。</div>';
    }
    return `<div class="event-timeline">${events.map((event) => `
      <article class="event-item lane-${escapeHtml(event.lane || 'event')}">
        <div class="event-dot"></div>
        <div class="event-body">
          <div class="event-title">${escapeHtml(event.type || 'event')} <span>${escapeHtml(event.status || event.role || '')}</span></div>
          <div class="event-copy">${escapeHtml(event.reason || event.role || event.signal_id || '--')}</div>
          <div class="event-meta">${escapeHtml(event.time || event.ts_ms || '')} ${event.order_id ? `· ${escapeHtml(event.order_id)}` : ''}</div>
        </div>
      </article>
    `).join('')}</div>`;
  }

  function renderDetail() {
    const item = selectedItem();
    if (!item) {
      $('detailTitle').textContent = '选择一个标的';
      $('detailCopy').textContent = '查看这个标的从选标到平仓的所有事件和原因。';
      $('symbolDetail').innerHTML = '<div class="empty-state">没有详情。</div>';
      return;
    }
    $('detailTitle').textContent = `${item.symbol} · ${item.review_status}`;
    $('detailCopy').textContent = item.selection_reason || item.signal_summary?.latest_reason || '暂无摘要原因。';
    $('lifecycleLink').href = pageUrl(item.lifecycle_url || '/ibkr_lifecycle_flow.html');
    const order = item.order_summary || {};
    const signal = item.signal_summary || {};
    const realized = item.realized || {};
    $('symbolDetail').innerHTML = `
      <div class="detail-cards">
        <article><span>信号</span><strong>${escapeHtml(signal.total || 0)}</strong><em>${escapeHtml(signal.latest_status || '--')}</em></article>
        <article><span>开仓成交</span><strong>${escapeHtml(order.entry_filled || 0)}</strong><em>orders ${escapeHtml(order.total || 0)}</em></article>
        <article><span>保护单</span><strong>${escapeHtml(order.protection_orders || 0)}</strong><em>TP/SL</em></article>
        <article><span>平仓</span><strong>${escapeHtml(order.exit_filled || 0)}</strong><em>PnL ${escapeHtml(formatNumber(realized.net_pnl || 0, 2))}</em></article>
      </div>
      <section class="detail-block">
        <h3>原因</h3>
        ${renderReasonChips(item)}
      </section>
      <section class="detail-block">
        <h3>事件时间线</h3>
        ${renderEvents(item)}
      </section>
    `;
  }

  function renderIssues() {
    const rows = (state.payload?.items || []).filter((item) => (item.issue_flags || []).length);
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
        <a href="${pageUrl(item.lifecycle_url || '/ibkr_lifecycle_flow.html')}">打开生命周期</a>
      </article>
    `)).join('')}`;
  }

  function renderIntegrations() {
    const rows = state.payload?.integrations || [];
    $('integrationList').innerHTML = rows.map((item) => `
      <article class="integration-card">
        <strong>${escapeHtml(item.id)}</strong>
        <span>${escapeHtml(item.endpoint || item.collection || '')}</span>
        <p>${escapeHtml(item.role || '')}</p>
      </article>
    `).join('');
  }

  function initChrome() {
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
    $('statusInput').value = query.get('status') || 'all';
    $('symbolInput').value = (query.get('symbol') || '').toUpperCase();
  }

  document.addEventListener('DOMContentLoaded', () => {
    initFromQuery();
    initChrome();
    $('reviewFilterForm').addEventListener('submit', (event) => {
      event.preventDefault();
      initChrome();
      loadReview();
    });
    $('resetButton').addEventListener('click', () => {
      $('reviewDateInput').value = todayEt();
      $('brokerModeInput').value = 'paper';
      $('dataModeInput').value = 'live';
      $('statusInput').value = 'all';
      $('symbolInput').value = '';
      $('includeEventsInput').checked = true;
      loadReview();
    });
    $('localSearchInput').addEventListener('input', (event) => {
      state.search = event.target.value || '';
      renderSymbols();
    });
    loadReview().catch((error) => showMessage(error.message || String(error)));
  });
})();
