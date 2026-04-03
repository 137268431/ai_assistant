const BASE_URL = 'https://pb.lzw-glory.top';
const token = localStorage.getItem('pb_token');

if (!token) {
  console.error('❌ 未找到 token');
  throw new Error('No token');
}

console.log('✓ Token:', token.slice(0, 30) + '...');

function cfg(key, value, defaultValue, displayName, groupName, sortOrder, description) {
  return {
    key,
    value,
    default_value: defaultValue,
    display_name: displayName,
    group_name: groupName,
    sort_order: sortOrder,
    description
  };
}

const configData = [
  cfg('ibkr_trading_enabled', 'TRUE', 'TRUE', '交易总开关', '核心交易', 100, 'OFF 时不下新单；仍执行信号拉取、取消确认、手动操作轮询与状态同步'),
  cfg('signal_auto_confirm', 'FALSE', 'FALSE', '信号自动确认', '核心交易', 110, 'TRUE 时信号直接进入 pending 状态自动执行，FALSE 时需在管理页面手动确认'),
  cfg('max_positions', '3', '3', '最大持仓数', '核心交易', 120, '最大同时持仓数'),
  cfg('max_daily_sl', '3', '3', '每日止损熔断次数', '核心交易', 130, '达到次数上限后停止当日新增交易'),
  cfg('max_loss_per_trade', '150', '150', '单笔最大亏损', '核心交易', 140, '单笔最大亏损上限($)'),

  cfg('risk_reward_ratio', '1.5', '1.5', '盈亏比', '风险参数', 200, '策略目标盈亏比'),
  cfg('sl_atr_mult', '2.0', '2.0', '止损 ATR 倍数', '风险参数', 210, '止损价距离 ATR 倍数'),
  cfg('tp_atr_mult', '3.0', '3.0', '止盈 ATR 倍数', '风险参数', 220, '止盈价距离 ATR 倍数'),
  cfg('atr_deviation', '0.30', '0.30', 'ATR 偏离阈值', '风险参数', 230, '触发 ATR 动态调整的偏离阈值'),
  cfg('atr_multiplier', '1.5', '1.5', 'ATR 计算倍数', '风险参数', 240, '用于 GetCurrentATR 计算动态止盈止损距离'),
  cfg('eod_keep_symbols', 'BOXX,IBKR', 'BOXX,IBKR', 'EOD 保留标的', '风险参数', 250, '收盘不平仓的标的，逗号分隔'),

  cfg('day_start_time', '09:20', '09:20', '日内开始时间', '执行时间', 300, '层级2 生命周期: 每日开盘前重置日内状态'),
  cfg('trading_start_time', '09:30', '09:30', '交易执行开始', '执行时间', 310, '层级3 执行窗口开始'),
  cfg('trading_end_time', '16:00', '16:00', '交易执行结束', '执行时间', 320, '层级3 执行窗口结束'),
  cfg('order_start_time', '09:30', '09:30', '下单窗口开始', '执行时间', 330, '层级4 下单窗口开始，仅允许新开仓'),
  cfg('order_end_time', '15:30', '15:30', '下单窗口结束', '执行时间', 340, '层级4 下单窗口结束，超过时间不再新开仓'),
  cfg('eod_close_time', '15:55', '15:55', '收盘清理时间', '执行时间', 350, 'EOD 执行平仓、撤单和收尾'),

  cfg('sync_start_time', '04:00', '04:00', '同步窗口开始', '任务调度', 400, '配置刷新与 Watchlist 同步的开始时间'),
  cfg('sync_end_time', '20:00', '20:00', '同步窗口结束', '任务调度', 410, '配置刷新与 Watchlist 同步的结束时间'),
  cfg('signal_interval_min', '2', '2', '信号拉取间隔', '任务调度', 420, '信号获取间隔(分钟)'),
  cfg('config_interval_min', '5', '5', '配置刷新间隔', '任务调度', 430, '联动 sync_*: 同步窗口内按该间隔刷新配置'),
  cfg('watchlist_interval_min', '5', '5', '标的同步间隔', '任务调度', 440, '联动 sync_*: 同步窗口内按该间隔同步标的'),
  cfg('signal_validity_minutes', '30', '30', '信号有效期', '任务调度', 450, '超过此时间的信号将被忽略'),
  cfg('order_validity_minutes', '30', '30', '订单有效期', '任务调度', 460, 'Init/Submitted 状态的订单超过此时间自动标记为 Canceled'),
  cfg('pb_scheduler_enabled', 'TRUE', 'TRUE', 'PB 前端定时刷新', '任务调度', 470, '仅控制 PB 前端页面的定时刷新，IBKR 算法调度不受此开关影响'),

  cfg('notification_channel', 'feishu', 'feishu', '通知渠道', '通知中心', 500, 'feishu=默认飞书，email=仅邮件，all=飞书+邮件；订单飞书由 PB 订单卡片触发'),
  cfg('alert_email', '137268431@qq.com', '137268431@qq.com', '邮件收件人', '通知中心', 510, '仅在通知渠道包含 email 时生效'),
  cfg('status_notify_enabled', 'TRUE', 'TRUE', '状态通知', '通知中心', 520, '开盘、重启等状态通知开关'),
  cfg('order_notify_enabled', 'TRUE', 'TRUE', '订单邮件补充通知', '通知中心', 530, '控制 IBKR 侧订单邮件补充通知；飞书订单卡片由 PB 同步触发'),
  cfg('daily_summary_notify_enabled', 'TRUE', 'TRUE', '日报通知', '通知中心', 540, '收盘后发送当日交易汇总'),
  cfg('manual_stop_notify_enabled', 'TRUE', 'TRUE', '手动停止通知', '通知中心', 550, '手动停止算法时发送通知'),
  cfg('health_check_notify_enabled', 'TRUE', 'TRUE', '健康检查通知', '通知中心', 560, '盘前、盘中、盘后健康状态汇报'),
  cfg('inspection_notify_enabled', 'TRUE', 'TRUE', '巡检告警', '通知中心', 570, '孤立持仓、恢复异常等巡检告警'),
  cfg('order_validation_notify_enabled', 'TRUE', 'TRUE', '订单校验异常告警', '通知中心', 580, '实际订单与缓存状态不一致时的异常告警'),
  cfg('atr_stop_adjust_notify_enabled', 'TRUE', 'TRUE', 'ATR 止损调整通知', '通知中心', 590, 'ATR 波动引发止损位调整时发送通知'),

  cfg('health_check_enabled', 'TRUE', 'TRUE', '健康检查总开关', '健康巡检', 700, '定期执行服务运行状态与持仓检查'),
  cfg('health_check_trading_interval_min', '30', '30', '盘中健康检查间隔', '健康巡检', 710, '交易时段内每 N 分钟执行一次全量检查'),
  cfg('health_check_nontrading_interval_hours', '2', '2', '非交易时段健康检查间隔', '健康巡检', 720, '盘前盘后每 N 小时执行一次全量检查'),
  cfg('clear_daily_cache', 'FALSE', 'FALSE', '清除当日缓存', '健康巡检', 730, '手动数据恢复后打开，清除所有 daily keys，恢复正常后关闭'),

  cfg('log_level', 'INFO', 'INFO', '日志级别', '运行模式', 800, 'DEBUG(详细) / INFO(重要) / WARN(告警) / ERROR(错误)'),
  cfg('deployment_mode', 'live', 'live', '部署模式', '运行模式', 810, 'live=实盘，paper=模拟盘；用于统一标记数据来源'),

  cfg('ibkr_write_mode', 'shadow', 'shadow', 'IBKR 写入模式', 'PB / IBKR 服务', 900, 'shadow(仅写新表) / primary(双写新旧表) / settled(仅写旧表, TV停写)'),
  cfg('ibkr_compute_public_url', 'https://qc.lzw-glory.top', 'https://qc.lzw-glory.top', 'IBKR Compute 地址', 'PB / IBKR 服务', 905, 'PocketBase 代理与运行页访问的公开 Compute 地址'),
  cfg('ibkr_compute_enabled', 'TRUE', 'TRUE', 'IBKR Compute 服务开关', 'PB / IBKR 服务', 910, '控制定时指标计算和盘前扫描'),
  cfg('ibkr_bar_publish_enabled', 'TRUE', 'TRUE', 'IBKR K线发布开关', 'PB / IBKR 服务', 920, '控制 BarPublisher 是否向 PB 推送 OHLCV 数据'),

  cfg('market_index_symbols', 'SPY,QQQ,VIX', 'SPY,QQQ,VIX', '大盘指数标的', '市场分析', 1000, '用于监控和关联分析的大盘指数代码，逗号分隔'),
  cfg('ibkr_target_filter_on', 'FALSE', 'FALSE', '每日目标筛选', '市场分析', 1010, '开启后信号需经 ibkr_targets 筛选才进入执行流程')
];

const watchlistData = [
  {"ticker": "BA", "exchange": "BATS", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:33", "created_cn": "2026-03-02 22:33", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GE", "exchange": "BATS", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LMT", "exchange": "BATS", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:36", "created_cn": "2026-03-02 22:36", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "RTX", "exchange": "BATS", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NKE", "exchange": "BATS", "industry": "Apparel/Footwear", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "DEO", "exchange": "BATS", "industry": "Beverages: Alcoholic", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CMCSA", "exchange": "BATS", "industry": "Cable/Satellite TV", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "ANET", "exchange": "BATS", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SNDK", "exchange": "BATS", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "STX", "exchange": "BATS", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "WDC", "exchange": "BATS", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "DELL", "exchange": "BATS", "industry": "Computer Processing Hardware", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SONY", "exchange": "BATS", "industry": "Computer Processing Hardware", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "BKR", "exchange": "BATS", "industry": "Contract Drilling", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SLB", "exchange": "BATS", "industry": "Contract Drilling", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "PDD", "exchange": "BATS", "industry": "Department Stores", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "CVS", "exchange": "BATS", "industry": "Drugstore Chains", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "D", "exchange": "BATS", "industry": "Electric Utilities", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VST", "exchange": "BATS", "industry": "Electric Utilities", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LITE", "exchange": "BATS", "industry": "Electrical Products", "created_us": "2026-03-02 09:33", "created_cn": "2026-03-02 22:33", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GLW", "exchange": "BATS", "industry": "Electronic Components", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "VRT", "exchange": "BATS", "industry": "Electronic Production Equipment", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "RKT", "exchange": "BATS", "industry": "Finance/Rental/Leasing", "created_us": "2026-03-02 09:36", "created_cn": "2026-03-02 22:36", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "HD", "exchange": "BATS", "industry": "Home Improvement Chains", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LOW", "exchange": "BATS", "industry": "Home Improvement Chains", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "RCL", "exchange": "BATS", "industry": "Hotels/Resorts/Cruise lines", "created_us": "2026-03-02 09:37", "created_cn": "2026-03-02 22:37", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LRCX", "exchange": "BATS", "industry": "Industrial Machinery", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "IBM", "exchange": "BATS", "industry": "Information Technology Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "INFY", "exchange": "BATS", "industry": "Information Technology Services", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "NET", "exchange": "BATS", "industry": "Information Technology Services", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "BP", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "CNQ", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "EOG", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "EQNR", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "OXY", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "PBR", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SHEL", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "SU", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "XOM", "exchange": "BATS", "industry": "Integrated Oil", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AMZN", "exchange": "BATS", "industry": "Internet Retail", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BABA", "exchange": "BATS", "industry": "Internet Retail", "created_us": "2026-02-25 12:02", "created_cn": "2026-02-26 01:02", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GOOG", "exchange": "BATS", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GOOGL", "exchange": "BATS", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "META", "exchange": "BATS", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NFLX", "exchange": "BATS", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BCS", "exchange": "BATS", "industry": "Investment Banks/Brokers", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "HOOD", "exchange": "BATS", "industry": "Investment Banks/Brokers", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BX", "exchange": "BATS", "industry": "Investment Managers", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "KKR", "exchange": "BATS", "industry": "Investment Managers", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "QQQ", "exchange": "BATS", "industry": "Investment Trusts/Mutual Funds", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-02 09:36", "updated_cn": "2026-03-02 22:36"},
  {"ticker": "SPY", "exchange": "BATS", "industry": "Investment Trusts/Mutual Funds", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BAC", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "BBVA", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "C", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-03-02 09:37", "created_cn": "2026-03-02 22:37", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "HSBC", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "JPM", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MUFG", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NWG", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-25 12:00", "created_cn": "2026-02-26 01:00", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SAN", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "WFC", "exchange": "BATS", "industry": "Major Banks", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "UNH", "exchange": "BATS", "industry": "Managed Health Care", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "MDLN", "exchange": "BATS", "industry": "Medical Specialties", "created_us": "2026-02-25 12:02", "created_cn": "2026-02-26 01:02", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "RELX", "exchange": "BATS", "industry": "Miscellaneous Commercial Services", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SHOP", "exchange": "BATS", "industry": "Miscellaneous Commercial Services", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "F", "exchange": "BATS", "industry": "Motor Vehicles", "created_us": "2026-02-25 12:07", "created_cn": "2026-02-26 01:07", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "TSLA", "exchange": "BATS", "industry": "Motor Vehicles", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "OKE", "exchange": "BATS", "industry": "Oil & Gas Pipelines", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "COP", "exchange": "BATS", "industry": "Oil & Gas Production", "created_us": "2026-03-02 09:34", "created_cn": "2026-03-02 22:34", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VLO", "exchange": "BATS", "industry": "Oil Refining/Marketing", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "CCJ", "exchange": "BATS", "industry": "Other Metals/Minerals", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "FCX", "exchange": "BATS", "industry": "Other Metals/Minerals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "RIO", "exchange": "BATS", "industry": "Other Metals/Minerals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SCCO", "exchange": "BATS", "industry": "Other Metals/Minerals", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "UBER", "exchange": "BATS", "industry": "Other Transportation", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "APP", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "CRCL", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CRM", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CRWD", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CRWV", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "INTU", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MSFT", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "NOW", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "ORCL", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "PANW", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "PLTR", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "RBLX", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SAP", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SNOW", "exchange": "BATS", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LLY", "exchange": "BATS", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NVO", "exchange": "BATS", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "PFE", "exchange": "BATS", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "ZTS", "exchange": "BATS", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "AEM", "exchange": "BATS", "industry": "Precious Metals", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AU", "exchange": "BATS", "industry": "Precious Metals", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "GOLD", "exchange": "BATS", "industry": "Precious Metals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "GFI", "exchange": "BATS", "industry": "Precious Metals", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NEM", "exchange": "BATS", "industry": "Precious Metals", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "WPM", "exchange": "BATS", "industry": "Precious Metals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "WELL", "exchange": "BATS", "industry": "Real Estate Investment Trusts", "created_us": "2026-03-02 09:34", "created_cn": "2026-03-02 22:34", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NU", "exchange": "BATS", "industry": "Regional Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "AMD", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "ARM", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "ASX", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AVGO", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "COHR", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "INTC", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MRVL", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MU", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "NVDA", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "QCOM", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "TSM", "exchange": "BATS", "industry": "Semiconductors", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "WMT", "exchange": "BATS", "industry": "Specialty Stores", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "BHP", "exchange": "BATS", "industry": "Steel", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VALE", "exchange": "BATS", "industry": "Steel", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AAPL", "exchange": "BATS", "industry": "Telecommunications Equipment", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "PCAR", "exchange": "BATS", "industry": "Trucks/Construction/Farm Machinery", "created_us": "2026-03-02 09:39", "created_cn": "2026-03-02 22:39", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "T", "exchange": "BATS", "industry": "Wireless Telecommunications", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "VZ", "exchange": "BATS", "industry": "Wireless Telecommunications", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"}
];

function escapeFilterValue(value) {
  return String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

async function findExistingRecord(collection, uniqueField, uniqueValue) {
  if (!uniqueField) return null;

  let filter = `${uniqueField} = "${escapeFilterValue(uniqueValue)}"`;
  if (collection === 'config' || collection === 'watchlist') {
    filter += ' && environment = "global"';
  }

  const res = await fetch(`${BASE_URL}/api/collections/${collection}/records?filter=${encodeURIComponent(filter)}&perPage=1`, {
    headers: { 'Authorization': token }
  });

  if (!res.ok) return null;

  const data = await res.json();
  return data?.items?.[0] || null;
}

function normalizeImportItem(collection, item) {
  if (collection === 'config') {
    return {
      ...item,
      environment: item.environment || 'global'
    };
  }

  if (collection === 'watchlist') {
    return {
      symbol: item.symbol || item.ticker,
      exchange: item.exchange || '',
      industry: item.industry || '',
      created_us: item.created_us || '',
      created_cn: item.created_cn || '',
      updated_us: item.updated_us || '',
      updated_cn: item.updated_cn || '',
      note: item.note || '',
      environment: item.environment || 'global'
    };
  }

  return item;
}

async function importData(collection, data, uniqueField = null) {
  console.log(`\n开始导入 ${collection}...`);
  let success = 0, failed = 0;

  for (const item of data) {
    try {
      const payload = normalizeImportItem(collection, item);
      const resolvedUniqueField = collection === 'watchlist' && uniqueField === 'ticker' ? 'symbol' : uniqueField;
      const uniqueValue = resolvedUniqueField ? payload[resolvedUniqueField] : null;
      const existing = resolvedUniqueField ? await findExistingRecord(collection, resolvedUniqueField, uniqueValue) : null;
      const method = existing ? 'PATCH' : 'POST';
      const endpoint = existing
        ? `${BASE_URL}/api/collections/${collection}/records/${existing.id}`
        : `${BASE_URL}/api/collections/${collection}/records`;

      const res = await fetch(endpoint, {
        method,
        headers: { 'Content-Type': 'application/json', 'Authorization': token },
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        console.log(`${existing ? '↺' : '✓'} ${payload.key || payload.symbol || item.ticker}`);
        success++;
      } else {
        const err = await res.json();
        console.error(`✗ ${payload.key || payload.symbol || item.ticker}:`, err.message);
        failed++;
      }
    } catch (e) {
      console.error(`✗ ${item.key || item.symbol || item.ticker}:`, e.message);
      failed++;
    }
    await new Promise(r => setTimeout(r, 100));
  }

  console.log(`${collection}: 成功 ${success}, 失败 ${failed}`);
}

(async () => {
  console.log('🚀 开始导入...\n');
  await importData('config', configData, 'key');
  await importData('watchlist', watchlistData, 'ticker');
  console.log('\n🎉 完成！刷新页面查看');
})();
