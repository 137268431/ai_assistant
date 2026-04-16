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
    let runtimeCurrentMarketDate = '';
    let rulesPayload = { selection: null, signals: null, computed_at_us: '' };
    let rulesLoadError = '';
    let filteredRows = [];
    let filteredCurrentTargetRows = [];
    const selectedSymbols = new Set();
    const dailyTargetsState = {
      selectedDate: '',
      items: [],
      searchResults: [],
      loaded: false,
      lastRefresh: '尚未加载'
    };
    const watchlistState = {
      items: [],
      searchResults: [],
      loaded: false,
      lastRefresh: '尚未加载',
      loadedRole: ''
    };
    const currentTargetState = {
      page: 1,
      perPage: 10,
      requestToken: 0,
      searchDebounceId: 0,
    };

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
      const response = await fetch(`${BASE_URL}/api/collections/${collection}/records/${recordId}`, {
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

    function getRequestedTab() {
      const value = String(new URLSearchParams(window.location.search).get('tab') || '').trim().toLowerCase();
      if (value === 'monitor') return 'monitor';
      if (value === 'watchlist') return 'watchlist';
      if (value === 'targets') return 'targets';
      return 'screener';
    }

    function getRequestedScreenerView() {
      const value = String(new URLSearchParams(window.location.search).get('view') || '').trim().toLowerCase();
      return value === 'universe' ? 'universe' : 'current';
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
        ? '市场上下文 / 指数情绪 / 只算指标不进交易'
        : 'watchlist / global pool / manual upkeep';
    }

    function renderDomainTabs() {
      const items = [
        {
          tab: 'screener',
          kicker: 'Screener',
          label: '筛选',
          copy: 'operable stocks / targets sync'
        },
        {
          tab: 'targets',
          kicker: 'Daily',
          label: '每日标的',
          copy: 'ibkr_targets upkeep / fixes / daily control'
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
        <div class="domain-tabs">
          ${items.map((item) => `
            <button class="domain-tab ${item.tab === activeTab ? 'active' : ''}" type="button" data-tab="${item.tab}">
              <div class="domain-tab-kicker">${escapeHtml(item.kicker)}</div>
              <div class="domain-tab-label">${escapeHtml(item.label)}</div>
              <div class="domain-tab-copy">${escapeHtml(item.copy)}</div>
            </button>
          `).join('')}
        </div>
      `;
    }

    function renderSummaryCards(cards) {
      document.getElementById('summaryGrid').innerHTML = cards.map((item) => `
        <div class="summary-card">
          <div class="summary-label">${escapeHtml(item.label)}</div>
          <div class="summary-value ${item.className || ''}">${escapeHtml(String(item.value))}</div>
          <div class="summary-copy">${escapeHtml(item.copy)}</div>
        </div>
      `).join('');
    }

    function renderRulesCard(panel, { actionHref = '', actionLabel = '' } = {}) {
      const chips = Array.isArray(panel?.chips) ? panel.chips : [];
      const sections = Array.isArray(panel?.sections) ? panel.sections : [];
      const footer = rulesPayload.computed_at_us ? `更新: ${rulesPayload.computed_at_us}` : '';
      return `
        <section class="panel rules-panel">
          <div class="rules-panel-head">
            <div>
              <div class="rules-panel-kicker">Rules Snapshot</div>
              <div class="rules-panel-title">${escapeHtml(panel?.title || '当前规则')}</div>
              <div class="rules-panel-subtitle">${escapeHtml(panel?.subtitle || '当前页面直接显示运行中的规则摘要。')}</div>
            </div>
            ${actionHref && actionLabel ? `<a class="rules-panel-action" href="${actionHref}">${escapeHtml(actionLabel)}</a>` : ''}
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
        </section>
      `;
    }

    function renderRulesBoard() {
      const mount = document.getElementById('rulesBoard');
      if (!mount) return;
      const marketDate = todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate')?.value || getUsDate();
      const cards = [];
      if (rulesPayload?.selection) {
        cards.push(renderRulesCard(rulesPayload.selection, {
          actionHref: buildPageUrl('/ibkr_screener.html', {
            tab: 'screener',
            view: activeScreenerView,
            market_date: marketDate,
          }, { environment: currentEnvironment }),
          actionLabel: '当前榜单'
        }));
      }
      if (rulesPayload?.signals) {
        cards.push(renderRulesCard(rulesPayload.signals, {
          actionHref: buildPageUrl('/ibkr_signals.html', {
            date: marketDate,
          }, { environment: currentEnvironment }),
          actionLabel: '打开信号页'
        }));
      }

      if (cards.length) {
        mount.innerHTML = cards.join('');
        return;
      }

      mount.innerHTML = `
        <section class="panel rules-panel">
          <div class="rules-panel-kicker">Rules Snapshot</div>
          <div class="rules-panel-title">规则摘要加载中</div>
          <div class="rules-empty">${escapeHtml(rulesLoadError || '正在读取当前 compute 逻辑与 runtime 配置。')}</div>
        </section>
      `;
    }

    async function loadRulesSummary() {
      rulesLoadError = '';
      renderRulesBoard();
      try {
        rulesPayload = await requestJson(`/api/custom/ibkr/rules${buildQuery({
          environment: currentEnvironment
        })}`);
      } catch (error) {
        console.error('loadRulesSummary failed:', error);
        rulesPayload = { selection: null, signals: null, computed_at_us: '' };
        rulesLoadError = error.message || String(error);
      }
      renderRulesBoard();
    }

    function renderScreenerSummary() {
      const summary = screenerPayload.summary || {};
      renderSummaryCards([
        { label: 'TOTAL', value: summary.total || 0, copy: 'watchlist + 今日 targets 聚合后的总标的数' },
        { label: 'LIVE BARS', value: summary.with_live_bars || 0, copy: '当日已有 5m bar 的标的数', className: 'teal' },
        { label: 'OPERABLE', value: summary.operable || 0, copy: '满足轻量可操作条件的标的数', className: 'good' },
        { label: 'CANDIDATES', value: summary.candidate_targets || 0, copy: '今日 candidate targets 数量', className: 'accent' },
        { label: 'ACTIVE', value: summary.active_targets || 0, copy: '今日 active targets 数量', className: 'good' },
        { label: 'AVG PRE', value: formatVolume(summary.avg_premarket_volume || 0), copy: '全表平均盘前量能', className: 'accent' }
      ]);
    }

    function renderCurrentTargetsSummary() {
      const summary = todayTargetsPayload.summary || {};
      renderSummaryCards([
        { label: 'TOTAL', value: summary.total || 0, copy: '当前交易日 candidate + active 标的数' },
        { label: 'READY', value: summary.technical_ready_count || 0, copy: '技术状态为 ready 的标的', className: 'good' },
        { label: 'SIGNALLED', value: summary.signaled_count || 0, copy: '今天已经进入 ibkr_signals 的标的', className: 'teal' },
        { label: 'NEEDS ACTION', value: (summary.awaiting_confirm_count || 0) + (summary.pending_count || 0), copy: 'awaiting_confirm + pending 的标的', className: 'accent' },
        { label: 'EXECUTED', value: summary.executed_count || 0, copy: '最新信号状态为 executed 的标的', className: 'good' },
        { label: 'STALE', value: summary.stale_count || 0, copy: '无当日 bar 或 freshness 超阈值', className: 'accent' }
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
      const roleLabel = getWatchlistRoleLabel();
      renderSummaryCards([
        { label: 'VISIBLE', value: items.length, copy: `当前筛选后展示的${roleLabel}记录数` },
        { label: 'CURRENT ENV', value: currentCount, copy: '当前运行环境命中的记录', className: 'good' },
        { label: 'GLOBAL', value: globalCount, copy: '全局共享池记录', className: 'teal' },
        { label: 'LEGACY', value: legacyCount, copy: '历史 environment 为空的记录', className: 'accent' }
      ]);
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
        { label: 'VISIBLE', value: items.length, copy: '当前日期展示的目标池记录数' },
        { label: 'ACTIVE', value: active, copy: '状态为 active 的记录', className: 'good' },
        { label: 'CANDIDATE', value: candidate, copy: '状态为 candidate 的记录', className: 'accent' },
        { label: 'REMOVED', value: removed, copy: '状态为 removed 的记录', className: 'teal' },
        { label: 'AVG SCORE', value: avgScore, copy: '当前列表平均 score', className: 'accent' }
      ]);
    }

    function updateHero() {
      document.getElementById('environmentHeader').innerHTML = renderEnvironmentBadge();
      if (isWatchlistRoleTab()) {
        const visible = getFilteredWatchlistItems().length;
        const roleLabel = getWatchlistRoleLabel();
        const isMonitorTab = getWatchlistRoleForTab() === 'market_monitor';
        document.getElementById('heroTitle').textContent = isMonitorTab ? '维护市场监控标的与 GLOBAL 共享池。' : '维护运行标池与 GLOBAL 共享池。';
        document.getElementById('heroCopy').textContent = isMonitorTab
          ? '在同一页里搜索真实 IBKR 合约、写入当前环境或 GLOBAL，并维护只做 bars / indicators / runtime 上下文的市场监控标的。'
          : '在同一页里搜索真实 IBKR 合约、写入当前环境或 GLOBAL，并直接清理手工加入的标池记录。';
        document.getElementById('marketDateMeta').textContent = `Scope ${getEnvironmentLabel(currentEnvironment)} + GLOBAL`;
        document.getElementById('refreshInfo').textContent = watchlistState.lastRefresh;
        document.getElementById('selectionInfo').textContent = `当前可见 ${visible} 条 · 当前角色 ${roleLabel}`;
        renderWatchlistSummary();
        document.getElementById('watchlistSearchPanelTitle').textContent = isMonitorTab ? '搜索可加入的市场监控标的' : '搜索可加入的标的';
        document.getElementById('watchlistSearchPanelCopy').textContent = isMonitorTab
          ? '支持 ticker 或公司名。写入后的标的只参与 bars / indicators / runtime 监控，不进入 scan、targets 与交易信号。'
          : '支持 ticker 或公司名。返回结果来自 IBKR Gateway，不依赖本地缓存前端匹配。';
        document.getElementById('watchlistListPanelTitle').textContent = isMonitorTab ? '已有市场监控记录' : '已有 watchlist 记录';
        document.getElementById('watchlistListPanelCopy').textContent = isMonitorTab
          ? '这里直接管理 PocketBase `watchlist` 中 role=`market_monitor` 的记录，默认支持当前环境与 GLOBAL 共享池一起看。'
          : '这里直接管理 PocketBase `watchlist` 集合中 role=`trade` 的记录，默认支持当前环境与 GLOBAL 共享池一起看。';
        document.getElementById('searchMeta').textContent = isMonitorTab ? '输入后回车或点击搜索，加入市场监控。' : '输入后回车或点击搜索。';
        document.getElementById('batchMeta').textContent = isMonitorTab
          ? '批量导入会逐个调用 IBKR 搜索并写入当前 scope，角色固定为 market_monitor。'
          : '批量导入会逐个调用 IBKR 搜索并写入当前 scope。';
        document.getElementById('deleteMeta').textContent = isMonitorTab ? '适合清理一组市场监控标的。' : '适合清理一组手工加入的标的。';
        return;
      }

      if (activeTab === 'targets') {
        const visible = getFilteredDailyTargetItems().length;
        document.getElementById('heroTitle').textContent = '维护每日操作标的与 ibkr_targets。';
        document.getElementById('heroCopy').textContent = '直接搜索 IBKR 合约并维护当日 ibkr_targets，可手动补录、修正状态和清理错误数据，不再单独跳转到 targets 页面。';
        document.getElementById('marketDateMeta').textContent = `Target Date ${getDailyTargetDate()}`;
        document.getElementById('refreshInfo').textContent = dailyTargetsState.lastRefresh;
        document.getElementById('selectionInfo').textContent = `当前 ${visible} 条 · 搜索候选 ${dailyTargetsState.searchResults.length} 个`;
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
        document.getElementById('heroTitle').textContent = '先看当前标的，再决定今天的盘中动作。';
        document.getElementById('heroCopy').textContent = '当前标的榜把 today targets、技术状态和今天已出信号放到一张表里，优先定位需要确认、等待执行和 ready 但尚未触发的标的。';
        document.getElementById('marketDateMeta').textContent = `Market Date ${todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || '--'}`;
        document.getElementById('refreshInfo').textContent = todayTargetsPayload.computed_at_us
          ? `更新: ${todayTargetsPayload.computed_at_us}`
          : '数据未刷新';
        document.getElementById('selectionInfo').textContent = `第 ${currentPage}/${totalPages} 页 · 本页 ${visible} 条 · ready ${visibleReady} 条 · needs action ${visibleActionable} 条 · 过滤后 ${filteredTotal} / 总 ${summary.total || 0}`;
        renderCurrentTargetsSummary();
        return;
      }

      const selectedRows = (screenerPayload.items || []).filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()));
      const operableCount = selectedRows.filter((row) => row.is_operable).length;
      document.getElementById('heroTitle').textContent = '把筛选和标池放进同一工作流，先筛再管。';
      document.getElementById('heroCopy').textContent = '先看哪些标的有活跃 bars、量能和方向，再在同一页里维护 watchlist 与 GLOBAL 共享池，避免在两个页面来回跳。';
      document.getElementById('marketDateMeta').textContent = `Market Date ${screenerPayload.market_date || document.getElementById('marketDate').value || '--'}`;
      document.getElementById('refreshInfo').textContent = screenerPayload.computed_at_us
        ? `更新: ${screenerPayload.computed_at_us}`
        : '数据未刷新';
      document.getElementById('selectionInfo').textContent = selectedRows.length
        ? `已选择 ${selectedRows.length} 个标的 · 可操作 ${operableCount} 个`
        : `已选择 ${selectedSymbols.size} 个标的`;
      renderScreenerSummary();
    }

    function activateScreenerView(view, { syncHistory = true } = {}) {
      activeScreenerView = view === 'universe' ? 'universe' : 'current';
      document.querySelectorAll('#screenerViewTabs .subview-tab').forEach((button) => {
        button.classList.toggle('active', button.dataset.view === activeScreenerView);
      });
      document.getElementById('currentViewPanel')?.classList.toggle('active', activeScreenerView === 'current');
      document.getElementById('universeViewPanel')?.classList.toggle('active', activeScreenerView === 'universe');
      if (syncHistory && activeTab === 'screener') syncUrl();
      if (activeTab === 'screener') updateHero();
    }

    function bindScreenerViewEvents() {
      document.querySelectorAll('#screenerViewTabs .subview-tab').forEach((button) => {
        button.addEventListener('click', () => activateScreenerView(button.dataset.view || 'current'));
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
      }
      const nextUrl = buildPageUrl('/ibkr_screener.html', params, { environment: currentEnvironment });
      if (`${location.pathname}${location.search}` !== nextUrl) {
        window.history.replaceState({}, '', nextUrl);
      }
    }

    async function activateTab(tab, { syncHistory = true, reloadCurrentTargets = true } = {}) {
      activeTab = ['watchlist', 'monitor', 'targets'].includes(tab) ? tab : 'screener';
      document.getElementById('pageBridge').innerHTML = renderDomainTabs();
      bindTabEvents();
      document.getElementById('screenerTab').classList.toggle('active', activeTab === 'screener');
      document.getElementById('targetsTab').classList.toggle('active', activeTab === 'targets');
      document.getElementById('watchlistTab').classList.toggle('active', isWatchlistRoleTab());
      if (syncHistory) syncUrl();
      if (activeTab === 'targets' && !dailyTargetsState.loaded) {
        await loadDailyTargets(false);
      }
      if (isWatchlistRoleTab() && (!watchlistState.loaded || watchlistState.loadedRole !== getWatchlistRoleForTab())) {
        await loadWatchlist(false);
      }
      if (activeTab === 'screener') {
        if (reloadCurrentTargets) {
          await loadTodayTargets(false);
        }
        activateScreenerView(activeScreenerView, { syncHistory: false });
      }
      updateHero();
    }

    function bindTabEvents() {
      document.querySelectorAll('.domain-tab').forEach((button) => {
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

    function mergeTodayTargetRowWithRealtimeQuote(row) {
      const quote = getRealtimeQuote(row?.symbol);
      return {
        ...row,
        display_price: quote?.last_price != null ? quote.last_price : row?.price,
        display_day_change_pct: quote?.day_change_pct != null ? quote.day_change_pct : row?.day_change_pct,
      };
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
        if (mount) mount.innerHTML = controls.join('');
      });

      const statusText = filteredTotal
        ? `第 ${page} / ${totalPages} 页 · 本页 ${returnedCount} 条 · 过滤后 ${filteredTotal} 条`
        : '第 1 / 1 页 · 当前没有结果';
      ['currentTargetsPaginationStatusTop', 'currentTargetsPaginationStatusBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (mount) mount.textContent = statusText;
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
      meta.textContent = `交易日 ${marketDate} · 第 ${currentPage}/${totalPages} 页 · 本页 ${rows.length} 条 · ready ${readyCount} · signaled ${signaledCount} · needs action ${needsActionCount} · 过滤后 ${filteredTotal} 条 · 全量 ${summary.total || 0}`;
      metaSecondary.textContent = `过滤后 ready ${filteredSummary.ready_count || 0} 条 · signaled ${filteredSummary.signaled_count || 0} 条 · needs action ${filteredSummary.needs_action_count || 0} 条。${workflow.scan_summary_time_et || '05:55'} ET 日筛先产出 candidate / active，${workflow.open_check_time_et || '09:20'} ET 盘前状态检查，盘中按 ${workflow.intraday_refresh_rule || '5m close-driven'} 刷新；当前排序先看 awaiting_confirm / pending，再看 ready 未出信号，最后看 executed / stale。`;
      renderCurrentTargetPagination();

      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-state">当前条件下没有符合的标的。</td></tr>';
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
              ${statusChip(formatCurrentStateLabel(row.technical_state), row.technical_state || 'watch')}<br>
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
    }

    async function loadTodayTargets(showToastOnSuccess = false) {
      const marketDate = document.getElementById('marketDate').value || getDailyTargetDate() || getUsDate();
      window.clearTimeout(currentTargetState.searchDebounceId);
      const requestToken = ++currentTargetState.requestToken;
      document.getElementById('currentTargetsMeta').textContent = `交易日 ${marketDate} · 正在加载...`;
      document.getElementById('currentTargetsMetaSecondary').textContent = '正在计算技术状态与今日信号聚合...';
      document.getElementById('currentTargetsTable').innerHTML = '<tr><td colspan="6" class="empty-state">加载中...</td></tr>';
      renderCurrentTargetPagination();

      try {
        const payload = await requestJson(`/api/custom/ibkr/today-targets${buildQuery(getCurrentTargetRequestParams(marketDate))}`);
        if (requestToken !== currentTargetState.requestToken) return;
        runtimeCurrentMarketDate = String(payload?.current_market_date || payload?.market_date || runtimeCurrentMarketDate || '').trim();
        const items = Array.isArray(payload?.items) ? payload.items : [];
        if (items.length) {
          try {
            await fetchRealtimeQuotes(items.map((row) => row?.symbol).filter(Boolean), { reset: false });
          } catch (error) {
            console.warn('加载今日标的实时报价失败:', error);
          }
        }
        if (requestToken !== currentTargetState.requestToken) return;
        todayTargetsPayload = {
          ...(payload || { items: [], summary: {}, market_date: marketDate, filtered_total: 0, total_pages: 1, page: 1 }),
          items: items.map((row) => mergeTodayTargetRowWithRealtimeQuote(row)),
        };
        currentTargetState.page = Math.max(1, Number(todayTargetsPayload.page || currentTargetState.page || 1) || 1);
        currentTargetState.perPage = getCurrentTargetPageSize();
        filteredCurrentTargetRows = Array.isArray(todayTargetsPayload.items) ? todayTargetsPayload.items : [];
        renderRulesBoard();
        renderCurrentTargetTable();
        if (activeTab === 'screener' && activeScreenerView === 'current') updateHero();
        if (showToastOnSuccess) showToast('今日交易标的已刷新');
      } catch (error) {
        if (requestToken !== currentTargetState.requestToken) return;
        todayTargetsPayload = { items: [], summary: {}, market_date: marketDate, filtered_total: 0, total_pages: 1, page: 1, filtered_summary: {} };
        filteredCurrentTargetRows = [];
        document.getElementById('currentTargetsMeta').textContent = `加载失败: ${error.message || error}`;
        document.getElementById('currentTargetsMetaSecondary').textContent = '当前标的榜加载失败。';
        document.getElementById('currentTargetsTable').innerHTML = `<tr><td colspan="6" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderCurrentTargetPagination();
        renderRulesBoard();
        if (activeTab === 'screener' && activeScreenerView === 'current') updateHero();
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

    function statusChip(label, className) {
      return `<span class="status-chip ${escapeHtml(className || '')}">${escapeHtml(label || '--')}</span>`;
    }

    function renderTable() {
      const tbody = document.getElementById('screenerTable');
      if (!Array.isArray(filteredRows) || !filteredRows.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">当前条件下没有符合的标的</td></tr>';
        document.getElementById('tableMeta').textContent = '0 条结果';
        document.getElementById('tableMetaSecondary').textContent = '';
        return;
      }

      tbody.innerHTML = filteredRows.map((row) => {
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

      document.getElementById('tableMeta').textContent = `当前可见 ${filteredRows.length} 条 · 可操作 ${filteredRows.filter((row) => row.is_operable).length} 条 · 已有 live bars ${filteredRows.filter((row) => row.has_live_bar).length} 条`;
      document.getElementById('tableMetaSecondary').textContent = `可见结果中已选择 ${filteredRows.filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase())).length} 条`;
    }

    function applyFilters() {
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
      showToast(`已选择 ${filteredRows.length} 个可见标的`);
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
        showToast(`已复制 ${filteredRows.length} 个 symbols`);
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
        await loadScreener(false);
      } catch (error) {
        showToast(`写入失败: ${error.message || error}`);
      } finally {
        hideLoading();
      }
    };

    async function loadScreener(showToastOnSuccess = false) {
      if (!initAuth()) return;
      const marketDate = document.getElementById('marketDate').value || '';
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

      try {
        showLoading('正在聚合筛选器数据...');
        const rulesPromise = loadRulesSummary();
        const payload = await requestJson(`/api/custom/ibkr/screener${buildQuery({
          environment: currentEnvironment,
          market_date: marketDate
        })}`);
        const items = Array.isArray(payload?.items) ? payload.items : [];
        if (items.length) {
          try {
            await fetchRealtimeQuotes(items.map((row) => row?.symbol).filter(Boolean), { reset: false });
          } catch (error) {
            console.warn('加载 screener 实时报价失败:', error);
          }
        }
        screenerPayload = {
          ...(payload || { items: [], summary: {}, filters: {} }),
          items: items.map((row) => mergeScreenerRowWithRealtimeQuote(row)),
        };
        populateSelect('exchangeFilter', payload.filters && payload.filters.exchanges);
        populateSelect('industryFilter', payload.filters && payload.filters.industries);
        populateSelect('targetStatusFilter', payload.filters && payload.filters.target_statuses);
        populateSelect('directionFilter', payload.filters && payload.filters.direction_biases);
        applyFilters();
        await rulesPromise;
        await loadTodayTargets(false);
        renderRulesBoard();
        if (activeTab === 'screener') updateHero();
        if (showToastOnSuccess) showToast('筛选器已刷新');
      } catch (error) {
        console.error('loadScreener failed:', error);
        document.getElementById('refreshInfo').textContent = '加载失败';
        document.getElementById('screenerTable').innerHTML = `<tr><td colspan="9" class="empty">${escapeHtml(error.message || error)}</td></tr>`;
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
        'operableOnly'
      ];
      ids.forEach((id) => {
        const element = document.getElementById(id);
        if (!element) return;
        const eventName = element.tagName === 'INPUT' && element.type === 'text' ? 'input' : 'change';
        element.addEventListener(eventName, applyFilters);
      });
      document.getElementById('marketDate').addEventListener('change', () => {
        const nextDate = document.getElementById('marketDate').value || getUsDate();
        currentTargetState.page = 1;
        dailyTargetsState.selectedDate = nextDate;
        if (document.getElementById('dailyTargetDate')) {
          document.getElementById('dailyTargetDate').value = nextDate;
        }
        loadScreener(false);
        if (activeTab === 'targets') {
          loadDailyTargets(false);
        }
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
      document.getElementById('openUniverseViewBtn')?.addEventListener('click', () => activateScreenerView('universe'));
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

    async function loadDailyTargets(showToastOnSuccess = false) {
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
        dailyTargetsState.lastRefresh = `更新: ${new Date().toLocaleTimeString()}`;
        document.getElementById('dailyTargetListMeta').textContent = `环境 ${getEnvironmentLabel(currentEnvironment)} / 日期 ${dailyTargetsState.selectedDate} / 原始记录 ${dailyTargetsState.items.length}`;
        renderDailyTargetRows();
        await loadTodayTargets(false);
        syncUrl();
        if (showToastOnSuccess) showToast('ibkr_targets 已刷新');
      } catch (error) {
        dailyTargetsState.items = [];
        dailyTargetsState.loaded = true;
        dailyTargetsState.lastRefresh = 'ibkr_targets 加载失败';
        document.getElementById('dailyTargetsTable').innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
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

    function renderWatchlistRows() {
      const items = getFilteredWatchlistItems();
      const table = document.getElementById('watchlistTable');
      if (!items.length) {
        table.innerHTML = `<tr><td colspan="9" class="empty-state">当前没有符合条件的 ${escapeHtml(getWatchlistRoleLabel())} 记录。</td></tr>`;
      } else {
        table.innerHTML = items.map((item) => `
          <tr>
            <td>
              <div class="meta-stack">
                <div class="table-symbol">${escapeHtml(item.symbol || '--')}</div>
                <small>${escapeHtml(item.id || '')}</small>
              </div>
            </td>
            <td>${escapeHtml(item.exchange || '--')}</td>
            <td>${escapeHtml(item.industry || '--')}</td>
            <td><span class="env-badge ${resolveRecordEnvClass(item.environment)}">${escapeHtml(formatRecordEnvironment(item.environment))}</span></td>
            <td>${escapeHtml(formatWatchlistRole(item.symbol_role || 'trade'))}</td>
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
                <button class="mini-btn" type="button" onclick="editItem('${escapeHtml(item.id || '')}')">编辑</button>
                <button class="mini-btn danger" type="button" onclick="removeItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}', '${escapeHtml(formatRecordEnvironment(item.environment))}')">删除</button>
              </div>
            </td>
          </tr>
        `).join('');
      }

      document.getElementById('listMeta').textContent = `已载入 ${watchlistState.items.length} 条原始记录 · 当前可见 ${items.length} 条`;
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
        watchlistState.items = Array.isArray(response.items) ? response.items : [];
        watchlistState.loaded = true;
        watchlistState.loadedRole = getWatchlistRoleForTab();
        watchlistState.lastRefresh = `更新: ${new Date().toLocaleTimeString()}`;
        renderWatchlistRows();
        if (showToastOnSuccess) showToast(`${getWatchlistRoleLabel()} 已刷新`);
      } catch (error) {
        watchlistState.items = [];
        watchlistState.loaded = true;
        watchlistState.loadedRole = getWatchlistRoleForTab();
        watchlistState.lastRefresh = 'watchlist 加载失败';
        document.getElementById('watchlistTable').innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
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

      const targets = watchlistState.items.filter((item) => symbols.includes(String(item.symbol || '').trim().toUpperCase()));
      if (!targets.length) {
        showToast('当前加载范围内没有匹配记录');
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
          ? '批量导入会逐个调用 IBKR 搜索并写入当前 scope，角色固定为 market_monitor。'
          : '批量导入会逐个调用 IBKR 搜索并写入当前 scope。';
      });
      document.getElementById('batchDeleteBtn').addEventListener('click', batchDeleteSymbols);
      document.getElementById('refreshListBtn').addEventListener('click', () => loadWatchlist(true));
      document.getElementById('scopeFilter').addEventListener('change', () => loadWatchlist(false));
      document.getElementById('listSearchInput').addEventListener('input', renderWatchlistRows);
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
      }
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
      document.getElementById('contextBar').innerHTML = renderPageContextBar('🔎 IBKR 筛选与标池', {
        description: '筛选 / 每日标的 / 标池 / 市场监控 一体化'
      });
      document.getElementById('pageBridge').innerHTML = renderDomainTabs();
      bindTabEvents();
      bindScreenerViewEvents();
      bindFilterEvents();
      bindCurrentTargetFilterEvents();
      attachDailyTargetEvents();
      syncWatchlistScopeOptions();
      attachWatchlistEvents();
      renderDailyTargetSearchResults();
      renderSearchResults();
      renderDailyTargetRows();
      renderWatchlistRows();
      activateScreenerView(activeScreenerView, { syncHistory: false });
      updateHero();
      await loadScreener(false);
      await activateTab(activeTab, {
        syncHistory: false,
        reloadCurrentTargets: activeTab !== 'screener',
      });
    });
