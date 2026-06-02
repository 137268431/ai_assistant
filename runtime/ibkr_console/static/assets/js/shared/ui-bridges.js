function renderPageBridge(items = []) {
  if (!Array.isArray(items) || items.length === 0) return '';
  return `
    <div class="page-bridge">
      ${items.map((item) => {
        const href = buildPageUrl(item.path || '/', item.params || {}, item.options || {});
        return `
          <a href="${href}" class="page-bridge-link${item.active ? ' active' : ''}">
            <span class="page-bridge-kicker">${item.kicker || ''}</span>
            <span class="page-bridge-label">${item.label || ''}</span>
            <span class="page-bridge-copy">${item.copy || ''}</span>
          </a>
        `;
      }).join('')}
    </div>
  `;
}

function renderSystemBridge(activePage) {
  return renderPageBridge([
    {
      path: '/ibkr_system.html',
      kicker: 'Overview',
      label: '总览',
      copy: 'TV / IBKR / runtime',
      active: activePage === '/ibkr_system.html'
    },
    {
      path: '/ibkr_runtime.html',
      kicker: 'Console',
      label: '控制台',
      copy: 'Gateway / 2FA',
      active: activePage === '/ibkr_runtime.html'
    },
    {
      path: '/ibkr_config.html',
      kicker: 'Config',
      label: '配置',
      copy: '环境配置',
      options: { allowGlobal: true },
      active: activePage === '/ibkr_config.html'
    }
  ]);
}

function renderExecutionBridge(activePage, params = {}) {
  const bridgeParams = params && typeof params === 'object' ? params : {};
  return renderPageBridge([
    {
      path: '/ibkr_signals.html',
      params: bridgeParams,
      kicker: 'Signals',
      label: '主信号',
      copy: '确认 / 执行',
      active: activePage === '/ibkr_signals.html'
    },
    {
      path: '/ibkr_execution_actions.html',
      params: bridgeParams,
      kicker: 'Actions',
      label: '执行动作',
      copy: '平仓 / 调整',
      active: ['/ibkr_execution_actions.html'].includes(activePage)
    },
    {
      path: '/orders.html',
      params: bridgeParams,
      kicker: 'Orders',
      label: '订单',
      copy: '状态 / 操作',
      active: activePage === '/orders.html' || activePage === '/ibkr_order_details.html'
    },
    {
      path: '/ibkr_lifecycle_flow.html',
      params: bridgeParams,
      kicker: 'Lifecycle',
      label: '生命周期',
      copy: '流程 / 事件',
      active: activePage === '/ibkr_lifecycle_flow.html'
    },
    {
      path: '/ibkr_trade_review.html',
      params: bridgeParams,
      kicker: 'Review',
      label: '每日复盘',
      copy: '原因 / 问题',
      active: activePage === '/ibkr_trade_review.html'
    },
    {
      path: '/ibkr_account.html',
      params: bridgeParams,
      kicker: 'Account',
      label: '账户',
      copy: '净值 / 持仓',
      active: activePage === '/ibkr_account.html'
    }
  ]);
}

function renderAnalyticsBridge(activePage, options = {}) {
  return renderPageBridge([
    {
      path: '/ibkr_signals.html',
      params: options && typeof options.signalParams === 'object' ? options.signalParams : {},
      kicker: 'TV Webhook',
      label: '信号执行',
      copy: '预警 / 开仓 / 平仓',
      active: activePage === '/ibkr_signals.html'
    },
    {
      path: '/ibkr_screener.html',
      params: { tab: 'screener', view: 'current' },
      kicker: 'Targets',
      label: '今日标的',
      copy: '活跃度 / 标池',
      active: activePage === '/ibkr_screener.html'
    }
  ]);
}

function renderHomeBridge() {
  return renderPageBridge([
    {
      path: '/ibkr_signals.html',
      kicker: 'Execution',
      label: '执行域',
      copy: '信号 / 订单 / 生命周期'
    },
    {
      path: '/ibkr_screener.html',
      params: { tab: 'screener', view: 'current' },
      kicker: 'Targets',
      label: '标的域',
      copy: '筛选 / 标池'
    },
    {
      path: '/ibkr_system.html',
      kicker: 'System',
      label: '系统域',
      copy: '总览 / 控制台 / 配置'
    }
  ]);
}

function renderOpsBridge(activePage) {
  return renderSystemBridge(activePage);
}

function renderBacktestsBridge(activePage) {
  return renderPageBridge([
    {
      path: '/ibkr_signals.html',
      kicker: 'TV Webhook',
      label: '信号执行',
      copy: '预警 / 开仓 / 平仓',
      active: activePage === '/ibkr_signals.html'
    },
    {
      path: '/ibkr_screener.html',
      params: { tab: 'targets', view: 'current' },
      kicker: 'Targets',
      label: '筛选与标池',
      copy: '筛标 / 目标',
      active: activePage === '/ibkr_screener.html'
    },
    {
      path: '/ibkr_system.html',
      kicker: 'System',
      label: '系统总览',
      copy: '配置 / 运行态',
      active: activePage === '/ibkr_system.html'
    }
  ]);
}
