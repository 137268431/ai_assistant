function getIbkrExtraObject(record) {
  return record && typeof record.extra === 'object' && record.extra ? record.extra : {};
}

function getIbkrIntervalMs(value) {
  const text = String(value ?? '').trim().toLowerCase();
  const mapping = {
    '5': 5 * 60 * 1000,
    '5m': 5 * 60 * 1000,
    '15': 15 * 60 * 1000,
    '15m': 15 * 60 * 1000,
    '30': 30 * 60 * 1000,
    '30m': 30 * 60 * 1000,
    '60': 60 * 60 * 1000,
    '1h': 60 * 60 * 1000,
    '240': 4 * 60 * 60 * 1000,
    '4h': 4 * 60 * 60 * 1000,
    'd': 24 * 60 * 60 * 1000,
    '1d': 24 * 60 * 60 * 1000,
  };
  return mapping[text] || 0;
}

function getIbkrBarStartLabel(record) {
  if (!record) return '--';
  if (record.bar_time_ms) return formatBarTimeMsToET(record.bar_time_ms);
  return record.us_time || '--';
}

function getIbkrBarCloseLabel(record) {
  if (!record) return '--';
  const extra = getIbkrExtraObject(record);
  if (extra.bar_close_us_time) return extra.bar_close_us_time;
  const startMs = Number(record.bar_time_ms || 0);
  const intervalMs = getIbkrIntervalMs(record.interval || extra.interval || extra.chart_tf);
  return startMs > 0 && intervalMs > 0 ? formatBarTimeMsToET(startMs + intervalMs) : '--';
}

function getIbkrComputedTimeLabel(record) {
  const extra = getIbkrExtraObject(record);
  if (extra.computed_at_us) return extra.computed_at_us;
  const fallback = record?.updated || record?.created || '';
  return fallback ? formatMarketTime(fallback, 'datetime') : '--';
}

// ── 时间格式化（UTC → 美东时间）──
function formatMarketTime(utcTimeString, format = 'datetime') {
  if (!utcTimeString) return '-';

  const date = new Date(utcTimeString);

  // 检查是否为有效日期
  if (isNaN(date.getTime())) return '-';

  const options = {
    timeZone: 'America/New_York',
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
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
  } else if (format === 'time') {
    // 只显示时间: 14:30:45
    return date.toLocaleTimeString('zh-CN', {
      timeZone: 'America/New_York',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
  } else if (format === 'short') {
    // 短格式: 03-15 14:30
    const dateStr = date.toLocaleDateString('zh-CN', {
      timeZone: 'America/New_York',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
    const timeStr = date.toLocaleTimeString('zh-CN', {
      timeZone: 'America/New_York',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
    return `${dateStr} ${timeStr}`;
  } else {
    // 完整格式: 2025-03-15 14:30:45
    const dateStr = date.toLocaleDateString('zh-CN', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    }).replace(/\//g, '-');
    const timeStr = date.toLocaleTimeString('zh-CN', {
      timeZone: 'America/New_York',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
    return `${dateStr} ${timeStr}`;
  }
}

// Backward-compatible alias; all console display now uses ET.
function formatBeijingTime(utcTimeString, format = 'datetime') {
  return formatMarketTime(utcTimeString, format);
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
  return formatMarketTime(utcTimeString, 'short');
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
  const indicatorExtra = typeof getIbkrExtraObject === 'function' ? getIbkrExtraObject(latestIndicator) : {};
  const barStartEt = typeof getIbkrBarStartLabel === 'function'
    ? getIbkrBarStartLabel(latestIndicator)
    : (latestIndicator.bar_time_ms ? formatBarTimeMsToET(latestIndicator.bar_time_ms) : (latestIndicator.us_time || '-'));
  const barCloseEt = typeof getIbkrBarCloseLabel === 'function'
    ? getIbkrBarCloseLabel(latestIndicator)
    : '-';
  const computedAt = typeof getIbkrComputedTimeLabel === 'function'
    ? getIbkrComputedTimeLabel(latestIndicator)
    : (latestIndicator.created ? formatMarketTime(latestIndicator.created) : '-');

  // 时间信息 section（放在最上面）
  const timeSection = `
    <div class="modal-section" style="background: var(--surface2); border-radius: 8px; padding: 12px; margin-bottom: 16px;">
      <div class="modal-section-title">时间信息</div>
      <div class="modal-grid">
        <div class="modal-item">
          <div class="modal-label">Bar Start (ET)</div>
          <div class="modal-value">${barStartEt || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Bar Close (ET)</div>
          <div class="modal-value">${barCloseEt || '-'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Computed At</div>
          <div class="modal-value">${computedAt || '-'}</div>
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
          <div class="modal-label">Bar Start (ET)</div>
          <div class="modal-value">${barStartEt || 'N/A'}</div>
        </div>
        <div class="modal-item">
          <div class="modal-label">Bar Close (ET)</div>
          <div class="modal-value">${barCloseEt || 'N/A'}</div>
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
