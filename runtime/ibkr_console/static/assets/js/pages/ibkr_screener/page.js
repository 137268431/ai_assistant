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
      const systemLogicLink = document.getElementById('systemLogicLink');
      if (systemLogicLink) {
        systemLogicLink.href = buildPageUrl('/ibkr_system_logic.html', {}, {
          environment: currentEnvironment,
          brokerMode: currentBrokerMode,
          dataEnvironment: currentEnvironment,
        });
      }
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
