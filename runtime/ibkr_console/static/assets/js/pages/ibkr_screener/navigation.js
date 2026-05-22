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
      const aliases = {
        candidate: 'signal_candidate',
        signal: 'signal_candidate',
        confirmed_signal: 'confirmed',
        conflict: 'direction_conflict',
        direction_mismatch: 'direction_conflict',
        target: 'target_candidate',
        pool_candidate: 'target_candidate',
      };
      if (aliases[normalized]) return aliases[normalized];
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
      const params = {
        filter: `key = "${escapeFilterValue(key)}" && (environment = "${escapeFilterValue(currentEnvironment)}" || environment = "global" || environment = "")`,
        sort: '-updated',
        perPage: 20,
        page: 1
      };
      const result = typeof cachedApiFetch === 'function'
        ? await cachedApiFetch('config', params, { ttlMs: 300000, ttl: 300000 })
        : await apiFetch('config', params);
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
