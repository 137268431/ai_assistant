// Shared UI compatibility facade. The classic-script implementation is split
// across ui-*.js files and loaded by common.js in dependency order.
(function sharedUiFacade(global) {
  const win = typeof window !== 'undefined' ? window : global;
  const uiBundleScripts = [
    '/assets/js/shared/ui-toast-nav.js',
    '/assets/js/shared/ui-time-indicator.js',
    '/assets/js/shared/ui-page.js',
    '/assets/js/shared/ui-bridges.js',
    '/assets/js/shared/ui-legacy.js'
  ];

  global.__IBKR_SHARED_UI_BUNDLE__ = uiBundleScripts.slice();

  function currentFunction(name) {
    if (typeof global[name] === 'function') return global[name];
    if (win && typeof win[name] === 'function') return win[name];
    return null;
  }

  function isUiBundleLoaded() {
    return ['showToast', 'renderPageTopSection', 'renderBacktestsBridge', 'renderReviewBridge', 'showLoading']
      .every((name) => typeof currentFunction(name) === 'function');
  }

  function hasScript(src) {
    if (typeof document === 'undefined' || !document.querySelector) return false;
    return Boolean(document.querySelector(`script[src="${src}"]`));
  }

  function requestBundleScripts() {
    if (isUiBundleLoaded() || typeof document === 'undefined') return;
    const missingScripts = uiBundleScripts.filter((src) => !hasScript(src));
    if (!missingScripts.length) return;
    if (document.readyState === 'loading' && typeof document.write === 'function') {
      document.write(missingScripts.map((src) => `<script src="${src}"></script>`).join(''));
      return;
    }
    if (!document.head || !document.createElement) return;
    missingScripts.forEach((src) => {
      const script = document.createElement('script');
      script.src = src;
      script.async = false;
      document.head.appendChild(script);
    });
  }

  function pageUrl(path, params, options) {
    return typeof buildPageUrl === 'function' ? buildPageUrl(path, params, options) : path;
  }

  function missingFunction(name) {
    return function missingSharedUiFunction() {
      throw new Error(`Shared UI bundle function ${name} is not loaded`);
    };
  }

  function escapeText(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderBridgeFallback(items = []) {
    if (!Array.isArray(items) || items.length === 0) return '';
    return `
      <div class="page-bridge">
        ${items.map((item) => `
          <a href="${pageUrl(item.path || '/', item.params || {}, item.options || {})}" class="page-bridge-link${item.active ? ' active' : ''}">
            <span class="page-bridge-kicker">${item.kicker || ''}</span>
            <span class="page-bridge-label">${item.label || ''}</span>
            <span class="page-bridge-copy">${item.copy || ''}</span>
          </a>
        `).join('')}
      </div>
    `;
  }

  function isReviewPage(activePage) {
    return ['/ibkr_stats.html', '/ibkr_trade_review.html', '/ibkr_lifecycle_flow.html'].includes(activePage);
  }

  function isReviewBridgePage(activePage) {
    return ['/ibkr_stats.html', '/ibkr_trade_review.html', '/ibkr_lifecycle_flow.html'].includes(activePage);
  }

  const fallbacks = {
    showToast(msg, duration = 2500) {
      if (typeof document === 'undefined') return;
      let toast = document.getElementById('toast');
      if (!toast) {
        toast = document.createElement('div');
        toast.id = 'toast';
        toast.className = 'toast';
        document.body.appendChild(toast);
      }
      if (toast._hideTimer) clearTimeout(toast._hideTimer);
      toast.textContent = msg;
      toast.classList.add('show');
      toast._hideTimer = setTimeout(() => toast.classList.remove('show'), duration);
    },
    renderNav(activePage) {
      const pages = [
        { path: '/index.html', aliases: ['/', '/index.html'], icon: '🏠', label: '首页' },
        { path: '/ibkr_signals.html', aliases: ['/ibkr_signals.html', '/ibkr_execution_actions.html', '/orders.html', '/ibkr_order_details.html', '/ibkr_account.html'], icon: '📡', label: '执行' },
        { path: '/ibkr_screener.html', aliases: ['/ibkr_screener.html', '/ibkr_watchlist.html', '/ibkr_targets.html'], icon: '🎯', label: '标的' },
        { path: '/ibkr_stats.html', aliases: ['/ibkr_stats.html', '/ibkr_trade_review.html', '/ibkr_lifecycle_flow.html'], icon: '🧾', label: '复盘' },
        { path: '/ibkr_system.html', aliases: ['/ibkr_system.html', '/ibkr_runtime.html', '/ibkr_config.html', '/ibkr_monitor.html', '/ibkr_warmup.html', '/ibkr_data_quality.html', '/ibkr_history_rebuild.html', '/ibkr_system_logic.html'], icon: '🖥️', label: '系统' }
      ];
      return `
        <div class="nav" style="--nav-count:${pages.length}">
          ${pages.map((page) => `
            <a href="${pageUrl(page.path)}" class="nav-item ${(page.path === activePage || (Array.isArray(page.aliases) && page.aliases.includes(activePage))) ? 'active' : ''}">
              <span class="nav-icon">${page.icon}</span>${page.label}
            </a>
          `).join('')}
        </div>
      `;
    },
    renderPageBridge: renderBridgeFallback,
    renderSystemBridge(activePage) {
      return global.renderPageBridge([
        { path: '/ibkr_system.html', kicker: 'Overview', label: '总览', copy: 'TV / IBKR / runtime', active: activePage === '/ibkr_system.html' },
        { path: '/ibkr_runtime.html', kicker: 'Console', label: '控制台', copy: 'Gateway / 2FA', active: activePage === '/ibkr_runtime.html' },
        { path: '/ibkr_config.html', kicker: 'Config', label: '配置', copy: '环境配置', options: { allowGlobal: true }, active: activePage === '/ibkr_config.html' }
      ]);
    },
    renderReviewBridge(activePage, params = {}) {
      const bridgeParams = params && typeof params === 'object' ? params : {};
      const cleanParams = (targetPath) => {
        const result = {};
        Object.entries(bridgeParams).forEach(([key, value]) => {
          if (value !== undefined && value !== null && value !== '' && !['limit', 'include_events'].includes(key)) {
            result[key] = value;
          }
        });
        const dateValue = result.date || result.market_date || '';
        if (targetPath === '/ibkr_trade_review.html') {
          if (dateValue) result.market_date = dateValue;
        } else if (dateValue) {
          result.date = dateValue;
          delete result.market_date;
        }
        return result;
      };
      return global.renderPageBridge([
        { path: '/ibkr_stats.html', params: cleanParams('/ibkr_stats.html'), kicker: 'PnL', label: '收益统计', copy: '收益 / 信号 / 订单', active: activePage === '/ibkr_stats.html' },
        { path: '/ibkr_trade_review.html', params: cleanParams('/ibkr_trade_review.html'), kicker: 'Daily Review', label: '每日复盘', copy: '原因 / 问题', active: activePage === '/ibkr_trade_review.html' },
        { path: '/ibkr_lifecycle_flow.html', params: cleanParams('/ibkr_lifecycle_flow.html'), kicker: 'Lifecycle', label: '生命周期', copy: '流程 / 事件', active: activePage === '/ibkr_lifecycle_flow.html' }
      ]);
    },
    renderExecutionBridge(activePage, params = {}) {
      if (isReviewPage(activePage)) {
        return global.renderReviewBridge(activePage, params);
      }
      const bridgeParams = params && typeof params === 'object' ? params : {};
      return global.renderPageBridge([
        { path: '/ibkr_signals.html', params: bridgeParams, kicker: 'Signals', label: '主信号', copy: '确认 / 执行', active: activePage === '/ibkr_signals.html' },
        { path: '/ibkr_execution_actions.html', params: bridgeParams, kicker: 'Actions', label: '执行动作', copy: '平仓 / 调整', active: ['/ibkr_execution_actions.html'].includes(activePage) },
        { path: '/orders.html', params: bridgeParams, kicker: 'Orders', label: '订单', copy: '状态 / 操作', active: activePage === '/orders.html' || activePage === '/ibkr_order_details.html' },
        { path: '/ibkr_account.html', params: bridgeParams, kicker: 'Account', label: '账户', copy: '净值 / 持仓', active: activePage === '/ibkr_account.html' }
      ]);
    },
    renderAnalyticsBridge(activePage, options = {}) {
      if (activePage === '/ibkr_stats.html') {
        return global.renderReviewBridge(activePage, options && typeof options.reviewParams === 'object' ? options.reviewParams : {});
      }
      return global.renderPageBridge([
        { path: '/ibkr_indicators.html', params: options && typeof options.indicatorParams === 'object' ? options.indicatorParams : {}, kicker: 'Indicators', label: '指标列表', copy: '技术快照 / Trace', active: activePage === '/ibkr_indicators.html' },
        { path: '/ibkr_chart.html', params: options && typeof options.chartParams === 'object' ? options.chartParams : {}, kicker: 'Chart', label: '图表工作台', copy: 'K 线 / 诊断', active: activePage === '/ibkr_chart.html' },
        { path: '/ibkr_stats.html', params: options && typeof options.statsParams === 'object' ? options.statsParams : {}, kicker: 'PnL', label: '收益统计', copy: '收益 / 信号 / 订单', active: activePage === '/ibkr_stats.html' }
      ]);
    },
    renderHomeBridge() {
      return global.renderPageBridge([
        { path: '/ibkr_signals.html', kicker: 'Execution', label: '执行域', copy: '信号 / 订单' },
        { path: '/ibkr_screener.html', params: { tab: 'screener', view: 'current' }, kicker: 'Targets', label: '标的域', copy: '筛选 / 标池' },
        { path: '/ibkr_system.html', kicker: 'System', label: '系统域', copy: '总览 / 控制台 / 配置' }
      ]);
    },
    renderOpsBridge(activePage) {
      return global.renderSystemBridge(activePage);
    },
    renderBacktestsBridge(activePage) {
      return global.renderPageBridge([
        { path: '/ibkr_signals.html', kicker: 'TV Webhook', label: '信号执行', copy: '预警 / 开仓 / 平仓', active: activePage === '/ibkr_signals.html' },
        { path: '/ibkr_screener.html', params: { tab: 'targets', view: 'current' }, kicker: 'Targets', label: '筛选与标池', copy: '筛标 / 目标', active: activePage === '/ibkr_screener.html' },
        { path: '/ibkr_system.html', kicker: 'System', label: '系统总览', copy: '配置 / 运行态', active: activePage === '/ibkr_system.html' }
      ]);
    },
    getCommonStyles() {
      return '<link rel="stylesheet" href="/assets/css/common.css?v=20260603-main-nav-no-backtest-v1">';
    },
    showConfirmDialog(options = {}) {
      const message = options.message || options.title || '确认操作？';
      return Promise.resolve(typeof confirm !== 'function' || confirm(message));
    },
    getIbkrExtraObject(record) {
      return record && typeof record.extra === 'object' && record.extra ? record.extra : {};
    },
    escapePageUiText: escapeText,
    handleLogout(event) {
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      if (typeof confirm !== 'function' || confirm('确定要登出吗？')) {
        if (typeof localStorage !== 'undefined') localStorage.removeItem('pb_token');
        if (typeof location !== 'undefined') location.href = pageUrl('/login.html', {}, { includeEnvironment: false });
      }
    },
    selectDate(date, btn) {
      if (typeof document !== 'undefined') {
        document.querySelectorAll('.date-btn').forEach((node) => node.classList.remove('active'));
        if (btn && btn.classList) btn.classList.add('active');
        const customDate = document.getElementById('customDate');
        if (customDate) customDate.value = date;
      }
      if (win && typeof win.onDateChange === 'function') win.onDateChange(date, date);
    },
    selectCustomDate(date) {
      if (typeof document !== 'undefined') {
        document.querySelectorAll('.date-btn').forEach((node) => node.classList.remove('active'));
      }
      if (win && typeof win.onDateChange === 'function') win.onDateChange(date, date);
    },
    selectDirection(direction, btn) {
      if (typeof document !== 'undefined') {
        document.querySelectorAll('.dir-tab').forEach((node) => node.classList.remove('active'));
        if (btn && btn.classList) btn.classList.add('active');
      }
      if (typeof onDirectionChange === 'function') onDirectionChange(direction);
    },
    handleRefresh() {
      if (typeof document !== 'undefined') {
        const btn = document.getElementById('refreshBtn');
        if (btn && btn.classList) {
          btn.classList.add('spinning');
          setTimeout(() => btn.classList.remove('spinning'), 600);
        }
      }
      if (typeof onRefresh === 'function') onRefresh();
    },
    handleIntervalChange(seconds) {
      if (typeof onIntervalChange === 'function') onIntervalChange(parseInt(seconds, 10));
    },
    closeIndicatorModal() {
      if (typeof document === 'undefined') return;
      const modal = document.getElementById('indicatorModal');
      if (modal) modal.classList.remove('show');
    }
  };

  const exportedNames = [
    'showToast', 'getIbkrExtraObject', 'getIbkrIntervalMs', 'getIbkrBarStartLabel',
    'getIbkrBarCloseLabel', 'getIbkrComputedTimeLabel', 'renderNav', 'handleLogout',
    'renderDatePicker', 'selectDate', 'selectCustomDate', 'renderDirectionTabs',
    'selectDirection', 'formatMarketTime', 'formatBeijingTime', 'formatRelativeTime', 'normalizeIndicatorRecord',
    'formatSignedPercentHtml', 'formatChangeTripletHtml', 'buildIndicatorBadges',
    'renderIndicatorModal', 'renderRefreshBar', 'handleRefresh', 'handleIntervalChange',
    'escapePageUiText', 'coerceRefreshDate', 'formatEtRefreshClock', 'formatEtRefreshDateTime',
    'getDateStringInTimeZone', 'getCurrentEtDateString', 'getEtDateStringFromMs',
    'shiftDateString', 'getEtDayStartMs', 'getEtDayBoundsMs', 'getEtDateRangeBoundsMs',
    'formatUtcDateTimeForPocketBase',
    'getIbkrCacheProfile', 'getMarketCalendarCacheOptions', 'invalidateIbkrDataCache',
    'cachedPageJson', 'cachedMarketCalendar',
    'buildPageRefreshTimeText', 'updatePageRefreshClockText', 'startPageRefreshClock',
    'setPageRefreshTime', 'normalizePageContextMetaItem', 'normalizePageContextMetaLabel',
    'extractPageContextDateToken', 'isPageContextTradingDateLabel', 'getPageContextUrlTradingDate',
    'getPageContextDomTradingDate', 'getPageContextFallbackTradingDate', 'resolvePageContextTradingDate',
    'resolvePageContextEnvironment', 'buildPageContextMetaItems', 'renderPageContextMeta',
    'setPageContextMeta', 'renderPageRefreshControl', 'createClientPaginationModel',
    'renderClientPaginationBar', 'renderPageTopSection',
    'renderPageLoadingOverlay', 'ensurePageLoadingOverlay', 'setPageLoading', 'withPageLoading',
    'spinPageRefreshButton', 'renderPageContextBar', 'renderPageBridge', 'renderSystemBridge',
    'renderExecutionBridge', 'renderReviewBridge', 'renderAnalyticsBridge', 'renderHomeBridge', 'renderOpsBridge',
    'renderBacktestsBridge', 'getCommonStyles', 'showLoading', 'hideLoading', 'withLoading',
    'guardedLoader', 'showConfirmDialog', 'getIndicatorModalStyles', 'renderIndicatorModalHTML',
    'showIndicatorModal', 'closeIndicatorModal'
  ];

  function install(name) {
    const fn = currentFunction(name) || fallbacks[name] || missingFunction(name);
    global[name] = fn;
    if (win) win[name] = fn;
  }

  requestBundleScripts();
  exportedNames.forEach(install);
})(globalThis);
