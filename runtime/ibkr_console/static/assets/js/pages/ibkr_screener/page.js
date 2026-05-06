let currentEnvironment = getCurrentRuntimeEnvironment();
    let activeTab = 'screener';
    let activeScreenerView = 'current';
    let screenerPayload = { items: [], summary: {}, filters: {} };
    let todayTargetsPayload = {
      items: [],
      summary: {},
      market_date: '',
      filtered_total: 0,
      filtered_summary: {},
      page: 1,
      total_pages: 1,
    };
    let windowProgressPayload = {
      items: [],
      summary: {},
      market_date: '',
      computed_at_us: '',
    };
    let runtimeCurrentMarketDate = '';
    let rulesPayload = { selection: null, signals: null, computed_at_us: '' };
    let rulesLoadError = '';
    let filteredRows = [];
    let filteredCurrentTargetRows = [];
    let activeWindowProgressStatus = 'all';
    const selectedSymbols = new Set();
    const dailyTargetsState = {
      selectedDate: '',
      items: [],
      searchResults: [],
      loaded: false,
      loadedDate: '',
      lastRefresh: '尚未加载'
    };
    const watchlistState = {
      items: [],
      searchResults: [],
      loaded: false,
      lastRefresh: '尚未加载',
      loadedRole: '',
      configLoadError: ''
    };
    const CONFIG_MONITOR_SOURCE = 'config_market_ws_symbols';
    const currentTargetState = {
      page: 1,
      perPage: 10,
      requestToken: 0,
      searchDebounceId: 0,
    };
    const windowProgressState = {
      requestToken: 0,
      loadedKey: '',
    };
    const WINDOW_PROGRESS_STATUS_TABS = [
      { key: 'all', label: '全部' },
      { key: 'candidate', label: '候选' },
      { key: 'blocked', label: '阻塞' },
      { key: 'near_expiry', label: '临期' },
      { key: 'active', label: '活跃' },
      { key: 'no_window', label: '无窗口' },
      { key: 'other', label: '其他' },
    ];
    const manualDailyScanState = {
      running: false,
      lastResult: null,
      status: 'idle',
      message: '等待补跑',
    };
    const screenerPaginationState = {
      page: 1,
      perPage: 10,
    };
    const watchlistPaginationState = {
      page: 1,
      perPage: 10,
    };
    let currentFiltersExpanded = false;
    let rulesLoaded = false;
    let rulesLoadingPromise = null;
    let screenerLoadKey = '';
    let responsiveStateBound = false;
    let lastPhoneViewport = null;
    const expandedRulesPanels = new Set();
    let activeReadyTipId = '';
    let activeReadyTipTrigger = null;
    let activeReadyTipHoverRoot = null;
    let readyTipCloseTimer = 0;

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function initAuth() {
      try {
        requireAuth(`${location.pathname}${location.search}`);
        return true;
      } catch (_) {
        return false;
      }
    }

    async function requestJson(path, { method = 'GET', body = null } = {}) {
      const token = getToken();
      const headers = { 'Content-Type': 'application/json' };
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetchWithRetry(`${BASE_URL}${path}`, {
        method,
        headers,
        body: body ? JSON.stringify(body) : null
      }, {
        attempts: 3,
        retryDelayMs: 500
      });
      const text = await response.text();
      let payload = {};
      try {
        payload = text ? JSON.parse(text) : {};
      } catch (_) {
        payload = { ok: false, raw: text };
      }
      if (response.status === 401 || response.status === 403) {
        handleAuthError();
        throw new Error('Authentication failed');
      }
      if (!response.ok) {
        throw new Error(payload.message || payload.error || `Request failed (${response.status})`);
      }
      return payload;
    }

    async function deleteRecord(collection, recordId) {
      const token = getToken();
      const headers = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch(`${PB_AUTH_BASE_URL}/api/collections/${collection}/records/${recordId}`, {
        method: 'DELETE',
        headers
      });
      if (response.status === 401 || response.status === 403) {
        handleAuthError();
        throw new Error('Authentication failed');
      }
      if (!response.ok) {
        let payload = {};
        try {
          payload = await response.json();
        } catch (_) {
          payload = {};
        }
        throw new Error(payload.message || payload.error || `Delete failed (${response.status})`);
      }
    }

    function getRuntimeWarning(payload) {
      const reconcile = payload?.runtime_reconcile;
      if (reconcile && reconcile.ok === false) {
        return reconcile.error || 'runtime reconcile failed';
      }
      return '';
    }

    function getWatchlistSyncWarning(payload) {
      const sync = payload?.watchlist_sync;
      if (sync && sync.ok === false) {
        return sync.error || 'watchlist sync failed';
      }
      return '';
    }

    function buildQuery(params) {
      const query = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') {
          query.set(key, String(value));
        }
      });
      const text = query.toString();
      return text ? `?${text}` : '';
    }

    function formatNumber(value, digits = 0) {
      const num = Number(value);
      if (!Number.isFinite(num)) return '--';
      return num.toLocaleString('en-US', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      });
    }

    function formatPrice(value) {
      const num = Number(value);
      if (!Number.isFinite(num) || num <= 0) return '--';
      return `$${formatNumber(num, 2)}`;
    }

    function formatPct(value) {
      const num = Number(value);
      if (!Number.isFinite(num)) return '--';
      const prefix = num > 0 ? '+' : '';
      return `${prefix}${formatNumber(num, 2)}%`;
    }

    function formatVolume(value) {
      const num = Number(value);
      if (!Number.isFinite(num) || num <= 0) return '--';
      if (num >= 1000000) return `${formatNumber(num / 1000000, 2)}M`;
      if (num >= 1000) return `${formatNumber(num / 1000, 1)}K`;
      return formatNumber(num, 0);
    }

    function formatFreshness(value) {
      const num = Number(value);
      if (!Number.isFinite(num) || num < 0) return '无当日bar';
      if (num < 60) return `${num} min`;
      return `${formatNumber(num / 60, 1)} h`;
    }

    function dataQualityChip(row) {
      const quality = row?.data_quality && typeof row.data_quality === 'object' ? row.data_quality : {};
      const status = String(quality.status || '').trim().toLowerCase();
      if (!status) return '';
      if (status === 'ready') return statusChip('数据完整', 'active');
      const count = Array.isArray(quality.needs_repair_intervals) ? quality.needs_repair_intervals.length : 0;
      return statusChip(`补偿中${count ? ` ${count}TF` : ''}`, status === 'stale' ? 'candidate' : 'stale');
    }

    function getUsDate() {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).format(new Date());
    }

    function setMarketDateFromUrl() {
      const query = new URLSearchParams(window.location.search);
      const fromUrl = query.get('market_date') || query.get('date') || '';
      if (fromUrl) {
        document.getElementById('marketDate').value = fromUrl;
        return;
      }
      document.getElementById('marketDate').value = getUsDate();
    }

    function getSelectedMarketDate() {
      return document.getElementById('marketDate')?.value || '';
    }

    function isSelectedDateToday() {
      return String(getSelectedMarketDate() || '').trim() === getUsDate();
    }

    function syncManualDailyScanButton() {
      const buttons = Array.from(document.querySelectorAll('[data-manual-daily-scan-btn]'));
      const feedback = document.getElementById('manualDailyScanFeedback');
      if (!buttons.length && !feedback) return;
      const selectedDate = String(getSelectedMarketDate() || '').trim();
      const isToday = isSelectedDateToday();
      const status = !isToday ? 'blocked' : (manualDailyScanState.running ? 'running' : manualDailyScanState.status);
      const statusMessage = isToday
        ? (manualDailyScanState.running ? '正在补跑并刷新目标池...' : manualDailyScanState.message)
        : `仅支持当前美东日期 ${getUsDate()}`;
      buttons.forEach((button) => {
        button.disabled = manualDailyScanState.running || !isToday;
        button.textContent = manualDailyScanState.running ? '补跑中' : '补跑日筛';
        button.setAttribute('aria-busy', manualDailyScanState.running ? 'true' : 'false');
        button.classList.remove('is-running', 'is-success', 'is-error', 'is-blocked');
        if (status !== 'idle') button.classList.add(`is-${status}`);
        button.title = isToday
          ? '手动补跑一次 09:20 ET 日筛，刷新今日 candidate / active。'
          : `只支持当前美东日期 ${getUsDate()}，当前选择 ${selectedDate || '--'}。`;
      });
      if (feedback) {
        const shouldShowFeedback = status !== 'idle';
        feedback.hidden = !shouldShowFeedback;
        feedback.textContent = shouldShowFeedback ? statusMessage : '';
        feedback.dataset.status = status;
      }
    }

    function getScreenerLoadKey(marketDate = getSelectedMarketDate()) {
      return `${currentEnvironment}::${String(marketDate || '').trim()}`;
    }

    function getRequestedTab() {
      const value = String(new URLSearchParams(window.location.search).get('tab') || '').trim().toLowerCase();
      if (value === 'monitor') return 'monitor';
      if (value === 'watchlist') return 'watchlist';
      if (value === 'targets') return 'targets';
      return 'screener';
    }

    function getRequestedScreenerView() {
      const value = String(new URLSearchParams(window.location.search).get('view') || '').trim().toLowerCase();
      if (value === 'universe') return 'universe';
      if (value === 'window-progress' || value === 'window_progress' || value === 'windows') return 'window-progress';
      return 'current';
    }

    function normalizeWindowProgressStatusTab(value) {
      const normalized = String(value || '').trim().toLowerCase().replace(/-/g, '_');
      if (normalized === 'all' || normalized === '') return 'all';
      if (WINDOW_PROGRESS_STATUS_TABS.some((tab) => tab.key === normalized)) return normalized;
      return 'all';
    }

    function getRequestedWindowProgressStatus() {
      return normalizeWindowProgressStatusTab(new URLSearchParams(window.location.search).get('window_status'));
    }

    function isPhoneViewport() {
      return (window.innerWidth || document.documentElement.clientWidth || 0) <= 720;
    }

    function renderMobileCardState(containerId, message) {
      const mount = document.getElementById(containerId);
      if (!mount) return;
      mount.innerHTML = `<div class="mobile-card-empty">${escapeHtml(message || '--')}</div>`;
    }

    function syncCurrentAdvancedFilters() {
      const root = document.getElementById('currentAdvancedFilters');
      const button = document.getElementById('toggleCurrentAdvancedFiltersBtn');
      if (!root || !button) return;

      root.classList.toggle('is-collapsed', !currentFiltersExpanded);
      button.setAttribute('aria-expanded', (!root.classList.contains('is-collapsed')).toString());
      button.textContent = currentFiltersExpanded ? '收起筛选' : '更多筛选';
    }

    function syncResponsiveState({ force = false } = {}) {
      const phoneViewport = isPhoneViewport();
      syncCurrentAdvancedFilters();
      if (force || lastPhoneViewport !== phoneViewport) {
        lastPhoneViewport = phoneViewport;
        renderRulesBoard();
      }
    }

    function bindResponsiveState() {
      if (responsiveStateBound) return;
      responsiveStateBound = true;
      let rafId = 0;
      const handleResize = () => {
        window.cancelAnimationFrame(rafId);
        rafId = window.requestAnimationFrame(() => syncResponsiveState());
      };
      window.addEventListener('resize', handleResize, { passive: true });
      syncResponsiveState({ force: true });
    }

    function isWatchlistRoleTab(tab = activeTab) {
      return tab === 'watchlist' || tab === 'monitor';
    }

    function getWatchlistRoleForTab(tab = activeTab) {
      return tab === 'monitor' ? 'market_monitor' : 'trade';
    }

    function getWatchlistRoleLabel(tab = activeTab) {
      return getWatchlistRoleForTab(tab) === 'market_monitor' ? '市场监控' : '标池';
    }

    function getWatchlistRoleCopy(tab = activeTab) {
      return getWatchlistRoleForTab(tab) === 'market_monitor'
        ? '市场上下文 / 只算不交易'
        : 'watchlist / global pool / manual upkeep';
    }

    function isConfigMonitorItem(item) {
      return String(item?._source || '').trim() === CONFIG_MONITOR_SOURCE;
    }

    function getConfigPageUrl() {
      return buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true, environment: currentEnvironment });
    }

    function pickScopedConfigRecord(records = []) {
      const priority = {
        [currentEnvironment]: 3,
        global: 2,
        '': 1,
      };
      return (Array.isArray(records) ? records : [])
        .slice()
        .sort((left, right) => {
          const leftEnv = String(left?.environment || '').trim().toLowerCase();
          const rightEnv = String(right?.environment || '').trim().toLowerCase();
          const priorityDiff = (priority[rightEnv] || 0) - (priority[leftEnv] || 0);
          if (priorityDiff !== 0) return priorityDiff;
          return String(right?.updated || '').localeCompare(String(left?.updated || ''));
        })[0] || null;
    }

    async function loadConfigRecordValue(key, fallbackValue = '') {
      const result = await apiFetch('config', {
        filter: `key = "${escapeFilterValue(key)}" && (environment = "${escapeFilterValue(currentEnvironment)}" || environment = "global" || environment = "")`,
        sort: '-updated',
        perPage: 20,
        page: 1
      });
      const record = pickScopedConfigRecord(Array.isArray(result?.items) ? result.items : []);
      return {
        value: record?.value ?? fallbackValue,
        record,
      };
    }

    async function loadConfiguredMarketMonitorItems(existingItems = []) {
      try {
        const { value, record } = await loadConfigRecordValue('ibkr_market_ws_symbols', '');
        const configuredSymbols = parseSymbolList(value);
        const existingMonitorSymbols = new Set(
          (Array.isArray(existingItems) ? existingItems : [])
            .filter((item) => normalizeWatchlistRole(item.symbol_role) === 'market_monitor')
            .map((item) => String(item.symbol || '').trim().toUpperCase())
            .filter(Boolean)
        );
        watchlistState.configLoadError = '';
        return configuredSymbols
          .filter((symbol) => !existingMonitorSymbols.has(symbol))
          .map((symbol) => ({
            id: `config:${record?.id || 'ibkr_market_ws_symbols'}:${symbol}`,
            symbol,
            exchange: 'CONFIG',
            industry: 'ibkr_market_ws_symbols',
            environment: String(record?.environment || 'global').trim().toLowerCase() || 'global',
            symbol_role: 'market_monitor',
            manual_member: 'config',
            note: '来自 ibkr_market_ws_symbols 配置；用于 runtime 默认市场监控，不直接交易。',
            updated: record?.updated || '',
            updated_us: record?.updated ? 'CONFIG' : '',
            us_time: record?.updated ? 'CONFIG' : '',
            _source: CONFIG_MONITOR_SOURCE,
          }));
      } catch (error) {
        console.warn('加载市场监控配置失败:', error);
        watchlistState.configLoadError = error.message || String(error);
        return [];
      }
    }

    function renderDomainTabs() {
      const items = [
        {
          tab: 'screener',
          kicker: 'Screener',
          label: '筛选',
          copy: '筛选 / 同步'
        },
        {
          tab: 'targets',
          kicker: 'Daily',
          label: '每日标的',
          copy: '维护 / 修正'
        },
        {
          tab: 'watchlist',
          kicker: 'Pool',
          label: '标池',
          copy: getWatchlistRoleCopy('watchlist')
        },
        {
          tab: 'monitor',
          kicker: 'Market',
          label: '市场监控',
          copy: getWatchlistRoleCopy('monitor')
        }
      ];
      return `
        <div class="page-bridge screener-domain-bridge">
          ${items.map((item) => `
            <button class="page-bridge-link screener-domain-tab ${item.tab === activeTab ? 'active' : ''}" type="button" data-tab="${item.tab}">
              <span class="page-bridge-kicker">${escapeHtml(item.kicker)}</span>
              <span class="page-bridge-label">${escapeHtml(item.label)}</span>
              <span class="page-bridge-copy">${escapeHtml(item.copy)}</span>
            </button>
          `).join('')}
        </div>
      `;
    }

    function renderSummaryCards(cards) {
      document.getElementById('summaryGrid').innerHTML = cards.map((item) => `
        <div class="summary-card">
          <div class="summary-label-row">
            <div class="summary-label">${escapeHtml(item.label)}</div>
            ${item.tip ? renderReadyTipButton(item.tip) : ''}
          </div>
          <div class="summary-value ${item.className || ''}">${escapeHtml(String(item.value))}</div>
          <div class="summary-copy">${escapeHtml(item.copy)}</div>
        </div>
      `).join('');
    }

    function normalizeTipList(value) {
      return Array.isArray(value) ? value.map((item) => String(item || '').trim()).filter(Boolean) : [];
    }

    function buildReadyDefinitionTip(definition) {
      const thresholds = definition?.thresholds || {};
      const requiredFlags = Number(definition?.required_aligned_flags || 2) || 2;
      const avgVolume = Number(thresholds.avg_10d_volume_gte || 500000) || 500000;
      const score = Number(thresholds.tradability_score_gte || 60) || 60;
      const freshness = Number(thresholds.freshness_lte_min || 90) || 90;
      return {
        title: definition?.title || 'READY 判定',
        summary: definition?.summary || 'READY = 可操作条件通过 + 方向一致技术条件达到阈值；等待信号触发，不代表已执行。',
        sections: [
          {
            label: '硬条件',
            items: [
              '当日 5m bar 已更新',
              `freshness <= ${freshness}m`,
              'price > 0',
              `10D 均量 >= ${formatVolume(avgVolume)}`,
              `tradability_score >= ${score}`,
              `方向一致技术条件 >= ${requiredFlags}`,
            ],
          },
          {
            label: 'Long 标签',
            items: normalizeTipList(definition?.long_flags),
          },
          {
            label: 'Short 标签',
            items: normalizeTipList(definition?.short_flags),
          },
        ],
      };
    }

    function buildReadyRowTip(row) {
      const explanation = row?.ready_explanation || {};
      const passed = normalizeTipList(explanation.passed);
      const missing = normalizeTipList(explanation.missing);
      const alignedFlags = normalizeTipList(explanation.aligned_flags);
      return {
        title: `${row?.symbol || '标的'} READY`,
        summary: explanation.summary || (row?.technical_state === 'ready'
          ? '已满足 READY 判定；等待信号触发，不代表已下单或成交。'
          : '尚未达到 READY；先处理缺失条件，再等待 5m close 刷新。'),
        tone: row?.technical_state === 'ready' ? 'good' : 'warn',
        sections: [
          { label: '已满足', items: passed },
          { label: '还缺', items: missing.length ? missing : ['当前无明显缺口'] },
          { label: '方向标签', items: alignedFlags.length ? alignedFlags : ['暂无方向一致技术标签'] },
        ],
      };
    }

    function renderReadyTipContent(tip, className = '', id = '') {
      const sections = Array.isArray(tip?.sections) ? tip.sections : [];
      return `
        <span class="ready-tip-popover ${escapeHtml(className)}" ${id ? `id="${escapeHtml(id)}"` : ''} role="tooltip">
          <span class="ready-tip-title">${escapeHtml(tip?.title || 'READY 判定')}</span>
          <span class="ready-tip-summary">${escapeHtml(tip?.summary || '')}</span>
          ${sections.map((section) => `
            <span class="ready-tip-section">
              <span class="ready-tip-section-label">${escapeHtml(section.label || '--')}</span>
              <span class="ready-tip-list">
                ${normalizeTipList(section.items).map((item) => `<span class="ready-tip-line">${escapeHtml(item)}</span>`).join('')}
              </span>
            </span>
          `).join('')}
        </span>
      `;
    }

    function renderReadyTipButton(tip) {
      const id = `ready-tip-${Math.random().toString(36).slice(2, 10)}`;
      return `
        <span class="ready-tip" data-ready-tip-id="${escapeHtml(id)}" data-ready-tip="${escapeHtml(JSON.stringify(tip || {}))}">
          <button
            class="ready-tip-button ${escapeHtml(tip?.tone || '')}"
            type="button"
            aria-label="${escapeHtml(tip?.title || 'READY tips')}"
            aria-expanded="false"
            data-ready-tip-trigger
          >i</button>
        </span>
      `;
    }

    function renderTechnicalStateWithTip(row) {
      return `
        <span class="state-chip-with-tip">
          ${statusChip(formatCurrentStateLabel(row?.technical_state), row?.technical_state || 'watch')}
          ${renderReadyTipButton(buildReadyRowTip(row))}
        </span>
      `;
    }

    function parseReadyTip(root) {
      try {
        return JSON.parse(root?.dataset.readyTip || '{}');
      } catch (_) {
        return {};
      }
    }

    function closeReadyTips() {
      window.clearTimeout(readyTipCloseTimer);
      readyTipCloseTimer = 0;
      activeReadyTipId = '';
      activeReadyTipTrigger = null;
      activeReadyTipHoverRoot = null;
      document.querySelectorAll('.ready-tip.is-open').forEach((node) => {
        node.classList.remove('is-open');
        const trigger = node.querySelector('[data-ready-tip-trigger]');
        trigger?.setAttribute('aria-expanded', 'false');
        trigger?.removeAttribute('aria-describedby');
      });
      document.getElementById('readyTipPortal')?.remove();
    }

    function scheduleReadyTipClose() {
      window.clearTimeout(readyTipCloseTimer);
      readyTipCloseTimer = window.setTimeout(() => {
        const rootHovered = Boolean(activeReadyTipHoverRoot?.matches(':hover'));
        const portalHovered = Boolean(document.getElementById('readyTipPortal')?.matches(':hover'));
        if (!rootHovered && !portalHovered) closeReadyTips();
      }, 140);
    }

    function positionReadyTipPortal(trigger, portal) {
      const rect = trigger.getBoundingClientRect();
      const gap = 8;
      const width = Math.min(340, Math.max(280, window.innerWidth - 24));
      let left = Math.min(window.innerWidth - width - 12, Math.max(12, rect.left));
      let top = rect.bottom + gap;
      portal.style.width = `${width}px`;
      portal.style.left = `${left}px`;
      portal.style.top = `${top}px`;
      portal.style.visibility = 'hidden';
      requestAnimationFrame(() => {
        const portalRect = portal.getBoundingClientRect();
        if (portalRect.bottom > window.innerHeight - 12) {
          top = Math.max(12, rect.top - portalRect.height - gap);
          portal.style.top = `${top}px`;
        }
        if (portalRect.right > window.innerWidth - 12) {
          left = Math.max(12, window.innerWidth - portalRect.width - 12);
          portal.style.left = `${left}px`;
        }
        portal.style.visibility = 'visible';
      });
    }

    function openReadyTipPortal(root, trigger) {
      const tip = parseReadyTip(root);
      document.getElementById('readyTipPortal')?.remove();
      const portal = document.createElement('div');
      portal.id = 'readyTipPortal';
      portal.className = 'ready-tip-portal';
      portal.innerHTML = renderReadyTipContent(tip, 'ready-tip-popover-portal', `${root.dataset.readyTipId || 'ready-tip'}-portal`);
      document.body.appendChild(portal);
      portal.addEventListener('mouseenter', () => {
        window.clearTimeout(readyTipCloseTimer);
      });
      portal.addEventListener('mouseleave', () => {
        if (activeReadyTipHoverRoot) scheduleReadyTipClose();
      });
      trigger.setAttribute('aria-describedby', `${root.dataset.readyTipId || 'ready-tip'}-portal`);
      positionReadyTipPortal(trigger, portal);
    }

    function openReadyTip(root, trigger, { hover = false } = {}) {
      if (!root || !trigger) return;
      const id = root.dataset.readyTipId || '';
      if (!hover) closeReadyTips();
      root.classList.add('is-open');
      trigger.setAttribute('aria-expanded', 'true');
      activeReadyTipId = id;
      activeReadyTipTrigger = trigger;
      if (hover) activeReadyTipHoverRoot = root;
      openReadyTipPortal(root, trigger);
    }

    function bindReadyTipEvents() {
      document.addEventListener('click', (event) => {
        const trigger = event.target.closest('[data-ready-tip-trigger]');
        if (!trigger) {
          if (!event.target.closest('.ready-tip')) closeReadyTips();
          return;
        }
        const root = trigger.closest('.ready-tip');
        const id = root?.dataset.readyTipId || '';
        const wasHoverOpen = root && root === activeReadyTipHoverRoot;
        const shouldOpen = wasHoverOpen || activeReadyTipId !== id || !root.classList.contains('is-open');
        closeReadyTips();
        if (!shouldOpen || !root) return;
        openReadyTip(root, trigger);
        event.preventDefault();
        event.stopPropagation();
      });
      document.addEventListener('mouseover', (event) => {
        const root = event.target.closest('.ready-tip');
        const trigger = root?.querySelector('[data-ready-tip-trigger]');
        if (!root || !trigger || activeReadyTipId) return;
        window.clearTimeout(readyTipCloseTimer);
        openReadyTip(root, trigger, { hover: true });
      });
      document.addEventListener('mouseout', (event) => {
        const root = event.target.closest('.ready-tip');
        if (!root || root !== activeReadyTipHoverRoot || root.contains(event.relatedTarget)) return;
        scheduleReadyTipClose();
      });
      document.addEventListener('focusin', (event) => {
        const trigger = event.target.closest('[data-ready-tip-trigger]');
        const root = trigger?.closest('.ready-tip');
        if (!root || !trigger || activeReadyTipId) return;
        window.clearTimeout(readyTipCloseTimer);
        openReadyTip(root, trigger, { hover: true });
      });
      document.addEventListener('focusout', (event) => {
        const root = event.target.closest('.ready-tip');
        if (!root || root !== activeReadyTipHoverRoot || root.contains(event.relatedTarget)) return;
        closeReadyTips();
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeReadyTips();
      });
      window.addEventListener('resize', () => {
        const portal = document.getElementById('readyTipPortal');
        if (portal && activeReadyTipTrigger) positionReadyTipPortal(activeReadyTipTrigger, portal);
      });
      window.addEventListener('scroll', () => {
        const portal = document.getElementById('readyTipPortal');
        if (portal && activeReadyTipTrigger) positionReadyTipPortal(activeReadyTipTrigger, portal);
      }, true);
    }

    function setHeroMetaLine(id, text = '') {
      const el = document.getElementById(id);
      if (!el) return;
      const value = String(text || '').trim();
      el.textContent = value;
      el.style.display = value ? '' : 'none';
    }

    function renderRulesCard(panelKey, panel, { actionHref = '', actionLabel = '' } = {}) {
      const chips = Array.isArray(panel?.chips) ? panel.chips : [];
      const sections = Array.isArray(panel?.sections) ? panel.sections : [];
      const footer = rulesPayload.computed_at_us ? '规则已加载' : '';
      const expanded = !isPhoneViewport() || expandedRulesPanels.has(panelKey);
      return `
        <section class="panel rules-panel" data-expanded="${expanded ? 'true' : 'false'}">
          <div class="rules-panel-head">
            <div>
              <div class="rules-panel-kicker">Rules Snapshot</div>
              <div class="rules-panel-title">${escapeHtml(panel?.title || '当前规则')}</div>
              <div class="rules-panel-subtitle">${escapeHtml(panel?.subtitle || '当前页面直接显示运行中的规则摘要。')}</div>
            </div>
            <div class="rules-panel-controls">
              ${actionHref && actionLabel ? `<a class="rules-panel-action" href="${actionHref}">${escapeHtml(actionLabel)}</a>` : ''}
              <button class="rules-panel-toggle" type="button" onclick="toggleRulesPanel('${escapeHtml(panelKey)}')" aria-expanded="${expanded ? 'true' : 'false'}">${expanded ? '收起规则' : '展开规则'}</button>
            </div>
          </div>
          ${chips.length ? `
            <div class="rules-chip-grid">
              ${chips.map((chip) => `
                <article class="rules-chip">
                  <div class="rules-chip-label">${escapeHtml(chip.label || '--')}</div>
                  <div class="rules-chip-value">${escapeHtml(String(chip.value || '--'))}</div>
                  <div class="rules-chip-copy">${escapeHtml(chip.copy || '--')}</div>
                </article>
              `).join('')}
            </div>
          ` : ''}
          <div class="rules-panel-collapsible">
            ${sections.map((section) => `
              <article class="rules-section">
                <div class="rules-section-label">${escapeHtml(section.title || '--')}</div>
                ${section.copy ? `<div class="rules-section-copy">${escapeHtml(section.copy)}</div>` : ''}
                <div class="rules-section-list">
                  ${(Array.isArray(section.lines) ? section.lines : []).map((line) => `
                    <div class="rules-line">${escapeHtml(line)}</div>
                  `).join('')}
                </div>
              </article>
            `).join('')}
            ${footer ? `<div class="rules-footer">${escapeHtml(footer)}</div>` : ''}
          </div>
        </section>
      `;
    }

    function renderRulesBoard() {
      const mount = document.getElementById('rulesBoard');
      if (!mount) return;
      const marketDate = todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate')?.value || getUsDate();
      const loadingExpanded = !isPhoneViewport();
      const cards = [];
      if (rulesPayload?.selection) {
        cards.push(renderRulesCard('selection', rulesPayload.selection, {
          actionHref: buildPageUrl('/ibkr_screener.html', {
            tab: 'screener',
            view: activeScreenerView,
            market_date: marketDate,
          }, { environment: currentEnvironment }),
          actionLabel: '当前榜单'
        }));
      }
      if (rulesPayload?.signals) {
        cards.push(renderRulesCard('signals', rulesPayload.signals, {
          actionHref: buildPageUrl('/ibkr_signals.html', {
            date: marketDate,
          }, { environment: currentEnvironment }),
          actionLabel: '回到信号列表'
        }));
      }

      if (cards.length) {
        mount.innerHTML = cards.join('');
        return;
      }

      mount.innerHTML = `
        <section class="panel rules-panel" data-expanded="${loadingExpanded ? 'true' : 'false'}">
          <div class="rules-panel-kicker">Rules Snapshot</div>
          <div class="rules-panel-title">规则摘要加载中</div>
          <div class="rules-empty">${escapeHtml(rulesLoadError || '正在读取当前 compute 逻辑与 runtime 配置。')}</div>
        </section>
      `;
    }

    window.toggleRulesPanel = function(panelKey) {
      const normalized = String(panelKey || '').trim();
      if (!normalized) return;
      if (expandedRulesPanels.has(normalized)) expandedRulesPanels.delete(normalized);
      else expandedRulesPanels.add(normalized);
      renderRulesBoard();
    };

    async function loadRulesSummary({ force = false } = {}) {
      if (!force && rulesLoaded && !rulesLoadError) {
        renderRulesBoard();
        return rulesPayload;
      }
      if (!force && rulesLoadingPromise) {
        return rulesLoadingPromise;
      }

      const promise = (async () => {
        rulesLoadError = '';
        renderRulesBoard();
        try {
          rulesPayload = await requestJson(`/api/custom/ibkr/rules${buildQuery({
            environment: currentEnvironment
          })}`);
          rulesLoaded = true;
        } catch (error) {
          console.error('loadRulesSummary failed:', error);
          rulesPayload = { selection: null, signals: null, computed_at_us: '' };
          rulesLoadError = error.message || String(error);
          rulesLoaded = false;
        }
        renderRulesBoard();
        return rulesPayload;
      })();

      if (!force) rulesLoadingPromise = promise;
      try {
        return await promise;
      } finally {
        if (rulesLoadingPromise === promise) {
          rulesLoadingPromise = null;
        }
      }
    }

    async function ensureActiveScreenerDataLoaded({ force = false } = {}) {
      if (activeScreenerView === 'universe') {
        return loadScreener(false, { loadCurrentTargetsAfter: false, force });
      }
      if (activeScreenerView === 'window-progress') {
        await Promise.all([
          loadRulesSummary(),
          loadWindowProgress(false, { force }),
        ]);
        return;
      }
      await Promise.all([
        loadRulesSummary(),
        loadTodayTargets(false),
      ]);
    }

    function renderScreenerSummary() {
      const summary = screenerPayload.summary || {};
      renderSummaryCards([
        { label: 'TOTAL', value: summary.total || 0, copy: '标的总数' },
        { label: 'LIVE BARS', value: summary.with_live_bars || 0, copy: '已有 5m bar', className: 'teal' },
        { label: 'OPERABLE', value: summary.operable || 0, copy: '可操作标的', className: 'good' },
        { label: 'CANDIDATES', value: summary.candidate_targets || 0, copy: 'candidate 数', className: 'accent' },
        { label: 'ACTIVE', value: summary.active_targets || 0, copy: 'active 数', className: 'good' },
        { label: 'DATA GAP', value: summary.incomplete_data || 0, copy: summary.incomplete_data ? '异步 API 补偿中' : '数据完整', className: summary.incomplete_data ? 'accent' : 'good' }
      ]);
    }

    function renderCurrentTargetsSummary() {
      const summary = todayTargetsPayload.summary || {};
      const readyTip = buildReadyDefinitionTip(todayTargetsPayload.workflow?.ready_definition || {});
      renderSummaryCards([
        { label: 'TOTAL', value: summary.total || 0, copy: '当日标的数' },
        { label: 'READY', value: summary.technical_ready_count || 0, copy: 'ready 标的', className: 'good', tip: readyTip },
        { label: 'SIGNALLED', value: summary.signaled_count || 0, copy: '已有信号', className: 'teal' },
        {
          label: 'NEEDS ACTION',
          value: (summary.awaiting_confirm_count || 0) + (summary.pending_count || 0),
          copy: '待处理',
          className: 'accent'
        },
        { label: 'EXECUTED', value: summary.executed_count || 0, copy: '已执行', className: 'good' },
        { label: 'STALE', value: summary.stale_count || 0, copy: '数据过期', className: 'accent' }
      ]);
    }

    function renderWindowProgressSummary() {
      const rows = getSortedWindowProgressRows();
      const summary = windowProgressPayload.summary || {};
      const counts = getWindowProgressStatusCounts(rows);
      const countByStatus = (status) => rows.filter((row) => getWindowProgressStatus(row) === status).length;
      renderSummaryCards([
        { label: 'WINDOWS', value: summary.total ?? rows.length, copy: '窗口记录数' },
        { label: 'CANDIDATE', value: summary.candidate_count ?? counts.candidate, copy: '候选信号', className: 'accent' },
        { label: 'BLOCKED', value: summary.blocked_count ?? counts.blocked, copy: '过滤阻塞', className: 'accent' },
        { label: 'NEAR EXPIRY', value: summary.near_expiry_count ?? counts.near_expiry, copy: '即将过期', className: 'teal' },
        { label: 'CONFIRMED', value: summary.confirmed_count ?? countByStatus('confirmed'), copy: '已确认', className: 'good' },
        { label: 'ACTIVE', value: summary.active_count ?? counts.active, copy: '上下轨 active', className: 'good' }
      ]);
    }

    function formatRecordEnvironment(environment) {
      const normalized = String(environment || '').trim().toLowerCase();
      if (!normalized) return 'LIVE*';
      return getEnvironmentLabel(normalized, true);
    }

    function formatWatchlistRole(role) {
      return normalizeWatchlistRole(role) === 'market_monitor' ? 'market_monitor' : 'trade';
    }

    function formatWatchlistMember(item) {
      if (isConfigMonitorItem(item)) return 'CONFIG';
      const raw = item ? item.manual_member : undefined;
      const normalized = String(raw == null ? '' : raw).trim().toLowerCase();
      if (raw === false || normalized === 'false' || normalized === '0' || normalized === 'no') {
        return 'AUTO(TARGET)';
      }
      return 'MANUAL';
    }

    function normalizeWatchlistRole(role) {
      return String(role || '').trim().toLowerCase() === 'market_monitor' ? 'market_monitor' : 'trade';
    }

    function resolveRecordEnvClass(environment) {
      const normalized = String(environment || '').trim().toLowerCase();
      if (!normalized) return 'env-live';
      return `env-${normalizeConfigEnvironment(normalized, 'global')}`;
    }

    function getFilteredWatchlistItems() {
      const keyword = String(document.getElementById('listSearchInput').value || '').trim().toUpperCase();
      const activeRole = getWatchlistRoleForTab();
      const rank = (environment) => {
        const normalized = String(environment || '').trim().toLowerCase();
        if (normalized === currentEnvironment) return 3;
        if (!normalized) return 2;
        if (normalized === 'global') return 1;
        return 0;
      };

      return watchlistState.items
        .filter((item) => normalizeWatchlistRole(item.symbol_role) === activeRole)
        .filter((item) => {
          if (!keyword) return true;
          return [
            item.symbol,
            item.exchange,
            item.industry,
            item.note,
            formatWatchlistRole(item.symbol_role),
            formatRecordEnvironment(item.environment),
            formatWatchlistMember(item),
          ].some((value) => String(value || '').toUpperCase().includes(keyword));
        })
        .sort((left, right) => {
          const rankDiff = rank(right.environment) - rank(left.environment);
          if (rankDiff !== 0) return rankDiff;
          return String(left.symbol || '').localeCompare(String(right.symbol || ''));
        });
    }

    function renderWatchlistSummary() {
      const items = getFilteredWatchlistItems();
      const currentCount = items.filter((item) => String(item.environment || '').trim().toLowerCase() === currentEnvironment).length;
      const globalCount = items.filter((item) => String(item.environment || '').trim().toLowerCase() === 'global').length;
      const legacyCount = items.filter((item) => !String(item.environment || '').trim()).length;
      const configCount = items.filter((item) => isConfigMonitorItem(item)).length;
      const roleLabel = getWatchlistRoleLabel();
      const cards = [
        { label: 'VISIBLE', value: items.length, copy: `可见${roleLabel}数` },
        { label: 'CURRENT ENV', value: currentCount, copy: '当前环境', className: 'good' },
        { label: 'GLOBAL', value: globalCount, copy: 'GLOBAL 共享', className: 'teal' },
      ];
      if (getWatchlistRoleForTab() === 'market_monitor') {
        cards.push({ label: 'CONFIG', value: configCount, copy: '配置监控', className: 'accent' });
      } else {
        cards.push({ label: 'LEGACY', value: legacyCount, copy: '旧记录', className: 'accent' });
      }
      renderSummaryCards(cards);
    }

    function getDailyTargetDate() {
      return document.getElementById('dailyTargetDate')?.value
        || dailyTargetsState.selectedDate
        || document.getElementById('marketDate')?.value
        || getUsDate();
    }

    function getManualTargetAllowedDate() {
      return runtimeCurrentMarketDate || getUsDate();
    }

    function isManualTargetDateAllowed(date = getDailyTargetDate()) {
      return String(date || '').trim() === String(getManualTargetAllowedDate() || '').trim();
    }

    function ensureManualTargetDateAllowed() {
      const selectedDate = getDailyTargetDate();
      const allowedDate = getManualTargetAllowedDate();
      if (isManualTargetDateAllowed(selectedDate)) {
        return true;
      }
      showToast(`手动加入目标池只支持当前交易日 ${allowedDate}`);
      return false;
    }

    function getFilteredDailyTargetItems() {
      const keyword = String(document.getElementById('dailyTargetTableSearchInput')?.value || '').trim().toUpperCase();
      return dailyTargetsState.items
        .filter((item) => {
          if (!keyword) return true;
          return [
            item.symbol,
            item.exchange,
            item.scan_reason,
            item.status,
            item.direction_bias
          ].some((value) => String(value || '').toUpperCase().includes(keyword));
        })
        .sort((left, right) => {
          const scoreDiff = Number(right.score || 0) - Number(left.score || 0);
          if (scoreDiff !== 0) return scoreDiff;
          return String(left.symbol || '').localeCompare(String(right.symbol || ''));
        });
    }

    function renderTargetsSummary() {
      const items = getFilteredDailyTargetItems();
      const active = items.filter((item) => String(item.status || '') === 'active').length;
      const candidate = items.filter((item) => String(item.status || '') === 'candidate').length;
      const removed = items.filter((item) => String(item.status || '') === 'removed').length;
      const avgScore = items.length
        ? (items.reduce((sum, item) => sum + Number(item.score || 0), 0) / items.length).toFixed(1)
        : '0.0';
      renderSummaryCards([
        { label: 'VISIBLE', value: items.length, copy: '可见记录' },
        { label: 'ACTIVE', value: active, copy: 'active 记录', className: 'good' },
        { label: 'CANDIDATE', value: candidate, copy: 'candidate 记录', className: 'accent' },
        { label: 'REMOVED', value: removed, copy: 'removed 记录', className: 'teal' },
        { label: 'AVG SCORE', value: avgScore, copy: '平均 score', className: 'accent' }
      ]);
    }

    function updateHero() {
      syncManualDailyScanButton();
      if (isWatchlistRoleTab()) {
        const visible = getFilteredWatchlistItems().length;
        const roleLabel = getWatchlistRoleLabel();
        const isMonitorTab = getWatchlistRoleForTab() === 'market_monitor';
        const configCount = watchlistState.items.filter((item) => isConfigMonitorItem(item)).length;
        document.getElementById('heroTitle').textContent = isMonitorTab ? '维护市场监控池。' : '维护运行标池。';
        document.getElementById('heroCopy').textContent = isMonitorTab
          ? '显示 ibkr_market_ws_symbols 配置监控 + 手动 market_monitor 记录；只监控不交易。'
          : '搜索 IBKR 合约，写入环境或 GLOBAL。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Scope', value: `${getEnvironmentLabel(currentEnvironment)} + GLOBAL` },
          { label: '角色', value: roleLabel },
          ...(isMonitorTab ? [{ label: '配置', value: `${configCount} 个` }] : []),
          { label: '可见', value: `${visible} 条` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', watchlistState.lastRefresh);
        setHeroMetaLine('selectionInfo');
        renderWatchlistSummary();
        document.getElementById('watchlistSearchPanelTitle').textContent = isMonitorTab ? '搜索可加入的市场监控标的' : '搜索可加入的标的';
        document.getElementById('watchlistSearchPanelCopy').textContent = isMonitorTab
          ? '支持 ticker 或公司名；只监控不交易。'
          : '支持 ticker 或公司名。';
        document.getElementById('watchlistListPanelTitle').textContent = isMonitorTab ? '已有市场监控记录' : '已有 watchlist 记录';
        document.getElementById('watchlistListPanelCopy').textContent = isMonitorTab
          ? '配置项来自 ibkr_market_ws_symbols，手动项来自 watchlist.market_monitor。'
          : '管理 trade watchlist 记录。';
        document.getElementById('searchMeta').textContent = isMonitorTab ? '输入后回车或点击搜索，加入市场监控。' : '输入后回车或点击搜索。';
        document.getElementById('batchMeta').textContent = isMonitorTab
          ? '批量搜索并写入 market_monitor。'
          : '批量搜索并写入当前 scope。';
        document.getElementById('deleteMeta').textContent = isMonitorTab ? '适合清理一组市场监控标的。' : '适合清理一组手工加入的标的。';
        return;
      }

      if (activeTab === 'targets') {
        const visible = getFilteredDailyTargetItems().length;
        document.getElementById('heroTitle').textContent = '维护每日标的。';
        document.getElementById('heroCopy').textContent = '搜索合约，补录、修正和清理 ibkr_targets。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Target Date', value: getDailyTargetDate() },
          { label: '当前', value: `${visible} 条` },
          { label: '搜索候选', value: `${dailyTargetsState.searchResults.length} 个` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', dailyTargetsState.lastRefresh);
        setHeroMetaLine('selectionInfo');
        renderTargetsSummary();
        return;
      }

      if (activeScreenerView === 'current') {
        const summary = todayTargetsPayload.summary || {};
        const visible = Array.isArray(filteredCurrentTargetRows) ? filteredCurrentTargetRows.length : 0;
        const visibleReady = (filteredCurrentTargetRows || []).filter((row) => row.technical_state === 'ready').length;
        const visibleActionable = (filteredCurrentTargetRows || []).filter((row) => ['awaiting_confirm', 'pending'].includes(String(row.latest_signal_status || ''))).length;
        const currentPage = Math.max(1, Number(todayTargetsPayload.page || currentTargetState.page || 1) || 1);
        const totalPages = Math.max(1, Number(todayTargetsPayload.total_pages || 1) || 1);
        const filteredTotal = Number(todayTargetsPayload.filtered_total || visible || 0) || 0;
        document.getElementById('heroTitle').textContent = '当前标的工作台。';
        document.getElementById('heroCopy').textContent = '聚合 targets、技术状态和待处理信号。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Market Date', value: todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || '--' },
          { label: '页码', value: `${currentPage}/${totalPages}` },
          { label: '可见', value: `${visible} 条` },
          { label: 'ready/action', value: `${visibleReady}/${visibleActionable}` },
          { label: '过滤', value: `${filteredTotal}/${summary.total || 0}` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', todayTargetsPayload.computed_at_us
          ? '数据已加载'
          : '数据未刷新');
        setHeroMetaLine('selectionInfo');
        renderCurrentTargetsSummary();
        return;
      }

      if (activeScreenerView === 'window-progress') {
        const rows = getSortedWindowProgressRows();
        const marketDate = windowProgressPayload.market_date || todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || '--';
        const candidateCount = rows.filter((row) => getWindowProgressStatus(row) === 'candidate').length;
        const blockedCount = rows.filter((row) => getWindowProgressStatus(row) === 'blocked').length;
        const nearExpiryCount = rows.filter((row) => getWindowProgressStatus(row) === 'near_expiry').length;
        document.getElementById('heroTitle').textContent = '窗口进度工作台。';
        document.getElementById('heroCopy').textContent = '跟踪上下轨窗口、组件收集、剩余 bars 和 trace 链路。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Market Date', value: marketDate },
          { label: '窗口', value: `${rows.length} 条` },
          { label: 'candidate', value: `${candidateCount}` },
          { label: 'blocked', value: `${blockedCount}` },
          { label: 'near expiry', value: `${nearExpiryCount}` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', windowProgressPayload.computed_at_us ? '窗口进度已加载' : '窗口进度未刷新');
        setHeroMetaLine('selectionInfo');
        renderWindowProgressSummary();
        return;
      }

      const selectedRows = (screenerPayload.items || []).filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()));
      const operableCount = selectedRows.filter((row) => row.is_operable).length;
      document.getElementById('heroTitle').textContent = '先筛选，再入池。';
      document.getElementById('heroCopy').textContent = '查看 bars、量能和方向，并维护 watchlist。';
      setPageContextMeta([
        { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
        { label: 'Market Date', value: screenerPayload.market_date || document.getElementById('marketDate').value || '--' },
        { label: '已选择', value: `${selectedRows.length || selectedSymbols.size} 个` },
        { label: '可操作', value: `${operableCount} 个` },
      ]);
      setHeroMetaLine('marketDateMeta');
      setHeroMetaLine('refreshInfo', screenerPayload.computed_at_us
        ? '数据已加载'
        : '数据未刷新');
      setHeroMetaLine('selectionInfo');
      renderScreenerSummary();
    }

    async function activateScreenerView(view, { syncHistory = true, ensureData = true, force = false } = {}) {
      activeScreenerView = ['universe', 'window-progress'].includes(view) ? view : 'current';
      document.querySelectorAll('#screenerViewTabs .subview-tab').forEach((button) => {
        button.classList.toggle('active', button.dataset.view === activeScreenerView);
      });
      document.getElementById('currentViewPanel')?.classList.toggle('active', activeScreenerView === 'current');
      document.getElementById('windowProgressViewPanel')?.classList.toggle('active', activeScreenerView === 'window-progress');
      document.getElementById('universeViewPanel')?.classList.toggle('active', activeScreenerView === 'universe');
      if (syncHistory && activeTab === 'screener') syncUrl();
      if (activeTab === 'screener') {
        updateHero();
        if (ensureData) {
          await ensureActiveScreenerDataLoaded({ force });
        }
      }
    }

    function bindScreenerViewEvents() {
      document.querySelectorAll('#screenerViewTabs .subview-tab').forEach((button) => {
        button.addEventListener('click', async () => {
          await activateScreenerView(button.dataset.view || 'current');
        });
      });
    }

    function bindWindowProgressStatusEvents() {
      document.querySelectorAll('#windowProgressStatusTabs [data-window-status]').forEach((button) => {
        button.addEventListener('click', () => {
          const nextStatus = normalizeWindowProgressStatusTab(button.dataset.windowStatus);
          if (nextStatus === activeWindowProgressStatus) return;
          activeWindowProgressStatus = nextStatus;
          renderWindowProgressTable();
          if (activeTab === 'screener' && activeScreenerView === 'window-progress') {
            syncUrl();
          }
        });
      });
    }

    function syncUrl() {
      const screenerDate = document.getElementById('marketDate').value || getDailyTargetDate() || '';
      const params = { market_date: screenerDate };
      if (activeTab === 'watchlist') params.tab = 'watchlist';
      if (activeTab === 'monitor') params.tab = 'monitor';
      if (activeTab === 'targets') {
        params.tab = 'targets';
        params.date = getDailyTargetDate();
        params.market_date = getDailyTargetDate();
      }
      if (activeTab === 'screener') {
        params.view = activeScreenerView;
        if (activeScreenerView === 'window-progress' && normalizeWindowProgressStatusTab(activeWindowProgressStatus) !== 'all') {
          params.window_status = normalizeWindowProgressStatusTab(activeWindowProgressStatus);
        }
      }
      const nextUrl = buildPageUrl('/ibkr_screener.html', params, { environment: currentEnvironment });
      if (`${location.pathname}${location.search}` !== nextUrl) {
        window.history.replaceState({}, '', nextUrl);
      }
    }

    async function activateTab(tab, { syncHistory = true, ensureScreenerData = true } = {}) {
      activeTab = ['watchlist', 'monitor', 'targets'].includes(tab) ? tab : 'screener';
      document.getElementById('pageBridge').innerHTML = renderDomainTabs();
      bindTabEvents();
      document.getElementById('screenerTab').classList.toggle('active', activeTab === 'screener');
      document.getElementById('targetsTab').classList.toggle('active', activeTab === 'targets');
      document.getElementById('watchlistTab').classList.toggle('active', isWatchlistRoleTab());
      if (syncHistory) syncUrl();
      if (activeTab === 'targets' && (!dailyTargetsState.loaded || dailyTargetsState.loadedDate !== getDailyTargetDate())) {
        await loadDailyTargets(false);
      }
      if (isWatchlistRoleTab() && (!watchlistState.loaded || watchlistState.loadedRole !== getWatchlistRoleForTab())) {
        await loadWatchlist(false);
      }
      if (activeTab === 'screener') {
        await activateScreenerView(activeScreenerView, {
          syncHistory: false,
          ensureData: ensureScreenerData,
        });
      }
      updateHero();
    }

    function bindTabEvents() {
      document.querySelectorAll('.screener-domain-tab').forEach((button) => {
        button.addEventListener('click', async () => {
          await activateTab(button.dataset.tab || 'screener');
        });
      });
    }

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
      const needsActionCount = rows.filter((row) => ['awaiting_confirm', 'pending'].includes(String(row.latest_signal_status || ''))).length;
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

    function mergeScreenerRowWithRealtimeQuote(row) {
      const quote = getRealtimeQuote(row?.symbol);
      return {
        ...row,
        display_price: quote?.last_price != null ? quote.last_price : row?.price,
        display_day_change_pct: quote?.day_change_pct != null ? quote.day_change_pct : row?.day_change_pct,
        display_price_source: quote?.last_price != null ? 'realtime_quote' : (row?.price_source || '--'),
        realtime_quote_age_s: quote?.quote_age_s != null ? quote.quote_age_s : null,
      };
    }

    async function refreshScreenerQuotes(items, loadKey) {
      if (!Array.isArray(items) || !items.length) return;
      try {
        await fetchRealtimeQuotesIfNeeded(items.map((row) => row?.symbol).filter(Boolean), {
          reset: false,
          maxAgeMs: 15000,
        });
      } catch (error) {
        console.warn('加载 screener 实时报价失败:', error);
        return;
      }
      if (screenerLoadKey !== loadKey) return;
      screenerPayload = {
        ...screenerPayload,
        items: Array.isArray(screenerPayload.items)
          ? screenerPayload.items.map((row) => mergeScreenerRowWithRealtimeQuote(row))
          : [],
      };
      applyFilters({ resetPage: false });
      if (activeTab === 'screener') updateHero();
    }

    function statusChip(label, className) {
      return `<span class="status-chip ${escapeHtml(className || '')}">${escapeHtml(label || '--')}</span>`;
    }

    function getScreenerPageSize() {
      const rawValue = Number(document.getElementById('screenerPageSize')?.value || screenerPaginationState.perPage || 10);
      if (!Number.isFinite(rawValue) || rawValue <= 0) return 10;
      return Math.max(1, Math.min(100, Math.trunc(rawValue)));
    }

    function getScreenerPageButtons(page, totalPages) {
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

    function getScreenerPageRows() {
      screenerPaginationState.perPage = getScreenerPageSize();
      const totalRows = Array.isArray(filteredRows) ? filteredRows.length : 0;
      const totalPages = Math.max(1, Math.ceil(totalRows / screenerPaginationState.perPage));
      screenerPaginationState.page = Math.max(1, Math.min(totalPages, Number(screenerPaginationState.page) || 1));
      const startIndex = (screenerPaginationState.page - 1) * screenerPaginationState.perPage;
      return filteredRows.slice(startIndex, startIndex + screenerPaginationState.perPage);
    }

    function renderScreenerPagination(pageRows) {
      screenerPaginationState.perPage = getScreenerPageSize();
      const totalRows = Array.isArray(filteredRows) ? filteredRows.length : 0;
      const totalPages = Math.max(1, Math.ceil(totalRows / screenerPaginationState.perPage));
      const page = Math.max(1, Math.min(totalPages, Number(screenerPaginationState.page) || 1));
      screenerPaginationState.page = page;
      const returnedCount = Array.isArray(pageRows) ? pageRows.length : 0;
      const pageButtons = getScreenerPageButtons(page, totalPages);
      const shouldShowPagination = totalRows > screenerPaginationState.perPage;
      const controls = [];

      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setScreenerPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>`);
      let lastPage = 0;
      pageButtons.forEach((value) => {
        if (lastPage && value - lastPage > 1) {
          controls.push('<span class="pagination-ellipsis">...</span>');
        }
        controls.push(`<button class="mini-btn pagination-btn ${value === page ? 'active' : ''}" type="button" onclick="setScreenerPage(${value})">${value}</button>`);
        lastPage = value;
      });
      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setScreenerPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`);

      ['screenerPaginationTop', 'screenerPaginationBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (!mount) return;
        const bar = mount.closest('.pagination-bar');
        if (bar) bar.hidden = !shouldShowPagination;
        mount.innerHTML = shouldShowPagination ? controls.join('') : '';
      });

      const statusText = totalRows
        ? `第 ${page} / ${totalPages} 页 · 本页 ${returnedCount} 条 · 过滤后 ${totalRows} 条`
        : '第 1 / 1 页 · 当前没有结果';
      ['screenerPaginationStatusTop', 'screenerPaginationStatusBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (mount) mount.textContent = shouldShowPagination ? statusText : '';
      });
    }

    window.setScreenerPage = function setScreenerPage(page) {
      const totalPages = Math.max(1, Math.ceil((filteredRows.length || 0) / getScreenerPageSize()));
      const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
      if (nextPage === screenerPaginationState.page) return;
      screenerPaginationState.page = nextPage;
      renderTable();
    };

    function renderTable() {
      const tbody = document.getElementById('screenerTable');
      if (!Array.isArray(filteredRows) || !filteredRows.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">当前条件下没有符合的标的</td></tr>';
        renderScreenerCards([]);
        renderScreenerPagination([]);
        document.getElementById('tableMeta').textContent = '0 条结果';
        document.getElementById('tableMetaSecondary').textContent = '';
        return;
      }

      const pageRows = getScreenerPageRows();
      tbody.innerHTML = pageRows.map((row) => {
        const checked = selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()) ? 'checked' : '';
        return `
          <tr>
            <td>
              <input class="row-check" type="checkbox" ${checked} onchange="toggleSelection('${escapeHtml(row.symbol)}', this.checked)" />
            </td>
            <td>
              <a class="symbol-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: row.symbol, interval: '5m' }, { environment: currentEnvironment })}">${escapeHtml(row.symbol)}</a><br>
              <span class="muted mono">${escapeHtml(row.display_price_source || row.price_source || '--')}</span>
            </td>
            <td>
              ${escapeHtml(row.exchange || '--')}<br>
              <span class="muted">${escapeHtml(row.industry || '--')}</span>
            </td>
            <td>
              <strong>${escapeHtml(formatPrice(row.display_price ?? row.price))}</strong><br>
              <span class="mono">${escapeHtml(formatPct(row.display_day_change_pct ?? row.day_change_pct))}</span><br>
              <span class="muted">7D ${escapeHtml(formatPct(row.change_7d))}</span>
            </td>
            <td>
              <span class="mono">ATR ${escapeHtml(formatPct(row.atr_pct))}</span><br>
              <span class="muted">10D ${escapeHtml(formatVolume(row.avg_10d_volume))}</span><br>
              <span class="muted">PRE ${escapeHtml(formatVolume(row.premarket_volume))} · DAY ${escapeHtml(formatVolume(row.today_volume))}</span>
            </td>
            <td>
              ${row.has_live_bar ? statusChip(formatFreshness(row.freshness_min), Number(row.freshness_min) <= 30 ? 'active' : 'candidate') : statusChip('无当日bar', '')}<br>
              ${dataQualityChip(row)}<br>
              <span class="muted">${escapeHtml(row.latest_us_time || '--')}</span>
            </td>
            <td>
              ${statusChip(row.target_status || 'none', row.target_status || '')}<br>
              ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}<br>
              <span class="muted">score ${escapeHtml(formatNumber(row.target_score || 0, 1))}</span>
            </td>
            <td>
              ${buildScorePill(row)}<br>
              <span class="muted">${row.is_operable ? '可操作' : '人工复核'}</span>
            </td>
            <td>
              <div class="reason-block">
                <div class="reason-label">筛选理由</div>
                <div class="reason-copy">${escapeHtml(row.scan_reason || row.note || '--')}</div>
              </div>
              <div class="reason-block" style="margin-top:10px;">
                <div class="reason-label">可操作依据</div>
                ${buildReasonPills(row)}
              </div>
              <div style="margin-top:10px;">
                <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: row.symbol, interval: '5m' }, { environment: currentEnvironment })}">Chart</a>
              </div>
            </td>
          </tr>
        `;
      }).join('');
      renderScreenerCards(pageRows);
      renderScreenerPagination(pageRows);

      document.getElementById('tableMeta').textContent = `过滤 ${filteredRows.length} · 本页 ${pageRows.length} · 可操作 ${filteredRows.filter((row) => row.is_operable).length} · live bars ${filteredRows.filter((row) => row.has_live_bar).length} · 补偿中 ${filteredRows.filter((row) => Boolean(row?.data_quality?.needs_repair)).length}`;
      document.getElementById('tableMetaSecondary').textContent = `已选 ${filteredRows.filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase())).length} 条`;
    }

    function applyFilters({ resetPage = true } = {}) {
      if (resetPage) screenerPaginationState.page = 1;
      const filters = getRowFilters();
      const rows = (screenerPayload.items || []).filter((row) => {
        if (filters.symbol_search) {
          const haystack = `${row.symbol || ''} ${row.exchange || ''} ${row.industry || ''}`.toUpperCase();
          if (!haystack.includes(filters.symbol_search)) return false;
        }
        if (filters.exchange && row.exchange !== filters.exchange) return false;
        if (filters.industry && row.industry !== filters.industry) return false;
        if (filters.target_status && row.target_status !== filters.target_status) return false;
        if (filters.direction && row.direction_bias !== filters.direction) return false;
        if (!inRange(row.display_price ?? row.price, filters.price_min, filters.price_max)) {
          if (Number.isFinite(filters.price_min) || Number.isFinite(filters.price_max)) return false;
        }
        if (!inRange(row.display_day_change_pct ?? row.day_change_pct, filters.day_change_min, filters.day_change_max)) {
          if (Number.isFinite(filters.day_change_min) || Number.isFinite(filters.day_change_max)) return false;
        }
        if (Number.isFinite(filters.atr_pct_min) && Number(row.atr_pct || 0) < filters.atr_pct_min) return false;
        if (Number.isFinite(filters.avg_volume_min) && Number(row.avg_10d_volume || 0) < filters.avg_volume_min) return false;
        if (Number.isFinite(filters.premarket_volume_min) && Number(row.premarket_volume || 0) < filters.premarket_volume_min) return false;
        if (Number.isFinite(filters.target_score_min) && Number(row.target_score || 0) < filters.target_score_min) return false;
        if (Number.isFinite(filters.freshness_max)) {
          if (!Number.isFinite(Number(row.freshness_min)) || Number(row.freshness_min) > filters.freshness_max) return false;
        }
        if (filters.operable_only && !row.is_operable) return false;
        return true;
      });

      filteredRows = sortRows(rows, filters.sort_by);
      renderTable();
      updateHero();
    }

    function updateSelectionInfo() {
      if (activeTab === 'screener') updateHero();
    }

    window.toggleSelection = function(symbol, checked) {
      const normalized = String(symbol || '').trim().toUpperCase();
      if (!normalized) return;
      if (checked) selectedSymbols.add(normalized);
      else selectedSymbols.delete(normalized);
      updateSelectionInfo();
      renderTable();
    };

    window.selectVisibleRows = function() {
      filteredRows.forEach((row) => selectedSymbols.add(String(row.symbol || '').trim().toUpperCase()));
      renderTable();
      updateSelectionInfo();
      showToast(`已选择 ${filteredRows.length} 个过滤结果标的`);
    };

    window.clearSelection = function() {
      selectedSymbols.clear();
      renderTable();
      updateSelectionInfo();
      showToast('选择已清空');
    };

    window.copyVisibleSymbols = async function() {
      const text = filteredRows.map((row) => row.symbol).filter(Boolean).join(',');
      if (!text) {
        showToast('当前没有可复制的标的');
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        showToast(`已复制 ${filteredRows.length} 个过滤结果 symbols`);
      } catch (_) {
        showToast('复制失败，请检查浏览器权限');
      }
    };

    window.pushSelectedTargets = async function() {
      const marketDate = document.getElementById('marketDate').value || screenerPayload.market_date || '';
      const items = (screenerPayload.items || []).filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()));
      if (!items.length) {
        showToast('请先选择要写入的标的');
        return;
      }
      try {
        showLoading('正在写入今日 targets...');
        const payload = await requestJson('/api/custom/ibkr/screener/targets', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            market_date: marketDate,
            items
          }
        });
        showToast(`写入完成: created ${payload.created || 0}, updated ${payload.updated || 0}, skipped ${payload.skipped || 0}`);
        await loadScreener(false, { force: true });
      } catch (error) {
        showToast(`写入失败: ${error.message || error}`);
      } finally {
        hideLoading();
      }
    };

    function summarizeManualDailyScanResult(payload) {
      const schedulerResult = payload?.scheduler_result && typeof payload.scheduler_result === 'object'
        ? payload.scheduler_result
        : {};
      const scanResult = payload?.scan_result && typeof payload.scan_result === 'object'
        ? payload.scan_result
        : (schedulerResult.payload && typeof schedulerResult.payload === 'object' ? schedulerResult.payload : {});
      if (schedulerResult.skipped) {
        return `补跑跳过: ${schedulerResult.reason || payload?.error || 'scheduler skipped'}`;
      }
      const active = Number(scanResult.active || 0) || 0;
      const candidates = Number(scanResult.candidates || 0) || 0;
      const removed = Number(scanResult.removed || 0) || 0;
      const errors = Number(scanResult.errors || 0) || 0;
      return `补跑完成: active ${active}, candidate ${candidates}, removed ${removed}, errors ${errors}`;
    }

    function didManualDailyScanSucceed(payload) {
      const schedulerResult = payload?.scheduler_result && typeof payload.scheduler_result === 'object'
        ? payload.scheduler_result
        : {};
      const scanResult = payload?.scan_result && typeof payload.scan_result === 'object'
        ? payload.scan_result
        : (schedulerResult.payload && typeof schedulerResult.payload === 'object' ? schedulerResult.payload : {});
      if (schedulerResult.skipped) return false;
      const errors = Number(scanResult.errors || 0) || 0;
      return !errors;
    }

    function confirmManualDailyScan() {
      return new Promise((resolve) => {
        const existing = document.getElementById('manualDailyScanConfirmOverlay');
        if (existing) {
          resolve(false);
          return;
        }

        const overlay = document.createElement('div');
        overlay.id = 'manualDailyScanConfirmOverlay';
        overlay.className = 'manual-daily-scan-confirm-overlay';
        overlay.setAttribute('role', 'presentation');
        overlay.innerHTML = `
          <div class="manual-daily-scan-confirm" role="dialog" aria-modal="true" aria-labelledby="manualDailyScanConfirmTitle" aria-describedby="manualDailyScanConfirmCopy">
            <div class="manual-daily-scan-confirm-kicker">Manual Daily Scan</div>
            <div id="manualDailyScanConfirmTitle" class="manual-daily-scan-confirm-title">确认补跑今日日筛</div>
            <div id="manualDailyScanConfirmCopy" class="manual-daily-scan-confirm-copy">
              <div>这会重新计算今日 <code>candidate / active</code>。</div>
              <div>手动加入的标的会保留。</div>
              <div>不会直接下单，也不会触发信号确认。</div>
            </div>
            <div class="manual-daily-scan-confirm-actions">
              <button type="button" class="mini-btn" data-confirm-action="cancel">取消</button>
              <button type="button" class="mini-btn scan-rerun-btn" data-confirm-action="confirm">确认补跑</button>
            </div>
          </div>
        `;

        const cleanup = (value) => {
          document.removeEventListener('keydown', handleKeydown);
          overlay.remove();
          resolve(value);
        };
        const handleKeydown = (event) => {
          if (event.key === 'Escape') cleanup(false);
        };

        overlay.addEventListener('click', (event) => {
          if (event.target === overlay) cleanup(false);
        });
        overlay.querySelector('[data-confirm-action="cancel"]')?.addEventListener('click', () => cleanup(false));
        overlay.querySelector('[data-confirm-action="confirm"]')?.addEventListener('click', () => cleanup(true));
        document.addEventListener('keydown', handleKeydown);
        document.body.appendChild(overlay);
        window.requestAnimationFrame(() => {
          overlay.classList.add('show');
          overlay.querySelector('[data-confirm-action="confirm"]')?.focus();
        });
      });
    }

    window.rerunTodayDailyScan = async function() {
      if (manualDailyScanState.running) return;
      if (!isSelectedDateToday()) {
        manualDailyScanState.status = 'error';
        manualDailyScanState.message = `补跑失败: 只支持当前美东日期 ${getUsDate()}`;
        showToast(`只支持补跑当前美东日期 ${getUsDate()}`);
        syncManualDailyScanButton();
        return;
      }
      const confirmed = await confirmManualDailyScan();
      if (!confirmed) return;

      manualDailyScanState.running = true;
      manualDailyScanState.status = 'running';
      manualDailyScanState.message = '正在补跑并刷新目标池...';
      syncManualDailyScanButton();
      try {
        showLoading('正在补跑今日日筛...');
        const payload = await requestJson('/api/custom/system/scheduler/jobs/run', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            job_id: 'ibkr_scan_runtime',
            trigger_source: 'console_manual_daily_scan'
          }
        });
        manualDailyScanState.lastResult = payload;
        const resultMessage = summarizeManualDailyScanResult(payload);
        const succeeded = didManualDailyScanSucceed(payload);
        const resultDetail = resultMessage.replace(/^补跑(?:完成|跳过): /, '');
        manualDailyScanState.status = succeeded ? 'success' : 'error';
        manualDailyScanState.message = succeeded
          ? `补跑成功: ${resultDetail}`
          : `补跑未成功: ${resultDetail}`;
        showToast(manualDailyScanState.message);
        currentTargetState.page = 1;
        screenerLoadKey = '';
        windowProgressState.loadedKey = '';
        await loadScreener(false, { force: true });
        await loadDailyTargets(false);
      } catch (error) {
        const errorMessage = `补跑失败: ${error.message || error}`;
        manualDailyScanState.status = 'error';
        manualDailyScanState.message = errorMessage;
        showToast(errorMessage);
      } finally {
        manualDailyScanState.running = false;
        syncManualDailyScanButton();
        hideLoading();
      }
    };

    async function loadScreener(showToastOnSuccess = false, { loadCurrentTargetsAfter = true, force = false } = {}) {
      if (!initAuth()) return;
      const marketDate = document.getElementById('marketDate').value || '';
      const loadKey = getScreenerLoadKey(marketDate);
      const nextParams = { market_date: marketDate };
      if (activeTab === 'screener') {
        nextParams.view = activeScreenerView;
      } else if (activeTab === 'watchlist') {
        nextParams.tab = 'watchlist';
      } else if (activeTab === 'monitor') {
        nextParams.tab = 'monitor';
      } else if (activeTab === 'targets') {
        const targetDate = getDailyTargetDate() || marketDate;
        nextParams.tab = 'targets';
        nextParams.date = targetDate;
        nextParams.market_date = targetDate;
      }
      const nextUrl = buildPageUrl('/ibkr_screener.html', nextParams, { environment: currentEnvironment });
      if (`${location.pathname}${location.search}` !== nextUrl) {
        window.history.replaceState({}, '', nextUrl);
      }

      if (!force && screenerLoadKey === loadKey) {
        populateSelect('exchangeFilter', screenerPayload.filters && screenerPayload.filters.exchanges);
        populateSelect('industryFilter', screenerPayload.filters && screenerPayload.filters.industries);
        populateSelect('targetStatusFilter', screenerPayload.filters && screenerPayload.filters.target_statuses);
        populateSelect('directionFilter', screenerPayload.filters && screenerPayload.filters.direction_biases);
        applyFilters();
        renderRulesBoard();
        if (loadCurrentTargetsAfter) {
          await loadTodayTargets(false);
        }
        setPageRefreshTime();
        if (activeTab === 'screener') updateHero();
        if (showToastOnSuccess) showToast('筛选器已刷新');
        return screenerPayload;
      }

      try {
        showLoading('正在聚合筛选器数据...');
        const rulesPromise = loadRulesSummary();
        const payload = await requestJson(`/api/custom/ibkr/screener${buildQuery({
          environment: currentEnvironment,
          market_date: marketDate
        })}`);
        const items = Array.isArray(payload?.items) ? payload.items : [];
        screenerPayload = {
          ...(payload || { items: [], summary: {}, filters: {} }),
          items: items.map((row) => mergeScreenerRowWithRealtimeQuote(row)),
        };
        screenerLoadKey = loadKey;
        populateSelect('exchangeFilter', payload.filters && payload.filters.exchanges);
        populateSelect('industryFilter', payload.filters && payload.filters.industries);
        populateSelect('targetStatusFilter', payload.filters && payload.filters.target_statuses);
        populateSelect('directionFilter', payload.filters && payload.filters.direction_biases);
        applyFilters();
        await rulesPromise;
        if (loadCurrentTargetsAfter) {
          await loadTodayTargets(false);
        }
        renderRulesBoard();
        setPageRefreshTime();
        if (activeTab === 'screener') updateHero();
        void refreshScreenerQuotes(items, loadKey);
        if (showToastOnSuccess) showToast('筛选器已刷新');
        return screenerPayload;
      } catch (error) {
        console.error('loadScreener failed:', error);
        screenerLoadKey = '';
        document.getElementById('refreshInfo').textContent = '加载失败';
        document.getElementById('screenerTable').innerHTML = `<tr><td colspan="9" class="empty">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('screenerCards', error.message || error);
        showToast(`加载失败: ${error.message || error}`);
      } finally {
        hideLoading();
      }
    }

    function bindFilterEvents() {
      const ids = [
        'symbolSearch',
        'exchangeFilter',
        'industryFilter',
        'targetStatusFilter',
        'directionFilter',
        'priceMin',
        'priceMax',
        'dayChangeMin',
        'dayChangeMax',
        'atrPctMin',
        'avgVolumeMin',
        'premarketVolumeMin',
        'targetScoreMin',
        'freshnessMax',
        'sortBy',
        'screenerPageSize',
        'operableOnly'
      ];
      ids.forEach((id) => {
        const element = document.getElementById(id);
        if (!element) return;
        const eventName = element.tagName === 'INPUT' && element.type === 'text' ? 'input' : 'change';
        element.addEventListener(eventName, applyFilters);
      });
      document.getElementById('marketDate').addEventListener('change', async () => {
        const nextDate = document.getElementById('marketDate').value || getUsDate();
        currentTargetState.page = 1;
        dailyTargetsState.selectedDate = nextDate;
        screenerLoadKey = '';
        windowProgressState.loadedKey = '';
        if (document.getElementById('dailyTargetDate')) {
          document.getElementById('dailyTargetDate').value = nextDate;
        }
        syncManualDailyScanButton();
        syncUrl();
        if (activeTab === 'screener') {
          await ensureActiveScreenerDataLoaded({ force: true });
          return;
        }
        if (activeTab === 'targets') {
          await loadDailyTargets(false);
          return;
        }
        updateHero();
      });
    }

    function bindCurrentTargetFilterEvents() {
      const immediateIds = [
        'currentTechnicalStateFilter',
        'currentSignalStateFilter',
        'currentTargetStatusFilter',
        'currentDirectionBiasFilter',
        'currentTargetSortBy',
        'currentTargetPageSize',
        'currentReadyOnly',
        'currentSignaledOnly',
      ];
      immediateIds.forEach((id) => {
        const element = document.getElementById(id);
        if (!element) return;
        element.addEventListener('change', () => applyCurrentTargetFilters({ resetPage: true }));
      });
      const searchInput = document.getElementById('currentTargetSearch');
      if (searchInput) {
        searchInput.addEventListener('input', scheduleCurrentTargetReload);
        searchInput.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter') return;
          window.clearTimeout(currentTargetState.searchDebounceId);
          applyCurrentTargetFilters({ resetPage: true });
        });
      }
      document.getElementById('openUniverseViewBtn')?.addEventListener('click', async () => {
        await activateScreenerView('universe');
      });
      document.getElementById('refreshWindowProgressBtn')?.addEventListener('click', () => {
        loadWindowProgress(true, { force: true });
      });
      document.getElementById('toggleCurrentAdvancedFiltersBtn')?.addEventListener('click', () => {
        currentFiltersExpanded = !currentFiltersExpanded;
        syncCurrentAdvancedFilters();
      });
      syncCurrentAdvancedFilters();
      syncManualDailyScanButton();
    }

    function parseSymbolList(rawValue) {
      const seen = new Set();
      return String(rawValue || '')
        .split(/[\s,;，；]+/)
        .map((value) => String(value || '').trim().toUpperCase())
        .filter((value) => {
          if (!value || seen.has(value)) return false;
          seen.add(value);
          return true;
        });
    }

    function findExistingBySymbol(symbol) {
      const normalized = String(symbol || '').trim().toUpperCase();
      return watchlistState.items.filter((item) => String(item.symbol || '').trim().toUpperCase() === normalized);
    }

    function formatExistingWatchlistScopes(items) {
      const seen = new Set();
      return (Array.isArray(items) ? items : [])
        .map((item) => `${formatRecordEnvironment(item.environment)} ${formatWatchlistRole(item.symbol_role)} ${formatWatchlistMember(item)}`)
        .filter((label) => {
          if (!label || seen.has(label)) return false;
          seen.add(label);
          return true;
        })
        .join(' / ');
    }

    function pickBestCandidate(items, symbol) {
      const normalized = String(symbol || '').trim().toUpperCase();
      const list = Array.isArray(items) ? items.slice() : [];
      const exchangeRank = (exchange) => {
        const normalizedExchange = String(exchange || '').trim().toUpperCase();
        if (normalizedExchange === 'NASDAQ' || normalizedExchange === 'NYSE') return 2;
        if (['AMEX', 'ARCA', 'BATS', 'SMART'].includes(normalizedExchange)) return 1;
        return 0;
      };
      list.sort((left, right) => {
        const leftExact = String(left.symbol || '').trim().toUpperCase() === normalized ? 1 : 0;
        const rightExact = String(right.symbol || '').trim().toUpperCase() === normalized ? 1 : 0;
        if (rightExact !== leftExact) return rightExact - leftExact;
        const leftUs = left.is_us ? 1 : 0;
        const rightUs = right.is_us ? 1 : 0;
        if (rightUs !== leftUs) return rightUs - leftUs;
        const exchangeDiff = exchangeRank(right.exchange) - exchangeRank(left.exchange);
        if (exchangeDiff !== 0) return exchangeDiff;
        return Number(right.score || 0) - Number(left.score || 0);
      });
      return list[0] || null;
    }

    async function searchBestContract(symbol) {
      const payload = await requestJson(`/api/custom/ibkr/contracts/search${buildQuery({
        environment: currentEnvironment,
        q: symbol,
        limit: 6
      })}`);
      return pickBestCandidate(payload.items, symbol);
    }

    function buildWatchlistBody(item, scope, noteOverride) {
      const secTypes = Array.isArray(item.sec_types) ? item.sec_types : [];
      return {
        environment: currentEnvironment,
        source: 'manual_page_add',
        scope,
        manual_member: true,
        symbol_role: getWatchlistRoleForTab(),
        symbol: item.symbol,
        exchange: item.exchange || '',
        industry: item.industry || item.asset_class || (secTypes.length ? secTypes.join('/') : ''),
        asset_class: item.asset_class || '',
        sec_types: secTypes,
        description: item.description || '',
        note: noteOverride != null ? String(noteOverride || '').trim() : String(item.note || '').trim()
      };
    }

    function findExistingDailyTarget(symbol) {
      const normalized = String(symbol || '').trim().toUpperCase();
      return dailyTargetsState.items.find((item) => String(item.symbol || '').trim().toUpperCase() === normalized) || null;
    }

    function buildDailyTargetBody(item, draft) {
      return {
        environment: currentEnvironment,
        source: 'manual_page_add',
        symbol: item.symbol,
        exchange: item.exchange || '',
        date: draft.date,
        direction_bias: draft.direction_bias,
        score: draft.score,
        scan_reason: draft.scan_reason,
        status: draft.status,
        extra: {
          source: 'screener_targets_tab',
          conid: item.conid || 0,
          company_name: item.company_name || '',
          description: item.description || '',
          asset_class: item.asset_class || '',
          sec_types: Array.isArray(item.sec_types) ? item.sec_types : [],
          is_us: Boolean(item.is_us)
        }
      };
    }

    function getDailyTargetDraft() {
      const scoreValue = Number(document.getElementById('dailyTargetScoreInput').value || 0);
      return {
        date: getDailyTargetDate(),
        status: document.getElementById('dailyTargetStatusInput').value || 'candidate',
        direction_bias: document.getElementById('dailyTargetDirectionInput').value || 'neutral',
        score: Number.isFinite(scoreValue) ? scoreValue : 0,
        scan_reason: String(document.getElementById('dailyTargetReasonInput').value || '').trim()
      };
    }

    function renderDailyTargetSearchResults() {
      const mount = document.getElementById('dailyTargetSearchResults');
      const draft = getDailyTargetDraft();
      const manualDateAllowed = isManualTargetDateAllowed(draft.date);

      if (!dailyTargetsState.searchResults.length) {
        mount.innerHTML = '<div class="empty-state">暂无搜索结果。</div>';
        return;
      }

      mount.innerHTML = dailyTargetsState.searchResults.map((item, index) => {
        const secTypes = Array.isArray(item.sec_types) ? item.sec_types : [];
        const description = item.description || item.company_name || '无描述';
        const existing = findExistingDailyTarget(item.symbol);
        return `
          <article class="result-card">
            <div class="card-top">
              <span class="symbol-chip">${escapeHtml(item.symbol || '--')}</span>
              <span class="pool-pill">${escapeHtml(item.exchange || 'SMART')}</span>
              <span class="status-chip ${escapeHtml(draft.status)}">${escapeHtml(draft.status)}</span>
              ${existing ? `<span class="pool-pill exists">已存在 ${escapeHtml(existing.status || 'candidate')}</span>` : ''}
            </div>
            <div>
              <div class="card-title">${escapeHtml(item.company_name || item.symbol || '--')}</div>
              <div class="card-copy">${escapeHtml(description)}</div>
            </div>
            <div class="pill-row">
              <span class="status-chip">date ${escapeHtml(draft.date)}</span>
              <span class="status-chip ${escapeHtml(draft.direction_bias)}">${escapeHtml(draft.direction_bias)}</span>
              <span class="status-chip">score ${escapeHtml(String(draft.score))}</span>
              ${(secTypes.length ? secTypes : [item.asset_class || 'UNKNOWN']).map((type) => `<span class="pool-pill">${escapeHtml(type)}</span>`).join('')}
            </div>
            <div class="card-bottom">
              <button class="btn primary" type="button" onclick="addDailyTargetCandidate(${index})" ${manualDateAllowed ? '' : 'disabled'}>${manualDateAllowed ? '加入目标池' : '仅支持当前交易日'}</button>
              <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">查看图表</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function renderDailyTargetRows() {
      const items = getFilteredDailyTargetItems();
      const table = document.getElementById('dailyTargetsTable');
      if (!items.length) {
        table.innerHTML = '<tr><td colspan="9" class="empty-state">当前日期没有目标池记录。</td></tr>';
        renderDailyTargetCards([]);
        if (activeTab === 'targets') updateHero();
        return;
      }

      table.innerHTML = items.map((item) => `
        <tr>
          <td>
            <div class="meta-stack">
              <div class="table-symbol">${escapeHtml(item.symbol || '--')}</div>
              <small>${escapeHtml(item.id || '')}</small>
            </div>
          </td>
          <td>${escapeHtml(item.exchange || '--')}</td>
          <td>${escapeHtml(item.date || '--')}</td>
          <td><span class="status-chip ${escapeHtml(item.direction_bias || 'neutral')}">${escapeHtml(item.direction_bias || 'neutral')}</span></td>
          <td>${escapeHtml(Number(item.score || 0).toFixed(1))}</td>
          <td><span class="status-chip ${escapeHtml(item.status || 'candidate')}">${escapeHtml(item.status || 'candidate')}</span></td>
          <td>${escapeHtml(item.scan_reason || '--')}</td>
          <td>
            <div class="meta-stack">
              <span>${escapeHtml(item.us_time || '--')}</span>
              <small>${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</small>
            </div>
          </td>
          <td>
            <div class="row-actions">
              <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">Chart</a>
              <button class="mini-btn" type="button" onclick="editDailyTargetItem('${escapeHtml(item.id || '')}')">编辑</button>
              <button class="mini-btn danger" type="button" onclick="removeDailyTargetItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}')">删除</button>
            </div>
          </td>
        </tr>
      `).join('');
      renderDailyTargetCards(items);

      if (activeTab === 'targets') updateHero();
    }

    async function searchDailyTargetContracts() {
      const keyword = String(document.getElementById('dailyTargetSearchInput').value || '').trim();
      if (!keyword) {
        showToast('请输入 ticker 或公司名');
        return;
      }

      document.getElementById('dailyTargetSearchMeta').textContent = `正在查询 ${keyword} ...`;
      document.getElementById('dailyTargetSearchResults').innerHTML = '<div class="empty-state">搜索中...</div>';
      try {
        const payload = await requestJson(`/api/custom/ibkr/contracts/search${buildQuery({
          environment: currentEnvironment,
          q: keyword,
          limit: 12
        })}`);
        dailyTargetsState.searchResults = Array.isArray(payload.items) ? payload.items : [];
        document.getElementById('dailyTargetSearchMeta').textContent = `IBKR 返回 ${dailyTargetsState.searchResults.length} 个候选`;
        renderDailyTargetSearchResults();
      } catch (error) {
        dailyTargetsState.searchResults = [];
        document.getElementById('dailyTargetSearchMeta').textContent = `搜索失败: ${error.message || error}`;
        document.getElementById('dailyTargetSearchResults').innerHTML = `<div class="empty-state">${escapeHtml(error.message || error)}</div>`;
      }
    }

    async function loadDailyTargets(showToastOnSuccess = false, { refreshCurrentTargets = false } = {}) {
      dailyTargetsState.selectedDate = getDailyTargetDate();
      document.getElementById('dailyTargetDate').value = dailyTargetsState.selectedDate;

      try {
        const response = await apiFetch('ibkr_targets', {
          filter: `environment = "${escapeFilterValue(currentEnvironment)}" && date = "${escapeFilterValue(dailyTargetsState.selectedDate)}"`,
          sort: '-updated',
          perPage: 200
        });
        dailyTargetsState.items = Array.isArray(response.items) ? response.items : [];
        dailyTargetsState.loaded = true;
        dailyTargetsState.loadedDate = dailyTargetsState.selectedDate;
        dailyTargetsState.lastRefresh = 'ibkr_targets 已加载';
        setPageRefreshTime();
        document.getElementById('dailyTargetListMeta').textContent = `${getEnvironmentLabel(currentEnvironment)} / ${dailyTargetsState.selectedDate} / ${dailyTargetsState.items.length} 条`;
        renderDailyTargetRows();
        if (refreshCurrentTargets) {
          await loadTodayTargets(false);
        }
        syncUrl();
        if (showToastOnSuccess) showToast('ibkr_targets 已刷新');
      } catch (error) {
        dailyTargetsState.items = [];
        dailyTargetsState.loaded = false;
        dailyTargetsState.loadedDate = '';
        dailyTargetsState.lastRefresh = 'ibkr_targets 加载失败';
        document.getElementById('dailyTargetsTable').innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('dailyTargetsCards', error.message || error);
        document.getElementById('dailyTargetListMeta').textContent = `加载失败: ${error.message || error}`;
        if (activeTab === 'targets') updateHero();
      }
    }

    async function addDailyTargetCandidate(index) {
      const item = dailyTargetsState.searchResults[index];
      if (!item) return;
      if (!ensureManualTargetDateAllowed()) return;

      const draft = getDailyTargetDraft();
      try {
        const payload = await requestJson('/api/custom/ibkr/targets/upsert', {
          method: 'POST',
          body: buildDailyTargetBody(item, draft)
        });
        showToast(`${item.symbol} 已加入 ${draft.date} 目标池`);
        const warning = getRuntimeWarning(payload) || getWatchlistSyncWarning(payload);
        if (warning) showToast(`预热提示: ${warning}`);
        await loadDailyTargets(false);
      } catch (error) {
        showToast(`加入失败: ${error.message || error}`);
      }
    }

    async function editDailyTargetItem(recordId) {
      const item = dailyTargetsState.items.find((row) => String(row.id || '') === String(recordId || ''));
      if (!item) {
        showToast('记录不存在');
        return;
      }

      const nextStatus = window.prompt('修改 status(candidate/active/removed)', item.status || 'candidate');
      if (nextStatus === null) return;
      if (!['candidate', 'active', 'removed'].includes(String(nextStatus || '').trim())) {
        showToast('status 无效');
        return;
      }
      const nextDirection = window.prompt('修改 direction_bias(long/short/neutral)', item.direction_bias || 'neutral');
      if (nextDirection === null) return;
      if (!['long', 'short', 'neutral'].includes(String(nextDirection || '').trim())) {
        showToast('direction_bias 无效');
        return;
      }
      const nextScoreText = window.prompt('修改 score', String(item.score ?? 0));
      if (nextScoreText === null) return;
      const nextScore = Number(nextScoreText);
      if (!Number.isFinite(nextScore)) {
        showToast('score 无效');
        return;
      }
      const nextReason = window.prompt('修改 scan_reason', item.scan_reason || '');
      if (nextReason === null) return;

      try {
        const payload = await requestJson('/api/custom/ibkr/targets/upsert', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_edit',
            symbol: item.symbol,
            exchange: item.exchange || '',
            date: item.date || dailyTargetsState.selectedDate,
            direction_bias: String(nextDirection || '').trim(),
            score: nextScore,
            scan_reason: String(nextReason || '').trim(),
            status: String(nextStatus || '').trim(),
            extra: item.extra && typeof item.extra === 'object' ? item.extra : {}
          }
        });
        showToast(`${item.symbol} 已更新`);
        const warning = getRuntimeWarning(payload) || getWatchlistSyncWarning(payload);
        if (warning) showToast(`联动提示: ${warning}`);
        await loadDailyTargets(false);
      } catch (error) {
        showToast(`更新失败: ${error.message || error}`);
      }
    }

    async function removeDailyTargetItem(recordId, symbol) {
      if (!recordId) return;
      if (!window.confirm(`确认删除 ${symbol} 的目标池记录？`)) return;
      try {
        const payload = await requestJson('/api/custom/ibkr/targets/remove', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_remove',
            record_id: recordId,
            symbol
          }
        });
        showToast(`${symbol} 已删除`);
        const warning = getRuntimeWarning(payload) || getWatchlistSyncWarning(payload);
        if (warning) showToast(`清理提示: ${warning}`);
        await loadDailyTargets(false);
        await loadWatchlist(false);
      } catch (error) {
        showToast(`删除失败: ${error.message || error}`);
      }
    }

    async function batchAddDailyTargetSymbols() {
      const symbols = parseSymbolList(document.getElementById('dailyTargetBatchSymbolsInput').value);
      const draft = getDailyTargetDraft();
      if (!ensureManualTargetDateAllowed()) return;
      if (!symbols.length) {
        showToast('先输入要批量加入的 symbols');
        return;
      }

      let success = 0;
      let failed = 0;
      for (let index = 0; index < symbols.length; index += 1) {
        const symbol = symbols[index];
        document.getElementById('dailyTargetBatchMeta').textContent = `处理中 ${index + 1}/${symbols.length}: ${symbol}`;
        try {
          const candidate = await searchBestContract(symbol);
          if (!candidate) throw new Error('IBKR 未返回候选');
          await requestJson('/api/custom/ibkr/targets/upsert', {
            method: 'POST',
            body: buildDailyTargetBody(candidate, draft)
          });
          success += 1;
        } catch (_) {
          failed += 1;
        }
      }

      document.getElementById('dailyTargetBatchMeta').textContent = `批量导入完成: 成功 ${success} / 失败 ${failed}`;
      showToast(`批量导入完成: ${success}/${symbols.length}`);
      await loadDailyTargets(false);
    }

    async function batchDeleteDailyTargetSymbols() {
      const symbols = parseSymbolList(document.getElementById('dailyTargetBatchDeleteInput').value);
      if (!symbols.length) {
        showToast('先输入要删除的 symbols');
        return;
      }

      const targets = dailyTargetsState.items.filter((item) => symbols.includes(String(item.symbol || '').trim().toUpperCase()));
      if (!targets.length) {
        showToast('当前日期下没有匹配记录');
        return;
      }
      if (!window.confirm(`确认删除 ${targets.length} 条目标池记录？`)) return;

      let deleted = 0;
      for (const item of targets) {
        try {
          await requestJson('/api/custom/ibkr/targets/remove', {
            method: 'POST',
            body: {
              environment: currentEnvironment,
              source: 'manual_page_remove',
              record_id: item.id,
              symbol: item.symbol || ''
            }
          });
          deleted += 1;
        } catch (_) {
          // ignore batch delete failures and continue
        }
      }

      document.getElementById('dailyTargetDeleteMeta').textContent = `批量删除完成: 删除 ${deleted} / 匹配 ${targets.length}`;
      showToast(`批量删除完成: ${deleted}/${targets.length}`);
      await loadDailyTargets(false);
      await loadWatchlist(false);
    }

    function attachDailyTargetEvents() {
      document.getElementById('dailyTargetSearchBtn').addEventListener('click', searchDailyTargetContracts);
      document.getElementById('dailyTargetSearchInput').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') searchDailyTargetContracts();
      });
      document.getElementById('dailyTargetBatchAddBtn').addEventListener('click', batchAddDailyTargetSymbols);
      document.getElementById('dailyTargetBatchClearBtn').addEventListener('click', () => {
        document.getElementById('dailyTargetBatchSymbolsInput').value = '';
        document.getElementById('dailyTargetBatchMeta').textContent = '批量导入只支持当前交易日的应急手动加入，会逐个调用 IBKR 搜索并触发运行时预热。';
      });
      document.getElementById('dailyTargetBatchDeleteBtn').addEventListener('click', batchDeleteDailyTargetSymbols);
      document.getElementById('dailyTargetRefreshBtn').addEventListener('click', () => loadDailyTargets(true));
      document.getElementById('dailyTargetTableSearchInput').addEventListener('input', renderDailyTargetRows);
      document.getElementById('dailyTargetDate').addEventListener('change', () => {
        const nextDate = document.getElementById('dailyTargetDate').value || getUsDate();
        dailyTargetsState.selectedDate = nextDate;
        document.getElementById('marketDate').value = nextDate;
        renderDailyTargetSearchResults();
        loadDailyTargets(false);
      });
      ['dailyTargetStatusInput', 'dailyTargetDirectionInput', 'dailyTargetScoreInput', 'dailyTargetReasonInput'].forEach((id) => {
        document.getElementById(id).addEventListener('input', renderDailyTargetSearchResults);
        document.getElementById(id).addEventListener('change', renderDailyTargetSearchResults);
      });
    }

    function buildWatchlistFilter() {
      const scope = document.getElementById('scopeFilter').value || 'all';
      const current = escapeFilterValue(currentEnvironment);
      const activeRole = getWatchlistRoleForTab();
      const roleFilter = activeRole === 'trade'
        ? '(symbol_role = "trade" || symbol_role = "")'
        : 'symbol_role = "market_monitor"';
      if (scope === 'global') return `(environment = "global" && ${roleFilter})`;
      if (scope === 'current') {
        return currentEnvironment === 'live'
          ? `((environment = "live" || environment = "") && ${roleFilter})`
          : `(environment = "${current}" && ${roleFilter})`;
      }
      return currentEnvironment === 'live'
        ? `((environment = "global" || environment = "live" || environment = "") && ${roleFilter})`
        : `((environment = "global" || environment = "${current}") && ${roleFilter})`;
    }

    function syncWatchlistScopeOptions() {
      const scopeSelect = document.getElementById('targetScope');
      scopeSelect.innerHTML = `
        <option value="${escapeHtml(currentEnvironment)}">${escapeHtml(getEnvironmentLabel(currentEnvironment))}</option>
        <option value="global">GLOBAL</option>
      `;
    }

    function renderSearchResults() {
      const mount = document.getElementById('searchResults');
      const scope = document.getElementById('targetScope').value || currentEnvironment;
      const scopeLabel = scope === 'global' ? 'GLOBAL' : getEnvironmentLabel(scope);
      const roleLabel = getWatchlistRoleLabel();

      if (!watchlistState.searchResults.length) {
        mount.innerHTML = '<div class="empty-state">暂无搜索结果。</div>';
        return;
      }

      mount.innerHTML = watchlistState.searchResults.map((item, index) => {
        const secTypes = Array.isArray(item.sec_types) ? item.sec_types : [];
        const description = item.description || item.company_name || '无描述';
        const existing = findExistingBySymbol(item.symbol);
        const existingLabel = formatExistingWatchlistScopes(existing);
        return `
          <article class="result-card">
            <div class="card-top">
              <span class="symbol-chip">${escapeHtml(item.symbol || '--')}</span>
              <span class="pool-pill">${escapeHtml(item.exchange || 'SMART')}</span>
              <span class="pool-pill ${item.is_us ? '' : 'neutral'}">${item.is_us ? 'US' : 'NON-US'}</span>
              ${existing.length ? `<span class="pool-pill exists">已存在 ${escapeHtml(existingLabel)}</span>` : ''}
            </div>
            <div>
              <div class="card-title">${escapeHtml(item.company_name || item.symbol || '--')}</div>
              <div class="card-copy">${escapeHtml(description)}</div>
            </div>
            <div class="pill-row">
              <span class="status-chip">conid ${escapeHtml(item.conid || '--')}</span>
              <span class="status-chip">score ${escapeHtml(item.score || 0)}</span>
              ${(secTypes.length ? secTypes : [item.asset_class || 'UNKNOWN']).map((type) => `<span class="pool-pill">${escapeHtml(type)}</span>`).join('')}
            </div>
            <div class="card-bottom">
              <button class="btn primary" type="button" onclick="addCandidate(${index})">加入 ${escapeHtml(scopeLabel)} ${escapeHtml(roleLabel)}</button>
              <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">查看图表</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function getWatchlistPageSize() {
      const rawValue = Number(document.getElementById('watchlistPageSize')?.value || watchlistPaginationState.perPage || 10);
      if (!Number.isFinite(rawValue) || rawValue <= 0) return 10;
      return Math.max(1, Math.min(100, Math.trunc(rawValue)));
    }

    function getWatchlistPageButtons(page, totalPages) {
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

    function getWatchlistPageItems(items) {
      const rows = Array.isArray(items) ? items : [];
      watchlistPaginationState.perPage = getWatchlistPageSize();
      const totalPages = Math.max(1, Math.ceil(rows.length / watchlistPaginationState.perPage));
      watchlistPaginationState.page = Math.max(1, Math.min(totalPages, Number(watchlistPaginationState.page) || 1));
      const startIndex = (watchlistPaginationState.page - 1) * watchlistPaginationState.perPage;
      return rows.slice(startIndex, startIndex + watchlistPaginationState.perPage);
    }

    function renderWatchlistPagination(items, pageItems) {
      const rows = Array.isArray(items) ? items : [];
      watchlistPaginationState.perPage = getWatchlistPageSize();
      const totalRows = rows.length;
      const totalPages = Math.max(1, Math.ceil(totalRows / watchlistPaginationState.perPage));
      const page = Math.max(1, Math.min(totalPages, Number(watchlistPaginationState.page) || 1));
      watchlistPaginationState.page = page;
      const returnedCount = Array.isArray(pageItems) ? pageItems.length : 0;
      const shouldShowPagination = totalRows > watchlistPaginationState.perPage;
      const controls = [];

      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setWatchlistPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>`);
      let lastPage = 0;
      getWatchlistPageButtons(page, totalPages).forEach((value) => {
        if (lastPage && value - lastPage > 1) {
          controls.push('<span class="pagination-ellipsis">...</span>');
        }
        controls.push(`<button class="mini-btn pagination-btn ${value === page ? 'active' : ''}" type="button" onclick="setWatchlistPage(${value})">${value}</button>`);
        lastPage = value;
      });
      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setWatchlistPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`);

      ['watchlistPaginationTop', 'watchlistPaginationBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (!mount) return;
        const bar = mount.closest('.pagination-bar');
        if (bar) bar.hidden = !shouldShowPagination;
        mount.innerHTML = shouldShowPagination ? controls.join('') : '';
      });

      const statusText = totalRows
        ? `第 ${page} / ${totalPages} 页 · 本页 ${returnedCount} 条 · 可见 ${totalRows} 条`
        : '第 1 / 1 页 · 当前没有结果';
      ['watchlistPaginationStatusTop', 'watchlistPaginationStatusBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (mount) mount.textContent = shouldShowPagination ? statusText : '';
      });
    }

    window.setWatchlistPage = function setWatchlistPage(page) {
      const items = getFilteredWatchlistItems();
      const totalPages = Math.max(1, Math.ceil(items.length / getWatchlistPageSize()));
      const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
      if (nextPage === watchlistPaginationState.page) return;
      watchlistPaginationState.page = nextPage;
      renderWatchlistRows();
    };

    function renderWatchlistRows() {
      const items = getFilteredWatchlistItems();
      const table = document.getElementById('watchlistTable');
      if (!items.length) {
        table.innerHTML = `<tr><td colspan="9" class="empty-state">暂无 ${escapeHtml(getWatchlistRoleLabel())} 记录。</td></tr>`;
        renderWatchlistCards([]);
        renderWatchlistPagination(items, []);
      } else {
        const pageItems = getWatchlistPageItems(items);
        table.innerHTML = pageItems.map((item) => {
          const configItem = isConfigMonitorItem(item);
          return `
            <tr>
              <td>
                <div class="meta-stack">
                  <div class="table-symbol">${escapeHtml(item.symbol || '--')}</div>
                  <small>${escapeHtml(configItem ? 'ibkr_market_ws_symbols' : (item.id || ''))}</small>
                </div>
              </td>
              <td>${escapeHtml(item.exchange || '--')}</td>
              <td>${escapeHtml(item.industry || '--')}</td>
              <td><span class="env-badge ${resolveRecordEnvClass(item.environment)}">${escapeHtml(formatRecordEnvironment(item.environment))}</span></td>
              <td>${configItem ? statusChip('CONFIG', 'config') : escapeHtml(formatWatchlistRole(item.symbol_role || 'trade'))}</td>
              <td>${escapeHtml(formatWatchlistMember(item))}</td>
              <td>${escapeHtml(item.note || '--')}</td>
              <td>
                <div class="meta-stack">
                  <span>${escapeHtml(item.updated_us || item.us_time || '--')}</span>
                  <small>${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</small>
                </div>
              </td>
              <td>
                <div class="row-actions">
                  <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">Chart</a>
                  ${configItem
                    ? `<a class="mini-link" href="${getConfigPageUrl()}">改配置</a>`
                    : `<button class="mini-btn" type="button" onclick="editItem('${escapeHtml(item.id || '')}')">编辑</button>
                       <button class="mini-btn danger" type="button" onclick="removeItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}', '${escapeHtml(formatRecordEnvironment(item.environment))}')">删除</button>`
                  }
                </div>
              </td>
            </tr>
          `;
        }).join('');
        renderWatchlistCards(pageItems);
        renderWatchlistPagination(items, pageItems);
      }

      const configSuffix = watchlistState.configLoadError ? ` · 配置读取失败: ${watchlistState.configLoadError}` : '';
      document.getElementById('listMeta').textContent = `载入 ${watchlistState.items.length} · 可见 ${items.length} · 每页 ${getWatchlistPageSize()}${configSuffix}`;
      if (isWatchlistRoleTab()) {
        renderSearchResults();
        updateHero();
      }
    }

    async function searchContracts() {
      const keyword = String(document.getElementById('searchInput').value || '').trim();
      if (!keyword) {
        showToast('请输入 ticker 或公司名');
        return;
      }

      document.getElementById('searchMeta').textContent = `正在查询 ${keyword} ...`;
      document.getElementById('searchResults').innerHTML = '<div class="empty-state">搜索中...</div>';
      try {
        const payload = await requestJson(`/api/custom/ibkr/contracts/search${buildQuery({
          environment: currentEnvironment,
          q: keyword,
          limit: 12
        })}`);
        watchlistState.searchResults = Array.isArray(payload.items) ? payload.items : [];
        document.getElementById('searchMeta').textContent = `IBKR 返回 ${watchlistState.searchResults.length} 个候选`;
        renderSearchResults();
      } catch (error) {
        watchlistState.searchResults = [];
        document.getElementById('searchMeta').textContent = `搜索失败: ${error.message || error}`;
        document.getElementById('searchResults').innerHTML = `<div class="empty-state">${escapeHtml(error.message || error)}</div>`;
      }
    }

    async function loadWatchlist(showToastOnSuccess = false) {
      try {
        const response = await apiFetch('watchlist', {
          filter: buildWatchlistFilter(),
          sort: '-updated',
          perPage: 200
        });
        const collectionItems = Array.isArray(response.items) ? response.items : [];
        const configItems = getWatchlistRoleForTab() === 'market_monitor'
          ? await loadConfiguredMarketMonitorItems(collectionItems)
          : [];
        watchlistState.items = [...collectionItems, ...configItems];
        watchlistState.loaded = true;
        watchlistState.loadedRole = getWatchlistRoleForTab();
        watchlistState.lastRefresh = `${getWatchlistRoleLabel()} 已加载`;
        setPageRefreshTime();
        renderWatchlistRows();
        if (showToastOnSuccess) showToast(`${getWatchlistRoleLabel()} 已刷新`);
      } catch (error) {
        watchlistState.items = [];
        watchlistState.loaded = true;
        watchlistState.loadedRole = getWatchlistRoleForTab();
        watchlistState.configLoadError = '';
        watchlistState.lastRefresh = 'watchlist 加载失败';
        document.getElementById('watchlistTable').innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('watchlistCards', error.message || error);
        document.getElementById('listMeta').textContent = `加载失败: ${error.message || error}`;
        if (isWatchlistRoleTab()) {
          renderSearchResults();
          updateHero();
        }
      }
    }

    async function addCandidate(index) {
      const item = watchlistState.searchResults[index];
      if (!item) return;
      const scope = document.getElementById('targetScope').value || currentEnvironment;
      const note = String(document.getElementById('noteInput').value || '').trim();
      try {
        const payload = await requestJson('/api/custom/ibkr/watchlist/upsert', {
          method: 'POST',
          body: buildWatchlistBody(item, scope, note)
        });
        showToast(`${item.symbol} 已写入 ${scope === 'global' ? 'GLOBAL' : getEnvironmentLabel(scope)} ${getWatchlistRoleLabel()}`);
        const warning = getRuntimeWarning(payload);
        if (warning) showToast(`预热提示: ${warning}`);
        await loadWatchlist(false);
        if (payload.action === 'created' || payload.action === 'updated') {
          document.getElementById('noteInput').value = '';
        }
      } catch (error) {
        showToast(`加入失败: ${error.message || error}`);
      }
    }

    async function editItem(recordId) {
      const item = watchlistState.items.find((row) => String(row.id || '') === String(recordId || ''));
      if (!item) {
        showToast('记录不存在');
        return;
      }
      if (isConfigMonitorItem(item)) {
        showToast('配置来源的市场监控标的请到配置页修改 ibkr_market_ws_symbols');
        return;
      }

      const rawScope = String(item.environment || '').trim().toLowerCase();
      const originalScope = rawScope || currentEnvironment;
      const nextScopeInput = window.prompt(`修改 scope，仅支持 ${currentEnvironment} / global`, originalScope);
      if (nextScopeInput === null) return;
      const nextScope = String(nextScopeInput || '').trim().toLowerCase() || originalScope;
      if (![currentEnvironment, 'global'].includes(nextScope)) {
        showToast('scope 仅支持当前环境或 global');
        return;
      }

      const nextExchange = window.prompt('修改 exchange', item.exchange || '');
      if (nextExchange === null) return;
      const nextIndustry = window.prompt('修改 industry', item.industry || '');
      if (nextIndustry === null) return;
      const nextNote = window.prompt('修改 note', item.note || '');
      if (nextNote === null) return;
      const nextRole = window.prompt('修改 symbol_role(trade/market_monitor)', normalizeWatchlistRole(item.symbol_role || 'trade'));
      if (nextRole === null) return;
      if (!['trade', 'market_monitor'].includes(String(nextRole || '').trim())) {
        showToast('symbol_role 仅支持 trade 或 market_monitor');
        return;
      }

      try {
        const payload = await requestJson('/api/custom/ibkr/watchlist/upsert', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_edit',
            scope: nextScope,
            manual_member: true,
            symbol_role: String(nextRole || '').trim(),
            symbol: item.symbol,
            exchange: String(nextExchange || '').trim().toUpperCase(),
            industry: String(nextIndustry || '').trim(),
            note: String(nextNote || '').trim()
          }
        });
        if ((nextScope !== originalScope || !rawScope) && recordId) {
          await requestJson('/api/custom/ibkr/watchlist/remove', {
            method: 'POST',
            body: {
              environment: currentEnvironment,
              source: 'manual_page_scope_move',
              record_id: recordId,
              symbol: item.symbol,
              remove_current_day_targets: false
            }
          });
        }
        showToast(`${item.symbol} 已更新`);
        const warning = getRuntimeWarning(payload);
        if (warning) showToast(`预热提示: ${warning}`);
        await loadWatchlist(false);
      } catch (error) {
        showToast(`更新失败: ${error.message || error}`);
      }
    }

    async function removeItem(recordId, symbol, scopeLabel) {
      if (!recordId) return;
      const item = watchlistState.items.find((row) => String(row.id || '') === String(recordId || ''));
      if (isConfigMonitorItem(item)) {
        showToast('配置来源的市场监控标的请到配置页修改 ibkr_market_ws_symbols');
        return;
      }
      if (!window.confirm(`确认删除 ${symbol} (${scopeLabel}) ?`)) return;
      try {
        const payload = await requestJson('/api/custom/ibkr/watchlist/remove', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_remove',
            record_id: recordId,
            symbol
          }
        });
        showToast(`${symbol} 已删除`);
        const warning = getRuntimeWarning(payload);
        if (warning) showToast(`清理提示: ${warning}`);
        await loadWatchlist(false);
        await loadDailyTargets(false);
      } catch (error) {
        showToast(`删除失败: ${error.message || error}`);
      }
    }

    async function batchAddSymbols() {
      const symbols = parseSymbolList(document.getElementById('batchSymbolsInput').value);
      const scope = document.getElementById('targetScope').value || currentEnvironment;
      const note = String(document.getElementById('noteInput').value || '').trim();
      if (!symbols.length) {
        showToast('先输入要批量加入的 symbols');
        return;
      }

      let success = 0;
      let failed = 0;
      for (let index = 0; index < symbols.length; index += 1) {
        const symbol = symbols[index];
        document.getElementById('batchMeta').textContent = `处理中 ${index + 1}/${symbols.length}: ${symbol}`;
        try {
          const candidate = await searchBestContract(symbol);
          if (!candidate) throw new Error('IBKR 未返回候选');
          await requestJson('/api/custom/ibkr/watchlist/upsert', {
            method: 'POST',
            body: buildWatchlistBody(candidate, scope, note)
          });
          success += 1;
        } catch (_) {
          failed += 1;
        }
      }

      document.getElementById('batchMeta').textContent = `批量导入完成: 成功 ${success} / 失败 ${failed}`;
      showToast(`批量导入完成: ${success}/${symbols.length}`);
      await loadWatchlist(false);
    }

    async function batchDeleteSymbols() {
      const symbols = parseSymbolList(document.getElementById('batchDeleteInput').value);
      if (!symbols.length) {
        showToast('先输入要删除的 symbols');
        return;
      }

      const matched = watchlistState.items.filter((item) => symbols.includes(String(item.symbol || '').trim().toUpperCase()));
      const targets = matched.filter((item) => !isConfigMonitorItem(item));
      if (!targets.length) {
        showToast(matched.length ? '匹配项来自配置，请到配置页修改 ibkr_market_ws_symbols' : '当前加载范围内没有匹配记录');
        return;
      }
      if (!window.confirm(`确认删除 ${targets.length} 条记录？`)) return;

      let deleted = 0;
      for (const item of targets) {
        try {
          await requestJson('/api/custom/ibkr/watchlist/remove', {
            method: 'POST',
            body: {
              environment: currentEnvironment,
              source: 'manual_page_remove',
              record_id: item.id,
              symbol: item.symbol || ''
            }
          });
          deleted += 1;
        } catch (_) {
          // ignore batch delete failures and continue
        }
      }

      document.getElementById('deleteMeta').textContent = `批量删除完成: 删除 ${deleted} / 匹配 ${targets.length}`;
      showToast(`批量删除完成: ${deleted}/${targets.length}`);
      await loadWatchlist(false);
      await loadDailyTargets(false);
    }

    function attachWatchlistEvents() {
      document.getElementById('searchBtn').addEventListener('click', searchContracts);
      document.getElementById('searchInput').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') searchContracts();
      });
      document.getElementById('batchAddBtn').addEventListener('click', batchAddSymbols);
      document.getElementById('clearBatchBtn').addEventListener('click', () => {
        document.getElementById('batchSymbolsInput').value = '';
        document.getElementById('batchMeta').textContent = getWatchlistRoleForTab() === 'market_monitor'
          ? '批量搜索并写入 market_monitor。'
          : '批量搜索并写入当前 scope。';
      });
      document.getElementById('batchDeleteBtn').addEventListener('click', batchDeleteSymbols);
      document.getElementById('refreshListBtn').addEventListener('click', () => loadWatchlist(true));
      document.getElementById('scopeFilter').addEventListener('change', () => {
        watchlistPaginationState.page = 1;
        loadWatchlist(false);
      });
      document.getElementById('listSearchInput').addEventListener('input', () => {
        watchlistPaginationState.page = 1;
        renderWatchlistRows();
      });
      document.getElementById('watchlistPageSize').addEventListener('change', () => {
        watchlistPaginationState.page = 1;
        renderWatchlistRows();
      });
      document.getElementById('targetScope').addEventListener('change', renderSearchResults);
    }

    window.addDailyTargetCandidate = addDailyTargetCandidate;
    window.editDailyTargetItem = editDailyTargetItem;
    window.removeDailyTargetItem = removeDailyTargetItem;
    window.addCandidate = addCandidate;
    window.editItem = editItem;
    window.removeItem = removeItem;

    window.onEnvironmentChange = function(environment) {
      currentEnvironment = environment;
      const params = { market_date: document.getElementById('marketDate').value || '' };
      if (activeTab === 'screener') {
        params.view = activeScreenerView;
        if (activeScreenerView === 'window-progress' && normalizeWindowProgressStatusTab(activeWindowProgressStatus) !== 'all') {
          params.window_status = normalizeWindowProgressStatusTab(activeWindowProgressStatus);
        }
      }
      windowProgressState.loadedKey = '';
      if (activeTab === 'watchlist') params.tab = 'watchlist';
      if (activeTab === 'monitor') params.tab = 'monitor';
      if (activeTab === 'targets') {
        params.tab = 'targets';
        params.date = getDailyTargetDate();
        params.market_date = getDailyTargetDate();
      }
      window.location.href = buildPageUrl('/ibkr_screener.html', params, { environment: currentEnvironment });
    };

    document.addEventListener('DOMContentLoaded', async () => {
      if (!initAuth()) return;
      const query = new URLSearchParams(window.location.search);
      activeTab = getRequestedTab();
      activeScreenerView = getRequestedScreenerView();
      activeWindowProgressStatus = getRequestedWindowProgressStatus();
      setMarketDateFromUrl();
      dailyTargetsState.selectedDate = query.get('date')
        || document.getElementById('marketDate').value
        || getUsDate();
      const currentFocus = String(query.get('focus') || '').trim().toUpperCase();
      if (currentFocus) {
        document.getElementById('currentTargetSearch').value = currentFocus;
      }
      document.getElementById('dailyTargetDate').value = dailyTargetsState.selectedDate;
      document.getElementById('nav').innerHTML = renderNav('/ibkr_screener.html');
      document.getElementById('contextBar').innerHTML = renderPageContextBar('🔎 IBKR 筛选', {
        subtitle: '筛选 / 标的 / 标池'
      });
      document.getElementById('pageBridge').innerHTML = renderDomainTabs();
      bindTabEvents();
      bindScreenerViewEvents();
      bindWindowProgressStatusEvents();
      bindResponsiveState();
      bindFilterEvents();
      bindCurrentTargetFilterEvents();
      bindReadyTipEvents();
      attachDailyTargetEvents();
      syncWatchlistScopeOptions();
      attachWatchlistEvents();
      renderDailyTargetSearchResults();
      renderSearchResults();
      renderDailyTargetRows();
      renderWatchlistRows();
      await activateTab(activeTab, {
        syncHistory: false,
      });
    });
