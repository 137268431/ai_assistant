// ═══════════════════════════════════════════════════════════════
// common.js - 公共模块
// 所有管理页面的共享逻辑和组件
// ═══════════════════════════════════════════════════════════════

// ── 配置常量 ──
const BASE_URL = 'https://pb.lzw-glory.top';

// ── Token 管理 ──
function getToken() {
  return localStorage.getItem('pb_token') || '';
}

function clearToken() {
  localStorage.removeItem('pb_token');
}

function redirectToLogin(fromPage) {
  clearToken();
  const target = fromPage || location.pathname;
  location.href = `/login.html?from=${target}`;
}

function handleAuthError() {
  redirectToLogin(location.pathname);
}

// 通用错误处理包装器 - 自动处理认证错误
function withAuthCheck(promise, errorHandler) {
  return promise.catch(error => {
    // 检查是否是认证错误 (401/403 或消息包含认证失败)
    if (error.status === 401 || error.status === 403 ||
        error.message?.includes('Authentication') ||
        error.message?.includes('Unauthorized') ||
        error.message?.includes('auth')) {
      handleAuthError();
      return;
    }

    // 调用自定义错误处理
    if (errorHandler) errorHandler(error);
  });
}

function requireAuth(fromPage) {
  if (!getToken()) {
    location.href = `/login.html?from=${fromPage}`;
    throw new Error('Not authenticated');
  }
}

// ── API 请求封装 ──
async function apiFetch(collection, params = {}) {
  const token = getToken();
  const headers = {
    'Content-Type': 'application/json'
  };

  // PocketBase 0.36+ 使用 Bearer token
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  let url = `${BASE_URL}/api/collections/${collection}/records`;

  // 构建查询参数
  const queryParams = new URLSearchParams();
  if (params.filter) queryParams.append('filter', params.filter);
  if (params.sort) queryParams.append('sort', params.sort);
  if (params.perPage) queryParams.append('perPage', params.perPage);
  if (params.page) queryParams.append('page', params.page);

  const queryString = queryParams.toString();
  if (queryString) url += `?${queryString}`;

  const options = {
    method: params.method || 'GET',
    headers
  };

  if (params.body) {
    options.body = JSON.stringify(params.body);
  }

  const res = await fetch(url, options);

  // 401/403 自动跳转登录
  if (res.status === 401 || res.status === 403) {
    localStorage.removeItem('pb_token');
    location.href = `/login.html?from=${location.pathname}`;
    throw new Error('Authentication failed');
  }

  if (!res.ok) {
    const error = await res.json();
    throw new Error(error.message || 'Request failed');
  }

  return res.json();
}

// ── Toast 通知 ──
function showToast(msg, duration = 2500) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), duration);
}

// ── 底部导航 ──
function renderNav(activePage) {
  const pages = [
    { path: '/index.html', icon: '🏠', label: '首页' },
    { path: '/signals.html', icon: '📡', label: '信号' },
    { path: '/reverse_signals.html', icon: '🔄', label: '反转' },
    { path: '/orders.html', icon: '📋', label: '订单' },
    { path: '/order_details.html', icon: '📜', label: '明细' },
    { path: '/indicators.html', icon: '📈', label: '指标' },
    { path: '/stats.html', icon: '📊', label: '统计' }
  ];

  return `
    <div class="nav">
      ${pages.map(p => `
        <a href="${p.path}" class="nav-item ${p.path === activePage ? 'active' : ''}">
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
    location.href = '/login.html';
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

// 日期选择回调（需要在页面中定义 onDateChange）
window.selectDate = function(date, btn) {
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('customDate').value = date;
  if (typeof onDateChange === 'function') {
    onDateChange(date, date);
  }
};


window.selectCustomDate = function(date) {
  document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
  if (typeof onDateChange === 'function') {
    onDateChange(date, date);
  }
};

// ── 多空方向筛选 ──
function renderDirectionTabs(onChange) {
  return `
    <div class="direction-tabs">
      <button class="dir-tab active" data-dir="all" onclick="selectDirection('all', this)">全部</button>
      <button class="dir-tab" data-dir="long" onclick="selectDirection('long', this)">做多</button>
      <button class="dir-tab" data-dir="short" onclick="selectDirection('short', this)">做空</button>
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

// ── 标准化指标记录（snake_case extra → camelCase）──
function normalizeIndicatorRecord(record) {
  if (!record) return null;

  // 处理 extra 可能是 JSON 字符串的情况
  let extra = record.extra;
  if (typeof extra === 'string') {
    try {
      extra = JSON.parse(extra);
    } catch (e) {
      extra = {};
    }
  }
  extra = extra || {};

  return {
    // Top-level fields
    symbol: record.symbol,
    exchange: record.exchange,
    interval: record.interval,
    scriptTag: record.script_tag,
    usTime: record.us_time,
    cnTime: record.cn_time,
    barTimeMs: record.bar_time_ms,
    barIndex: record.bar_index,
    created: record.created,

    // OHLCV
    open: extra.open,
    high: extra.high,
    low: extra.low,
    close: extra.close,
    volume: extra.volume,

    // Price changes
    dayChangePct: extra.day_change_pct,
    prevCloseChangePct: extra.prev_close_change_pct,
    change7d: extra.change_7d,

    // EMA
    emaFast: extra.ema_fast,
    emaSlow: extra.ema_slow,
    emaTrend: extra.ema_trend,
    emaLongest: extra.ema_longest,
    trendDir: extra.trend_dir,
    emaBullish: extra.ema_bullish,
    emaBearish: extra.ema_bearish,
    emaBullTouch: extra.ema_bull_touch,
    emaBearTouch: extra.ema_bear_touch,
    slopeSlow: extra.slope_slow,
    slopeTrend: extra.slope_trend,
    slopeLongest: extra.slope_longest,

    // Divergences
    crsiBullDiv: extra.crsi_bull_div,
    crsiBearDiv: extra.crsi_bear_div,
    obvBullDiv: extra.obv_bull_div,
    obvBearDiv: extra.obv_bear_div,
    crsiHidBull: extra.crsi_hid_bull,
    crsiHidBear: extra.crsi_hid_bear,
    obvHidBull: extra.obv_hid_bull,
    obvHidBear: extra.obv_hid_bear,

    // Fractals & Channels
    fractalBull: extra.fractal_bull,
    fractalBear: extra.fractal_bear,
    sdLower: extra.sd_lower,
    sdUpper: extra.sd_upper,
    sdZone: extra.sd_zone,
    sdTrend: extra.sd_trend,
    sdReg: extra.sd_reg,
    sdStdDev: extra.sd_std_dev,

    // DTP & RSI
    dtpDir: extra.dtp_dir,
    dtpPhase: extra.dtp_phase,
    dtpPhaseBars: extra.dtp_phase_bars,
    dtpAvg: extra.dtp_avg,
    dtpAtr: extra.dtp_atr,
    crsi: extra.crsi,
    crsiOB: extra.crsi_ob,
    crsiOS: extra.crsi_os,
    crsiUb: extra.crsi_ub,
    crsiDb: extra.crsi_db,
    obvRsi: extra.obv_rsi,
    atr: extra.atr,
    atrRaw: extra.atr_raw,
    atrPct: extra.atr_pct,

    // VWAP
    vwap: extra.vwap,
    vwapUpper1: extra.vwap_upper1,
    vwapLower1: extra.vwap_lower1,
    vwapUpper2: extra.vwap_upper2,
    vwapLower2: extra.vwap_lower2,
    vwapDist: extra.vwap_dist,
    vwapBullish: extra.vwap_bullish
  };
}

// ── 构建技术指标徽章 HTML ──
function buildIndicatorBadges(signal, latestIndicator) {
  const badges = [];

  // 涨幅徽章（从最新指标数据获取）
  const indicator = latestIndicator || {};
  const dayChangePct = indicator.dayChangePct || indicator.day_change_pct || 0;
  const prevCloseChangePct = indicator.prevCloseChangePct || indicator.prev_close_change_pct || 0;
  const change7d = indicator.change7d || indicator.change_7d || 0;

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
    if (latestIndicator.crsiBullDiv || latestIndicator.obvBullDiv) {
      badges.push(`<span class="indicator-badge badge-div">多头背离</span>`);
    }
    if (latestIndicator.crsiBearDiv || latestIndicator.obvBearDiv) {
      badges.push(`<span class="indicator-badge badge-div">空头背离</span>`);
    }
    // 分形信号
    if (latestIndicator.fractalBull) {
      badges.push(`<span class="indicator-badge badge-fractal">分形↑</span>`);
    }
    if (latestIndicator.fractalBear) {
      badges.push(`<span class="indicator-badge badge-fractal">分形↓</span>`);
    }
    // EMA触及
    if (latestIndicator.emaBullTouch) {
      badges.push(`<span class="indicator-badge badge-ema">EMA触及↑</span>`);
    }
    if (latestIndicator.emaBearTouch) {
      badges.push(`<span class="indicator-badge badge-ema">EMA触及↓</span>`);
    }
    // EMA 多头/空头状态
    if (latestIndicator.emaBullish) {
      badges.push(`<span class="indicator-badge badge-ema">EMA 多头</span>`);
    }
    if (latestIndicator.emaBearish) {
      badges.push(`<span class="indicator-badge badge-ema">EMA 空头</span>`);
    }
  }

  return badges.join('');
}

// ── 渲染技术指标详情浮层 ──
function renderIndicatorModal(latestIndicator) {
  if (!latestIndicator) return '<div class="modal-section">暂无技术指标数据</div>';

  // 时间信息 section（放在最上面）
  const timeSection = `
    <div class="modal-section" style="background: var(--surface2); border-radius: 8px; padding: 12px; margin-bottom: 16px;">
      <div class="modal-section-title">⏰ 时间信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">美国时间</div>
          <div class="modal-value">${latestIndicator.usTime || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">中国时间</div>
          <div class="modal-value">${latestIndicator.cnTime || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Bar时间戳</div>
          <div class="modal-value" style="font-size: 11px;">${latestIndicator.barTimeMs ? new Date(latestIndicator.barTimeMs).toISOString() : '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">创建时间</div>
          <div class="modal-value" style="font-size: 11px;">${latestIndicator.created ? new Date(latestIndicator.created).toISOString() : '-'}</div>
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
          <div class="modal-value">$${(latestIndicator.close || 0).toFixed(2)}</div>
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
          <div class="modal-label">涨幅</div>
          <div class="modal-value" style="color: ${latestIndicator.dayChangePct > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.dayChangePct > 0 ? '+' : ''}${(latestIndicator.dayChangePct || 0).toFixed(2)}% / ${latestIndicator.prevCloseChangePct > 0 ? '+' : ''}${(latestIndicator.prevCloseChangePct || 0).toFixed(2)}% / ${latestIndicator.change7d > 0 ? '+' : ''}${(latestIndicator.change7d || 0).toFixed(2)}%
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR波动率</div>
          <div class="modal-value" style="color: ${latestIndicator.atrPct >= 3 ? '#e53e3e' : latestIndicator.atrPct >= 1.5 ? '#ed8936' : '#48bb78'}">
            ${latestIndicator.atrPct >= 3 ? '⚡高' : latestIndicator.atrPct >= 1.5 ? '〜中' : '·低'} ${(latestIndicator.atrPct || 0).toFixed(2)}%
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">EMA 均线</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">EMA Fast (20)</div>
          <div class="modal-value">$${(latestIndicator.emaFast || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Slow (50)</div>
          <div class="modal-value">$${(latestIndicator.emaSlow || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Trend (100)</div>
          <div class="modal-value">$${(latestIndicator.emaTrend || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA Longest (200)</div>
          <div class="modal-value">$${(latestIndicator.emaLongest || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">趋势方向</div>
          <div class="modal-value">${latestIndicator.trendDir === 1 ? '📈 多头' : latestIndicator.trendDir === -1 ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA状态</div>
          <div class="modal-value">${latestIndicator.emaBullish ? '📈 多头' : latestIndicator.emaBearish ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">EMA触及</div>
          <div class="modal-value">
            ${latestIndicator.emaBullTouch ? '✅ 多头触及' : latestIndicator.emaBearTouch ? '✅ 空头触及' : '❌ 无'}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Slow斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slopeSlow > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slopeSlow > 0 ? '+' : ''}${(latestIndicator.slopeSlow || 0).toFixed(4)}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Trend斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slopeTrend > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slopeTrend > 0 ? '+' : ''}${(latestIndicator.slopeTrend || 0).toFixed(4)}
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Longest斜率</div>
          <div class="modal-value" style="color: ${latestIndicator.slopeLongest > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.slopeLongest > 0 ? '+' : ''}${(latestIndicator.slopeLongest || 0).toFixed(4)}
          </div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">背离信号</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">cRSI 多头背离</div>
          <div class="modal-value">${latestIndicator.crsiBullDiv ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 空头背离</div>
          <div class="modal-value">${latestIndicator.crsiBearDiv ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 隐藏多头</div>
          <div class="modal-value">${latestIndicator.crsiHidBull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 隐藏空头</div>
          <div class="modal-value">${latestIndicator.crsiHidBear ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 多头背离</div>
          <div class="modal-value">${latestIndicator.obvBullDiv ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 空头背离</div>
          <div class="modal-value">${latestIndicator.obvBearDiv ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 隐藏多头</div>
          <div class="modal-value">${latestIndicator.obvHidBull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV 隐藏空头</div>
          <div class="modal-value">${latestIndicator.obvHidBear ? '✅ 是' : '❌ 否'}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">分形 & 通道</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">分形多头</div>
          <div class="modal-value">${latestIndicator.fractalBull ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">分形空头</div>
          <div class="modal-value">${latestIndicator.fractalBear ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 下轨触及</div>
          <div class="modal-value">${latestIndicator.sdLower ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 上轨触及</div>
          <div class="modal-value">${latestIndicator.sdUpper ? '✅ 是' : '❌ 否'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 区域</div>
          <div class="modal-value">${latestIndicator.sdZone === 1 ? '超买' : latestIndicator.sdZone === -1 ? '超卖' : '正常'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 趋势</div>
          <div class="modal-value">${latestIndicator.sdTrend === 1 ? '📈 上升' : latestIndicator.sdTrend === -1 ? '📉 下降' : '➡️ 平坦'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 标准差</div>
          <div class="modal-value">${(latestIndicator.sdStdDev || 0).toFixed(4)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">SD 回归值</div>
          <div class="modal-value">${(latestIndicator.sdReg || 0).toFixed(2)}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">DTP & RSI</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">DTP 方向</div>
          <div class="modal-value">${latestIndicator.dtpDir === 1 ? '📈 多头' : latestIndicator.dtpDir === -1 ? '📉 空头' : '➡️ 中性'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 阶段</div>
          <div class="modal-value">${latestIndicator.dtpPhase || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 阶段Bar数</div>
          <div class="modal-value">${latestIndicator.dtpPhaseBars || 0}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP 平均值</div>
          <div class="modal-value">${(latestIndicator.dtpAvg || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">DTP ATR</div>
          <div class="modal-value">${(latestIndicator.dtpAtr || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI</div>
          <div class="modal-value">${(latestIndicator.crsi || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 上界</div>
          <div class="modal-value">${(latestIndicator.crsiUb || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 下界</div>
          <div class="modal-value">${(latestIndicator.crsiDb || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">cRSI 状态</div>
          <div class="modal-value">${latestIndicator.crsiOB ? '超买' : latestIndicator.crsiOS ? '超卖' : '正常'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">OBV RSI</div>
          <div class="modal-value">${(latestIndicator.obvRsi || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR</div>
          <div class="modal-value">$${(latestIndicator.atr || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">ATR Raw</div>
          <div class="modal-value">${(latestIndicator.atrRaw || 0).toFixed(4)}</div>
        </div>
      </div>
    </div>

    <div class="modal-section">
      <div class="modal-section-title">止损管理</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">止损距离(%)</div>
          <div class="modal-value">${(latestIndicator.slDistPct || 0).toFixed(2)}%</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">止损ATR比率</div>
          <div class="modal-value" style="color: ${latestIndicator.slAtrRatio >= 0.8 ? 'var(--long)' : 'var(--short)'}">
            ${(latestIndicator.slAtrRatio || 0).toFixed(2)}x ${latestIndicator.slAtrRatio >= 0.8 ? '✅' : '⚠️'}
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
          <div class="modal-value" style="color: ${latestIndicator.vwapDist > 0 ? 'var(--long)' : 'var(--short)'}">
            ${latestIndicator.vwapDist > 0 ? '+' : ''}${(latestIndicator.vwapDist || 0).toFixed(2)}%
          </div>
        </div>
        <div class="modal-item">
          <div class="modal-label">VWAP 趋势</div>
          <div class="modal-value">${latestIndicator.vwapBullish ? '📈 多头' : '📉 空头'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">U1 上轨</div>
          <div class="modal-value">$${(latestIndicator.vwapUpper1 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">L1 下轨</div>
          <div class="modal-value">$${(latestIndicator.vwapLower1 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">U2 上轨</div>
          <div class="modal-value">$${(latestIndicator.vwapUpper2 || 0).toFixed(2)}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">L2 下轨</div>
          <div class="modal-value">$${(latestIndicator.vwapLower2 || 0).toFixed(2)}</div>
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
          <div class="modal-value">${latestIndicator.usTime || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">北京时间</div>
          <div class="modal-value">${latestIndicator.cnTime || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">K线索引</div>
          <div class="modal-value">${latestIndicator.barIndex || 'N/A'}</div>
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

// ── 通用 CSS 样式 ──
function getCommonStyles() {
  return `
    <style>
      :root {
        --bg:        #080B10;
        --surface:   #0E1420;
        --surface2:  #141C2E;
        --border:    rgba(99,179,237,0.1);
        --text:      #E2EAF4;
        --muted:     #8BA4C4;
        --accent:    #63B3ED;
        --long:      #48BB78;
        --short:     #FC8181;
        --pending:   #F6AD55;
        --confirmed: #63B3ED;
        --executed:  #68D391;
        --rejected:  #718096;
        --failed:    #FC8181;
      }

      * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }

      /* 防止 iOS 输入时放大页面 */
      input, textarea, select {
        font-size: 16px;
      }

      body {
        background: var(--bg);
        color: var(--text);
        font-family: 'Sora', sans-serif;
        min-height: 100vh;
        padding-bottom: 80px;
        overflow-x: hidden;
      }

      body::before {
        content: '';
        position: fixed;
        inset: 0;
        background-image:
          linear-gradient(rgba(99,179,237,0.03) 1px, transparent 1px),
          linear-gradient(90deg, rgba(99,179,237,0.03) 1px, transparent 1px);
        background-size: 32px 32px;
        pointer-events: none;
        z-index: 0;
      }

      /* ── Toast ── */
      .toast {
        position: fixed;
        top: 24px;
        left: 50%;
        transform: translateX(-50%) translateY(-60px);
        background: var(--surface2);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 12px 20px;
        font-size: 13px;
        z-index: 200;
        white-space: nowrap;
        transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        pointer-events: none;
      }

      .toast.show {
        transform: translateX(-50%) translateY(0);
      }

      /* ── Bottom Nav ── */
      .nav {
        position: fixed;
        bottom: 0;
        left: 0; right: 0;
        background: rgba(8,11,16,0.96);
        backdrop-filter: blur(16px);
        border-top: 1px solid var(--border);
        display: grid;
        grid-template-columns: repeat(7, 1fr);
        padding: 10px 0 max(20px, env(safe-area-inset-bottom));
        z-index: 50;
        gap: 0;
      }

      .nav-item {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 3px;
        text-decoration: none;
        color: var(--muted);
        font-size: 8px;
        font-family: 'JetBrains Mono', monospace;
        letter-spacing: 0.5px;
        transition: color 0.2s;
        min-width: 0;
        padding: 0 2px;
      }

      .nav-item.active { color: var(--accent); }
      .nav-icon { font-size: 18px; line-height: 1; }

      /* ── Date Picker ── */
      .date-picker {
        display: flex;
        gap: 8px;
        align-items: center;
        margin-bottom: 12px;
      }

      .date-shortcuts {
        display: flex;
        gap: 6px;
      }

      .date-btn {
        padding: 8px 16px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: transparent;
        color: var(--muted);
        font-size: clamp(11px, 2.8vw, 13px);
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        transition: all 0.2s;
        white-space: nowrap;
        flex: 1;
        min-width: 0;
      }

      .date-btn.active {
        background: var(--accent);
        border-color: var(--accent);
        color: var(--bg);
        font-weight: 700;
      }

      .date-input {
        padding: 6px 10px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface2);
        color: var(--text);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        outline: none;
      }

      .date-input:focus {
        border-color: var(--accent);
      }

      /* ── Direction Tabs ── */
      .direction-tabs {
        display: flex;
        gap: 6px;
        margin-bottom: 12px;
      }

      .dir-tab {
        flex: 1;
        padding: 10px 16px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: transparent;
        color: #C4D4E4;
        font-size: clamp(11px, 3vw, 13px);
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        transition: all 0.2s;
        min-width: 0;
      }

      .dir-tab.active {
        background: var(--accent);
        border-color: var(--accent);
        color: var(--bg);
        font-weight: 700;
      }

      .dir-tab[data-dir="long"].active {
        background: var(--long);
        border-color: var(--long);
      }

      .dir-tab[data-dir="short"].active {
        background: var(--short);
        border-color: var(--short);
      }

      /* ── Refresh Bar ── */
      .refresh-bar {
        display: flex;
        gap: 8px;
        align-items: center;
      }

      .refresh-btn {
        background: none;
        border: 1px solid var(--border);
        border-radius: 8px;
        color: var(--muted);
        padding: 4px 8px;
        font-size: 14px;
        cursor: pointer;
        transition: all 0.2s;
      }

      .refresh-btn:active {
        color: var(--accent);
        border-color: var(--accent);
      }

      .refresh-btn.spinning {
        animation: spin 0.6s linear;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      .interval-select {
        padding: 4px 8px;
        border-radius: 8px;
        border: 1px solid var(--border);
        background: var(--surface2);
        color: var(--text);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        outline: none;
        cursor: pointer;
      }

      .interval-select:focus {
        border-color: var(--accent);
      }

      /* ── Header ── */
      .header {
        position: sticky;
        top: 0;
        z-index: 50;
        background: rgba(8,11,16,0.92);
        backdrop-filter: blur(16px);
        border-bottom: 1px solid var(--border);
        padding: 16px 20px 12px;
      }

      .header-top {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        margin-bottom: 14px;
      }

      .logo {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .logo-label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 9px;
        letter-spacing: 3px;
        color: var(--accent);
        opacity: 0.7;
      }

      .logo-title {
        font-size: 18px;
        font-weight: 700;
        letter-spacing: -0.5px;
      }

      .live-badge {
        display: flex;
        align-items: center;
        gap: 6px;
        background: rgba(72,187,120,0.1);
        border: 1px solid rgba(72,187,120,0.25);
        border-radius: 20px;
        padding: 5px 10px;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        color: var(--long);
        letter-spacing: 1px;
      }

      .live-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--long);
        animation: blink 1.4s infinite;
      }

      @keyframes blink {
        0%, 100% { opacity: 1; }
        50%       { opacity: 0.2; }
      }

      /* ── Content ── */
      .content {
        position: relative;
        z-index: 1;
        padding: 12px 16px;
      }

      /* ── Loading & Empty ── */
      .loading {
        text-align: center;
        padding: 40px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        letter-spacing: 1px;
      }

      /* Loading 动画 */
      .loading-spinner {
        display: inline-block;
        width: 20px;
        height: 20px;
        border: 2px solid var(--border);
        border-top-color: var(--accent);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
        margin-right: 8px;
        vertical-align: middle;
      }

      @keyframes spin {
        to { transform: rotate(360deg); }
      }

      /* 全局 Loading 遮罩层 */
      .global-loading {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(8, 11, 16, 0.85);
        backdrop-filter: blur(4px);
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        z-index: 9999;
        gap: 16px;
      }

      .global-loading .spinner {
        width: 40px;
        height: 40px;
        border: 3px solid var(--border);
        border-top-color: var(--accent);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }

      .global-loading .text {
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
        font-size: 13px;
        letter-spacing: 1px;
      }

      .global-loading.hidden {
        display: none;
      }

      .empty {
        text-align: center;
        padding: 60px 20px;
        color: var(--muted);
      }

      .empty-icon {
        font-size: 40px;
        margin-bottom: 12px;
        opacity: 0.4;
      }

      .empty-text {
        font-size: 13px;
      }

      /* ── 技术指标徽章 ── */
      .indicator-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 10px;
      }

      .indicator-badge {
        padding: 4px 8px;
        border-radius: 6px;
        font-size: 10px;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 700;
        letter-spacing: 0.5px;
        white-space: nowrap;
      }

      .badge-change-up {
        background: rgba(72,187,120,0.2);
        color: var(--long);
      }

      .badge-change-down {
        background: rgba(252,129,129,0.2);
        color: var(--short);
      }

      .badge-div {
        background: rgba(99,179,237,0.2);
        color: var(--accent);
      }

      .badge-fractal {
        background: rgba(246,173,85,0.2);
        color: var(--pending);
      }

      .badge-ema {
        background: rgba(139,92,246,0.2);
        color: #a78bfa;
      }

      .view-details-btn {
        margin-top: 10px;
        padding: 8px;
        background: rgba(99,179,237,0.1);
        border: 1px solid rgba(99,179,237,0.2);
        border-radius: 8px;
        color: var(--accent);
        font-size: 11px;
        font-family: 'JetBrains Mono', monospace;
        cursor: pointer;
        transition: all 0.2s;
        text-align: center;
      }

      .view-details-btn:active {
        background: rgba(99,179,237,0.2);
      }

      /* ── 浮层样式 ── */
      .modal-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.8);
        z-index: 1000;
        display: none;
        align-items: center;
        justify-content: center;
        padding: 20px;
      }

      .modal-overlay.show {
        display: flex;
      }

      .modal-content {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 12px;
        max-width: 600px;
        width: 100%;
        max-height: 80vh;
        overflow-y: auto;
        padding: 20px;
      }

      .modal-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid var(--border);
      }

      .modal-title {
        font-size: 16px;
        font-weight: 700;
        color: var(--text);
      }

      .modal-close {
        background: none;
        border: none;
        color: var(--muted);
        font-size: 24px;
        cursor: pointer;
        padding: 0;
        width: 32px;
        height: 32px;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      .modal-section {
        margin-bottom: 16px;
      }

      .modal-section-title {
        font-size: 12px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
        margin-bottom: 8px;
        letter-spacing: 1px;
      }

      .modal-grid {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
      }

      .modal-item {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .modal-label {
        font-size: 10px;
        color: var(--muted);
        font-family: 'JetBrains Mono', monospace;
      }

      .modal-value {
        font-size: 13px;
        font-weight: 700;
        color: var(--text);
      }
    </style>
  `;
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

// ── 导出（如果使用模块化）──
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    BASE_URL,
    getToken,
    requireAuth,
    apiFetch,
    showToast,
    renderNav,
    renderDatePicker,
    renderDirectionTabs,
    renderRefreshBar,
    getCommonStyles,
    formatBeijingTime,
    formatRelativeTime,
    showLoading,
    hideLoading,
    withLoading
  };
}

// ── 技术指标弹窗（signals.html / indicators.html 共用） ──
function getIndicatorModalStyles() {
  return `
    .modal-overlay {
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.8);
      z-index: 1000;
      display: none;
      align-items: center; justify-content: center;
      padding: 20px;
    }
    .modal-overlay.show { display: flex; }
    .modal-content {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      max-width: 600px; width: 100%;
      max-height: 80vh; overflow-y: auto;
      padding: 20px;
    }
    .modal-header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 16px; padding-bottom: 12px;
      border-bottom: 1px solid var(--border);
    }
    .modal-title { font-size: 16px; font-weight: 700; color: var(--text); }
    .modal-close {
      background: none; border: none; color: var(--muted);
      font-size: 24px; cursor: pointer;
      padding: 0; width: 32px; height: 32px;
      display: flex; align-items: center; justify-content: center;
    }
    .modal-section { margin-bottom: 16px; }
    .modal-section-title {
      font-size: 12px; color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
      margin-bottom: 8px; letter-spacing: 1px;
    }
    .modal-grid {
      display: grid; grid-template-columns: repeat(2, 1fr);
      gap: 10px;
    }
    .modal-item { display: flex; flex-direction: column; gap: 4px; }
    .modal-label {
      font-size: 10px; color: var(--muted);
      font-family: 'JetBrains Mono', monospace;
    }
    .modal-value {
      font-size: 13px; font-weight: 700; color: var(--text);
    }
  `;
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
  // 首次渲染 HTML
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
