// ── Toast 通知 ──
function showToast(msg, duration = 2500) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  if (toast._hideTimer) {
    clearTimeout(toast._hideTimer);
    toast._hideTimer = null;
  }
  toast.textContent = msg;
  toast.classList.add('show');
  toast._hideTimer = setTimeout(() => {
    toast.classList.remove('show');
    toast._hideTimer = null;
  }, duration);
}

// ── 底部导航 ──
function renderNav(activePage) {
  const pages = [
    { path: '/index.html', icon: '🏠', label: '首页' },
    { path: '/ibkr_signals.html', aliases: ['/ibkr_signals.html', '/ibkr_reverse_signals.html', '/orders.html', '/ibkr_order_details.html', '/ibkr_account.html'], icon: '📡', label: '执行' },
    { path: '/ibkr_screener.html', aliases: ['/ibkr_screener.html', '/ibkr_watchlist.html', '/ibkr_targets.html'], icon: '🔎', label: '标的' },
    { path: '/ibkr_chart.html', aliases: ['/ibkr_chart.html', '/ibkr_indicators.html', '/ibkr_stats.html'], icon: '📈', label: '研究' },
    { path: '/ibkr_backtests.html', icon: '🧪', label: '回测' },
    { path: '/ibkr_monitor.html', aliases: ['/ibkr_monitor.html', '/ibkr_warmup.html', '/ibkr_data_quality.html', '/ibkr_history_rebuild.html'], icon: '🛠️', label: '运维' },
    { path: '/ibkr_system.html', aliases: ['/ibkr_system.html', '/ibkr_runtime.html', '/ibkr_config.html'], icon: '🖥️', label: '系统' }
  ];

  return `
    <div class="nav" style="--nav-count:${pages.length}">
      ${pages.map(p => `
        <a href="${buildPageUrl(p.path)}" class="nav-item ${(p.path === activePage || (Array.isArray(p.aliases) && p.aliases.includes(activePage))) ? 'active' : ''}">
          <span class="nav-icon">${p.icon}</span>${p.label}
        </a>
      `).join('')}
    </div>
  `;
}

// ── 登出功能 ──
window.handleLogout = function(event) {
  event.preventDefault();
  if (confirm('确定要登出吗？')) {
    localStorage.removeItem('pb_token');
    location.href = buildPageUrl('/login.html', {}, { includeEnvironment: false });
  }
};

// ── 日期选择器 ──
function renderDatePicker(onChange) {
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  const formatDate = (d) => d.toISOString().split('T')[0];

  return `
    <div class="date-picker">
      <div class="date-shortcuts">
        <button class="date-btn" data-date="${formatDate(yesterday)}" onclick="selectDate('${formatDate(yesterday)}', this)">昨天</button>
        <button class="date-btn active" data-date="${formatDate(today)}" onclick="selectDate('${formatDate(today)}', this)">今天</button>
      </div>
      <input type="date" class="date-input" id="customDate" value="${formatDate(today)}" onchange="selectCustomDate(this.value)">
    </div>
  `;
}

// 日期选择回调（需要在页面中定义 window.onDateChange）
window.selectDate = function(date, btn) {
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('customDate').value = date;
  if (typeof window.onDateChange === 'function') {
    window.onDateChange(date, date);
  }
};


window.selectCustomDate = function(date) {
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  if (typeof window.onDateChange === 'function') {
    window.onDateChange(date, date);
  }
};

// ── 多空方向筛选 ──
function renderDirectionTabs(onChange) {
  return `
    <div class="direction-tabs">
      <button class="dir-tab active" data-dir="all" onclick="selectDirection('all', this)">全部<span class="tab-count" data-dir="all">(<span class="count-value">0</span>)</span></button>
      <button class="dir-tab" data-dir="long" onclick="selectDirection('long', this)">做多<span class="tab-count" data-dir="long">(<span class="count-value">0</span>)</span></button>
      <button class="dir-tab" data-dir="short" onclick="selectDirection('short', this)">做空<span class="tab-count" data-dir="short">(<span class="count-value">0</span>)</span></button>
    </div>
  `;
}

window.selectDirection = function(direction, btn) {
  document.querySelectorAll('.dir-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (typeof onDirectionChange === 'function') {
    onDirectionChange(direction);
  }
};

// ── 时间格式化（UTC → 北京时间）──
function formatBeijingTime(utcTimeString, format = 'datetime') {
  if (!utcTimeString) return '-';

  const date = new Date(utcTimeString);

  // 检查是否为有效日期
  if (isNaN(date.getTime())) return '-';

  const options = {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  };

  if (format === 'date') {
    // 只显示日期: 2025-03-15
    return date.toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
  } else if (format === 'time') {
    // 只显示时间: 14:30:45
    return date.toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
  } else if (format === 'short') {
    // 短格式: 03-15 14:30
    const dateStr = date.toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
    const timeStr = date.toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
    return `${dateStr} ${timeStr}`;
  } else {
    // 完整格式: 2025-03-15 14:30:45
    const dateStr = date.toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
    const timeStr = date.toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
    return `${dateStr} ${timeStr}`;
  }
}

// ── 相对时间（多久之前）──
function formatRelativeTime(utcTimeString) {
  if (!utcTimeString) return '-';

  const date = new Date(utcTimeString);
  if (isNaN(date.getTime())) return '-';

  const now = new Date();
  const diffMs = now - date;
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);

  if (diffSec < 60) return `${diffSec}秒前`;
  if (diffMin < 60) return `${diffMin}分钟前`;
  if (diffHour < 24) return `${diffHour}小时前`;
  if (diffDay < 7) return `${diffDay}天前`;

  // 超过7天显示完整日期
  return formatBeijingTime(utcTimeString, 'short');
}

// ── 标准化指标记录（直接返回原生数据）──
function normalizeIndicatorRecord(record) {
  if (!record) return null;

  // 处理 extra 可能是 JSON 字符串的情况，解析后展开到 record 中
  if (typeof record.extra === 'string') {
    try {
      record.extra = JSON.parse(record.extra);
    } catch (e) {
      record.extra = {};
    }
  }

  // 把 extra 的字段展开到 record 中（保持 snake_case）
  return { ...record, ...record.extra };
}

function formatSignedPercentHtml(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '<span style="color: var(--muted)">--</span>';
  const color = num > 0 ? 'var(--long)' : num < 0 ? 'var(--short)' : 'var(--muted)';
  const sign = num > 0 ? '+' : '';
  return `<span style="color: ${color}">${sign}${num.toFixed(2)}%</span>`;
}

function formatChangeTripletHtml(dayChangePct, prevCloseChangePct, change7d) {
  return [
    formatSignedPercentHtml(dayChangePct),
    formatSignedPercentHtml(prevCloseChangePct),
    formatSignedPercentHtml(change7d),
  ].join(' / ');
}

// ── 构建技术指标徽章 HTML ──
function buildIndicatorBadges(signal, latestIndicator) {
  const badges = [];

  // 涨幅徽章（从最新指标数据获取）
  const indicator = mergeIndicatorWithRealtimeQuote(latestIndicator || {}) || {};
  const dayChangePct = indicator.display_day_change_pct ?? indicator.dayChangePct ?? indicator.day_change_pct ?? 0;
  const prevCloseChangePct = indicator.display_prev_close_change_pct ?? indicator.prevCloseChangePct ?? indicator.prev_close_change_pct ?? 0;
  const change7d = indicator.display_change_7d ?? indicator.change7d ?? indicator.change_7d ?? 0;

  if (dayChangePct !== 0) {
    const changeClass = dayChangePct > 0 ? 'badge-change-up' : 'badge-change-down';
    const changeSymbol = dayChangePct > 0 ? '+' : '';
    badges.push(`<span class="indicator-badge ${changeClass}">日内${changeSymbol}${dayChangePct.toFixed(2)}%</span>`);
  }
  if (prevCloseChangePct !== 0) {
    const changeClass = prevCloseChangePct > 0 ? 'badge-change-up' : 'badge-change-down';
    const changeSymbol = prevCloseChangePct > 0 ? '+' : '';
    badges.push(`<span class="indicator-badge ${changeClass}">较昨${changeSymbol}${prevCloseChangePct.toFixed(2)}%</span>`);
  }
  if (change7d !== 0) {
    const changeClass = change7d > 0 ? 'badge-change-up' : 'badge-change-down';
    const changeSymbol = change7d > 0 ? '+' : '';
    badges.push(`<span class="indicator-badge ${changeClass}">7日${changeSymbol}${change7d.toFixed(2)}%</span>`);
  }

  // 技术指标徽章
  if (latestIndicator) {
    // 背离信号
    if (latestIndicator.crsi_bull_div || latestIndicator.obv_bull_div) {
      badges.push(`<span class="indicator-badge badge-div">多头背离</span>`);
    }
    if (latestIndicator.crsi_bear_div || latestIndicator.obv_bear_div) {
      badges.push(`<span class="indicator-badge badge-div">空头背离</span>`);
    }
    // 分形信号
    if (latestIndicator.fractal_bull) {
      badges.push(`<span class="indicator-badge badge-fractal">分形↑</span>`);
    }
    if (latestIndicator.fractal_bear) {
      badges.push(`<span class="indicator-badge badge-fractal">分形↓</span>`);
    }
    // EMA触及
    if (latestIndicator.ema_bull_touch) {
      badges.push(`<span class="indicator-badge badge-ema">EMA触及↑</span>`);
    }
    if (latestIndicator.ema_bear_touch) {
      badges.push(`<span class="indicator-badge badge-ema">EMA触及↓</span>`);
    }
    // EMA 多头/空头状态
    if (latestIndicator.ema_bullish) {
      badges.push(`<span class="indicator-badge badge-ema">EMA 多头</span>`);
    }
    if (latestIndicator.ema_bearish) {
      badges.push(`<span class="indicator-badge badge-ema">EMA 空头</span>`);
    }
  }

  return badges.join('');
}

// ── 渲染技术指标详情浮层 ──
function renderIndicatorModal(latestIndicator) {
  if (!latestIndicator) return '<div class="modal-section">暂无技术指标数据</div>';
  const indicatorView = mergeIndicatorWithRealtimeQuote(latestIndicator) || latestIndicator;

  // 时间信息 section（放在最上面）
  const timeSection = `
    <div class="modal-section" style="background: var(--surface2); border-radius: 8px; padding: 12px; margin-bottom: 16px;">
      <div class="modal-section-title">⏰ 时间信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">美国时间</div>
          <div class="modal-value">${latestIndicator.us_time || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">中国时间</div>
          <div class="modal-value">${latestIndicator.cn_time || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Bar时间戳</div>
          <div class="modal-value">${latestIndicator.bar_time_ms ? formatBarTimeMsToET(latestIndicator.bar_time_ms) : '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">创建时间</div>
          <div class="modal-value">${latestIndicator.created ? formatBeijingTime(latestIndicator.created) : '-'}</div>
        </div>
      </div>
    </div>
  `;

  return `
    ${timeSection}
    <div class="modal-section">
      <div class="modal-section-title">价格信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">收盘价</div>
          <div class="modal-value">$${(indicatorView.display_close || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">最高价</div>
          <div class="modal-value">$${(latestIndicator.high || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">最低价</div>
          <div class="modal-value">$${(latestIndicator.low || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">开盘价</div>
          <div class="modal-value">$${(latestIndicator.open || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">涨幅(当日/昨日/7日)</div>
          <div class="modal-value">
            ${formatChangeTripletHtml(
              indicatorView.display_day_change_pct || 0,
              indicatorView.display_prev_close_change_pct || 0,
              indicatorView.display_change_7d || 0
            )}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR波动率</div>
          <div class="modal-value" style="color: ${latestIndicator.atr_pct >= 3 ? '#e53e3e' : latestIndicator.atr_pct >= 1.5 ? '#ed8936' : '#48bb78'}">
            ${latestIndicator.atr_pct >= 3 ? '⚡高' : latestIndicator.atr_pct >= 1.5 ? '〜中' : '·低'} ${(latestIndicator.atr_pct || 0).toFixed(2)}%
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">EMA 均线</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">EMA Fast (20)</div>
          <div class="modal-value">$${(latestIndicator.ema_fast || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Slow (50)</div>
          <div class="modal-value">$${(latestIndicator.ema_slow || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Trend (100)</div>
          <div class="modal-value">$${(latestIndicator.ema_trend || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Longest (200)</div>
          <div class="modal-value">$${(latestIndicator.ema_longest || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">趋势方向</div>
          <div class="modal-value">${latestIndicator.trend_dir === 1 ? '📈 多头' : latestIndicator.trend_dir === -1 ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA状态</div>
          <div class="modal-value">${latestIndicator.ema_bullish ? '📈 多头' : latestIndicator.ema_bearish ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA触及</div>
          <div class="modal-value">
            ${latestIndicator.ema_bull_touch ? '✅ 多头触及' : latestIndicator.ema_bear_touch ? '✅ 空头触及' : '❌ 无'}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Slow斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slope_slow > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slope_slow > 0 ? '+' : ''}${(latestIndicator.slope_slow || 0).toFixed(4)}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Trend斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slope_trend > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slope_trend > 0 ? '+' : ''}${(latestIndicator.slope_trend || 0).toFixed(4)}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Longest斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slope_longest > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slope_longest > 0 ? '+' : ''}${(latestIndicator.slope_longest || 0).toFixed(4)}
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">背离信号</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">cRSI 多头背离</div>
          <div class="modal-value">${latestIndicator.crsi_bull_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 空头背离</div>
          <div class="modal-value">${latestIndicator.crsi_bear_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 隐藏多头</div>
          <div class="modal-value">${latestIndicator.crsi_hid_bull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 隐藏空头</div>
          <div class="modal-value">${latestIndicator.crsi_hid_bear ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 多头背离</div>
          <div class="modal-value">${latestIndicator.obv_bull_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 空头背离</div>
          <div class="modal-value">${latestIndicator.obv_bear_div ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 隐藏多头</div>
          <div class="modal-value">${latestIndicator.obv_hid_bull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 隐藏空头</div>
          <div class="modal-value">${latestIndicator.obv_hid_bear ? '✅ 是' : '❌ 否'}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">分形 & 通道</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">分形多头</div>
          <div class="modal-value">${latestIndicator.fractal_bull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">分形空头</div>
          <div class="modal-value">${latestIndicator.fractal_bear ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 下轨触及</div>
          <div class="modal-value">${latestIndicator.sd_lower ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 上轨触及</div>
          <div class="modal-value">${latestIndicator.sd_upper ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 区域</div>
          <div class="modal-value">${latestIndicator.sd_zone === 1 ? '超买' : latestIndicator.sd_zone === -1 ? '超卖' : '正常'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 趋势</div>
          <div class="modal-value">${latestIndicator.sd_trend === 1 ? '📈 上升' : latestIndicator.sd_trend === -1 ? '📉 下降' : '➡️ 平坦'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 标准差</div>
          <div class="modal-value">${(latestIndicator.sd_std_dev || 0).toFixed(4)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 回归值</div>
          <div class="modal-value">${(latestIndicator.sd_reg || 0).toFixed(2)}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">DTP & RSI</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">DTP 方向</div>
          <div class="modal-value">${latestIndicator.dtp_dir === 1 ? '📈 多头' : latestIndicator.dtp_dir === -1 ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 阶段</div>
          <div class="modal-value">${latestIndicator.dtp_phase || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 阶段Bar数</div>
          <div class="modal-value">${latestIndicator.dtp_phase_bars || 0}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 平均值</div>
          <div class="modal-value">${(latestIndicator.dtp_avg || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP ATR</div>
          <div class="modal-value">${(latestIndicator.dtp_atr || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI</div>
          <div class="modal-value">${(latestIndicator.crsi || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 上界</div>
          <div class="modal-value">${(latestIndicator.crsi_ub || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 下界</div>
          <div class="modal-value">${(latestIndicator.crsi_db || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 状态</div>
          <div class="modal-value">${latestIndicator.crsi_ob ? '超买' : latestIndicator.crsi_os ? '超卖' : '正常'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV RSI</div>
          <div class="modal-value">${(latestIndicator.obv_rsi || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR</div>
          <div class="modal-value">$${(latestIndicator.atr || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR Raw</div>
          <div class="modal-value">${(latestIndicator.atr_raw || 0).toFixed(4)}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">止损管理</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">止损距离(%)</div>
          <div class="modal-value">${(latestIndicator.sl_dist_pct || 0).toFixed(2)}%</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">止损ATR比率</div>
          <div class="modal-value" style="color: ${latestIndicator.sl_atr_ratio >= 0.8 ? 'var(--long)' : 'var(--short)'}">
            ${(latestIndicator.sl_atr_ratio || 0).toFixed(2)}x ${latestIndicator.sl_atr_ratio >= 0.8 ? '✅' : '⚠️'}
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">VWAP</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">VWAP</div>
          <div class="modal-value">$${(latestIndicator.vwap || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">VWAP 偏离</div>
          <div class="modal-value" style="color: ${latestIndicator.vwap_dist > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.vwap_dist > 0 ? '+' : ''}${(latestIndicator.vwap_dist || 0).toFixed(2)}%
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">VWAP 趋势</div>
          <div class="modal-value">${latestIndicator.vwap_bullish ? '📈 多头' : '📉 空头'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">U1 上轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_upper1 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">L1 下轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_lower1 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">U2 上轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_upper2 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">L2 下轨</div>
          <div class="modal-value">$${(latestIndicator.vwap_lower2 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">成交量</div>
          <div class="modal-value">${(latestIndicator.volume || 0).toLocaleString()}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">时间信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">美东时间</div>
          <div class="modal-value">${latestIndicator.us_time || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">北京时间</div>
          <div class="modal-value">${latestIndicator.cn_time || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">K线索引</div>
          <div class="modal-value">${latestIndicator.bar_index || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">时间周期</div>
          <div class="modal-value">${latestIndicator.interval || 'N/A'}</div>
        </div>
      </div>
    </div>
  `;
}

// ── 刷新栏（刷新按钮 + 周期选择）──
function renderRefreshBar(onRefresh, onIntervalChange) {
  return `
    <div class="refresh-bar">
      <button class="refresh-btn" id="refreshBtn" onclick="handleRefresh()">↻</button>
      <select class="interval-select" id="intervalSelect" onchange="handleIntervalChange(this.value)">
        <option value="0">关闭</option>
        <option value="10">10秒</option>
        <option value="30" selected>30秒</option>
        <option value="60">60秒</option>
      </select>
    </div>
  `;
}

window.handleRefresh = function() {
  const btn = document.getElementById('refreshBtn');
  btn.classList.add('spinning');
  setTimeout(() => btn.classList.remove('spinning'), 600);
  if (typeof onRefresh === 'function') {
    onRefresh();
  }
};

window.handleIntervalChange = function(seconds) {
  if (typeof onIntervalChange === 'function') {
    onIntervalChange(parseInt(seconds));
  }
};

function escapePageUiText(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderPageRefreshControl(options = {}) {
  const refreshOptions = options && typeof options === 'object' ? options : {};
  const mode = refreshOptions.mode === 'manual' ? 'manual' : 'polling';
  const buttonId = refreshOptions.buttonId || 'refreshBtn';
  const intervalId = refreshOptions.intervalId || 'refreshInterval';
  const buttonLabel = refreshOptions.buttonLabel || '↻';
  const buttonTitle = refreshOptions.buttonTitle || '刷新';
  const buttonClass = refreshOptions.buttonClass || '';
  const selectClass = refreshOptions.selectClass || '';
  const compact = refreshOptions.compact !== false;
  const onClick = refreshOptions.onClick || 'manualRefresh()';
  const onChange = refreshOptions.onChange || 'changeRefreshInterval(this.value)';
  const defaultSeconds = String(refreshOptions.defaultSeconds ?? '30');
  const optionItems = Array.isArray(refreshOptions.options) && refreshOptions.options.length
    ? refreshOptions.options
    : [
        { value: '0', label: '关闭' },
        { value: '5', label: '5秒' },
        { value: '30', label: '30秒' },
        { value: '60', label: '1分钟' },
      ];
  const compactClass = compact ? ' is-compact' : '';
  const safeButtonClass = buttonClass ? ` ${buttonClass}` : '';
  const safeSelectClass = selectClass ? ` ${selectClass}` : '';

  return `
    <div class="refresh-control${compactClass}" data-refresh-mode="${mode}">
      <button
        class="refresh-btn page-refresh-trigger${compactClass}${safeButtonClass}"
        id="${escapePageUiText(buttonId)}"
        type="button"
        title="${escapePageUiText(buttonTitle)}"
        onclick="${escapePageUiText(onClick)}"
      >${escapePageUiText(buttonLabel)}</button>
      ${mode === 'polling' ? `
        <select
          class="refresh-select${safeSelectClass}"
          id="${escapePageUiText(intervalId)}"
          onchange="${escapePageUiText(onChange)}"
        >
          ${optionItems.map((item) => {
            const value = String(item?.value ?? '');
            const label = String(item?.label ?? value);
            return `<option value="${escapePageUiText(value)}" ${value === defaultSeconds ? 'selected' : ''}>${escapePageUiText(label)}</option>`;
          }).join('')}
        </select>
      ` : ''}
    </div>
  `;
}

function renderPageTopSection(options = {}) {
  const section = options && typeof options === 'object' ? options : {};
  const mode = section.mode === 'hero' ? 'hero' : 'compact';
  const titleHtml = section.titleHtml ?? escapePageUiText(section.title || '');
  const copyHtml = section.copyHtml ?? (section.copy ? escapePageUiText(section.copy) : '');
  const kickerHtml = section.kickerHtml ?? (section.kicker ? escapePageUiText(section.kicker) : '');
  const metaHtml = section.metaHtml || '';
  const actionsHtml = section.actionsHtml || '';
  const statusHtml = section.statusHtml || '';
  const modeClass = mode === 'hero' ? 'hero' : 'page-header';
  const titleClass = mode === 'hero' ? 'hero-title' : 'page-title';
  const copyClass = mode === 'hero' ? 'hero-copy' : 'page-copy';
  const kickerClass = mode === 'hero' ? 'hero-kicker' : 'page-kicker';
  const rowClass = mode === 'hero' ? 'hero-top' : 'page-top-section-row';
  const sideClass = mode === 'hero' ? 'hero-actions' : 'page-top-section-side';
  const metaClass = mode === 'hero' ? 'hero-meta' : 'page-top-section-meta';

  return `
    <section class="${modeClass} page-top-section" data-page-top-section="${mode}">
      <div class="${rowClass}">
        <div class="page-top-section-main">
          ${kickerHtml ? `<div class="${kickerClass}">${kickerHtml}</div>` : ''}
          <div class="${titleClass}">${titleHtml}</div>
          ${copyHtml ? `<div class="${copyClass}">${copyHtml}</div>` : ''}
        </div>
        ${(metaHtml || actionsHtml) ? `
          <div class="${sideClass}">
            ${actionsHtml ? `<div class="page-top-section-actions">${actionsHtml}</div>` : ''}
            ${metaHtml ? `<div class="${metaClass}">${metaHtml}</div>` : ''}
          </div>
        ` : ''}
      </div>
      ${statusHtml ? `<div class="page-top-section-status">${statusHtml}</div>` : ''}
    </section>
  `;
}

function renderPageLoadingOverlay(options = {}) {
  const overlay = options && typeof options === 'object' ? options : {};
  const overlayId = overlay.overlayId || 'pageLoading';
  const titleId = overlay.titleId || 'pageLoadingTitle';
  const copyId = overlay.copyId || 'pageLoadingCopy';
  const title = overlay.title || '页面加载中';
  const copy = overlay.copy || '正在同步当前页面需要的数据，请稍候。';

  return `
    <div class="page-loading-overlay" id="${escapePageUiText(overlayId)}">
      <div class="page-loading-card">
        <div class="page-loading-title" id="${escapePageUiText(titleId)}">${escapePageUiText(title)}</div>
        <div class="page-loading-copy" id="${escapePageUiText(copyId)}">${escapePageUiText(copy)}</div>
        <div class="page-loading-bars">
          <div class="page-loading-bar"></div>
          <div class="page-loading-bar"></div>
          <div class="page-loading-bar"></div>
        </div>
      </div>
    </div>
  `;
}

function ensurePageLoadingOverlay(options = {}) {
  const overlay = options && typeof options === 'object' ? options : {};
  const overlayId = overlay.overlayId || 'pageLoading';
  let element = document.getElementById(overlayId);
  if (element) return element;
  document.body.insertAdjacentHTML('afterbegin', renderPageLoadingOverlay(overlay));
  element = document.getElementById(overlayId);
  return element;
}

function setPageLoading(active, options = {}) {
  const overlay = options && typeof options === 'object' ? options : {};
  const overlayId = overlay.overlayId || 'pageLoading';
  const titleId = overlay.titleId || 'pageLoadingTitle';
  const copyId = overlay.copyId || 'pageLoadingCopy';
  const element = ensurePageLoadingOverlay(overlay);
  if (!element) return;

  if (overlay.title) {
    const titleEl = document.getElementById(titleId);
    if (titleEl) titleEl.textContent = overlay.title;
  }
  if (overlay.copy) {
    const copyEl = document.getElementById(copyId);
    if (copyEl) copyEl.textContent = overlay.copy;
  }
  element.classList.toggle('is-hidden', !active);
}

function withPageLoading(task, options = {}) {
  setPageLoading(true, options);
  const result = typeof task === 'function' ? task() : task;
  return Promise.resolve(result).finally(() => {
    setPageLoading(false, options);
  });
}

function spinPageRefreshButton(buttonId = 'refreshBtn') {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.classList.add('spinning');
  setTimeout(() => button.classList.remove('spinning'), 600);
}

function renderPageContextBar(title, options = {}) {
  const allowGlobal = Boolean(options.allowGlobal);
  const description = options.description || '';
  const subtitle = options.subtitle || '';
  const actionsHtml = options.actionsHtml || '';
  const safeTitle = title ? escapePageUiText(title) : '';
  const safeDescription = description ? escapePageUiText(description) : '';
  const safeSubtitle = subtitle ? escapePageUiText(subtitle) : '';
  return `
    <div class="page-context-bar${safeSubtitle ? ' has-subtitle' : ''}">
      <div class="page-context-main">
        <div class="page-context-title">
          ${safeTitle ? `<span class="page-context-heading">${safeTitle}</span>` : ''}
          ${renderEnvironmentBadge({ allowGlobal })}
          ${safeDescription ? `<span class="page-context-description">${safeDescription}</span>` : ''}
        </div>
        ${safeSubtitle ? `<div class="page-context-subtitle">${safeSubtitle}</div>` : ''}
      </div>
      <div class="page-context-tools">
        ${actionsHtml ? `<div class="page-context-actions">${actionsHtml}</div>` : ''}
        ${renderEnvironmentSwitcher({ allowGlobal })}
      </div>
    </div>
  `;
}

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
      copy: '健康 / 配置',
      active: activePage === '/ibkr_system.html'
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
      copy: '信号 / 订单'
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
      path: '/ibkr_monitor.html',
      kicker: 'Ops',
      label: '运维域',
      copy: '监控 / 数据'
    },
    {
      path: '/ibkr_system.html',
      kicker: 'System',
      label: '系统域',
      copy: '总览 / 配置'
    }
  ]);
}

function renderOpsBridge(activePage) {
  return renderPageBridge([
    {
      path: '/ibkr_monitor.html',
      kicker: 'Dashboard',
      label: '监控大盘',
      copy: '请求 / 健康',
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
      copy: '重导 / recompute',
      active: activePage === '/ibkr_history_rebuild.html'
    }
  ]);
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

// ── 通用 CSS 样式 ──
function getCommonStyles() {
  return '<link rel="stylesheet" href="/assets/css/common.css">';
}

// ── 全局 Loading 遮罩层 ──
function showLoading(text = '加载中...') {
  let loading = document.getElementById('globalLoading');
  if (!loading) {
    loading = document.createElement('div');
    loading.id = 'globalLoading';
    loading.className = 'global-loading hidden';
    loading.innerHTML = `
      <div class="spinner"></div>
      <div class="text">${text}</div>
    `;
    document.body.appendChild(loading);
  }
  loading.querySelector('.text').textContent = text;
  loading.classList.remove('hidden');
}

function hideLoading() {
  const loading = document.getElementById('globalLoading');
  if (loading) {
    loading.classList.add('hidden');
  }
}

// 自动管理 Loading 状态的包装函数
function withLoading(promise, text = '加载中...') {
  showLoading(text);
  return promise.finally(() => hideLoading());
}

/**
 * 创建带防重入 guard 的加载函数
 * 用法：const loadFn = guardedLoader(async () => { ... }, '加载中...')
 * 之后用 loadFn() 替代原始调用，重复调用会被忽略
 */
function guardedLoader(asyncFn, loadingText) {
  let isRunning = false;
  return async function() {
    if (isRunning) return;
    isRunning = true;
    showLoading(loadingText || '加载中...');
    try {
      await asyncFn();
    } finally {
      isRunning = false;
      hideLoading();
    }
  };
}

// ── 技术指标弹窗（ibkr_signals.html / ibkr_indicators.html 共用） ──
function getIndicatorModalStyles() {
  return '@import url("/assets/css/indicator-modal.css");';
}

function renderIndicatorModalHTML() {
  return `<div class="modal-overlay" id="indicatorModal" onclick="if(event.target===this)window.closeIndicatorModal()">
    <div class="modal-content">
      <div class="modal-header">
        <div class="modal-title">技术指标详情</div>
        <button class="modal-close" onclick="window.closeIndicatorModal()">×</button>
      </div>
      <div id="modalBody"></div>
    </div>
  </div>`;
}

function showIndicatorModal(normalized) {
  const container = document.getElementById('indicatorModalContainer');
  if (!container) return;
  if (!document.getElementById('indicatorModal')) {
    container.innerHTML = renderIndicatorModalHTML();
  }
  document.getElementById('modalBody').innerHTML = renderIndicatorModal(normalized);
  document.getElementById('indicatorModal').classList.add('show');
}

window.closeIndicatorModal = function() {
  const modal = document.getElementById('indicatorModal');
  if (modal) modal.classList.remove('show');
};

window.showIndicatorModal = showIndicatorModal;
window.getIndicatorModalStyles = getIndicatorModalStyles;
window.renderPageRefreshControl = renderPageRefreshControl;
window.renderPageTopSection = renderPageTopSection;
window.renderPageLoadingOverlay = renderPageLoadingOverlay;
window.ensurePageLoadingOverlay = ensurePageLoadingOverlay;
window.setPageLoading = setPageLoading;
window.withPageLoading = withPageLoading;
window.spinPageRefreshButton = spinPageRefreshButton;
window.renderSystemBridge = renderSystemBridge;
window.renderExecutionBridge = renderExecutionBridge;
window.renderAnalyticsBridge = renderAnalyticsBridge;
window.renderHomeBridge = renderHomeBridge;
window.renderOpsBridge = renderOpsBridge;
window.renderBacktestsBridge = renderBacktestsBridge;
