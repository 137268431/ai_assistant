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
  cfg('ibkr_trading_enabled', 'TRUE', 'TRUE', '交易总开关', '运行总控', 100, 'OFF 时不下新单；仍执行信号拉取、取消确认、手动操作轮询与状态同步'),
  cfg('ibkr_compute_enabled', 'TRUE', 'TRUE', 'Compute 调度开关', '运行总控', 110, '控制自动 compute / scan 调度；关闭后不再自动计算指标和执行盘前扫描'),
  cfg('pb_scheduler_enabled', 'TRUE', 'TRUE', 'PB 调度总开关', '运行总控', 120, '控制 PocketBase cron 调度；关闭后信号过期、订单过期、健康巡检、状态提醒等定时任务都会停止'),
  cfg('pb_cron_signal_expiry_enabled', 'TRUE', 'TRUE', '信号过期清理', 'PB Cron 调度', 130, '扫描 pending / awaiting_confirm 信号，超时后自动标记为 expired。Cron: */5 * * * *；周期: 每 5 分钟；时间窗口: 全天。受 PB 调度总开关和本开关共同控制。'),
  cfg('pb_cron_order_expiry_enabled', 'TRUE', 'TRUE', '订单过期取消', 'PB Cron 调度', 131, '扫描 Init / Submitted 订单，超时后自动标记为 Canceled。Cron: */5 * * * *；周期: 每 5 分钟；时间窗口: 全天。受 PB 调度总开关和本开关共同控制。'),
  cfg('pb_cron_order_detail_integrity_guard_enabled', 'TRUE', 'TRUE', '订单明细自愈', 'PB Cron 调度', 131.5, '巡检 orders 与 ibkr_order_details 的当前状态是否一致，缺失时自动回补当前状态明细。Cron: */10 * * * *；周期: 每 10 分钟；时间窗口: 全天。受 PB 调度总开关和本开关共同控制；默认静默补齐，不发送订单通知卡片。'),
  cfg('pb_cron_ibkr_compute_runtime_enabled', 'TRUE', 'TRUE', 'Compute + 状态摘要', 'PB Cron 调度', 132, '触发 compute 调度，并串行执行系统健康检查与状态摘要通知。Cron: */5 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:55 每 5 分钟；时间窗口: 整点发送合并状态摘要，:30 再发送一次状态摘要；同轮心跳/健康检查并入摘要，异常巡检告警仍即时单发。状态摘要在 :00 / :30 合并同轮心跳和健康检查结果；若状态摘要关闭，整点 ok 心跳才会回退到 health_check_notify_enabled；warning / error 仍受 inspection_notify_enabled 控制。'),
  cfg('pb_cron_ibkr_scan_runtime_enabled', 'TRUE', 'TRUE', '盘前 Scan', 'PB Cron 调度', 133, '触发开盘前 scan 调度，刷新当天候选信号与市场扫描结果。Cron: */5 7-9 * * 1-5；周期: 工作日 UTC 07:00-09:55 每 5 分钟；时间窗口: 盘前窗口。受 PB 调度总开关、Compute 开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_auth_edge_guard_enabled', 'TRUE', 'TRUE', '2FA 即时巡检', 'PB Cron 调度', 134, '巡检 Session / 2FA 的边沿变化，并在会话失效、401 或进入待验证状态时立即告警。Cron: */1 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:59 每 1 分钟；时间窗口: 盘前到盘后。受 PB 调度总开关和本开关共同控制；长时间未恢复仍由 2FA 长时间未恢复巡检继续补报。'),
  cfg('pb_cron_ibkr_auth_pending_guard_enabled', 'TRUE', 'TRUE', '2FA 长时间未恢复巡检', 'PB Cron 调度', 135, '巡检 Session / 2FA 长时间未恢复状态，并在需要时发出系统告警。Cron: */10 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:50 每 10 分钟；时间窗口: 盘前到盘后。受 PB 调度总开关和本开关共同控制。'),
  cfg('pb_cron_system_data_gap_guard_enabled', 'TRUE', 'TRUE', '数据缺口巡检', 'PB Cron 调度', 136, '巡检 bars / indicators / 序列缺口，并在检测到市场活动异常时发出告警。Cron: */10 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:50 每 10 分钟；时间窗口: 盘前到盘后。受 PB 调度总开关、Compute 开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_2fa_hourly_check_enabled', 'TRUE', 'TRUE', '2FA 每小时提醒', 'PB Cron 调度', 137, '若 2FA 仍未恢复，则按小时补发飞书验证卡片提醒。Cron: 5 4-20 * * 1-5；周期: 工作日 UTC 每小时 05 分；时间窗口: 盘前到盘后。受 PB 调度总开关和本开关共同控制。'),
  cfg('pb_cron_system_market_open_reminder_enabled', 'TRUE', 'TRUE', '开盘前状态提醒', 'PB Cron 调度', 138, '每日 09:20 发送开盘前系统状态；若为周末或未检测到交易计划，则改发闭市/休市提醒。Cron: */5 * * * *；周期: 每 5 分钟轮询一次；时间窗口: 内部按 ET 09:20 仅发送一次。受 PB 调度总开关、本开关和 status_notify_enabled 共同控制。'),
  cfg('pb_cron_system_daily_report_enabled', 'TRUE', 'TRUE', '系统日报', 'PB Cron 调度', 139, '汇总当日信号、订单、bars、targets 和系统事件，并在收盘后发送日报。Cron: */5 * * * *；周期: 每 5 分钟轮询一次；时间窗口: 内部按 ET 16:05 仅发送一次，周末/非交易日也会发送闭市汇总。日报是否真正发送，还受 daily_summary_notify_enabled 控制。'),
  cfg('pb_cron_ibkr_history_retention_enabled', 'TRUE', 'TRUE', '历史数据留存', 'PB Cron 调度', 140, '清理超过留存窗口的 ibkr_bars / ibkr_indicators / ibkr_signals / ibkr_reverse_signals / ibkr_targets / ibkr_bar_integrity 历史数据。Cron: 10 * * * *；周期: 每小时第 10 分钟；时间窗口: 全天。受 PB 调度总开关、本开关以及 ibkr_history_retention_enabled / ibkr_history_retention_days 配置共同控制；运行态线程会在每小时第 12 分钟做同小时兜底。'),

  cfg('ibkr_signal_source', 'both', 'both', '信号来源', '信号与反转', 200, 'both=接收全部 pending 信号；tradingview=只处理 TV webhook；ibkr_compute=只处理 compute 生成信号'),
  cfg('signal_poll_interval_sec', '120', '120', '信号轮询秒数', '信号与反转', 210, 'IBKR Compute 拉取 pending 信号并处理反转请求的轮询间隔秒数'),
  cfg('signal_validity_minutes', '30', '30', '信号有效期', '信号与反转', 220, '超过此时间的 pending / awaiting_confirm 信号将被自动标记为 expired'),
  cfg('reverse_signal_threshold', '6', '6', '反转通知阈值', '信号与反转', 230, '仅当反转评分达到该阈值时发送反转卡片通知'),

  cfg('trade_window_start_time', '09:35', '09:35', '交易开始时间', '交易窗口', 300, '信号允许进入交易校验的开始时间，ET 时区'),
  cfg('trade_window_end_time', '15:30', '15:30', '交易结束时间', '交易窗口', 310, '超过该时间后不再接受新交易信号，ET 时区'),
  cfg('order_window_end_time', '15:00', '15:00', '下单截止时间', '交易窗口', 320, '超过该时间后新信号不再进入下单环节，ET 时区'),

  cfg('position_limit_max', '3', '3', '当日持仓上限', '交易风控', 400, '当日成功下单计数达到该上限后，新的交易信号将被拒绝'),
  cfg('order_validity_minutes', '30', '30', '订单有效期', '交易风控', 410, 'Init / Submitted 状态的订单超过此时间自动标记为 Canceled'),

  cfg('eod_close_time', '15:55', '15:55', 'EOD 平仓时间', '日终规则', 500, '到达该 ET 时间后自动执行日终平仓'),
  cfg('eod_keep_symbols', 'BOXX,IBKR', 'BOXX,IBKR', 'EOD 保留标的', '日终规则', 510, '日终平仓时跳过这些标的，逗号分隔；默认保留 BOXX 与 IBKR'),

  cfg('watchlist_interval_min', '5', '5', '标的同步间隔', '标的订阅', 600, '联动 sync_*: 同步窗口内按该间隔刷新 watchlist 股票池'),
  cfg('ibkr_target_refresh_sec', '60', '60', '目标订阅刷新秒数', '标的订阅', 610, '按 ibkr_targets 刷新当日实时订阅列表'),
  cfg('ibkr_daily_scan_time_et', '09:20', '09:20', '日筛触发时间', '标的订阅', 612, '每日自动初筛触发时间，ET 时区；当前设计为 09:20 盘前初筛'),
  cfg('ibkr_daily_scan_min_avg_10d_volume', '100000', '100000', '日筛最小10D均量', '标的订阅', 614, '自动日筛硬门槛：10 日平均成交量至少达到该值'),
  cfg('ibkr_daily_scan_min_premarket_volume', '5000', '5000', '日筛最小盘前量', '标的订阅', 616, '自动日筛硬门槛：盘前累计成交量至少达到该值'),
  cfg('ibkr_daily_scan_min_atr_pct', '0.15', '0.15', '日筛最小 ATR%', '标的订阅', 618, '自动日筛硬门槛：ATR 百分比至少达到该值'),
  cfg('ibkr_daily_scan_min_abs_day_change_pct', '1.0', '1.0', '日筛最小日涨跌%', '标的订阅', 619, '自动日筛硬门槛：当日涨跌幅绝对值至少达到该值'),
  cfg('ibkr_target_subscription_limit', '80', '80', 'Trade 订阅上限', '标的订阅', 620, 'trade 标的单独上限；实际 trade 可用预算会再与总订阅上限扣除 monitor 预留后的余额取更小值'),
  cfg('ibkr_total_subscription_limit', '80', '80', '总订阅上限', '标的订阅', 625, 'WS 总订阅上限，包含 trade targets 与 market monitor 订阅'),

  cfg('ibkr_bar_publish_enabled', 'TRUE', 'TRUE', 'K线发布开关', '行情链路', 700, '控制实时 / 回补 bars 是否写入 PocketBase；关闭后页面与指标链路不会收到新 OHLCV'),
  cfg('ibkr_active_repair_interval_min', '5', '5', '活跃修复间隔', '行情链路', 705, '当前实时订阅标的的缺口 / rollup 异常巡检间隔，按 5m 链路优先修复'),
  cfg('ibkr_watchlist_backfill_interval_min', '30', '30', '底池回补间隔', '行情链路', 710, '非目标标的按批次执行 5m 增量回补的间隔'),
  cfg('ibkr_watchlist_backfill_batch_size', '12', '12', '底池回补批次', '行情链路', 720, '每轮底池回补最多处理多少个非目标标的'),
  cfg('ibkr_watchlist_backfill_stale_min', '20', '20', '底池回补滞后阈值', '行情链路', 730, '仅当最近 5m bar 超过该阈值未更新时才触发回补'),
  cfg('ibkr_watchlist_integrity_enabled', 'TRUE', 'TRUE', '底池完整性巡检', '行情链路', 740, '启用后按批次巡检非目标标的的 5m bars 完整性，并将结果写入 ibkr_bar_integrity'),
  cfg('ibkr_watchlist_integrity_batch_size', '8', '8', '底池巡检批次', '行情链路', 750, '每轮底池完整性巡检最多处理多少个非目标标的'),
  cfg('ibkr_history_retention_enabled', 'TRUE', 'TRUE', '历史留存清理', '行情链路', 760, '统一控制历史行情链路数据的留存清理；默认由 PB cron 在每小时第 10 分钟触发，运行态线程会在每小时第 12 分钟做同小时兜底；关闭后两条路径都会跳过执行'),
  cfg('ibkr_history_retention_days', '365', '365', '历史留存天数', '行情链路', 770, '历史行情链路数据默认仅保留最近多少天；当前会作用于 ibkr_bars / ibkr_indicators / ibkr_signals / ibkr_reverse_signals / ibkr_targets / ibkr_bar_integrity'),

  cfg('system_status_chat_id', 'oc_b7b52fc28816d90e27ce50ca7922a9ac', 'oc_b7b52fc28816d90e27ce50ca7922a9ac', '状态群 Chat ID', '通知路由', 600, '正常状态提醒与日常运行反馈默认发送到这里'),
  cfg('system_2fa_chat_id', 'oc_c48c10447685e80cfea0c003864aa51f', 'oc_c48c10447685e80cfea0c003864aa51f', '2FA 群 Chat ID', '通知路由', 605, '所有 2FA 卡片、2FA 超时/失败/待确认、Session 失效与运行态未认证提醒统一发送到这里'),
  cfg('system_alert_chat_id', 'oc_91aa4f84bc6fedb125b1a263d91d4104', 'oc_91aa4f84bc6fedb125b1a263d91d4104', '告警群 Chat ID', '通知路由', 610, '所有非 2FA 的 warning / error 级别且影响系统运行的异常默认发送到这里'),
  cfg('signal_chat_id', 'oc_edb26dcc52938b7833ac9f32ae6b1620', 'oc_edb26dcc52938b7833ac9f32ae6b1620', '信号群 Chat ID', '通知路由', 620, '新信号卡片默认发送到这里'),
  cfg('order_chat_id', 'oc_5ca4585e1fd108c2c662dfc358684945', 'oc_5ca4585e1fd108c2c662dfc358684945', '订单群 Chat ID', '通知路由', 630, '订单创建、状态流转与 TP/SL 卡片默认发送到这里'),
  cfg('reverse_chat_id', 'oc_2931e2b8501df3a9d869d7aebceb8fe2', 'oc_2931e2b8501df3a9d869d7aebceb8fe2', '反转群 Chat ID', '通知路由', 640, '反转信号与反转执行卡片默认发送到这里'),

  cfg('status_notify_enabled', 'TRUE', 'TRUE', '状态摘要通知', '系统通知', 900, '开盘前 09:20、重启和定时系统状态摘要通知开关；:00 / :30 会合并同轮心跳与健康检查结果，不再额外发送重复卡片'),
  cfg('daily_summary_notify_enabled', 'TRUE', 'TRUE', '日报通知', '系统通知', 910, '收盘后发送当日交易汇总；周末或非交易日也会发送闭市汇总'),
  cfg('manual_stop_notify_enabled', 'TRUE', 'TRUE', '手动停止通知', '系统通知', 920, '手动停止算法时发送通知'),
  cfg('health_check_notify_enabled', 'TRUE', 'TRUE', '兜底心跳通知', '系统通知', 930, '仅在状态摘要关闭时，用于发送独立 ok 心跳兜底；warning / error 级别仍走 inspection_notify_enabled'),
  cfg('inspection_notify_enabled', 'TRUE', 'TRUE', '巡检告警', '系统通知', 940, '孤立持仓、恢复异常、数据缺口等巡检告警'),
  cfg('system_monitor_host_load_consecutive_count', '2', '2', 'Load 连续命中次数', '系统通知', 945, '仅对 host_load_high / host_load_critical 生效；Monitor Cron 每 5 分钟检查一次，默认需连续 2 次命中才发告警'),

  cfg('ibkr_compute_public_url', 'https://qc.lzw-glory.top', 'https://qc.lzw-glory.top', 'Compute 公网地址', '服务接入', 1000, 'PocketBase 代理、运行页和 PB cron 回调访问的公开 Compute 地址')
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
      environment: item.environment || 'global',
      symbol_role: item.symbol_role || 'trade'
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
