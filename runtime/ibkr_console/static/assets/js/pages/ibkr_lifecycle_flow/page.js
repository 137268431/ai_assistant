(function lifecycleFlowPage() {
  'use strict';

  const PAGE_PATH = '/ibkr_lifecycle_flow.html';
  const ENDPOINT_PATH = '/api/custom/ibkr/lifecycle-flow';
  const QUERY_KEYS = ['mode', 'environment', 'date', 'symbol', 'signal_id', 'trade_group_id', 'order_id', 'run_id', 'backtest_date'];
  const RUNTIME_ENVS = ['live', 'paper', 'backtest'];
  const STORAGE_DATE_KEY = 'ibkr_lifecycle_flow_last_date';

  const FILL_SOURCE_META = {
    actual_ibkr: {
      label: 'actual_ibkr',
      title: '真实 IBKR 成交',
      tone: 'real',
      short: 'IBKR',
      copy: '来自真实 IBKR 成交/执行回填，可以作为真实成交价展示。'
    },
    paper_ibkr: {
      label: 'paper_ibkr',
      title: 'Paper IBKR 成交',
      tone: 'paper',
      short: 'PAPER',
      copy: '来自 IBKR paper 账户成交/执行回填，只代表 paper 环境。'
    },
    backtest_simulated: {
      label: 'backtest_simulated',
      title: '回测模拟价',
      tone: 'sim',
      short: 'SIM',
      copy: '来自回测撮合/模拟模型，不能当作真实 IBKR 成交价。'
    },
    unknown: {
      label: 'unknown',
      title: '来源未知',
      tone: 'unverified',
      short: 'UNK',
      copy: '来源未知或未回填，不允许作为真实成交价展示。'
    }
  };

  const PRICE_KIND_META = {
    actual_fill: {
      label: '成交价',
      short: 'FILL',
      title: 'IBKR 成交价',
      tone: 'real',
      copy: '来自成交/执行回填的价格。'
    },
    simulated_fill: {
      label: '模拟成交价',
      short: 'SIM',
      title: '回测模拟成交价',
      tone: 'sim',
      copy: '来自回测撮合/模拟模型，不是真实 IBKR 成交价。'
    },
    take_profit_price: {
      label: '止盈价',
      short: 'TP',
      title: '止盈保护价格',
      tone: 'reference',
      copy: '这是止盈保护单价格，不代表真实成交价。'
    },
    stop_loss_price: {
      label: '止损价',
      short: 'SL',
      title: '止损保护价格',
      tone: 'reference',
      copy: '这是止损保护单价格，不代表真实成交价。'
    },
    entry_limit: {
      label: '开仓限价',
      short: 'LIMIT',
      title: '开仓订单限价',
      tone: 'reference',
      copy: '这是订单提交/限价参考，不代表真实成交价。'
    },
    close_limit: {
      label: '平仓限价',
      short: 'LIMIT',
      title: '平仓订单限价',
      tone: 'reference',
      copy: '这是订单提交/限价参考，不代表真实成交价。'
    },
    order_reference_price: {
      label: '订单参考价',
      short: 'REF',
      title: '订单参考价格',
      tone: 'reference',
      copy: '这是订单或策略参考价格，不代表真实成交价。'
    },
    order_detail_unverified_fill: {
      label: '订单明细价',
      short: 'DETAIL',
      title: '订单明细价格（未验证成交）',
      tone: 'unverified',
      copy: '来自订单明细同步字段，未匹配逐笔 IBKR execution fill；不能作为确认成交价。'
    },
    market_reference_price: {
      label: '当前参考价',
      short: 'MARK',
      title: '市场参考价格',
      tone: 'reference',
      copy: '这是实时/当前市场参考价，不代表成交价。'
    },
    not_a_fill: {
      label: '参考价',
      short: 'REF',
      title: '非成交价格',
      tone: 'reference',
      copy: '这是非成交价格字段，不代表真实成交价。'
    }
  };

  const STAGE_ORDER = [
    'selection', 'universe', 'bars', 'indicators', 'signal', 'confirmation', 'confirm',
    'execution', 'order', 'fill', 'protection', 'protective', 'position',
    'risk_adjustment', 'risk', 'scale', 'reverse', 'exit', 'close', 'pnl',
    'backtest', 'interrupt', 'event', 'unknown'
  ];

  const LANE_LABELS = {
    universe: 'Universe',
    market_data: 'Market Data',
    compute: 'Compute',
    execution: 'Execution',
    account: 'Account',
    risk: 'Risk',
    backtest: 'Backtest',
    system: 'System',
    selection: 'Selection',
    confirmation: 'Confirmation',
    protection: 'Protection',
    risk_adjustment: 'Risk Adjust',
    scale: 'Scale/Rebuy',
    exit: 'Exit',
    interrupt: 'Interrupt'
  };

  const LANE_TONE_META = {
    selection: { border: '#f472b6', gradient: '#3a1731 #170a16' },
    confirmation: { border: '#fbbf24', gradient: '#3c2b0d #171107' },
    compute: { border: '#22d3ee', gradient: '#0d3540 #061820' },
    execution: { border: '#60a5fa', gradient: '#0f3150 #071827' },
    protection: { border: '#fb7185', gradient: '#3d1720 #1b0a10' },
    risk_adjustment: { border: '#fb923c', gradient: '#3d220f #1b0e06' },
    scale: { border: '#a78bfa', gradient: '#291d4f #100b24' },
    exit: { border: '#f87171', gradient: '#3b1717 #1a0a0a' },
    interrupt: { border: '#ef4444', gradient: '#3c1212 #1a0808' },
    universe: { border: '#a3e635', gradient: '#26380e #101908' },
    market_data: { border: '#38bdf8', gradient: '#0f3348 #071724' },
    account: { border: '#34d399', gradient: '#0f392c #071a14' },
    risk: { border: '#fb923c', gradient: '#3d220f #1b0e06' },
    backtest: { border: '#f6ad55', gradient: '#3a2813 #1d150b' },
    system: { border: '#94a3b8', gradient: '#1f2937 #0d1720' }
  };

  const EVENT_LABELS = {
    target_selected: '选股入选',
    signal_generated: '信号生成',
    signal_pending: '信号待确认',
    signal_pending_confirmation: '信号待确认',
    signal_confirmed: '信号确认',
    signal_rejected: '信号拒绝',
    signal_expired: '信号过期',
    signal_blocked: '信号阻塞',
    signal_status: '信号状态',
    entry_submitted: '开仓提交',
    entry_canceled: '开仓取消',
    entry_filled: '开仓成交',
    entry_partially_filled: '开仓部分成交',
    entry_incremental_fill: '追加开仓',
    fill_execution: 'IBKR 实际成交',
    trade_opened: '交易打开',
    take_profit_created: '止盈单创建',
    take_profit_canceled: '止盈单取消',
    take_profit_modified: '止盈价格修改',
    stop_loss_created: '止损单创建',
    stop_loss_canceled: '止损单取消',
    stop_loss_modified: '止损价格修改',
    partial_take_profit_filled: '部分止盈成交',
    partial_stop_loss_filled: '部分止损成交',
    scale_in_rebuy_filled: '买回/加仓成交',
    reverse_action: '反向/调整事件',
    reverse_adjust_sl: '反向止损调整',
    target_policy_stop_adjust: '目标止损调整',
    exit_policy_stop_adjust: '退出策略止损调整',
    exit_policy_target_adjust: '退出策略止盈调整',
    exit_take_profit: '止盈平仓',
    exit_stop_loss: '止损平仓',
    exit_reverse: '反向平仓',
    exit_eod: '收盘平仓',
    manual_close: '手动平仓',
    trade_closed: '交易平仓',
    close_submitted: '平仓提交',
    close_canceled: '平仓取消',
    order_detail_snapshot: '订单明细快照',
    lifecycle_endpoint: '生命周期收口',
    protection_qty_mismatch: '保护单数量不匹配',
    fill_missing_actual: '缺少实际成交',
    system_interrupt: '系统事件',
    warning_rollup: '异常汇总'
  };

  const STATUS_LABELS = {
    ok: 'OK',
    done: '完成',
    active: '进行中',
    pending: '待处理',
    partially_filled: '部分成交',
    warning: '警告',
    error: '异常',
    blocked: '阻塞',
    terminal: '已结束',
    generated: '已生成',
    filled: '已成交',
    executed: '已执行',
    expired: '已过期',
    rejected: '已拒绝',
    cancelled: '已取消',
    canceled: '已取消',
    unknown: '待确认'
  };

  const GENERIC_LABELS = new Set([
    'unknown',
    'status',
    'status change',
    'status_change',
    'alert',
    'expired',
    'pending',
    'submitted',
    'filled',
    'closed',
    'complete',
    'completed',
    'done'
  ]);

  const state = {
    cy: null,
    model: null,
    selectedNodeId: '',
    showTimeline: false,
    loading: false
  };

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function firstNonEmpty(...values) {
    for (const value of values) {
      if (value === undefined || value === null) continue;
      const text = String(value).trim();
      if (text) return value;
    }
    return '';
  }

  function asObject(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function asArray(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') {
      if (Array.isArray(value.items)) return value.items;
      if (Array.isArray(value.records)) return value.records;
      if (Array.isArray(value.nodes)) return value.nodes;
      if (Array.isArray(value.events)) return value.events;
    }
    return [];
  }

  function normalizeText(value, fallback = '') {
    const text = String(value ?? '').trim();
    return text || fallback;
  }

  function safeClassToken(value, fallback = 'unknown') {
    const text = String(value || fallback).trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '_');
    return text || fallback;
  }

  function humanizeKey(value) {
    const text = String(value || '').trim();
    if (!text) return '--';
    return text
      .replace(/[_-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function labelForEventType(eventType) {
    const normalized = String(eventType || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
    return EVENT_LABELS[normalized] || '';
  }

  function displayStatusLabel(status) {
    const normalized = String(status || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
    return STATUS_LABELS[normalized] || humanizeKey(normalized || 'unknown');
  }

  function displayLifecycleLabel(data, typeText, stage) {
    const source = data && typeof data === 'object' ? data : {};
    const eventType = String(firstNonEmpty(source.event_type, source.type, source.kind, typeText) || '')
      .trim()
      .toLowerCase()
      .replace(/[-\s]+/g, '_');
    const mapped = labelForEventType(eventType);
    const rawLabel = normalizeText(firstNonEmpty(source.label, source.title, source.name));
    const genericLabel = rawLabel.toLowerCase().replace(/[-_]+/g, ' ');
    if (mapped && (!rawLabel || GENERIC_LABELS.has(genericLabel) || rawLabel.toLowerCase() === eventType)) {
      return mapped;
    }
    return rawLabel || mapped || humanizeKey(stage);
  }

  function normalizeEnvironment(value) {
    const text = String(value || '').trim().toLowerCase();
    if (text === 'prod' || text === 'production') return 'live';
    if (text === 'sim' || text === 'simulation') return 'paper';
    return RUNTIME_ENVS.includes(text) ? text : 'live';
  }

  function normalizeFillSource(value) {
    const text = String(value || '').trim().toLowerCase();
    const normalized = text.replace(/[-\s]+/g, '_');
    if (['actual', 'live', 'ibkr', 'real_ibkr', 'ibkr_actual', 'broker', 'live_ibkr'].includes(normalized)) return 'actual_ibkr';
    if (['paper', 'paper_account', 'ibkr_paper', 'paper_account_ibkr'].includes(normalized)) return 'paper_ibkr';
    if (['backtest', 'simulated', 'simulation', 'backtest_sim', 'model', 'replay'].includes(normalized)) return 'backtest_simulated';
    return FILL_SOURCE_META[normalized] ? normalized : 'unknown';
  }

  function fillSourcePill(fillSource) {
    const source = normalizeFillSource(fillSource);
    const meta = FILL_SOURCE_META[source] || FILL_SOURCE_META.unknown;
    return `<span class="fill-pill ${source}" title="${escapeHtml(meta.copy)}">${escapeHtml(meta.label)}</span>`;
  }

  function normalizePriceKind(value) {
    const normalized = String(value || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
    return PRICE_KIND_META[normalized] ? normalized : '';
  }

  function inferPriceKind(record, primary, source) {
    const explicit = normalizePriceKind(firstNonEmpty(getNested(record, 'price_kind'), asObject(record?.data).price_kind));
    if (explicit && explicit !== 'not_a_fill') return explicit;

    const eventType = String(firstNonEmpty(getNested(record, 'event_type'), getNested(record, 'type'), getNested(record, 'kind')) || '')
      .trim()
      .toLowerCase()
      .replace(/[-\s]+/g, '_');
    const role = String(firstNonEmpty(getNested(record, 'role'), getNested(record, 'order_role')) || '')
      .trim()
      .toLowerCase()
      .replace(/[-\s]+/g, '_');
    const rawFillSource = String(firstNonEmpty(getNested(record, 'fill_source'), asObject(record?.data).fill_source) || '')
      .trim()
      .toLowerCase();
    const key = String(primary?.key || '').toLowerCase();
    const haystack = `${eventType} ${role} ${key}`;

    if (eventType === 'order_detail_snapshot' && rawFillSource === 'unknown') return 'order_detail_unverified_fill';
    if (source === 'backtest_simulated' && /fill|filled|opened|closed|trade/.test(haystack)) return 'simulated_fill';
    if ((source === 'actual_ibkr' || source === 'paper_ibkr') && /fill|execution|executed|avg/.test(haystack)) return 'actual_fill';
    if (/take_profit|repair_tp|\btp\b/.test(haystack)) return 'take_profit_price';
    if (/stop_loss|repair_sl|\bsl\b|stop_price/.test(haystack)) return 'stop_loss_price';
    if (/entry_submitted|entry_limit|limit_price/.test(haystack) && /entry|limit/.test(haystack)) return 'entry_limit';
    if (/close_submitted|close_limit/.test(haystack)) return 'close_limit';
    if (/current_price|mark_price/.test(haystack)) return 'market_reference_price';
    if (/limit|price/.test(key)) return 'order_reference_price';
    return explicit || 'not_a_fill';
  }

  function isVerifiedFillKind(priceKind) {
    return priceKind === 'actual_fill' || priceKind === 'simulated_fill';
  }

  function shouldShowFillSourcePill(priceFacts, fillSource) {
    const source = normalizeFillSource(fillSource || priceFacts?.fillSource);
    if (source === 'actual_ibkr' || source === 'paper_ibkr' || source === 'backtest_simulated') return true;
    return Boolean(priceFacts?.hasPrice && isVerifiedFillKind(priceFacts.priceKind));
  }

  function priceKindPill(priceFacts) {
    if (!priceFacts?.hasPrice) return '';
    const kind = normalizePriceKind(priceFacts.priceKind) || 'not_a_fill';
    if (shouldShowFillSourcePill(priceFacts, priceFacts.fillSource)) {
      return fillSourcePill(priceFacts.fillSource);
    }
    const meta = PRICE_KIND_META[kind] || PRICE_KIND_META.not_a_fill;
    return `<span class="fill-pill price_kind ${escapeHtml(safeClassToken(kind))}" title="${escapeHtml(meta.copy)}">${escapeHtml(meta.short)}</span>`;
  }

  function normalizeStatus(value) {
    const text = String(value || '').trim().toLowerCase();
    if (!text) return 'unknown';
    if (['ok', 'success', 'completed', 'complete', 'filled', 'executed', 'done'].includes(text)) return 'ok';
    if (['active', 'open', 'working'].includes(text)) return 'active';
    if (['pending', 'new', 'created', 'queued', 'awaiting', 'waiting', 'partial', 'partially_filled'].includes(text)) return 'pending';
    if (['warn', 'warning', 'stale', 'simulated', 'estimated', 'manual'].includes(text)) return 'warning';
    if (['error', 'failed', 'rejected', 'cancelled', 'canceled', 'blocked', 'expired'].includes(text)) return 'error';
    return text;
  }

  function normalizeStage(value) {
    const text = String(value || '').trim().toLowerCase();
    if (!text) return 'unknown';
    if ([
      'selection', 'signal', 'confirmation', 'execution', 'protection',
      'position', 'risk_adjustment', 'scale', 'exit', 'interrupt'
    ].includes(text)) return text;
    if (/target|watchlist|screener|universe|daily_scan|admission/.test(text)) return 'universe';
    if (/bar|quote|market_data|ohlc|candle/.test(text)) return 'bars';
    if (/indicator|ema|macd|rsi|compute/.test(text)) return 'indicators';
    if (/signal|setup|entry/.test(text)) return 'signal';
    if (/confirm|ack|approve|decision/.test(text)) return 'confirm';
    if (/order|submit|broker|gateway/.test(text)) return 'order';
    if (/fill|execution|avg_fill|trade_fill/.test(text)) return 'fill';
    if (/position|holding|portfolio/.test(text)) return 'position';
    if (/protect|stop|take_profit|tp|sl|risk/.test(text)) return 'protective';
    if (/reverse|flip/.test(text)) return 'reverse';
    if (/close|exit|liquidate/.test(text)) return 'close';
    if (/pnl|profit|loss|account|cash|equity/.test(text)) return 'pnl';
    if (/backtest|replay|sim/.test(text)) return 'backtest';
    if (/event|system|cron|job/.test(text)) return 'event';
    return safeClassToken(text, 'unknown');
  }

  function deriveLane(stage, value) {
    const stageText = normalizeStage(stage);
    const haystack = `${stage || ''} ${value || ''}`.toLowerCase();
    if ([
      'selection', 'confirmation', 'execution', 'protection',
      'position', 'risk_adjustment', 'scale', 'exit', 'interrupt'
    ].includes(stageText)) return stageText;
    if (['universe'].includes(stageText)) return 'universe';
    if (['bars'].includes(stageText) || /bar|quote|market/.test(haystack)) return 'market_data';
    if (['indicators', 'signal', 'confirm'].includes(stageText) || /compute|indicator|signal/.test(haystack)) return 'compute';
    if (['order', 'fill', 'reverse', 'close'].includes(stageText) || /order|fill|broker|gateway|reverse|close/.test(haystack)) return 'execution';
    if (['position', 'pnl'].includes(stageText) || /account|position|portfolio|pnl/.test(haystack)) return 'account';
    if (['protective'].includes(stageText) || /risk|stop|protect|tp|sl/.test(haystack)) return 'risk';
    if (['backtest'].includes(stageText) || /backtest|replay|sim/.test(haystack)) return 'backtest';
    return 'system';
  }

  function normalizeTimeValue(value) {
    if (value === undefined || value === null || value === '') return '';
    const text = String(value).trim();
    if (!text) return '';
    if (/^\d{10,13}$/.test(text)) {
      const ms = text.length === 10 ? Number(text) * 1000 : Number(text);
      const date = new Date(ms);
      return Number.isNaN(date.getTime()) ? text : date.toISOString();
    }
    return text;
  }

  function formatTime(value) {
    const text = normalizeTimeValue(value);
    if (!text) return '--';
    if (typeof formatTimeLabel === 'function') {
      try {
        const formatted = formatTimeLabel(text);
        if (formatted) return formatted;
      } catch (_) {
        // Keep local fallback below.
      }
    }
    const date = new Date(text);
    if (!Number.isNaN(date.getTime())) {
      try {
        return new Intl.DateTimeFormat('en-US', {
          timeZone: 'America/New_York',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hourCycle: 'h23'
        }).format(date).replace(',', '') + ' ET';
      } catch (_) {
        return date.toISOString().replace('T', ' ').slice(0, 19);
      }
    }
    return text.replace('T', ' ').slice(0, 24);
  }

  function getNested(record, key) {
    if (!record || typeof record !== 'object') return undefined;
    if (Object.prototype.hasOwnProperty.call(record, key)) return record[key];
    const extra = asObject(record.extra);
    if (Object.prototype.hasOwnProperty.call(extra, key)) return extra[key];
    const payload = asObject(record.payload);
    if (Object.prototype.hasOwnProperty.call(payload, key)) return payload[key];
    const context = asObject(record.context);
    if (Object.prototype.hasOwnProperty.call(context, key)) return context[key];
    const recordObj = asObject(record.record);
    if (Object.prototype.hasOwnProperty.call(recordObj, key)) return recordObj[key];
    return undefined;
  }

  function isPresent(value) {
    return value !== undefined && value !== null && value !== '';
  }

  function formatChangeValue(value) {
    if (!isPresent(value)) return '--';
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    const numeric = Number(value);
    if (Number.isFinite(numeric) && String(value).trim() !== '') {
      return numeric.toLocaleString('en-US', { maximumFractionDigits: 4 });
    }
    return String(value);
  }

  function firstValueFromRoots(roots, keys) {
    for (const root of roots) {
      if (!root || typeof root !== 'object') continue;
      for (const key of keys) {
        if (Object.prototype.hasOwnProperty.call(root, key) && isPresent(root[key])) return root[key];
      }
    }
    return '';
  }

  function normalizeChangeItem(item) {
    const data = asObject(item);
    const field = normalizeText(firstNonEmpty(data.field, data.key, data.name));
    const label = normalizeText(firstNonEmpty(data.label, data.title, field));
    const before = firstNonEmpty(data.before, data.old, data.previous, data.from);
    const after = firstNonEmpty(data.after, data.new, data.current, data.to);
    if (!label || (!isPresent(before) && !isPresent(after))) return null;
    if (String(before ?? '') === String(after ?? '')) return null;
    return {
      field,
      label,
      before,
      after,
      beforeText: normalizeText(firstNonEmpty(data.before_text, data.beforeText), formatChangeValue(before)),
      afterText: normalizeText(firstNonEmpty(data.after_text, data.afterText), formatChangeValue(after))
    };
  }

  function fallbackChangeItems(record) {
    const data = asObject(record?.data);
    const roots = [
      record,
      data,
      asObject(record?.details),
      asObject(data.details),
      asObject(record?.extra),
      asObject(data.extra)
    ];
    const specs = [
      {
        field: 'stop_loss',
        label: 'SL',
        before: ['old_sl', 'old_stop_loss', 'previous_sl', 'previous_stop_loss', 'before_sl'],
        after: ['new_sl', 'stop_loss', 'sl_price', 'stop_price']
      },
      {
        field: 'take_profit',
        label: 'TP',
        before: ['old_tp', 'old_take_profit', 'previous_tp', 'previous_take_profit', 'before_tp'],
        after: ['new_tp', 'take_profit', 'tp_price', 'target_price']
      },
      {
        field: 'status',
        label: '状态',
        before: ['previous_status', 'old_status', 'status_before'],
        after: ['current_status', 'new_status', 'status_after', 'status'],
        requireBefore: true
      }
    ];
    return specs
      .map((spec) => {
        const before = firstValueFromRoots(roots, spec.before);
        if (spec.requireBefore && !isPresent(before)) return null;
        return normalizeChangeItem({
          field: spec.field,
          label: spec.label,
          before,
          after: firstValueFromRoots(roots, spec.after)
        });
      })
      .filter(Boolean);
  }

  function collectChangeItems(record) {
    const data = asObject(record?.data);
    const roots = [record, data, asObject(record?.details), asObject(data.details), asObject(record?.extra), asObject(data.extra)];
    for (const root of roots) {
      if (Array.isArray(root?.changes)) {
        const changes = root.changes.map(normalizeChangeItem).filter(Boolean);
        if (changes.length) return changes;
      }
    }
    return fallbackChangeItems(record || {});
  }

  function changeSummaryForRecord(record) {
    const data = asObject(record?.data);
    const explicit = normalizeText(firstNonEmpty(
      record?.change_summary,
      record?.changeSummary,
      data.change_summary,
      data.changeSummary,
      asObject(record?.details).change_summary,
      asObject(data.details).change_summary
    ));
    if (explicit) return explicit;
    const changes = collectChangeItems(record || {});
    return changes.slice(0, 3)
      .map((item) => `${item.label} ${item.beforeText} -> ${item.afterText}`)
      .join(' / ');
  }

  function collectPriceCandidates(record) {
    const data = asObject(record?.data);
    const roots = [
      record,
      data,
      asObject(record?.details),
      asObject(data.details),
      asObject(record?.extra),
      asObject(record?.payload),
      asObject(record?.record),
      asObject(record?.context)
    ];
    const fields = [
      ['avg_fill_price', 'Avg Fill'],
      ['average_fill_price', 'Avg Fill'],
      ['fill_price', 'Fill'],
      ['filled_price', 'Fill'],
      ['execution_price', 'Execution'],
      ['executed_price', 'Execution'],
      ['price', 'Price'],
      ['entry_price', 'Entry'],
      ['exit_price', 'Exit'],
      ['limit_price', 'Limit'],
      ['stop_price', 'Stop'],
      ['take_profit_price', 'Take Profit'],
      ['stop_loss_price', 'Stop Loss'],
      ['current_price', 'Mark'],
      ['mark_price', 'Mark'],
      ['estimated_price', 'Estimated'],
      ['estimated_fill_price', 'Estimated Fill'],
      ['estimated_entry_price', 'Estimated Entry'],
      ['estimated_exit_price', 'Estimated Exit']
    ];
    const candidates = [];
    const seen = new Set();

    for (const root of roots) {
      if (!root || typeof root !== 'object') continue;
      for (const [key, label] of fields) {
        if (!Object.prototype.hasOwnProperty.call(root, key)) continue;
        const value = root[key];
        if (value === undefined || value === null || value === '') continue;
        const numeric = Number(value);
        if (!Number.isFinite(numeric) && !String(value).trim()) continue;
        const signature = `${key}:${String(value)}`;
        if (seen.has(signature)) continue;
        seen.add(signature);
        candidates.push({ key, label, value, numeric, estimated: key.includes('estimated') });
      }
    }
    return candidates;
  }

  function inferEstimatedFlag(record, candidates) {
    const sourceText = [
      getNested(record, 'price_source'),
      getNested(record, 'source'),
      getNested(record, 'fill_price_source'),
      getNested(record, 'execution_price_source')
    ].map((value) => String(value || '').toLowerCase()).join(' ');
    return /estimate|estimated|proxy|derived/.test(sourceText) || candidates.some((item) => item.estimated);
  }

  function getPriceFacts(record, fillSource) {
    const source = normalizeFillSource(fillSource || getNested(record, 'fill_source'));
    const candidates = collectPriceCandidates(record || {});
    const primary = candidates.find((item) => /fill|execution|executed|avg/.test(item.key)) || candidates[0] || null;
    const estimated = inferEstimatedFlag(record || {}, candidates);
    const priceKind = inferPriceKind(record || {}, primary, source);
    const kindMeta = PRICE_KIND_META[priceKind] || PRICE_KIND_META.not_a_fill;
    if (!primary) {
      return {
        hasPrice: false,
        fillSource: source,
        priceKind,
        priceLabel: kindMeta.label,
        isFillPrice: isVerifiedFillKind(priceKind),
        estimated,
        value: '',
        valueText: '--',
        label: '无价格字段',
        shortLabel: '',
        trustTitle: kindMeta.title || FILL_SOURCE_META[source].title,
        trustCopy: kindMeta.copy || FILL_SOURCE_META[source].copy,
        tone: kindMeta.tone || FILL_SOURCE_META[source].tone,
        candidates
      };
    }

    const valueText = Number.isFinite(primary.numeric)
      ? primary.numeric.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
      : String(primary.value);
    const meta = FILL_SOURCE_META[source] || FILL_SOURCE_META.unknown;
    let label = normalizeText(firstNonEmpty(getNested(record, 'price_label'), kindMeta.label, primary.label));
    let shortLabel = `${kindMeta.short || 'REF'} ${valueText}`;
    let trustTitle = kindMeta.title || meta.title;
    let trustCopy = kindMeta.copy || meta.copy;
    let tone = kindMeta.tone || meta.tone;

    if (estimated) {
      label = `估算${label}`;
      shortLabel = `EST ${valueText}`;
      trustTitle = '估算价格（非真实成交）';
      trustCopy = '该价格来自 estimated/proxy/derived 字段，只能辅助判断，不能当真实成交价展示。';
      tone = 'unverified';
    } else if (priceKind === 'actual_fill') {
      label = `${label}（${meta.title}）`;
      shortLabel = `${meta.short} ${valueText}`;
      trustTitle = meta.title;
      trustCopy = meta.copy;
      tone = meta.tone;
    } else if (source === 'backtest_simulated') {
      label = `${label}（回测模拟）`;
      shortLabel = `SIM ${valueText}`;
      trustTitle = FILL_SOURCE_META.backtest_simulated.title;
      trustCopy = FILL_SOURCE_META.backtest_simulated.copy;
      tone = FILL_SOURCE_META.backtest_simulated.tone;
    } else if (source === 'paper_ibkr') {
      label = `${label}（Paper IBKR）`;
      shortLabel = `PAPER ${valueText}`;
      trustTitle = FILL_SOURCE_META.paper_ibkr.title;
      trustCopy = FILL_SOURCE_META.paper_ibkr.copy;
      tone = FILL_SOURCE_META.paper_ibkr.tone;
    } else if (source === 'actual_ibkr' && isVerifiedFillKind(priceKind)) {
      label = `${label}（actual IBKR）`;
      shortLabel = `IBKR ${valueText}`;
      trustTitle = FILL_SOURCE_META.actual_ibkr.title;
      trustCopy = FILL_SOURCE_META.actual_ibkr.copy;
      tone = FILL_SOURCE_META.actual_ibkr.tone;
    } else if (source === 'unknown' && isVerifiedFillKind(priceKind)) {
      label = `${label}（来源未知）`;
      shortLabel = `UNK ${valueText}`;
      trustTitle = FILL_SOURCE_META.unknown.title;
      trustCopy = FILL_SOURCE_META.unknown.copy;
      tone = FILL_SOURCE_META.unknown.tone;
    }

    return {
      hasPrice: true,
      fillSource: source,
      priceKind,
      priceLabel: label,
      isFillPrice: isVerifiedFillKind(priceKind),
      estimated,
      value: primary.value,
      valueText,
      label,
      shortLabel,
      trustTitle,
      trustCopy,
      tone,
      candidates
    };
  }

  function extractElementData(item) {
    if (!item || typeof item !== 'object') return {};
    if (item.data && typeof item.data === 'object') return { ...item.data, __raw: item };
    return item;
  }

  function stageRank(stage) {
    const normalized = normalizeStage(stage);
    const index = STAGE_ORDER.indexOf(normalized);
    return index >= 0 ? index : STAGE_ORDER.length - 1;
  }

  function compareByTimeOrStage(a, b) {
    const aTime = Date.parse(normalizeTimeValue(a.time || a.created || a.timestamp || ''));
    const bTime = Date.parse(normalizeTimeValue(b.time || b.created || b.timestamp || ''));
    if (Number.isFinite(aTime) && Number.isFinite(bTime) && aTime !== bTime) return aTime - bTime;
    if (Number.isFinite(aTime) && !Number.isFinite(bTime)) return -1;
    if (!Number.isFinite(aTime) && Number.isFinite(bTime)) return 1;
    return stageRank(a.stage) - stageRank(b.stage);
  }

  function normalizeNode(raw, index) {
    const data = extractElementData(raw);
    const extra = asObject(data.extra);
    const id = normalizeText(firstNonEmpty(
      data.id,
      data.node_id,
      data.key,
      data.unique_id,
      data.order_unique_id,
      data.signal_id,
      data.trade_group_id,
      `node_${index + 1}`
    ));
    const typeText = normalizeText(firstNonEmpty(data.type, data.kind, data.collection, data.entity, data.event_type, data.table));
    const stage = normalizeStage(firstNonEmpty(data.stage, data.phase, data.lifecycle_stage, typeText, data.label, data.title));
    const lane = safeClassToken(firstNonEmpty(data.lane, data.swimlane, data.domain, deriveLane(stage, `${typeText} ${data.label || ''}`)), 'system');
    const status = normalizeStatus(firstNonEmpty(data.status, data.state, data.outcome, data.result, data.order_status));
    const symbol = normalizeText(firstNonEmpty(data.symbol, data.ticker, getNested(data, 'symbol'))).toUpperCase();
    const signalId = normalizeText(firstNonEmpty(data.signal_id, getNested(data, 'signal_id')));
    const tradeGroupId = normalizeText(firstNonEmpty(data.trade_group_id, getNested(data, 'trade_group_id')));
    const orderId = normalizeText(firstNonEmpty(data.order_id, data.broker_order_id, data.order_unique_id, getNested(data, 'order_id')));
    const fillSource = normalizeFillSource(firstNonEmpty(
      data.fill_source,
      data.fillSource,
      data.execution_fill_source,
      data.price_fill_source,
      data.source_fill,
      extra.fill_source,
      data.environment === 'backtest' ? 'backtest_simulated' : ''
    ));
    const label = displayLifecycleLabel(data, typeText, stage);
    const time = normalizeTimeValue(firstNonEmpty(
      data.time,
      data.timestamp,
      data.event_time,
      data.created,
      data.updated,
      data.bar_time,
      data.ts_ms,
      data.bar_time_ms,
      extra.event_time,
      extra.created_at
    ));
    const priceFacts = getPriceFacts(data, fillSource);
    const copy = normalizeText(firstNonEmpty(
      data.copy,
      data.summary,
      data.message,
      data.reason,
      data.description,
      extra.message,
      extra.reason,
      typeText
    ));
    const displayTitle = symbol ? `${symbol} · ${label}` : label;
    const timeLabel = formatTime(time);
    const changeSummary = changeSummaryForRecord(data);
    const displayLabelParts = [displayTitle, `${timeLabel} · ${displayStatusLabel(status)}`];
    const compactFacts = changeSummary || priceFacts.shortLabel;
    if (compactFacts) displayLabelParts.push(compactFacts);

    return {
      id,
      label,
      displayLabel: displayLabelParts.join('\n'),
      type: typeText || stage,
      symbol,
      signalId,
      tradeGroupId,
      orderId,
      stage,
      lane,
      laneLabel: LANE_LABELS[lane] || humanizeKey(lane),
      status,
      fillSource,
      time,
      copy,
      raw: data.__raw || raw,
      data,
      priceFacts,
      changes: collectChangeItems(data),
      changeSummary,
      rank: stageRank(stage)
    };
  }

  function normalizeEdge(raw, index, nodeIdSet) {
    const data = extractElementData(raw);
    const source = normalizeText(firstNonEmpty(data.source, data.from, data.source_id, data.sourceId, data.src));
    const target = normalizeText(firstNonEmpty(data.target, data.to, data.target_id, data.targetId, data.dst));
    if (!source || !target || !nodeIdSet.has(source) || !nodeIdSet.has(target)) return null;
    const rawLabel = normalizeText(firstNonEmpty(data.label, data.title, data.type, data.reason));
    const label = ['next', 'sequence', 'auto'].includes(rawLabel.toLowerCase()) ? '' : rawLabel;
    return {
      id: normalizeText(firstNonEmpty(data.id, data.edge_id, `${source}__${target}__${index}`)),
      source,
      target,
      label,
      status: normalizeStatus(firstNonEmpty(data.status, data.state, data.outcome)),
      raw: data.__raw || raw,
      data
    };
  }

  function normalizeEvent(raw, index) {
    const data = extractElementData(raw);
    const typeText = normalizeText(firstNonEmpty(data.type, data.kind, data.event_type, data.collection, data.table));
    const stage = normalizeStage(firstNonEmpty(data.stage, data.phase, data.lifecycle_stage, typeText, data.label, data.title));
    const fillSource = normalizeFillSource(firstNonEmpty(data.fill_source, data.fillSource, data.execution_fill_source, getNested(data, 'fill_source')));
    const status = normalizeStatus(firstNonEmpty(data.status, data.state, data.outcome, data.result));
    const title = displayLifecycleLabel(data, typeText, stage);
    const time = normalizeTimeValue(firstNonEmpty(data.time, data.timestamp, data.event_time, data.created, data.updated, data.ts_ms, data.bar_time_ms));
    const copy = normalizeText(firstNonEmpty(data.message, data.copy, data.summary, data.reason, data.description, data.detail));
    const nodeId = normalizeText(firstNonEmpty(data.node_id, data.id, data.order_unique_id, data.signal_id, data.trade_group_id, `event_${index + 1}`));
    const symbol = normalizeText(firstNonEmpty(data.symbol, data.ticker, getNested(data, 'symbol'))).toUpperCase();
    const changeSummary = changeSummaryForRecord(data);
    return {
      id: nodeId,
      title,
      symbol,
      signalId: normalizeText(firstNonEmpty(data.signal_id, getNested(data, 'signal_id'))),
      tradeGroupId: normalizeText(firstNonEmpty(data.trade_group_id, getNested(data, 'trade_group_id'))),
      orderId: normalizeText(firstNonEmpty(data.order_id, data.broker_order_id, data.order_unique_id, getNested(data, 'order_id'))),
      stage,
      lane: safeClassToken(firstNonEmpty(data.lane, data.swimlane, deriveLane(stage, `${typeText} ${title}`)), 'system'),
      status,
      fillSource,
      time,
      copy,
      raw: data.__raw || raw,
      data,
      priceFacts: getPriceFacts(data, fillSource),
      changes: collectChangeItems(data),
      changeSummary
    };
  }

  function pickGraphPayload(payload) {
    const root = asObject(payload);
    return asObject(root.graph) || asObject(root.flow) || root;
  }

  function findArrayPayload(payload, keys) {
    const root = asObject(payload);
    const graph = pickGraphPayload(payload);
    const candidates = [root, graph, asObject(root.data), asObject(root.lifecycle), asObject(root.flow), asObject(graph.elements)];
    for (const candidate of candidates) {
      if (!candidate || typeof candidate !== 'object') continue;
      for (const key of keys) {
        if (Array.isArray(candidate[key])) return candidate[key];
      }
    }
    return [];
  }

  function buildNodesFromEvents(events) {
    return events.map((event, index) => normalizeNode({
      id: event.id || `event_node_${index + 1}`,
      label: event.title,
      stage: event.stage,
      lane: event.lane,
      status: event.status,
      symbol: event.symbol,
      signal_id: event.signalId,
      trade_group_id: event.tradeGroupId,
      order_id: event.orderId,
      fill_source: event.fillSource,
      time: event.time,
      message: event.copy,
      changes: event.changes,
      change_summary: event.changeSummary,
      raw_event: event.raw
    }, index));
  }

  function buildSequentialEdges(nodes) {
    return nodes.slice(1).map((node, index) => ({
      id: `${nodes[index].id}__${node.id}__auto`,
      source: nodes[index].id,
      target: node.id,
      label: '',
      status: 'ok',
      raw: { inferred: true }
    }));
  }

  function normalizeWarnings(payload, nodes) {
    const root = asObject(payload);
    const rawWarnings = [
      ...asArray(root.warnings),
      ...asArray(root.warning),
      ...asArray(root.issues),
      ...asArray(root.alerts),
      ...asArray(asObject(root.summary).warnings)
    ];

    const warnings = rawWarnings.map((item, index) => {
      if (typeof item === 'string') {
        return { id: `api_warning_${index}`, severity: 'medium', title: 'API Warning', copy: item };
      }
      const data = asObject(item);
      return {
        id: normalizeText(firstNonEmpty(data.id, data.code, `api_warning_${index}`)),
        severity: normalizeText(firstNonEmpty(data.severity, data.level, data.tone, 'medium')).toLowerCase(),
        title: normalizeText(firstNonEmpty(data.title, data.label, data.code, 'Lifecycle warning')),
        copy: normalizeText(firstNonEmpty(data.message, data.copy, data.detail, data.reason, JSON.stringify(data)))
      };
    });

    const pricedNodes = nodes.filter((node) => node.priceFacts.hasPrice);
    if (pricedNodes.some((node) => node.fillSource === 'unknown' && node.priceFacts.isFillPrice)) {
      warnings.push({
        id: 'unknown_fill_source_prices',
        severity: 'high',
        title: '存在 unknown fill_source 价格',
        copy: '这些价格来源未知，页面只会标记为未验证，不会展示成真实 IBKR 成交价。'
      });
    }
    if (pricedNodes.some((node) => node.priceFacts.estimated)) {
      warnings.push({
        id: 'estimated_prices_present',
        severity: 'high',
        title: '存在估算价格字段',
        copy: 'estimated/proxy/derived 价格仅作为辅助信息；不能替代 actual_ibkr 或 paper_ibkr 成交回填。'
      });
    }
    if (nodes.some((node) => node.priceFacts.priceKind === 'simulated_fill' && node.priceFacts.hasPrice)) {
      warnings.push({
        id: 'backtest_simulated_prices',
        severity: 'medium',
        title: '存在 backtest_simulated 价格',
        copy: '回测模拟价用于复盘和校验，不是真实 IBKR 成交价。'
      });
    }
    if (!nodes.length) {
      warnings.push({
        id: 'empty_lifecycle',
        severity: 'medium',
        title: '未返回流程节点',
        copy: '请检查筛选条件或确认 /api/custom/ibkr/lifecycle-flow 是否已返回 nodes/events。'
      });
    }

    return warnings;
  }

  function normalizeCurrent(payload, nodes, events) {
    const root = asObject(payload);
    const current = asObject(root.current_step || root.current || root.current_stage || root.current_phase || asObject(root.summary).current);
    const currentId = normalizeText(firstNonEmpty(current.node_id, current.id, root.current_node_id, asObject(root.summary).current_node_id));
    let node = currentId ? nodes.find((item) => item.id === currentId) : null;
    if (!node) {
      node = nodes.find((item) => ['pending', 'warning', 'error'].includes(item.status)) || nodes[nodes.length - 1] || null;
    }
    if (node) return { ...node, fromNode: true, apiCurrent: current };

    const event = events[events.length - 1] || null;
    if (event) {
      return {
        id: event.id,
        label: event.title,
        symbol: event.symbol,
        signalId: event.signalId,
        tradeGroupId: event.tradeGroupId,
        orderId: event.orderId,
        stage: event.stage,
        lane: event.lane,
        status: event.status,
        fillSource: event.fillSource,
        time: event.time,
        copy: event.copy,
        priceFacts: event.priceFacts,
        raw: event.raw,
        fromEvent: true,
        apiCurrent: current
      };
    }

    if (Object.keys(current).length) {
      const fillSource = normalizeFillSource(firstNonEmpty(current.fill_source, current.fillSource));
      return {
        id: normalizeText(firstNonEmpty(current.id, 'current')),
        label: normalizeText(firstNonEmpty(current.label, current.title, current.stage, '当前阶段')),
        symbol: normalizeText(firstNonEmpty(current.symbol, getNested(current, 'symbol'))).toUpperCase(),
        signalId: normalizeText(firstNonEmpty(current.signal_id, getNested(current, 'signal_id'))),
        tradeGroupId: normalizeText(firstNonEmpty(current.trade_group_id, getNested(current, 'trade_group_id'))),
        orderId: normalizeText(firstNonEmpty(current.order_id, current.broker_order_id, getNested(current, 'order_id'))),
        stage: normalizeStage(firstNonEmpty(current.stage, current.phase)),
        lane: deriveLane(current.stage, current.label),
        status: normalizeStatus(firstNonEmpty(current.status, current.state)),
        fillSource,
        time: normalizeTimeValue(firstNonEmpty(current.time, current.timestamp, current.updated, current.created)),
        copy: normalizeText(firstNonEmpty(current.message, current.copy, current.summary, current.reason)),
        priceFacts: getPriceFacts(current, fillSource),
        raw: current,
        apiCurrent: current
      };
    }

    return null;
  }

  function isPlaceholderUnknownNode(node, connectedIds) {
    if (!node || connectedIds.has(node.id)) return false;
    const label = String(node.label || '').trim().toLowerCase();
    const status = String(node.status || '').trim().toLowerCase();
    const type = String(node.type || '').trim().toLowerCase();
    const copy = String(node.copy || '').trim().toLowerCase();
    const noMeaningfulText = (!label || label === 'unknown' || label === '--')
      && (!copy || copy === 'unknown' || copy === '--');
    const noMeaningfulType = !type || type === 'unknown' || type === 'event';
    return noMeaningfulText
      && status === 'unknown'
      && noMeaningfulType
      && !node.time
      && !node.priceFacts?.hasPrice;
  }

  function normalizeModel(payload) {
    const root = asObject(payload);
    const rawNodes = findArrayPayload(root, ['nodes', 'node_items']);
    const rawEdges = findArrayPayload(root, ['edges', 'links', 'edge_items']);
    const rawEvents = findArrayPayload(root, ['events', 'timeline', 'logs', 'event_items', 'items']);

    let events = rawEvents.map(normalizeEvent).sort(compareByTimeOrStage);
    let nodes = rawNodes.map(normalizeNode).sort(compareByTimeOrStage);
    if (!nodes.length && events.length) nodes = buildNodesFromEvents(events);

    const seenNodes = new Map();
    nodes.forEach((node) => {
      if (!seenNodes.has(node.id)) seenNodes.set(node.id, node);
    });
    nodes = Array.from(seenNodes.values()).sort(compareByTimeOrStage);
    nodes = nodes.filter((node) => String(node.type || '').trim().toLowerCase() !== 'order_detail_snapshot');

    const nodeIdSet = new Set(nodes.map((node) => node.id));
    let edges = rawEdges.map((edge, index) => normalizeEdge(edge, index, nodeIdSet)).filter(Boolean);
    if (!edges.length && nodes.length > 1) edges = buildSequentialEdges(nodes);

    const connectedIds = new Set();
    edges.forEach((edge) => {
      connectedIds.add(edge.source);
      connectedIds.add(edge.target);
    });
    const filteredNodes = nodes.filter((node) => !isPlaceholderUnknownNode(node, connectedIds));
    if (filteredNodes.length !== nodes.length) {
      const filteredIds = new Set(filteredNodes.map((node) => node.id));
      nodes = filteredNodes;
      edges = edges.filter((edge) => filteredIds.has(edge.source) && filteredIds.has(edge.target));
    }

    const warnings = normalizeWarnings(root, nodes);
    const current = normalizeCurrent(root, nodes, events);
    const summary = asObject(root.summary || root.meta || pickGraphPayload(root).summary);

    return {
      raw: root,
      nodes,
      edges,
      events,
      warnings,
      current,
      summary,
      generatedAt: normalizeTimeValue(firstNonEmpty(root.generated_at, root.updated_at, root.created_at, summary.generated_at, new Date().toISOString()))
    };
  }

  function getDefaultDate() {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get('date') || '';
    if (fromUrl) return fromUrl;
    const stored = localStorage.getItem(STORAGE_DATE_KEY) || '';
    if (/^\d{4}-\d{2}-\d{2}$/.test(stored)) return stored;
    if (typeof getCurrentEtDateString === 'function') return getCurrentEtDateString();
    return new Date().toISOString().slice(0, 10);
  }

  function readFiltersFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return {
      mode: normalizeText(params.get('mode'), 'auto'),
      environment: normalizeEnvironment(params.get('environment') || (typeof getCurrentRuntimeEnvironment === 'function' ? getCurrentRuntimeEnvironment() : 'live')),
      date: params.get('date') || getDefaultDate(),
      symbol: normalizeText(params.get('symbol')).toUpperCase(),
      signal_id: normalizeText(params.get('signal_id')),
      trade_group_id: normalizeText(params.get('trade_group_id')),
      order_id: normalizeText(params.get('order_id')),
      run_id: normalizeText(params.get('run_id')),
      backtest_date: normalizeText(params.get('backtest_date'))
    };
  }

  function getFiltersFromForm() {
    return {
      mode: normalizeText($('modeInput')?.value, 'auto'),
      environment: normalizeEnvironment($('environmentInput')?.value),
      date: normalizeText($('dateInput')?.value),
      symbol: normalizeText($('symbolInput')?.value).toUpperCase(),
      signal_id: normalizeText($('signalIdInput')?.value),
      trade_group_id: normalizeText($('tradeGroupIdInput')?.value),
      order_id: normalizeText(new URLSearchParams(window.location.search).get('order_id')),
      run_id: normalizeText($('runIdInput')?.value),
      backtest_date: normalizeText($('backtestDateInput')?.value)
    };
  }

  function setFiltersToForm(filters) {
    const safe = filters || readFiltersFromUrl();
    if ($('modeInput')) $('modeInput').value = safe.mode || 'auto';
    if ($('environmentInput')) $('environmentInput').value = normalizeEnvironment(safe.environment);
    if ($('dateInput')) $('dateInput').value = safe.date || '';
    if ($('symbolInput')) $('symbolInput').value = safe.symbol || '';
    if ($('signalIdInput')) $('signalIdInput').value = safe.signal_id || '';
    if ($('tradeGroupIdInput')) $('tradeGroupIdInput').value = safe.trade_group_id || '';
    if ($('runIdInput')) $('runIdInput').value = safe.run_id || '';
    if ($('backtestDateInput')) $('backtestDateInput').value = safe.backtest_date || '';
  }

  function compactQuery(filters) {
    const query = {};
    QUERY_KEYS.forEach((key) => {
      const value = normalizeText(filters[key]);
      if (!value) return;
      if (key === 'mode' && value === 'auto') return;
      query[key] = key === 'symbol' ? value.toUpperCase() : value;
    });
    if (!query.mode && query.run_id) query.mode = 'backtest';
    if (!query.environment) query.environment = normalizeEnvironment(filters.environment);
    return query;
  }

  function buildEndpointUrl(filters) {
    const params = compactQuery(filters);
    return buildPageUrl(ENDPOINT_PATH, params, { environment: normalizeEnvironment(filters.environment) });
  }

  function buildPageStateUrl(filters) {
    const params = compactQuery(filters);
    return buildPageUrl(PAGE_PATH, params, { environment: normalizeEnvironment(filters.environment) });
  }

  function effectiveMode(filters) {
    const mode = normalizeText(filters?.mode, 'auto').toLowerCase();
    if (mode === 'backtest' || normalizeEnvironment(filters?.environment) === 'backtest' || normalizeText(filters?.run_id)) return 'backtest';
    if (mode === 'paper' || normalizeEnvironment(filters?.environment) === 'paper') return 'paper';
    return 'live';
  }

  function hasLifecycleContext(filters) {
    const mode = effectiveMode(filters);
    if (mode === 'backtest') return Boolean(normalizeText(filters?.run_id));
    return Boolean(
      normalizeText(filters?.symbol)
      || normalizeText(filters?.signal_id)
      || normalizeText(filters?.trade_group_id)
      || normalizeText(filters?.order_id)
    );
  }

  async function requestLifecycleJson(filters) {
    const token = typeof getToken === 'function' ? getToken() : '';
    const headers = { Accept: 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;
    const url = buildEndpointUrl(filters);
    const fetcher = typeof fetchWithRetry === 'function'
      ? fetchWithRetry(url, { headers }, { attempts: 3, retryDelayMs: 500 })
      : fetch(url, { headers, cache: 'no-store' });
    const response = await fetcher;
    const text = await response.text();
    let payload = {};
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (_) {
      payload = { ok: false, raw: text };
    }
    if (response.status === 401 || response.status === 403) {
      if (typeof handleAuthError === 'function') handleAuthError();
      throw new Error('Authentication failed');
    }
    if (!response.ok) {
      throw new Error(payload.message || payload.error || `Request failed (${response.status})`);
    }
    return payload;
  }

  function setLoading(active) {
    state.loading = active;
    ['fitGraphButton', 'relayoutGraphButton', 'toggleTimelineButton'].forEach((id) => {
      const node = $(id);
      if (node) node.disabled = active;
    });
    const submit = document.querySelector('#lifecycleFilterForm button[type="submit"]');
    if (submit) submit.disabled = active;
    if ($('refreshInfo')) {
      $('refreshInfo').textContent = active ? '加载生命周期...' : `更新 ${typeof formatEtRefreshClock === 'function' ? formatEtRefreshClock(new Date()) : new Date().toLocaleTimeString()}`;
    }
  }

  function renderLoadingState() {
    if ($('currentStageCard')) {
      $('currentStageCard').innerHTML = '<div class="card-kicker">Current Phase</div><div class="loading-state">正在读取生命周期流程...</div>';
    }
    if ($('warningCard')) {
      $('warningCard').innerHTML = '<div class="card-kicker">Warnings</div><div class="loading-state">正在检查 fill_source 与链路异常...</div>';
    }
    if ($('cyGraph')) $('cyGraph').innerHTML = '';
    if ($('timelineFallback')) $('timelineFallback').innerHTML = '<div class="loading-state">正在生成时间线...</div>';
    if ($('nodeDetails')) $('nodeDetails').innerHTML = '<div class="loading-state">等待节点...</div>';
    if ($('eventList')) $('eventList').innerHTML = '<div class="loading-state">正在读取事件...</div>';
  }

  function renderError(error) {
    const message = escapeHtml(error?.message || String(error || 'unknown error'));
    const html = `<div class="error-state">生命周期流程读取失败：${message}<br><span class="mono">${escapeHtml(ENDPOINT_PATH)}</span></div>`;
    if ($('currentStageCard')) $('currentStageCard').innerHTML = `<div class="card-kicker">Current Phase</div>${html}`;
    if ($('warningCard')) $('warningCard').innerHTML = `<div class="card-kicker">Warnings</div><div class="warning-list"><div class="warning-item severity-high"><div class="warning-title">请求失败</div><div class="warning-copy">${message}</div></div></div>`;
    if ($('timelineFallback')) {
      $('timelineFallback').hidden = false;
      $('timelineFallback').innerHTML = html;
    }
    if ($('cyGraph')) $('cyGraph').hidden = true;
    if ($('nodeDetails')) $('nodeDetails').innerHTML = html;
    if ($('eventList')) $('eventList').innerHTML = html;
    if ($('graphModeInfo')) $('graphModeInfo').textContent = 'Error fallback';
  }

  function renderGuideState(filters) {
    const model = {
      raw: {},
      nodes: [],
      edges: [],
      events: [],
      warnings: [],
      current: null,
      summary: {},
      generatedAt: new Date().toISOString()
    };
    state.model = model;
    if (state.cy) {
      state.cy.destroy();
      state.cy = null;
    }
    updateContext(filters, model);
    if ($('currentStageCard')) {
      $('currentStageCard').innerHTML = `
        <div class="card-kicker">Current Phase</div>
        <div class="stage-title">选择一笔交易或回测</div>
        <div class="stage-copy">从信号、订单、当前标的、回测 Tracking 进入会自动带上上下文；也可以手动填写 symbol / signal_id / trade_group_id / order_id 或 backtest run_id。</div>
        <div class="stage-chip-row">
          <span class="flow-chip">live/paper: symbol 或 order_id</span>
          <span class="flow-chip">backtest: run_id</span>
          <span class="flow-chip">严格区分 actual / paper / simulated</span>
        </div>
      `;
    }
    if ($('warningCard')) {
      $('warningCard').innerHTML = `
        <div class="card-kicker">Warnings</div>
        <div class="warning-list">
          <div class="warning-item severity-low">
            <div class="warning-title">入口提示</div>
            <div class="warning-copy">默认不会拉全局系统事件，避免出现与交易无关的孤立节点。请先选定一笔交易生命周期。</div>
          </div>
        </div>
      `;
    }
    if ($('laneStrip')) {
      $('laneStrip').innerHTML = '<div class="lane-chip"><span class="lane-chip-name">等待上下文</span><span class="lane-chip-count">0</span></div>';
    }
    if ($('cyGraph')) {
      $('cyGraph').hidden = false;
      $('cyGraph').innerHTML = `
        <div class="graph-guide">
          <div class="graph-guide-kicker">Lifecycle Ready</div>
          <div class="graph-guide-title">请选择交易上下文</div>
          <div class="graph-guide-copy">推荐从“信号 / 订单 / 回测 Tracking”里的流程图按钮进入；这样图里会包含选股原因、信号原因、实际成交、保护单、平仓和中断事件。</div>
        </div>
      `;
    }
    if ($('timelineFallback')) {
      $('timelineFallback').hidden = true;
      $('timelineFallback').innerHTML = '';
    }
    if ($('nodeDetails')) {
      $('nodeDetails').innerHTML = '<div class="empty-state">有流程节点后，点击节点查看价格来源、原因和原始字段。</div>';
    }
    if ($('eventList')) {
      $('eventList').innerHTML = '<div class="empty-state">等待 symbol / signal_id / trade_group_id / order_id / run_id。</div>';
    }
    if ($('eventCount')) $('eventCount').textContent = '0 events';
    if ($('graphModeInfo')) $('graphModeInfo').textContent = 'Guide';
  }

  function updateContext(filters, model) {
    if (typeof setPageContextMeta === 'function') {
      setPageContextMeta([
        { label: '环境', value: normalizeEnvironment(filters.environment).toUpperCase(), tone: normalizeEnvironment(filters.environment) },
        { label: '交易日', value: filters.date || '--' },
        { label: 'Mode', value: filters.mode || 'auto' },
        { label: 'Symbol', value: filters.symbol || '--' }
      ]);
    }
    if ($('graphSummary')) {
      $('graphSummary').textContent = `${model.nodes.length} nodes · ${model.edges.length} edges · ${model.events.length} events · lane-colored`;
    }
    if ($('eventSummary')) {
      $('eventSummary').textContent = `按时间排序展示 ${model.events.length || model.nodes.length} 个生命周期事件。`;
    }
    if ($('eventCount')) $('eventCount').textContent = `${model.events.length} events`;
    if ($('endpointInfo')) $('endpointInfo').textContent = `GET ${buildEndpointUrl(filters)}`;
  }

  function renderCurrentStage(model) {
    const current = model.current;
    if (!$('currentStageCard')) return;
    if (!current) {
      $('currentStageCard').innerHTML = `
        <div class="card-kicker">Current Phase</div>
        <div class="stage-placeholder">未能判断当前阶段。请检查 API 是否返回 current/current_stage 或可排序节点。</div>
      `;
      return;
    }
    const priceFacts = current.priceFacts || getPriceFacts(current.raw || current, current.fillSource);
    const source = normalizeFillSource(current.fillSource);
    const sourceMeta = FILL_SOURCE_META[source] || FILL_SOURCE_META.unknown;
    const priceLine = priceFacts.hasPrice
      ? `${priceFacts.label}: ${priceFacts.valueText}`
      : sourceMeta.copy;
    const sourceChip = shouldShowFillSourcePill(priceFacts, source) ? fillSourcePill(source) : priceKindPill(priceFacts);
    const changeSummary = current.changeSummary || changeSummaryForRecord(current.raw || current.data || current);
    $('currentStageCard').innerHTML = `
      <div class="card-kicker">Current Phase</div>
      <div class="stage-title">${escapeHtml(current.symbol ? `${current.symbol} / ${current.label || humanizeKey(current.stage)}` : (current.label || humanizeKey(current.stage)))}</div>
      <div class="stage-copy">${escapeHtml(changeSummary || current.copy || priceLine || '当前阶段无说明。')}</div>
      <div class="stage-chip-row">
        <span class="flow-chip">stage=${escapeHtml(current.stage || '--')}</span>
        <span class="flow-chip">status=${escapeHtml(displayStatusLabel(current.status || 'unknown'))}</span>
        <span class="flow-chip">lane=${escapeHtml(LANE_LABELS[current.lane] || current.lane || '--')}</span>
        ${sourceChip}
        <span class="flow-chip">${escapeHtml(formatTime(current.time))}</span>
      </div>
      <div class="stage-copy">价格判读：${escapeHtml(priceFacts.trustTitle)}。${escapeHtml(priceFacts.trustCopy)}</div>
    `;
  }

  function renderWarnings(model) {
    if (!$('warningCard')) return;
    const baseNotice = {
      id: 'fill_source_policy',
      severity: 'low',
      title: '价格来源展示策略',
      copy: '节点底色按 swimlane/stage 区分；成交价按 fill_source 区分；止盈/止损/限价按 price_kind 显示。'
    };
    const warnings = [baseNotice, ...model.warnings];
    $('warningCard').innerHTML = `
      <div class="card-kicker">Warnings</div>
      <div class="warning-list">
        ${warnings.slice(0, 3).map((warning) => `
          <div class="warning-item severity-${escapeHtml(safeClassToken(warning.severity || 'medium', 'medium'))}">
            <div class="warning-title">${escapeHtml(warning.title || 'Warning')}</div>
            <div class="warning-copy">${escapeHtml(warning.copy || warning.message || '--')}</div>
          </div>
        `).join('')}
      </div>
    `;
  }

  function laneCounts(nodes) {
    const counts = new Map();
    nodes.forEach((node) => {
      const lane = node.lane || 'system';
      counts.set(lane, (counts.get(lane) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }

  function renderLaneStrip(model) {
    if (!$('laneStrip')) return;
    const counts = laneCounts(model.nodes);
    if (!counts.length) {
      $('laneStrip').innerHTML = '<div class="lane-chip"><span class="lane-chip-name">No lanes</span><span class="lane-chip-count">0</span></div>';
      return;
    }
    $('laneStrip').innerHTML = counts.map(([lane, count]) => `
      <div class="lane-chip lane-${escapeHtml(safeClassToken(lane))}">
        <span class="lane-chip-name">${escapeHtml(LANE_LABELS[lane] || humanizeKey(lane))}</span>
        <span class="lane-chip-count">${count}</span>
      </div>
    `).join('');
  }

  function cytoscapeElements(model) {
    const nodes = model.nodes.map((node) => ({
      group: 'nodes',
      data: {
        id: node.id,
        label: node.displayLabel,
        title: node.label,
        symbol: node.symbol,
        stage: node.stage,
        lane: node.lane,
        status: node.status,
        fillSource: node.fillSource,
        priceKind: node.priceFacts?.priceKind || '',
        changeSummary: node.changeSummary || '',
        rank: node.rank
      },
      classes: [
        `type-${safeClassToken(node.type)}`,
        `stage-${safeClassToken(node.stage)}`,
        `lane-${safeClassToken(node.lane)}`,
        `status-${safeClassToken(node.status)}`,
        `fill-${safeClassToken(node.fillSource)}`
      ].join(' ')
    }));
    const edges = model.edges.map((edge) => ({
      group: 'edges',
      data: {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        label: edge.label || ''
      },
      classes: [`status-${safeClassToken(edge.status)}`].join(' ')
    }));
    return [...nodes, ...edges];
  }

  function laneToneStyleItems() {
    return Object.entries(LANE_TONE_META).map(([lane, tone]) => ({
      selector: `.lane-${safeClassToken(lane)}`,
      style: {
        'border-color': tone.border,
        'background-gradient-stop-colors': tone.gradient
      }
    }));
  }

  function graphStyle() {
    return [
      {
        selector: 'node',
        style: {
          'shape': 'round-rectangle',
          'width': '196px',
          'min-height': '58px',
          'height': 'label',
          'padding': '14px',
          'background-color': '#0f2432',
          'background-gradient-stop-colors': '#123449 #0a1a26',
          'background-gradient-stop-positions': '0 100',
          'background-gradient-direction': 'to-bottom',
          'border-width': 2,
          'border-color': 'rgba(147,197,253,0.28)',
          'label': 'data(label)',
          'font-family': 'Sora, sans-serif',
          'font-weight': 700,
          'font-size': '9.5px',
          'line-height': 1.25,
          'color': '#e8f1f8',
          'text-wrap': 'wrap',
          'text-max-width': '172px',
          'text-valign': 'center',
          'text-halign': 'center',
          'overlay-padding': '8px',
          'shadow-blur': 18,
          'shadow-color': 'rgba(0,0,0,0.28)',
          'shadow-offset-y': 8,
          'shadow-opacity': 0.45
        }
      },
      { selector: '.fill-unknown', style: { 'border-color': 'rgba(144,167,184,0.52)', 'background-gradient-stop-colors': '#1d2933 #0d1720' } },
      ...laneToneStyleItems(),
      { selector: '.fill-actual_ibkr', style: { 'border-color': '#68d391', 'background-gradient-stop-colors': '#123629 #0a1c18' } },
      { selector: '.fill-paper_ibkr', style: { 'border-color': '#63b3ed', 'background-gradient-stop-colors': '#12304b #091b2a' } },
      { selector: '.fill-backtest_simulated', style: { 'border-color': '#f6ad55', 'background-gradient-stop-colors': '#3a2813 #1d150b' } },
      { selector: '.status-error', style: { 'border-color': '#fc8181', 'background-gradient-stop-colors': '#3a171b #1e0e13' } },
      { selector: '.status-active', style: { 'border-color': '#5eead4', 'border-style': 'dashed', 'background-gradient-stop-colors': '#12343a #071c22' } },
      { selector: '.status-warning', style: { 'border-color': '#f6ad55' } },
      { selector: '.status-terminal', style: { 'border-color': '#a0aec0', 'background-gradient-stop-colors': '#1c2c39 #0c1620' } },
      { selector: '.status-pending', style: { 'border-style': 'dashed' } },
      { selector: '.type-lifecycle_endpoint', style: { 'shape': 'hexagon', 'border-width': 3 } },
      { selector: 'node:selected', style: { 'border-width': 4, 'border-color': '#5eead4', 'shadow-color': 'rgba(94,234,212,0.38)', 'shadow-opacity': 0.82 } },
      {
        selector: 'edge',
        style: {
          'width': 2,
          'line-color': 'rgba(147,197,253,0.44)',
          'target-arrow-color': 'rgba(147,197,253,0.58)',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'label': 'data(label)',
          'font-family': 'JetBrains Mono, monospace',
          'font-size': '8px',
          'color': '#90a7b8',
          'text-background-color': '#071018',
          'text-background-opacity': 0.78,
          'text-background-padding': '3px',
          'text-rotation': 'autorotate',
          'arrow-scale': 0.85
        }
      },
      { selector: 'edge:selected', style: { 'line-color': '#5eead4', 'target-arrow-color': '#5eead4', 'width': 3 } },
      { selector: 'edge.status-error', style: { 'line-color': '#fc8181', 'target-arrow-color': '#fc8181' } }
    ];
  }

  function runGraphLayout() {
    if (!state.cy) return;
    const useDagre = Boolean(window.cytoscape && window.dagre);
    const nodeCount = Array.isArray(state.model?.nodes) ? state.model.nodes.length : 0;
    const maxFitZoom = nodeCount <= 4 ? 0.78 : 1.12;
    const options = useDagre
      ? {
          name: 'dagre',
          rankDir: window.innerWidth < 760 ? 'TB' : 'LR',
          nodeSep: window.innerWidth < 760 ? 26 : 42,
          rankSep: window.innerWidth < 760 ? 54 : 82,
          edgeSep: 12,
          padding: 44,
          animate: true,
          animationDuration: 550,
          fit: true
        }
      : {
          name: 'breadthfirst',
          directed: true,
          padding: 44,
          spacingFactor: 1.25,
          animate: true,
          animationDuration: 400,
          fit: true
        };
    try {
      state.cy.one('layoutstop', () => {
        if (!state.cy) return;
        if (state.cy.zoom() > maxFitZoom) {
          state.cy.zoom(maxFitZoom);
          state.cy.center();
        }
      });
      state.cy.layout(options).run();
    } catch (_) {
      state.cy.layout({ name: 'grid', fit: true, padding: 44, animate: true }).run();
    }
  }

  function renderGraph(model) {
    const graphEl = $('cyGraph');
    const timelineEl = $('timelineFallback');
    if (!graphEl || !timelineEl) return;
    renderLaneStrip(model);

    if (!window.cytoscape || state.showTimeline) {
      graphEl.hidden = true;
      timelineEl.hidden = false;
      renderTimeline(model, window.cytoscape ? '已切换到时间线视图。' : 'Cytoscape.js 未加载，已降级为事件时间线。');
      if ($('graphModeInfo')) $('graphModeInfo').textContent = window.cytoscape ? 'Timeline view' : 'Timeline fallback';
      return;
    }

    graphEl.hidden = false;
    timelineEl.hidden = true;
    if ($('graphModeInfo')) $('graphModeInfo').textContent = window.dagre ? 'Cytoscape DAG + dagre' : 'Cytoscape fallback layout';

    if (state.cy) {
      state.cy.destroy();
      state.cy = null;
    }

    state.cy = window.cytoscape({
      container: graphEl,
      elements: cytoscapeElements(model),
      style: graphStyle(),
      wheelSensitivity: 0.18,
      minZoom: 0.22,
      maxZoom: 2.5,
      boxSelectionEnabled: false,
      autounselectify: false
    });

    state.cy.on('tap', 'node', (event) => {
      const id = event.target.id();
      selectNode(id, true);
    });

    state.cy.on('tap', (event) => {
      if (event.target === state.cy) {
        state.cy.elements().unselect();
      }
    });

    runGraphLayout();
    const currentId = model.current?.id && model.nodes.some((node) => node.id === model.current.id)
      ? model.current.id
      : model.nodes[0]?.id;
    if (currentId) selectNode(currentId, false);
  }

  function timelineItems(model) {
    if (model.events.length) return model.events;
    return model.nodes.map((node) => ({
      id: node.id,
      title: node.label,
      symbol: node.symbol,
      signalId: node.signalId,
      tradeGroupId: node.tradeGroupId,
      orderId: node.orderId,
      stage: node.stage,
      lane: node.lane,
      status: node.status,
      fillSource: node.fillSource,
      time: node.time,
      copy: node.copy,
      priceFacts: node.priceFacts,
      changes: node.changes,
      changeSummary: node.changeSummary,
      raw: node.raw,
      data: node.data
    }));
  }

  function renderTimeline(model, notice = '') {
    const timelineEl = $('timelineFallback');
    if (!timelineEl) return;
    const items = timelineItems(model);
    if (!items.length) {
      timelineEl.innerHTML = `<div class="empty-state">${escapeHtml(notice || '没有可展示的生命周期事件。')}</div>`;
      return;
    }
    timelineEl.innerHTML = `
      ${notice ? `<div class="empty-state">${escapeHtml(notice)}</div>` : ''}
      <div class="timeline-track">
        ${items.map((item) => renderTimelineItem(item, 'timeline')).join('')}
      </div>
    `;
  }

  function renderTimelineItem(item, prefix) {
    const priceFacts = item.priceFacts || getPriceFacts(item.raw || item.data || item, item.fillSource);
    const source = normalizeFillSource(item.fillSource);
    const title = item.symbol ? `${item.symbol} · ${item.title || item.label || humanizeKey(item.stage)}` : (item.title || item.label || humanizeKey(item.stage));
    const sourceChip = shouldShowFillSourcePill(priceFacts, source) ? fillSourcePill(source) : priceKindPill(priceFacts);
    const changeSummary = item.changeSummary || changeSummaryForRecord(item.raw || item.data || item);
    return `
      <article class="${prefix}-item fill-${escapeHtml(source)}">
        <div class="${prefix}-title">${escapeHtml(title)}</div>
        <div class="${prefix}-copy">${escapeHtml(changeSummary || item.copy || priceFacts.trustCopy || '--')}</div>
        <div class="event-meta-row">
          <span class="event-chip">${escapeHtml(formatTime(item.time))}</span>
          <span class="event-chip">stage=${escapeHtml(item.stage || '--')}</span>
          <span class="event-chip">status=${escapeHtml(item.status || '--')}</span>
          ${sourceChip}
          ${priceFacts.hasPrice ? `<span class="event-chip">${escapeHtml(priceFacts.shortLabel)}</span>` : ''}
          ${changeSummary ? `<span class="event-chip">change=${escapeHtml(changeSummary)}</span>` : ''}
        </div>
      </article>
    `;
  }

  function renderEvents(model) {
    const list = $('eventList');
    if (!list) return;
    const items = timelineItems(model);
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">暂无事件。若 API 返回 nodes，也会在图谱里展示。</div>';
      return;
    }
    list.innerHTML = items.map((item) => renderTimelineItem(item, 'event')).join('');
  }

  function renderPricePanel(priceFacts, fillSource) {
    const facts = priceFacts || getPriceFacts({}, fillSource);
    const source = normalizeFillSource(fillSource || facts.fillSource);
    const meta = FILL_SOURCE_META[source] || FILL_SOURCE_META.unknown;
    const toneClass = facts.estimated ? 'is-unverified' : `is-${facts.tone || meta.tone}`;
    const sourceChip = shouldShowFillSourcePill(facts, source) ? fillSourcePill(source) : priceKindPill(facts);
    return `
      <div class="price-panel ${toneClass}">
        <div class="price-title">
          <span>${escapeHtml(facts.trustTitle || meta.title)}</span>
          ${sourceChip}
        </div>
        <div class="price-value">${escapeHtml(facts.hasPrice ? facts.valueText : '--')}</div>
        <div class="price-copy">${escapeHtml(facts.hasPrice ? facts.label : '未返回价格字段')} · ${escapeHtml(facts.trustCopy || meta.copy)}</div>
      </div>
    `;
  }

  function renderDetailRow(label, value) {
    const safeValue = normalizeText(value, '--');
    return `
      <div class="detail-row">
        <div class="detail-label">${escapeHtml(label)}</div>
        <div class="detail-value">${escapeHtml(safeValue)}</div>
      </div>
    `;
  }

  function renderChangePanel(changes) {
    const items = Array.isArray(changes) ? changes.filter(Boolean) : [];
    if (!items.length) return '';
    return `
      <div class="change-panel">
        <div class="change-title">变更对比</div>
        <div class="change-list">
          ${items.map((item) => `
            <div class="change-row">
              <span class="change-label">${escapeHtml(item.label || item.field || 'change')}</span>
              <span class="change-before">${escapeHtml(item.beforeText || formatChangeValue(item.before))}</span>
              <span class="change-arrow">-&gt;</span>
              <span class="change-after">${escapeHtml(item.afterText || formatChangeValue(item.after))}</span>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  function stringifyRaw(raw) {
    let text = '';
    try {
      text = JSON.stringify(raw || {}, null, 2);
    } catch (_) {
      text = String(raw || '');
    }
    if (text.length > 9000) return `${text.slice(0, 9000)}\n... truncated ...`;
    return text;
  }

  function selectNode(nodeId, center) {
    const model = state.model;
    if (!model) return;
    const node = model.nodes.find((item) => item.id === nodeId) || model.current;
    if (!node) return;
    state.selectedNodeId = node.id;
    if (state.cy) {
      const cyNode = state.cy.getElementById(node.id);
      if (cyNode && cyNode.length) {
        state.cy.elements().unselect();
        cyNode.select();
        if (center) state.cy.animate({ center: { eles: cyNode }, duration: 260 });
      }
    }
    renderNodeDetails(node);
  }

  function renderNodeDetails(node) {
    const target = $('nodeDetails');
    if (!target) return;
    if (!node) {
      target.innerHTML = '<div class="empty-state">点击流程节点查看详情。</div>';
      return;
    }
    const facts = node.priceFacts || getPriceFacts(node.raw || node.data || node, node.fillSource);
    const rawData = node.data || asObject(node.raw);
    const detailTitle = node.symbol ? `${node.symbol} / ${node.label || node.id}` : (node.label || node.id);
    const sourceChip = shouldShowFillSourcePill(facts, node.fillSource) ? fillSourcePill(node.fillSource) : priceKindPill(facts);
    const changes = Array.isArray(node.changes) && node.changes.length ? node.changes : collectChangeItems(rawData);
    const changeSummary = node.changeSummary || changeSummaryForRecord(rawData);
    const keys = [
      ['id', node.id],
      ['stage', node.stage],
      ['lane', LANE_LABELS[node.lane] || node.lane],
      ['status', node.status],
      ['type', node.type],
      ['time_et', formatTime(node.time)],
      ['time_us', firstNonEmpty(getNested(rawData, 'us_time'), getNested(rawData, 'changed_at_us'))],
      ['time_cn', firstNonEmpty(getNested(rawData, 'cn_time'), getNested(rawData, 'changed_at_cn'))],
      ['symbol', firstNonEmpty(node.symbol, getNested(rawData, 'symbol'), getNested(rawData, 'ticker'))],
      ['signal_id', firstNonEmpty(node.signalId, getNested(rawData, 'signal_id'))],
      ['trade_group_id', firstNonEmpty(node.tradeGroupId, getNested(rawData, 'trade_group_id'))],
      ['order_id', firstNonEmpty(node.orderId, getNested(rawData, 'order_id'), getNested(rawData, 'broker_order_id'), getNested(rawData, 'order_unique_id'))],
      ['price_kind', facts.priceKind],
      ['run_id', getNested(rawData, 'run_id')]
    ];
    target.innerHTML = `
      <div class="detail-card">
        <div>
          <div class="detail-kicker">${escapeHtml(node.stage || 'node')}</div>
          <div class="detail-title">${escapeHtml(detailTitle)}</div>
          <div class="detail-subtitle">${escapeHtml(changeSummary || node.copy || '无节点说明。')}</div>
          <div class="detail-chip-row">
            <span class="flow-chip">${escapeHtml(node.status || '--')}</span>
            ${sourceChip}
            ${changeSummary ? `<span class="flow-chip">${escapeHtml(changeSummary)}</span>` : ''}
            ${facts.estimated ? '<span class="flow-chip">estimated_only</span>' : ''}
          </div>
        </div>
        ${renderPricePanel(facts, node.fillSource)}
        ${renderChangePanel(changes)}
        <div class="detail-grid">
          ${keys.map(([label, value]) => renderDetailRow(label, value)).join('')}
        </div>
        <details class="raw-details">
          <summary>Raw node payload</summary>
          <pre>${escapeHtml(stringifyRaw(node.raw || node.data || {}))}</pre>
        </details>
      </div>
    `;
  }

  function renderAll(model, filters) {
    state.model = model;
    renderCurrentStage(model);
    renderWarnings(model);
    updateContext(filters, model);
    renderGraph(model);
    renderEvents(model);
  }

  async function loadLifecycle({ updateUrl = false } = {}) {
    if (state.loading) return;
    const filters = getFiltersFromForm();
    if (filters.date) localStorage.setItem(STORAGE_DATE_KEY, filters.date);
    if (updateUrl) {
      window.history.pushState({}, '', buildPageStateUrl(filters));
    }
    if (!hasLifecycleContext(filters)) {
      renderGuideState(filters);
      return;
    }
    setLoading(true);
    renderLoadingState();
    try {
      const payload = await requestLifecycleJson(filters);
      const model = normalizeModel(payload);
      renderAll(model, filters);
      if (typeof showToast === 'function') showToast('生命周期流程已更新');
    } catch (error) {
      renderError(error);
      if (typeof showToast === 'function') showToast(`生命周期加载失败：${error.message || error}`);
    } finally {
      setLoading(false);
    }
  }

  function resetFilters() {
    const environment = typeof getCurrentRuntimeEnvironment === 'function' ? getCurrentRuntimeEnvironment() : 'live';
    setFiltersToForm({
      mode: 'auto',
      environment,
      date: typeof getCurrentEtDateString === 'function' ? getCurrentEtDateString() : new Date().toISOString().slice(0, 10),
      symbol: '',
      signal_id: '',
      trade_group_id: '',
      order_id: '',
      run_id: '',
      backtest_date: ''
    });
    loadLifecycle({ updateUrl: true });
  }

  function bindEvents() {
    $('lifecycleFilterForm')?.addEventListener('submit', (event) => {
      event.preventDefault();
      loadLifecycle({ updateUrl: true });
    });
    $('resetFiltersButton')?.addEventListener('click', resetFilters);
    $('fitGraphButton')?.addEventListener('click', () => {
      if (state.cy) state.cy.fit(undefined, 44);
    });
    $('relayoutGraphButton')?.addEventListener('click', () => runGraphLayout());
    $('toggleTimelineButton')?.addEventListener('click', () => {
      state.showTimeline = !state.showTimeline;
      if ($('toggleTimelineButton')) $('toggleTimelineButton').textContent = state.showTimeline ? 'DAG' : 'Timeline';
      if (state.model) renderGraph(state.model);
    });
    window.addEventListener('popstate', () => {
      setFiltersToForm(readFiltersFromUrl());
      loadLifecycle({ updateUrl: false });
    });
  }

  function renderChrome(filters) {
    if ($('nav') && typeof renderNav === 'function') $('nav').innerHTML = renderNav(PAGE_PATH);
    if ($('contextBar') && typeof renderPageContextBar === 'function') {
      $('contextBar').innerHTML = renderPageContextBar('🧬 IBKR 生命周期', {
        subtitle: 'DAG / 泳道 / fill_source 可信度'
      });
    }
    if ($('pageBridge') && (typeof renderExecutionBridge === 'function' || typeof renderPageBridge === 'function')) {
      const bridgeParams = {
        date: filters.date || '',
        symbol: filters.symbol || '',
        signal_id: filters.signal_id || '',
        trade_group_id: filters.trade_group_id || '',
        order_id: filters.order_id || '',
        run_id: filters.run_id || ''
      };
      $('pageBridge').innerHTML = typeof renderExecutionBridge === 'function'
        ? renderExecutionBridge(PAGE_PATH, bridgeParams)
        : renderPageBridge([
          { path: '/ibkr_signals.html', params: bridgeParams, kicker: 'Signals', label: '主信号', copy: '确认 / 执行' },
          { path: '/orders.html', params: bridgeParams, kicker: 'Orders', label: '订单', copy: '状态 / 操作' },
          { path: PAGE_PATH, params: bridgeParams, kicker: 'Lifecycle', label: '生命周期', copy: '流程 / 事件', active: true }
        ]);
    }
  }

  window.onEnvironmentChange = function onEnvironmentChange(environment) {
    const filters = getFiltersFromForm();
    filters.environment = normalizeEnvironment(environment);
    setFiltersToForm(filters);
    loadLifecycle({ updateUrl: true });
  };

  document.addEventListener('DOMContentLoaded', () => {
    try {
      if (typeof requireAuth === 'function') requireAuth(`${location.pathname}${location.search}`);
    } catch (_) {
      return;
    }

    const filters = readFiltersFromUrl();
    setFiltersToForm(filters);
    renderChrome(filters);
    bindEvents();
    loadLifecycle({ updateUrl: false });
  });
})();
