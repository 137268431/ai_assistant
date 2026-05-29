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
    { path: '/ibkr_signals.html', aliases: ['/ibkr_signals.html', '/ibkr_reverse_signals.html', '/orders.html', '/ibkr_order_details.html', '/ibkr_lifecycle_flow.html', '/ibkr_trade_review.html', '/ibkr_account.html'], icon: '📡', label: '执行' },
    { path: '/ibkr_screener.html', aliases: ['/ibkr_screener.html', '/ibkr_watchlist.html', '/ibkr_targets.html'], icon: '🎯', label: '目标' },
    {
      path: '/ibkr_system.html',
      aliases: [
        '/ibkr_system.html',
        '/ibkr_runtime.html',
        '/ibkr_config.html'
      ],
      icon: '🖥️',
      label: '系统'
    }
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
  const today = typeof getCurrentEtDateString === 'function'
    ? getCurrentEtDateString()
    : new Intl.DateTimeFormat('en-CA', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).format(new Date());
  const yesterday = typeof shiftDateString === 'function'
    ? shiftDateString(today, -1)
    : today;

  return `
    <div class="date-picker">
      <div class="date-shortcuts">
        <button class="date-btn" data-date="${yesterday}" onclick="selectDate('${yesterday}', this)">昨天</button>
        <button class="date-btn active" data-date="${today}" onclick="selectDate('${today}', this)">今天</button>
      </div>
      <input type="date" class="date-input" id="customDate" value="${today}" onchange="selectCustomDate(this.value)">
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
