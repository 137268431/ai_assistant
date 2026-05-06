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
    return ['showToast', 'renderPageTopSection', 'renderBacktestsBridge', 'showLoading']
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
        { path: '/index.html', icon: '🏠', label: '首页' },
        { path: '/ibkr_signals.html', aliases: ['/ibkr_signals.html', '/ibkr_reverse_signals.html', '/orders.html', '/ibkr_order_details.html', '/ibkr_account.html'], icon: '📡', label: '执行' },
        { path: '/ibkr_screener.html', aliases: ['/ibkr_screener.html', '/ibkr_watchlist.html', '/ibkr_targets.html'], icon: '🔎', label: '标的' },
        { path: '/ibkr_chart.html', aliases: ['/ibkr_chart.html', '/ibkr_indicators.html', '/ibkr_stats.html'], icon: '📈', label: '研究' },
        { path: '/ibkr_backtests.html', icon: '🧪', label: '回测' },
        { path: '/ibkr_system.html', aliases: ['/ibkr_system.html', '/ibkr_monitor.html', '/ibkr_warmup.html', '/ibkr_data_quality.html', '/ibkr_history_rebuild.html', '/ibkr_runtime.html', '/ibkr_config.html'], icon: '🖥️', label: '系统' }
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
      const opsPages = ['/ibkr_monitor.html', '/ibkr_warmup.html', '/ibkr_data_quality.html', '/ibkr_history_rebuild.html'];
      return global.renderPageBridge([
        { path: '/ibkr_system.html', kicker: 'Overview', label: '总览', copy: '健康 / 统计', active: activePage === '/ibkr_system.html' },
        { path: '/ibkr_monitor.html', kicker: 'Ops', label: '运维', copy: '监控 / 排障', active: opsPages.includes(activePage) },
        { path: '/ibkr_runtime.html', kicker: 'Console', label: '控制台', copy: '启动 / 2FA', active: activePage === '/ibkr_runtime.html' },
        { path: '/ibkr_config.html', kicker: 'Config', label: '配置', copy: '环境配置', options: { allowGlobal: true }, active: activePage === '/ibkr_config.html' }
      ]);
    },
    renderExecutionBridge(activePage, params = {}) {
      const bridgeParams = params && typeof params === 'object' ? params : {};
      return global.renderPageBridge([
        { path: '/ibkr_signals.html', params: bridgeParams, kicker: 'Signals', label: '主信号', copy: '确认 / 执行', active: activePage === '/ibkr_signals.html' },
        { path: '/ibkr_reverse_signals.html', params: bridgeParams, kicker: 'Reverse', label: '反转信号', copy: '平仓 / 调整', active: activePage === '/ibkr_reverse_signals.html' },
        { path: '/orders.html', params: bridgeParams, kicker: 'Orders', label: '订单', copy: '状态 / 操作', active: activePage === '/orders.html' || activePage === '/ibkr_order_details.html' },
        { path: '/ibkr_account.html', params: bridgeParams, kicker: 'Account', label: '账户', copy: '净值 / 持仓', active: activePage === '/ibkr_account.html' }
      ]);
    },
    renderAnalyticsBridge(activePage, options = {}) {
      const chartParams = options && typeof options.chartParams === 'object' && options.chartParams ? options.chartParams : {};
      return global.renderPageBridge([
        { path: '/ibkr_indicators.html', kicker: 'Indicators', label: '指标列表', copy: '快览 / 指标', active: activePage === '/ibkr_indicators.html' },
        { path: '/ibkr_chart.html', params: chartParams, kicker: 'Chart', label: '图表工作台', copy: '图表 / 信号', active: activePage === '/ibkr_chart.html' },
        { path: '/ibkr_stats.html', kicker: 'Stats', label: '统计', copy: '收益 / 执行', active: activePage === '/ibkr_stats.html' }
      ]);
    },
    renderHomeBridge() {
      return global.renderPageBridge([
        { path: '/ibkr_signals.html', kicker: 'Execution', label: '执行域', copy: '信号 / 订单' },
        { path: '/ibkr_screener.html', params: { tab: 'screener', view: 'current' }, kicker: 'Targets', label: '标的域', copy: '筛选 / 标池' },
        { path: '/ibkr_chart.html', kicker: 'Research', label: '研究域', copy: '图表 / 统计' },
        { path: '/ibkr_backtests.html', kicker: 'Backtest', label: '回测域', copy: '回放 / 验证' },
        { path: '/ibkr_system.html', kicker: 'System', label: '系统域', copy: '总览 / 运维 / 配置' }
      ]);
    },
    renderOpsBridge(activePage) {
      const opsBridge = global.renderPageBridge([
        { path: '/ibkr_monitor.html', kicker: 'Dashboard', label: '监控大盘', copy: '请求 / 订阅 / 主机', active: activePage === '/ibkr_monitor.html' },
        { path: '/ibkr_warmup.html', kicker: 'Warmup', label: '预热', copy: 'startup / gate', active: activePage === '/ibkr_warmup.html' },
        { path: '/ibkr_data_quality.html', kicker: 'Quality', label: '数据质量', copy: '缺口 / 修复', active: activePage === '/ibkr_data_quality.html' },
        { path: '/ibkr_history_rebuild.html', kicker: 'Rebuild', label: '历史重建', copy: '高级恢复', active: activePage === '/ibkr_history_rebuild.html' }
      ]);
      return `${global.renderSystemBridge(activePage)}${opsBridge}`;
    },
    renderBacktestsBridge(activePage) {
      return global.renderPageBridge([
        { path: '/ibkr_backtests.html', kicker: 'Replay', label: '回测工坊', copy: '运行 / replay', active: activePage === '/ibkr_backtests.html' },
        { path: '/ibkr_chart.html', kicker: 'Chart', label: '图表工作台', copy: 'bars / 指标', active: activePage === '/ibkr_chart.html' },
        { path: '/ibkr_signals.html', kicker: 'Signals', label: '主信号', copy: 'live 对照', active: activePage === '/ibkr_signals.html' },
        { path: '/ibkr_screener.html', params: { tab: 'targets', view: 'current' }, kicker: 'Targets', label: '筛选与标池', copy: '筛标 / 目标', active: activePage === '/ibkr_screener.html' }
      ]);
    },
    getCommonStyles() {
      return '<link rel="stylesheet" href="/assets/css/common.css">';
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
    'selectDirection', 'formatBeijingTime', 'formatRelativeTime', 'normalizeIndicatorRecord',
    'formatSignedPercentHtml', 'formatChangeTripletHtml', 'buildIndicatorBadges',
    'renderIndicatorModal', 'renderRefreshBar', 'handleRefresh', 'handleIntervalChange',
    'escapePageUiText', 'coerceRefreshDate', 'formatEtRefreshClock', 'formatEtRefreshDateTime',
    'buildPageRefreshTimeText', 'updatePageRefreshClockText', 'startPageRefreshClock',
    'setPageRefreshTime', 'normalizePageContextMetaItem', 'normalizePageContextMetaLabel',
    'extractPageContextDateToken', 'isPageContextTradingDateLabel', 'getPageContextUrlTradingDate',
    'getPageContextDomTradingDate', 'getPageContextFallbackTradingDate', 'resolvePageContextTradingDate',
    'resolvePageContextEnvironment', 'buildPageContextMetaItems', 'renderPageContextMeta',
    'setPageContextMeta', 'renderPageRefreshControl', 'renderPageTopSection',
    'renderPageLoadingOverlay', 'ensurePageLoadingOverlay', 'setPageLoading', 'withPageLoading',
    'spinPageRefreshButton', 'renderPageContextBar', 'renderPageBridge', 'renderSystemBridge',
    'renderExecutionBridge', 'renderAnalyticsBridge', 'renderHomeBridge', 'renderOpsBridge',
    'renderBacktestsBridge', 'getCommonStyles', 'showLoading', 'hideLoading', 'withLoading',
    'guardedLoader', 'getIndicatorModalStyles', 'renderIndicatorModalHTML', 'showIndicatorModal',
    'closeIndicatorModal'
  ];

  function install(name) {
    const fn = currentFunction(name) || fallbacks[name] || missingFunction(name);
    global[name] = fn;
    if (win) win[name] = fn;
  }

  requestBundleScripts();
  exportedNames.forEach(install);
})(globalThis);
