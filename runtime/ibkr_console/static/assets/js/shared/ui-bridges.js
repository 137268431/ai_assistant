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
  const opsPages = [
    '/ibkr_monitor.html',
    '/ibkr_warmup.html',
    '/ibkr_data_quality.html',
    '/ibkr_history_rebuild.html'
  ];
  return renderPageBridge([
    {
      path: '/ibkr_system.html',
      kicker: 'Overview',
      label: '总览',
      copy: '健康 / 统计',
      active: activePage === '/ibkr_system.html'
    },
    {
      path: '/ibkr_monitor.html',
      kicker: 'Ops',
      label: '运维',
      copy: '监控 / 排障',
      active: opsPages.includes(activePage)
    },
    {
      path: '/ibkr_system_logic.html',
      kicker: 'Logic',
      label: '逻辑',
      copy: '规则 / 调度',
      active: activePage === '/ibkr_system_logic.html'
    },
    {
      path: '/ibkr_runtime.html',
      kicker: 'Console',
      label: '控制台',
      copy: '启动 / 2FA',
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
      path: '/ibkr_reverse_signals.html',
      params: bridgeParams,
      kicker: 'Reverse',
      label: '反转信号',
      copy: '平仓 / 调整',
      active: activePage === '/ibkr_reverse_signals.html'
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
  const chartParams = options && typeof options.chartParams === 'object' && options.chartParams
    ? options.chartParams
    : {};

  return renderPageBridge([
    {
      path: '/ibkr_indicators.html',
      kicker: 'Indicators',
      label: '指标列表',
      copy: '快览 / 指标',
      active: activePage === '/ibkr_indicators.html'
    },
    {
      path: '/ibkr_chart.html',
      params: chartParams,
      kicker: 'Chart',
      label: '图表工作台',
      copy: '图表 / 信号',
      active: activePage === '/ibkr_chart.html'
    },
    {
      path: '/ibkr_stats.html',
      kicker: 'Stats',
      label: '统计',
      copy: '收益 / 执行',
      active: activePage === '/ibkr_stats.html'
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
      path: '/ibkr_chart.html',
      kicker: 'Research',
      label: '研究域',
      copy: '图表 / 统计'
    },
    {
      path: '/ibkr_backtests.html',
      kicker: 'Backtest',
      label: '回测域',
      copy: '回放 / 验证'
    },
    {
      path: '/ibkr_system.html',
      kicker: 'System',
      label: '系统域',
      copy: '总览 / 运维 / 配置'
    }
  ]);
}

function renderOpsBridge(activePage) {
  const opsBridge = renderPageBridge([
    {
      path: '/ibkr_monitor.html',
      kicker: 'Dashboard',
      label: '监控大盘',
      copy: '请求 / 订阅 / 主机',
      active: activePage === '/ibkr_monitor.html'
    },
    {
      path: '/ibkr_warmup.html',
      kicker: 'Warmup',
      label: '预热',
      copy: 'startup / gate',
      active: activePage === '/ibkr_warmup.html'
    },
    {
      path: '/ibkr_data_quality.html',
      kicker: 'Quality',
      label: '数据质量',
      copy: '缺口 / 修复',
      active: activePage === '/ibkr_data_quality.html'
    },
    {
      path: '/ibkr_history_rebuild.html',
      kicker: 'Rebuild',
      label: '历史重建',
      copy: '高级恢复',
      active: activePage === '/ibkr_history_rebuild.html'
    }
  ]);
  return `${renderSystemBridge(activePage)}${opsBridge}`;
}

function renderBacktestsBridge(activePage) {
  return renderPageBridge([
    {
      path: '/ibkr_backtests.html',
      kicker: 'Replay',
      label: '回测工坊',
      copy: '运行 / replay',
      active: activePage === '/ibkr_backtests.html'
    },
    {
      path: '/ibkr_chart.html',
      kicker: 'Chart',
      label: '图表工作台',
      copy: 'bars / 指标',
      active: activePage === '/ibkr_chart.html'
    },
    {
      path: '/ibkr_signals.html',
      kicker: 'Signals',
      label: '主信号',
      copy: 'live 对照',
      active: activePage === '/ibkr_signals.html'
    },
    {
      path: '/ibkr_screener.html',
      params: { tab: 'targets', view: 'current' },
      kicker: 'Targets',
      label: '筛选与标池',
      copy: '筛标 / 目标',
      active: activePage === '/ibkr_screener.html'
    }
  ]);
}
