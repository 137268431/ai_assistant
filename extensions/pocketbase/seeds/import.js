const BASE_URL = 'https://pb.lzw-glory.top'; // PocketBase data/auth origin
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
  cfg('pb_scheduler_enabled', 'TRUE', 'TRUE', 'Scheduler 总开关', '运行总控', 120, '控制 ibkr-scheduler 读取的兼容调度总开关；关闭后信号过期、订单过期、健康巡检、状态提醒等定时任务都会停止'),
  cfg('pb_cron_signal_expiry_enabled', 'TRUE', 'TRUE', '信号过期清理', 'Scheduler 调度(兼容 key)', 130, '扫描 pending / awaiting_confirm 信号，超时后自动标记为 expired。Cron: */5 * * * *；周期: 每 5 分钟；时间窗口: 全天。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_order_expiry_enabled', 'TRUE', 'TRUE', '订单过期取消', 'Scheduler 调度(兼容 key)', 131, '扫描 Init / Submitted 订单，超时后自动标记为 Canceled。Cron: */5 * * * *；周期: 每 5 分钟；时间窗口: 全天。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_order_detail_integrity_guard_enabled', 'TRUE', 'TRUE', '订单明细自愈', 'Scheduler 调度(兼容 key)', 131.5, '巡检 orders 与 ibkr_order_details 的当前状态是否一致，缺失时自动回补当前状态明细。Cron: */10 * * * *；周期: 每 10 分钟；时间窗口: 全天。受 Scheduler 总开关和本开关共同控制；默认静默补齐，不发送订单通知卡片。'),
  cfg('pb_cron_ibkr_compute_runtime_enabled', 'TRUE', 'TRUE', 'Compute + 状态摘要', 'Scheduler 调度(兼容 key)', 132, '触发 compute 调度。Cron: */5 * * * *；周期: 每 5 分钟检查一次；执行条件: 仅当已落库 5m bar ingest cursor 超过 compute dispatch cursor 时才调用 compute；空转时只比较 PB state 游标，覆盖盘前、盘中、盘后和 DST 切换。'),
  cfg('pb_cron_ibkr_scan_runtime_enabled', 'TRUE', 'TRUE', '盘前 Scan', 'Scheduler 调度(兼容 key)', 133, '按 09:20 ET 触发盘前 daily scan，刷新当天 candidate / active 目标池。Cron: 20 9 * * 1-5；时区: America/New_York；周期: 美东工作日 09:20；时间窗口: 09:20 ET 日筛。受 Scheduler 总开关、Compute 开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_early_expansion_topup_enabled', 'TRUE', 'TRUE', '早盘扩池补充', 'Scheduler 调度(兼容 key)', 133.2, '09:20 主池后，在 09:30-10:30 ET 每 10 分钟执行增量扩池；只增加新可操作标的并按需提醒。Cron: 30,40,50 9 * * 1-5 与 0,10,20,30 10 * * 1-5；时区: America/New_York。受 Scheduler 总开关、本开关和 status_notify_enabled 共同控制。'),
  cfg('pb_cron_ibkr_auth_edge_guard_enabled', 'TRUE', 'TRUE', '2FA 即时巡检', 'Scheduler 调度(兼容 key)', 134, '巡检 Session / 2FA 的边沿变化，并在会话失效、401 或进入待验证状态时立即告警。Cron: */1 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:59 每 1 分钟；时间窗口: 盘前到盘后。受 Scheduler 总开关和本开关共同控制；长时间未恢复仍由 2FA 长时间未恢复巡检继续补报。'),
  cfg('pb_cron_ibkr_auth_pending_guard_enabled', 'TRUE', 'TRUE', '2FA 长时间未恢复巡检', 'Scheduler 调度(兼容 key)', 135, '巡检 Session / 2FA 长时间未恢复状态，并在需要时发出系统告警。Cron: */10 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:50 每 10 分钟；时间窗口: 盘前到盘后。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_system_data_gap_guard_enabled', 'TRUE', 'TRUE', '数据缺口巡检', 'Scheduler 调度(兼容 key)', 136, '巡检 bars / indicators / 序列缺口，并在检测到市场活动异常时发出告警。Cron: */10 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:50 每 10 分钟；时间窗口: 盘前到盘后。受 Scheduler 总开关、Compute 开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_weekly_reauth_reminder_enabled', 'TRUE', 'TRUE', '周验证提醒', 'Scheduler 调度(兼容 key)', 137, '每周发送一张周验证提醒卡片；只提醒，不自动触发 Gateway 登录。Cron: 0 5 * * 1；周期: 每周一 UTC 05:00；时间窗口: 北京时间周一 13:00 / 美东周一 01:00(EDT) 或 00:00(EST)。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_weekly_reauth_followup_enabled', 'TRUE', 'TRUE', '周验证补提醒', 'Scheduler 调度(兼容 key)', 137.5, '若周验证仍停在待手动触发阶段，则补发一张飞书验证卡片提醒。Cron: 30 7 * * 1；周期: 每周一 UTC 07:30；时间窗口: 北京时间周一 15:30 / 美东周一 03:30(EDT) 或 02:30(EST)。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_2fa_hourly_check_enabled', 'TRUE', 'TRUE', '2FA 每小时提醒', 'Scheduler 调度(兼容 key)', 138, '若 2FA 仍未恢复，则按小时补发飞书验证卡片提醒。Cron: 5 4-20 * * 1-5；周期: 工作日 UTC 每小时 05 分；时间窗口: 盘前到盘后。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_system_market_open_reminder_enabled', 'TRUE', 'TRUE', '09:30 开盘交易摘要', 'Scheduler 调度(兼容 key)', 139, '仅 NYSE 交易日 09:30 发送开盘交易摘要，包含系统状态、今日标的、日筛错误和 SPY/QQQ/VIX 等大盘监控；休息日跳过。Cron: */5 * * * *；周期: 每 5 分钟轮询一次；时间窗口: 内部按 ET 09:30-09:39 仅发送一次。受 Scheduler 总开关、本开关和 status_notify_enabled 共同控制。'),
  cfg('pb_cron_system_daily_report_enabled', 'TRUE', 'TRUE', '系统日报', 'Scheduler 调度(兼容 key)', 139, '仅 NYSE 交易日汇总当日信号、订单、bars、targets 和系统事件，并在收盘后发送日报；休息日跳过。Cron: */5 * * * *；周期: 每 5 分钟轮询一次；时间窗口: 内部按 ET 16:05 仅发送一次。日报是否真正发送，还受 daily_summary_notify_enabled 控制。'),
  cfg('pb_cron_ibkr_history_retention_enabled', 'TRUE', 'TRUE', '历史数据留存', 'Scheduler 调度(兼容 key)', 140, '清理超过留存窗口的 ibkr_bars / ibkr_indicators / ibkr_signals / ibkr_reverse_signals / ibkr_targets / ibkr_bar_integrity / ibkr_bar_coverage_daily 历史数据。Cron: 10 * * * *；周期: 每小时第 10 分钟；时间窗口: 全天。受 Scheduler 总开关、本开关以及 ibkr_history_retention_enabled / ibkr_history_retention_days 配置共同控制；运行态线程会在每小时第 12 分钟做同小时兜底。'),
  cfg('pb_cron_ibkr_storage_governor_enabled', 'TRUE', 'TRUE', 'PocketBase 存储治理', 'Scheduler 调度(兼容 key)', 140.5, '按 balanced_50g 策略清理可重建指标、旧日志、TV 兼容数据和旧回测产物。Cron: 20 3 * * *；时区: America/New_York；周期: 每日美东 03:20。受 Scheduler 总开关和 storage_cleanup_enabled 控制；只删除安全过期数据，不自动 VACUUM。'),

  cfg('ibkr_signal_source', 'both', 'both', '信号来源', '信号与反转', 200, 'both=接收全部 pending 信号；tradingview=只处理 TV webhook；ibkr_compute=只处理 compute 生成信号'),
  cfg('signal_manual_confirm_enabled', 'TRUE', 'TRUE', '手动确认信号', '信号与反转', 205, '开启后，新信号会先进入 awaiting_confirm，由人工确认后再进入正式下单链路'),
  cfg('signal_poll_interval_sec', '5', '5', '信号轮询秒数', '信号与反转', 210, 'IBKR Compute 拉取 pending 信号并处理反转请求的兜底轮询间隔秒数'),
  cfg('signal_validity_minutes', '30', '30', '信号有效期', '信号与反转', 220, '超过此时间的 pending / awaiting_confirm 信号将被自动标记为 expired'),
  cfg('signal_window_max_bars', '12', '12', '信号窗口最大K线数', '信号与反转', 222, 'SD 窗口开启后最多保留多少根 5m K线；过期后清空组件，避免陈旧信号'),
  cfg('signal_strategy_profile', 'intraday_sd_v1', 'intraday_sd_v1', '信号策略配置', '信号与反转', 222.2, 'intraday_sd_v1=统一生成 SD squeeze / VWAP trend-pullback / legacy SD 候选；legacy 仅保留为旧版兼容 profile'),
  cfg('exit_policy_profile', 'setup_aware_hybrid_v1', 'setup_aware_hybrid_v1', '退出策略 Profile', '信号与反转', 222.25, 'setup_aware_hybrid_v1=按 setup 区分出场：MR 固定止盈，trend/breakout 使用软目标+追踪止盈+安全TP；fixed_atr_rr 为旧版固定 ATR/RR'),
  cfg('exit_policy_overrides', '', '', '退出策略覆盖JSON', '信号与反转', 222.26, '可选 JSON，例如 {"mr_reversion":{"tp_rr":1.5},"trend_pullback":{"chandelier_atr_mult":2.0}}；留空使用内置信号模式策略映射'),
  cfg('intraday_signal_validity_minutes', '15', '15', '日内信号有效分钟', '信号与反转', 222.4, 'intraday_sd_v1 新 setup 的 pending/awaiting_confirm 有效期；比普通信号更短，避免突破信号滞后成交'),
  cfg('entry_limit_mode', 'passive_limit_dynamic', 'passive_limit_dynamic', '入场价格模式', '信号与反转', 222.55, 'passive_limit_dynamic=按 5m ATR 与 bps clamp 生成被动 limit，多头 close-offset，空头 close+offset；marketable_limit_dynamic 作为旧别名兼容；marketable_limit_bps=旧版固定 bp 偏移'),
  cfg('entry_limit_atr_mult', '0.30', '0.30', '被动入场 ATR 倍数', '信号与反转', 222.56, '被动动态入场 offset 的 ATR 倍数；默认使用 5m ATR*0.30'),
  cfg('entry_limit_floor_bps', '15', '15', '被动入场最小bp', '信号与反转', 222.57, '被动动态入场 offset 下限，避免 ATR 太小时挂单离 close 过近'),
  cfg('entry_limit_cap_bps', '30', '30', '被动入场最大bp', '信号与反转', 222.58, '被动动态入场 offset 上限，避免挂单离 close 过远'),
  cfg('marketable_limit_bps', '10', '10', '旧版 Marketable Limit 偏移bp', '信号与反转', 222.6, '旧版 fixed bps 入场模式使用；passive_limit_dynamic 默认使用 ATR clamp 参数'),
  cfg('intraday_min_rvol_20', '0', '0', '日内最小 RVOL20', '信号与反转', 222.65, 'intraday_sd_v1 可选过滤：rvol_20 低于该值时不生成突破/回踩新 setup；0 表示关闭'),
  cfg('intraday_min_atr_pct', '0', '0', '日内最小 ATR%', '信号与反转', 222.66, 'intraday_sd_v1 可选过滤：atr_pct 低于该值时不生成突破/回踩新 setup；0 表示关闭'),
  cfg('intraday_max_atr_pct', '0', '0', '日内最大 ATR%', '信号与反转', 222.67, 'intraday_sd_v1 可选过滤：atr_pct 高于该值时不生成突破/回踩新 setup；0 表示关闭'),
  cfg('intraday_max_directional_day_change_pct', '0', '0', '日内顺方向涨跌幅上限', '信号与反转', 222.68, 'intraday_sd_v1 可选过滤：多头用 day_change_pct、空头用 -day_change_pct，超过该百分比时不追单；0 表示关闭'),
  cfg('intraday_trend_mismatch_max_abs_day_change_pct', '0', '0', '日内趋势不一致波动保护', '信号与反转', 222.69, 'intraday_sd_v1 可选过滤：SD trend 与方向不一致且 |day_change_pct| 超过该值时过滤；0 表示关闭'),
  cfg('intraday_vwap_pullback_long_require_trend_walk', 'false', 'false', 'VWAP 回踩多需 Trend Walk', '信号与反转', 222.695, 'intraday_sd_v1 可选过滤：开启后 vwap_trend_pullback_long 只接受 sd_regime=trend_walk_up，避免 breakout 初段假回踩'),
  cfg('intraday_include_legacy_signals', 'true', 'true', '日内策略包含 legacy SD', '信号与反转', 222.697, 'intraday_sd_v1 默认把 legacy SD 拆成独立 setup candidate，并与 SD squeeze / VWAP 回踩统一排序、去重、入场和出场'),
  cfg('intraday_entry_window_start_time', '09:35', '09:35', '日内策略开始时间', '信号与反转', 222.8, 'intraday_sd_v1 新 setup 的最早生成时间，ET；默认 09:35，避开第一根 5m 噪音'),
  cfg('intraday_entry_window_end_time', '10:30', '10:30', '日内策略截止时间', '信号与反转', 222.9, 'intraday_sd_v1 新 setup 的最晚生成时间，ET；回测显示 10:30 后胜率明显下降，默认只做早盘动量窗口'),
  cfg('orb_bars', '6', '6', 'ORB K线根数', '信号与反转', 223.0, '开盘区间高低点使用多少根 regular 5m K线；默认 6 根即前 30 分钟'),
  cfg('sd_squeeze_lookback', '120', '120', 'SD压缩回看根数', '信号与反转', 223.2, '计算 SD 通道宽度分位的回看 5m 根数，用于识别波动压缩'),
  cfg('sd_squeeze_rank_max', '0.25', '0.25', 'SD压缩分位阈值', '信号与反转', 223.4, 'SD 通道宽度分位低于该值时视为压缩；默认最低 25%'),
  cfg('sd_flat_slope_pct', '0.03', '0.03', 'SD中轨平缓阈值%', '信号与反转', 223.6, 'SD 中轨斜率绝对值低于该百分比时判定为横盘/压缩环境'),
  cfg('sd_breakout_confirm_bars', '1', '1', 'SD突破确认根数', '信号与反转', 223.8, '价格突破 SD 通道后需要连续确认的 K 线根数；默认 1 根'),
  cfg('sd_trend_walk_min_bars', '2', '2', 'SD趋势贴边根数', '信号与反转', 223.9, '判定 SD trend-walk 至少需要连续贴近通道的 K 线根数；默认 2 根'),
  cfg('ibkr_market_sentiment_enabled', 'TRUE', 'TRUE', '市场情绪标注', '信号与反转', 224, '开启后，compute 信号会基于 VIX/SPY/QQQ 写入市场情绪和顺逆势标注；默认只标注，不拦截信号'),
  cfg('ibkr_market_sentiment_mode', 'annotate', 'annotate', '市场情绪模式', '信号与反转', 224.1, 'annotate=只写入 market_sentiment/market_relation；off=关闭。未来可扩展为 filter'),
  cfg('ibkr_market_sentiment_symbols', 'VIX,SPY,QQQ', 'VIX,SPY,QQQ', '市场情绪标的', '信号与反转', 224.2, '市场情绪判断使用的指数/ETF，默认 VIX 作为恐慌主指标，SPY/QQQ 作为方向确认'),
  cfg('ibkr_market_sentiment_stale_min', '20', '20', '市场情绪过期分钟', '信号与反转', 224.3, 'VIX/SPY/QQQ 快照早于信号 bar 超过该分钟数时标记 stale，并回落为 unknown/neutral'),
  cfg('ibkr_market_sentiment_vix_calm_max', '20', '20', 'VIX平稳阈值', '信号与反转', 224.4, 'VIX 低于该值且 SPY/QQQ 偏强时标记 risk_on'),
  cfg('ibkr_market_sentiment_vix_risk_off', '25', '25', 'VIX风险阈值', '信号与反转', 224.5, 'VIX 高于该值时标记 risk_off，除非恐慌正在退潮'),
  cfg('ibkr_market_sentiment_vix_panic', '30', '30', 'VIX恐慌阈值', '信号与反转', 224.6, 'VIX 高于该值时标记 panic；不会直接当作看涨信号'),
  cfg('reverse_signal_threshold', '6', '6', '反转通知阈值', '信号与反转', 230, '仅当反转评分达到该阈值时发送反转卡片通知'),
  cfg('reverse_flip_enabled', 'FALSE', 'FALSE', '反向信号反手', '信号与反转', 235, '默认关闭；反向信号只用于取消/平仓/风控，不立即开反向新仓'),

  cfg('trade_window_start_time', '09:35', '09:35', '交易开始时间', '交易窗口', 300, '信号允许进入交易校验的开始时间，ET 时区'),
  cfg('trade_window_end_time', '15:30', '15:30', '交易结束时间', '交易窗口', 310, '超过该时间后不再接受新交易信号，ET 时区'),
  cfg('order_window_end_time', '15:00', '15:00', '下单截止时间', '交易窗口', 320, '超过该时间后新信号不再进入下单环节，ET 时区'),

  cfg('position_limit_max', '0', '0', '当日交易次数上限', '交易风控', 400, '当日成功下单计数达到该上限后，新的交易信号将被拒绝；0 表示不限制每日交易次数'),
  cfg('max_strategy_open_positions', '5', '5', '策略同时持仓上限', '交易风控', 402, '策略持仓与已提交未成交策略入场单合计达到该上限时，新信号保持 pending 等待容量'),
  cfg('ibkr_buying_power_guard_enabled', 'TRUE', 'TRUE', '购买力阈值保护', '交易风控', 402.1, '开启后新开仓会基于 IBKR BuyingPower 计算下单后剩余购买力；低于禁止阈值时拒绝下单'),
  cfg('ibkr_buying_power_warn_usd', '25000', '25000', '购买力预警金额', '交易风控', 402.2, '下单后剩余 BuyingPower 低于 max(该金额, NetLiq 百分比阈值) 时发送预警但允许继续'),
  cfg('ibkr_buying_power_warn_pct_net_liq', '20', '20', '购买力预警净值%', '交易风控', 402.3, '预警阈值的 NetLiq 百分比部分；默认 20%'),
  cfg('ibkr_buying_power_block_usd', '10000', '10000', '购买力禁止金额', '交易风控', 402.4, '下单后剩余 BuyingPower 低于 max(该金额, NetLiq 百分比阈值) 时禁止交易'),
  cfg('ibkr_buying_power_block_pct_net_liq', '10', '10', '购买力禁止净值%', '交易风控', 402.5, '禁止阈值的 NetLiq 百分比部分；默认 10%'),
  cfg('ibkr_buying_power_notify_enabled', 'TRUE', 'TRUE', '购买力通知', '交易风控', 402.6, '开启后手动/自动开仓会记录剩余购买力，预警或禁止时发送告警'),
  cfg('ibkr_buying_power_notify_cooldown_sec', '1800', '1800', '购买力预警冷却秒数', '交易风控', 402.7, '同一标的同一购买力状态重复预警的最短间隔；成功开仓通知不受此冷却限制'),
  cfg('fixed_position_symbols', 'BOXX,IBKR', 'BOXX,IBKR', '固定持仓标的', '交易风控', 403, '这些标的不允许策略开仓，也不占用策略同时持仓容量；逗号分隔'),
  cfg('cooldown_bars_after_sl', '6', '6', '止损后冷却K线数', '交易风控', 405, '同标的止损后冷却多少根 5m K线，冷却期间不再开新仓'),
  cfg('cooldown_bars_after_reverse', '3', '3', '反向退出后冷却K线数', '交易风控', 406, '同标的反向信号平仓后冷却多少根 5m K线，冷却期间不再开新仓'),
  cfg('atr_dynamic_stop_enabled', 'TRUE', 'TRUE', 'ATR动态止损', '交易风控', 407, '开启后仅允许按 ATR 收紧止损，不放宽风险，不调整 TP'),
  cfg('live_exit_policy_stop_update_enabled', 'TRUE', 'TRUE', '实盘追踪止盈调止损', '交易风控', 407.5, '开启后实盘按完成的 5m bar 使用共享 exit policy 计算，只允许收紧 stop，不放宽风险'),
  cfg('atr_stop_min_profit_r', '0.3', '0.3', 'ATR调止损最小盈利R', '交易风控', 408, '持仓至少达到该 R 倍盈利后才允许 ATR 动态收紧止损'),
  cfg('atr_stop_deviation_threshold', '0.30', '0.30', 'ATR调止损变化阈值', '交易风控', 408.2, '当前 ATR 相对上次记录 ATR 变化超过该比例才触发收紧评估'),
  cfg('atr_stop_min_change', '0.01', '0.01', 'ATR调止损最小价差', '交易风控', 408.4, '新旧止损价差至少达到该值才尝试改单'),
  cfg('order_validity_minutes', '30', '30', '订单有效期', '交易风控', 410, 'Init / Submitted 状态的订单超过此时间自动标记为 Canceled'),

  cfg('eod_close_time', '15:55', '15:55', 'EOD 平仓时间', '日终规则', 500, '到达该 ET 时间后自动执行日终平仓'),
  cfg('eod_keep_symbols', 'BOXX,IBKR', 'BOXX,IBKR', 'EOD 保留标的', '日终规则', 510, '日终平仓时跳过这些标的，逗号分隔；默认保留 BOXX 与 IBKR'),

  cfg('watchlist_interval_min', '5', '5', '标的同步间隔', '标的订阅', 600, '联动 sync_*: 同步窗口内按该间隔刷新 watchlist 股票池'),
  cfg('ibkr_target_refresh_sec', '60', '60', '目标订阅刷新秒数', '标的订阅', 610, '按 ibkr_targets 刷新当日实时订阅列表'),
  cfg('ibkr_scan_schedule', '09:20-10:00', '09:20-10:00', '盘前扫描窗口', '标的订阅', 611, '盘前自动扫描允许触发的 ET 时间窗口；默认 09:20-10:00，供 scan 调度与页面展示使用'),
  cfg('ibkr_daily_scan_time_et', '09:20', '09:20', '日筛触发时间', '标的订阅', 612, '每日自动初筛触发时间，ET 时区；当前设计为 09:20 盘前初筛'),
  cfg('ibkr_daily_scan_min_avg_10d_volume', '100000', '100000', '日筛最小10D均量', '标的订阅', 614, '自动日筛硬门槛：10 日平均成交量至少达到该值'),
  cfg('ibkr_daily_scan_min_premarket_volume', '5000', '5000', '日筛最小盘前量', '标的订阅', 616, '自动日筛硬门槛：盘前累计成交量至少达到该值'),
  cfg('ibkr_daily_scan_min_atr_pct', '0.15', '0.15', '日筛最小 ATR%', '标的订阅', 618, '自动日筛硬门槛：ATR 百分比至少达到该值'),
  cfg('ibkr_daily_scan_min_abs_day_change_pct', '1.0', '1.0', '日筛最小日涨跌%', '标的订阅', 619, '自动日筛硬门槛：当日涨跌幅绝对值至少达到该值'),
  cfg('ibkr_daily_scan_day_gain_trigger_enabled', 'TRUE', 'TRUE', '日筛涨幅触发', '标的订阅', 619.1, '开启后，当日涨幅达到配置阈值可作为日筛入选触发项，用于订阅早盘强势标的并等待回落或动能衰竭信号'),
  cfg('ibkr_daily_scan_day_gain_trigger_pct', '4.0', '4.0', '日筛涨幅触发阈值%', '标的订阅', 619.2, '当日涨幅达到该百分比时触发入选观察；默认 4%，基础质量门与数据完整性门仍会继续执行'),
  cfg('ibkr_daily_scan_data_completeness_enabled', 'TRUE', 'TRUE', '日筛数据完整性检查', '标的订阅', 619.45, '开启后日筛会检查扫描所需周期的数据覆盖情况；默认开启，用于避免指标链路基于缺失 bars 产生目标'),
  cfg('ibkr_daily_scan_data_completeness_blocking_enabled', 'TRUE', 'TRUE', '日筛数据完整性阻断', '标的订阅', 619.5, '开启后日筛会排除仍需修复的标的；默认开启，避免数据不完整标的进入 trade targets'),
  cfg('ibkr_daily_scan_data_completeness_intervals', '5m,15m,30m,1h,4h,1d', '5m,15m,30m,1h,4h,1d', '日筛完整性检查周期', '标的订阅', 619.51, '日筛记录完整性状态时检查的周期列表；默认覆盖 5m/15m/30m/1h/4h/1d'),
  cfg('ibkr_daily_scan_data_completeness_blocking_intervals', '5m', '5m', '日筛阻断周期', '标的订阅', 619.52, '日筛真正阻断 active 入选的周期列表；默认只阻断 5m，其他周期先记录告警与原因，避免高周期缺口导致全池不可交易'),
  cfg('ibkr_daily_scan_runtime_topup_wait_sec', '20', '20', '日筛等待 Runtime 回补秒数', '标的订阅', 619.6, '远程 compute 依赖 Runtime watchlist topup 时，若刚好遇到 5m close 后追新窗口，日筛最多等待这些秒数再判定数据完整性'),
  cfg('ibkr_daily_scan_runtime_topup_poll_sec', '2', '2', '日筛等待回补轮询秒数', '标的订阅', 619.7, '等待 Runtime watchlist topup 期间的状态轮询间隔秒数'),
  cfg('ibkr_daily_scan_indicator_snapshot_enabled', 'TRUE', 'TRUE', '日筛指标快照', '标的订阅', 619.8, '开启后日筛会保存多周期指标快照，供目标原因、图表和信号上下文复用'),
  cfg('ibkr_daily_scan_indicator_snapshot_intervals', '5m,15m,30m,1h,4h,1d', '5m,15m,30m,1h,4h,1d', '日筛指标快照周期', '标的订阅', 619.81, '日筛保存指标快照的周期列表；默认覆盖 5m/15m/30m/1h/4h/1d'),
  cfg('ibkr_daily_scan_rollup_enabled', 'TRUE', 'TRUE', '日筛多周期 Rollup', '标的订阅', 619.82, '开启后日筛先基于 5m official close bars 生成 15m/30m/1h/4h/1d 周期数据，降低高周期直接拉取压力'),
  cfg('ibkr_daily_scan_rollup_incremental', 'TRUE', 'TRUE', '日筛增量 Rollup', '标的订阅', 619.83, '开启后只补齐缺失或过期的 rollup 周期，避免每次扫描全量重算'),
  cfg('ibkr_daily_scan_rollup_intervals', '15m,30m,1h,4h,1d', '15m,30m,1h,4h,1d', '日筛 Rollup 周期', '标的订阅', 619.84, '日筛从 5m bars 生成的目标周期列表；默认生成 15m/30m/1h/4h/1d'),
  cfg('ibkr_daily_scan_materialize_enabled', 'TRUE', 'TRUE', '日筛指标物化', '标的订阅', 619.85, '开启后日筛会物化交易所需的近期 bars 和指标，减少开盘后实时链路临时计算压力'),
  cfg('ibkr_daily_scan_materialize_intervals', '5m,15m,30m,1h', '5m,15m,30m,1h', '日筛指标物化周期', '标的订阅', 619.86, '日筛物化 bars/指标的周期列表；默认覆盖 5m/15m/30m/1h，高周期保留为趋势快照上下文'),
  cfg('ibkr_timeframe_param_profiles_json', '{"5m":{"signal_strategy_profile":"intraday_sd_v1"},"15m":{"sd_length":96,"dtp_sma_length":80,"dtp_atr_length":160,"crsi_domcycle":24,"signal_window_max_bars":8},"30m":{"sd_length":80,"dtp_sma_length":70,"dtp_atr_length":140,"ema_slope_lookback":10},"1h":{"sd_length":80,"dtp_sma_length":60,"dtp_atr_length":120,"ema_slope_lookback":8},"4h":{"sd_length":60,"dtp_sma_length":50,"dtp_atr_length":100,"ema_slope_lookback":6},"1d":{"sd_length":50,"dtp_sma_length":40,"dtp_atr_length":80,"ema_slope_lookback":5}}', '{"5m":{"signal_strategy_profile":"intraday_sd_v1"},"15m":{"sd_length":96,"dtp_sma_length":80,"dtp_atr_length":160,"crsi_domcycle":24,"signal_window_max_bars":8},"30m":{"sd_length":80,"dtp_sma_length":70,"dtp_atr_length":140,"ema_slope_lookback":10},"1h":{"sd_length":80,"dtp_sma_length":60,"dtp_atr_length":120,"ema_slope_lookback":8},"4h":{"sd_length":60,"dtp_sma_length":50,"dtp_atr_length":100,"ema_slope_lookback":6},"1d":{"sd_length":50,"dtp_sma_length":40,"dtp_atr_length":80,"ema_slope_lookback":5}}', '多周期指标参数', '标的订阅', 619.87, '按周期覆盖 SD / DTP / CRSI / slope 等指标参数；避免 5m/15m/30m/1h/4h/1d 共用同一套技术指标窗口'),
  cfg('ibkr_target_subscription_limit', '80', '80', 'Trade 订阅上限', '标的订阅', 620, 'trade 标的单独上限；实际 trade 可用预算会再与总订阅上限扣除 monitor 预留后的余额取更小值'),
  cfg('ibkr_total_subscription_limit', '80', '80', '总订阅上限', '标的订阅', 625, 'WS 总订阅上限，包含 trade targets 与 market monitor 订阅'),
  cfg('ibkr_realtime_quote_stale_resubscribe_sec', '600', '600', 'Quote 自动重订阅阈值', '标的订阅', 627, '实时 quote 超过该秒数未更新时，运行态会强制 unsubscribe/subscribe 修复僵尸订阅'),
  cfg('ibkr_realtime_quote_resubscribe_cooldown_sec', '300', '300', 'Quote 重订阅冷却秒数', '标的订阅', 628, '同一标的自动重订阅后的最小冷却秒数，避免 IBKR streaming 频繁抖动'),
  cfg('ibkr_market_ws_symbols', 'SPY,QQQ,VIX', 'SPY,QQQ,VIX', '市场监控标的', '标的订阅', 626, '系统级 WS 市场监控默认订阅标的；用于 monitor / runtime 状态页和行情链路基准观测'),

  cfg('ibkr_bar_publish_enabled', 'TRUE', 'TRUE', 'K线发布开关', '行情链路', 700, '控制实时 / 回补 bars 是否写入 PocketBase；关闭后页面与指标链路不会收到新 OHLCV'),
  cfg('tv_webhook_ingest_enabled', 'TRUE', 'TRUE', 'TV Webhook 入库开关', '行情链路', 702, '控制 /webhook/tv 是否写入 tv_signals / tv_indicators；关闭后直接返回 skipped'),
  cfg('ibkr_active_repair_interval_min', '5', '5', '活跃修复间隔', '行情链路', 705, '当前实时订阅标的的缺口 / rollup 异常巡检间隔，按 5m 链路优先修复'),
  cfg('ibkr_watchlist_idle_topup_enabled', 'TRUE', 'TRUE', '底池空闲动态回补', '行情链路', 706, 'Runtime 空闲时为 watchlist 非活跃标的补齐 5m bars；受资源治理、active-first 与 5m due guard 共同限制'),
  cfg('ibkr_watchlist_idle_topup_dynamic_enabled', 'TRUE', 'TRUE', '动态回补模式', '行情链路', 706.1, 'TRUE 时按可用 symbol 预算和估算缺失 bars 动态选批；FALSE 时退回固定 batch size'),
  cfg('ibkr_watchlist_idle_topup_active_first_enabled', 'TRUE', 'TRUE', 'Active 优先保护', '行情链路', 706.2, '盘中有 active 标的时，回补必须让位给实时 5m close 链路，避免底池补数影响交易目标'),
  cfg('ibkr_watchlist_idle_topup_dynamic_loop_interval_sec', '2', '2', '动态回补循环秒数', '行情链路', 706.3, 'dynamic_enabled=TRUE 时的底池回补循环间隔；默认 2 秒，用于让 watchlist 在 5m close 后快速追新'),
  cfg('ibkr_watchlist_idle_topup_loop_interval_sec', '2', '2', '静态回补循环秒数', '行情链路', 706.4, 'dynamic_enabled=FALSE 时的底池回补循环间隔'),
  cfg('ibkr_watchlist_idle_topup_dynamic_max_symbols_per_cycle', '160', '160', '动态每轮 Symbol 上限', '行情链路', 706.5, '动态模式每轮最多选择多少个需要回补的 watchlist 标的；无 active 目标时应覆盖全池以降低新 bar 落库延迟'),
  cfg('ibkr_watchlist_idle_topup_max_estimated_bars_per_cycle', '0', '0', '动态每轮估算 Bars 上限', '行情链路', 706.6, '按缺失 5m bars 估算单轮工作量；0 表示不限制估算 bars 预算，适合只补刚闭合的一根'),
  cfg('ibkr_watchlist_idle_topup_batch_size', '160', '160', '静态回补批次', '行情链路', 706.7, 'dynamic_enabled=FALSE 时每轮固定处理多少个底池标的'),
  cfg('ibkr_watchlist_idle_topup_max_symbols_per_cycle', '0', '0', '兼容每轮 Symbol 上限', '行情链路', 706.8, '兼容上限；0 表示动态模式使用 dynamic_max_symbols_per_cycle，静态模式只使用 batch_size'),
  cfg('ibkr_watchlist_idle_topup_candidate_scan_size', '200', '200', '候选扫描窗口', '行情链路', 706.9, '每轮从 watchlist 游标扫描多少个候选，用于发现 missing/stale 的非活跃标的；默认覆盖当前全 watchlist'),
  cfg('ibkr_watchlist_idle_topup_request_period', '1d', '1d', '回补请求周期', '行情链路', 707, '底池 idle topup 向历史接口请求的回补 period；通常保持 1d 以降低 IBKR 压力'),
  cfg('ibkr_history_request_spacing', '0.05', '0.05', '历史请求排队间隔秒数', '行情链路', 707.05, 'Runtime 历史 bars 请求之间的全局排队间隔；watchlist 追新默认 0.05 秒，降低 5m close 后全池落库延迟'),
  cfg('ibkr_official_5m_close_delay_sec', '3', '3', '5m Close 安全等待秒数', '行情链路', 707.08, '5m bar close 后等待多少秒再拉 IBKR 官方历史 bars；越小越快，但过小可能遇到 IBKR 尚未产出该 bar'),
  cfg('ibkr_runtime_direct_topup_close_delay_sec', '8', '8', '高周期 Close 安全等待秒数', '行情链路', 707.09, '15m/30m/1h/4h/1d direct topup 在周期 close 后等待多少秒再拉 IBKR 历史 bars'),
  cfg('ibkr_runtime_direct_topup_loop_interval_sec', '1', '1', '高周期回补循环秒数', '行情链路', 707.091, '高周期 direct topup 空闲检查间隔；默认 1 秒，减少 close 后等待下一轮循环的延迟'),
  cfg('ibkr_runtime_direct_topup_parallel_enabled', 'true', 'true', '高周期批量追新', '行情链路', 707.092, '开启后同一轮把所有已到期高周期按优先级批量拉取、批量落库、批量触发 compute'),
  cfg('ibkr_runtime_direct_topup_interval_priority', '4h,1h,30m,15m,1d', '4h,1h,30m,15m,1d', '高周期追新优先级', '行情链路', 707.093, '多个高周期同时到期时的拉取顺序；默认先处理原先最容易延后的 4h/1h'),
  cfg('ibkr_watchlist_idle_topup_materialize_5m', 'TRUE', 'TRUE', '回补后物化 5m', '行情链路', 707.1, '回补后默认物化 5m 指标；同一批可仅为 active targets 持久化信号，普通 watchlist 只补 bars/指标'),
  cfg('ibkr_watchlist_idle_topup_progress_warn_sec', '7200', '7200', '回补进度提醒秒数', '行情链路', 707.2, '长时间无底池回补进展时用于状态提示，便于发现 IBKR 历史链路卡住'),
  cfg('ibkr_watchlist_active_due_guard_sec', '45', '45', 'Active 5m Due Guard 秒数', '行情链路', 707.3, '距离 active 目标下一根 5m close 少于该秒数时暂停底池回补，保护实时信号链路'),
  cfg('ibkr_watchlist_backfill_interval_min', '30', '30', '底池回补间隔', '行情链路', 710, '非目标标的按批次执行 5m 增量回补的间隔'),
  cfg('ibkr_watchlist_backfill_batch_size', '12', '12', '底池回补批次', '行情链路', 720, '每轮底池回补最多处理多少个非目标标的'),
  cfg('ibkr_watchlist_backfill_stale_min', '20', '20', '底池回补滞后阈值', '行情链路', 730, '仅当最近 5m bar 超过该阈值未更新时才触发回补'),
  cfg('ibkr_watchlist_integrity_enabled', 'TRUE', 'TRUE', '底池完整性巡检', '行情链路', 740, '启用后按批次巡检非目标标的的 5m bars 完整性，并将结果写入 ibkr_bar_integrity，同时增量更新 ibkr_bar_coverage_daily 日级覆盖账本'),
  cfg('ibkr_watchlist_integrity_batch_size', '8', '8', '底池巡检批次', '行情链路', 750, '每轮底池完整性巡检最多处理多少个非目标标的'),
  cfg('ibkr_history_retention_enabled', 'TRUE', 'TRUE', '历史留存清理', '行情链路', 760, '统一控制历史行情链路数据的留存清理；默认由 ibkr-scheduler 在每小时第 10 分钟触发，运行态线程会在每小时第 12 分钟做同小时兜底；关闭后两条路径都会跳过执行'),
  cfg('ibkr_history_retention_days', '365', '365', '历史留存天数', '行情链路', 770, '历史行情链路数据默认仅保留最近多少天；当前会作用于 ibkr_bars / ibkr_indicators / ibkr_signals / ibkr_reverse_signals / ibkr_targets / ibkr_bar_integrity / ibkr_bar_coverage_daily'),
  cfg('storage_cleanup_enabled', 'TRUE', 'TRUE', '存储治理开关', '存储治理', 780, '开启后，ibkr-scheduler 每日低峰触发 PocketBase 存储治理；只清理可重建或可归档数据，不删除 config / watchlist / 活跃信号订单 / bars 核心窗口'),
  cfg('storage_cleanup_profile', 'balanced_50g', 'balanced_50g', '存储治理 Profile', '存储治理', 781, '默认 balanced_50g：bars 保持 450 天，指标 90 天，质量/日志/TV/回测产物采用更短留存，适配 50G 硬盘'),
  cfg('storage_cleanup_backtest_recent_limit', '30', '30', '回测保留数量', '存储治理', 782, '除保护的最佳 batch/run 外，保留最近多少个 backtest batch/run；旧 run 的 trades/signals/targets/reverse rows 会被同步清理'),
  cfg('storage_cleanup_protected_batch_ids', '3gf4ouzj7oyvlao', '3gf4ouzj7oyvlao', '保护回测 Batch IDs', '存储治理', 783, '逗号分隔，永不自动删除的回测 batch；默认保护当前 intraday_sd_v1 最佳批次'),
  cfg('storage_cleanup_protected_run_ids', 'bm9wl0lagddsd6a', 'bm9wl0lagddsd6a', '保护回测 Run IDs', '存储治理', 784, '逗号分隔，永不自动删除的回测 run；默认保护当前 intraday_sd_v1 最佳 run'),
  cfg('storage_cleanup_vacuum_warn_gb', '1.0', '1.0', 'VACUUM 提醒GB', '存储治理', 785, '删除后 SQLite freelist 超过该 GB 时只发出 needs_vacuum 提醒，不自动压缩文件'),
  cfg('storage_cleanup_vacuum_warn_ratio', '0.15', '0.15', 'VACUUM 提醒比例', '存储治理', 786, '删除后 SQLite freelist 占总页数超过该比例时只发出 needs_vacuum 提醒，不自动压缩文件'),
  cfg('storage_cleanup_disk_low_free_pct', '15', '15', '磁盘低余量提醒%', '存储治理', 787, '磁盘剩余百分比低于该值时 storage cleanup 结果标记 needs_vacuum，并在 system_events 中留下 warning'),
  cfg('ibkr_server_boot_resume_only', 'TRUE', 'TRUE', '服务重启仅做 Resume', '启动策略', 774, '开启后，ibkr-compute / deploy / server_boot 自动恢复不会先重启 gateway，也不会强制 fresh 2FA；适合正常发布和进程重启'),
  cfg('ibkr_server_boot_publish_startup_card', 'TRUE', 'TRUE', '服务重启发送启动卡片', '启动策略', 776, '关闭时，server_boot / auto_restore 不新建启动卡片；开启后，即使只是 deploy 恢复，也会在启动群里单独发出当前轮次卡片'),

  cfg('system_status_chat_id', 'oc_b7b52fc28816d90e27ce50ca7922a9ac', 'oc_b7b52fc28816d90e27ce50ca7922a9ac', '状态群 Chat ID', '通知路由', 600, '定时状态提醒与日常运行反馈默认发送到这里；09:30 开盘摘要和 16:05 收盘汇总发送到启动群'),
  cfg('system_startup_chat_id', 'oc_cc5d0a950797b1c2c010953e14bceeff', 'oc_cc5d0a950797b1c2c010953e14bceeff', '启动群 Chat ID', '通知路由', 602, '所有启动轮次卡片、09:30 开盘摘要和 16:05 收盘汇总统一发送到这里'),
  cfg('system_2fa_chat_id', 'oc_c48c10447685e80cfea0c003864aa51f', 'oc_c48c10447685e80cfea0c003864aa51f', '2FA 群 Chat ID', '通知路由', 605, '所有 2FA 卡片、2FA 超时/失败/待确认、Session 失效与运行态未认证提醒统一发送到这里'),
  cfg('system_alert_chat_id', 'oc_91aa4f84bc6fedb125b1a263d91d4104', 'oc_91aa4f84bc6fedb125b1a263d91d4104', '告警群 Chat ID', '通知路由', 610, '所有非 2FA 的 warning / error 级别且影响系统运行的异常默认发送到这里'),
  cfg('signal_chat_id', 'oc_edb26dcc52938b7833ac9f32ae6b1620', 'oc_edb26dcc52938b7833ac9f32ae6b1620', '信号群 Chat ID', '通知路由', 620, '新信号卡片默认发送到这里'),
  cfg('backtest_chat_id', 'oc_8c4831630f2121ffe5ff9c7f72ec9e1e', 'oc_8c4831630f2121ffe5ff9c7f72ec9e1e', '回测群 Chat ID', '通知路由', 625, 'Backtest 单次回测和参数实验通知默认发送到这里'),
  cfg('order_chat_id', 'oc_5ca4585e1fd108c2c662dfc358684945', 'oc_5ca4585e1fd108c2c662dfc358684945', '订单群 Chat ID', '通知路由', 630, '订单创建、状态流转与 TP/SL 卡片默认发送到这里'),
  cfg('reverse_chat_id', 'oc_2931e2b8501df3a9d869d7aebceb8fe2', 'oc_2931e2b8501df3a9d869d7aebceb8fe2', '反转群 Chat ID', '通知路由', 640, '反转信号与反转执行卡片默认发送到这里'),

  cfg('status_notify_enabled', 'TRUE', 'TRUE', '状态摘要通知', '系统通知', 900, 'NYSE 交易日 09:30 开盘交易摘要、重启和定时系统状态摘要通知开关；09:30 由开盘摘要接管，:00 / :30 会合并同轮心跳与健康检查结果，不再额外发送重复卡片'),
  cfg('daily_summary_notify_enabled', 'TRUE', 'TRUE', '日报通知', '系统通知', 910, '仅 NYSE 交易日收盘后发送当日交易汇总；周末或非交易日跳过'),
  cfg('manual_stop_notify_enabled', 'TRUE', 'TRUE', '手动停止通知', '系统通知', 920, '手动停止算法时发送通知'),
  cfg('health_check_notify_enabled', 'TRUE', 'TRUE', '兜底心跳通知', '系统通知', 930, '仅在状态摘要关闭时，用于发送独立 ok 心跳兜底；warning / error 级别仍走 inspection_notify_enabled'),
  cfg('inspection_notify_enabled', 'TRUE', 'TRUE', '巡检告警', '系统通知', 940, '孤立持仓、恢复异常、数据缺口等巡检告警'),
  cfg('system_data_gap_bar_lag_alert_min', '20', '20', 'Bars 缺口告警分钟', '系统通知', 941, 'regular 5m bars 相对全局最新 regular bar 落后达到该分钟数时才触发数据缺口告警；watchlist 可继续回补，但告警不要过早打扰'),
  cfg('system_data_gap_indicator_lag_alert_min', '30', '30', '指标缺口告警分钟', '系统通知', 942, 'active/candidate 标的的指标相对最新 5m bar 落后超过该分钟数时才触发数据缺口告警'),
  cfg('system_data_gap_alert_cooldown_min', '30', '30', '数据缺口冷却分钟', '系统通知', 943, '同一数据缺口 fingerprint 重复通知的最短间隔分钟数'),
  cfg('system_data_gap_indicator_requires_targets', 'TRUE', 'TRUE', '指标缺口仅看目标', '系统通知', 944, 'TRUE 时 active/candidate 数为 0 不因 watchlist 普通标的缺少指标而告警；watchlist bars 缺口仍会巡检'),
  cfg('system_monitor_host_load_consecutive_count', '2', '2', 'Load 连续命中次数', '系统通知', 945, '仅对 host_load_high / host_load_critical 生效；Monitor Cron 每 5 分钟检查一次，默认需连续 2 次命中才发告警'),
  cfg('system_monitor_ws_message_age_regular_warn_sec', '60', '60', 'WS 盘中告警秒数', '系统通知', 946, 'regular session 下最近一条 WebSocket 消息超过多少秒后触发 warning'),
  cfg('system_monitor_ws_message_age_regular_critical_sec', '180', '180', 'WS 盘中严重秒数', '系统通知', 947, 'regular session 下最近一条 WebSocket 消息超过多少秒后触发 critical；必须大于盘中告警秒数'),
  cfg('system_monitor_ws_message_age_late_session_warn_sec', '600', '600', 'WS 盘后告警秒数', '系统通知', 948, 'close_transition / afterhours 下最近一条 WebSocket 消息超过多少秒后触发 warning'),
  cfg('system_monitor_ws_message_age_late_session_critical_sec', '1200', '1200', 'WS 盘后严重秒数', '系统通知', 949, 'close_transition / afterhours 下最近一条 WebSocket 消息超过多少秒后触发 critical；必须大于盘后告警秒数'),

  cfg('pb_public_url', 'https://quant.lzw-glory.top', 'https://quant.lzw-glory.top', 'Quant 公网地址(兼容旧 pb_public_url)', '服务接入', 995, '兼容旧 pb_public_url key；该 key 现在只是 quant 控制台公网地址的旧别名，不再表示 PocketBase public 域名'),
  cfg('ibkr_console_public_url', 'https://quant.lzw-glory.top', 'https://quant.lzw-glory.top', 'Console 公网地址', '服务接入', 996, '交易系统页面、飞书打开链接与默认 console 入口使用的公网域名'),
  cfg('pb_auth_public_url', 'https://pb.lzw-glory.top', 'https://pb.lzw-glory.top', 'PocketBase Auth 公网地址', '服务接入', 997, 'PocketBase auth / data / admin 专用公网域名；前端登录与直接 auth 请求走这里'),
  cfg('ibkr_api_public_url', 'https://quant.lzw-glory.top', 'https://quant.lzw-glory.top', 'API 公网地址', '服务接入', 998, '交易系统对外 API / webhook 入口；飞书 callback 与 console 默认通过 quant 入口访问'),
  cfg('ibkr_compute_internal_url', 'http://127.0.0.1:5100', 'http://127.0.0.1:5100', 'Compute 内网地址', '服务接入', 1000, 'ibkr-compute 的系统内 HTTP 地址；不再使用独立公网 compute 域名'),
  cfg('ibkr_runtime_internal_url', 'http://127.0.0.1:5101', 'http://127.0.0.1:5101', 'Runtime 内网地址', '服务接入', 1001, 'ibkr-runtime 的系统内 HTTP 地址'),
  cfg('ibkr_api_internal_url', 'http://127.0.0.1:5102', 'http://127.0.0.1:5102', 'API 内网地址', '服务接入', 1002, 'ibkr-api 的系统内 HTTP 地址'),
  cfg('ibkr_scheduler_internal_url', 'http://127.0.0.1:5103', 'http://127.0.0.1:5103', 'Scheduler 内网地址', '服务接入', 1003, 'ibkr-scheduler 的系统内 HTTP 地址')
];

const watchlistData = [
  {"ticker": "BA", "exchange": "NYSE", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:33", "created_cn": "2026-03-02 22:33", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GE", "exchange": "NYSE", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LMT", "exchange": "NYSE", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:36", "created_cn": "2026-03-02 22:36", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "RTX", "exchange": "NYSE", "industry": "Aerospace & Defense", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NKE", "exchange": "NYSE", "industry": "Apparel/Footwear", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "DEO", "exchange": "NYSE", "industry": "Beverages: Alcoholic", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CMCSA", "exchange": "NASDAQ", "industry": "Cable/Satellite TV", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "ANET", "exchange": "NYSE", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SNDK", "exchange": "NASDAQ", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "STX", "exchange": "NASDAQ", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "WDC", "exchange": "NASDAQ", "industry": "Computer Peripherals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "DELL", "exchange": "NYSE", "industry": "Computer Processing Hardware", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SONY", "exchange": "NYSE", "industry": "Computer Processing Hardware", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "BKR", "exchange": "NASDAQ", "industry": "Contract Drilling", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SLB", "exchange": "NYSE", "industry": "Contract Drilling", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "PDD", "exchange": "NASDAQ", "industry": "Department Stores", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "CVS", "exchange": "NYSE", "industry": "Drugstore Chains", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "D", "exchange": "NYSE", "industry": "Electric Utilities", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VST", "exchange": "NYSE", "industry": "Electric Utilities", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LITE", "exchange": "NASDAQ", "industry": "Electrical Products", "created_us": "2026-03-02 09:33", "created_cn": "2026-03-02 22:33", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GLW", "exchange": "NYSE", "industry": "Electronic Components", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "VRT", "exchange": "NYSE", "industry": "Electronic Production Equipment", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "RKT", "exchange": "NYSE", "industry": "Finance/Rental/Leasing", "created_us": "2026-03-02 09:36", "created_cn": "2026-03-02 22:36", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "HD", "exchange": "NYSE", "industry": "Home Improvement Chains", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LOW", "exchange": "NYSE", "industry": "Home Improvement Chains", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "RCL", "exchange": "NYSE", "industry": "Hotels/Resorts/Cruise lines", "created_us": "2026-03-02 09:37", "created_cn": "2026-03-02 22:37", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LRCX", "exchange": "NASDAQ", "industry": "Industrial Machinery", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "IBM", "exchange": "NYSE", "industry": "Information Technology Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "INFY", "exchange": "NYSE", "industry": "Information Technology Services", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "NET", "exchange": "NYSE", "industry": "Information Technology Services", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "BP", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "CNQ", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "EOG", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "EQNR", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "OXY", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "PBR", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SHEL", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "SU", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "XOM", "exchange": "NYSE", "industry": "Integrated Oil", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AMZN", "exchange": "NASDAQ", "industry": "Internet Retail", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BABA", "exchange": "NYSE", "industry": "Internet Retail", "created_us": "2026-02-25 12:02", "created_cn": "2026-02-26 01:02", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GOOG", "exchange": "NASDAQ", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "GOOGL", "exchange": "NASDAQ", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "META", "exchange": "NASDAQ", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NFLX", "exchange": "NASDAQ", "industry": "Internet Software/Services", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BCS", "exchange": "NYSE", "industry": "Investment Banks/Brokers", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "HOOD", "exchange": "NASDAQ", "industry": "Investment Banks/Brokers", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BX", "exchange": "NYSE", "industry": "Investment Managers", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "KKR", "exchange": "NYSE", "industry": "Investment Managers", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "QQQ", "exchange": "NASDAQ", "industry": "Investment Trusts/Mutual Funds", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-02 09:36", "updated_cn": "2026-03-02 22:36"},
  {"ticker": "SPY", "exchange": "ARCA", "industry": "Investment Trusts/Mutual Funds", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "BAC", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "BBVA", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "C", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-03-02 09:37", "created_cn": "2026-03-02 22:37", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "HSBC", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "JPM", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MUFG", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NWG", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-25 12:00", "created_cn": "2026-02-26 01:00", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SAN", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "WFC", "exchange": "NYSE", "industry": "Major Banks", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "UNH", "exchange": "NYSE", "industry": "Managed Health Care", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "MDLN", "exchange": "NASDAQ", "industry": "Medical Specialties", "created_us": "2026-02-25 12:02", "created_cn": "2026-02-26 01:02", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "RELX", "exchange": "NYSE", "industry": "Miscellaneous Commercial Services", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SHOP", "exchange": "NASDAQ", "industry": "Miscellaneous Commercial Services", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "F", "exchange": "NYSE", "industry": "Motor Vehicles", "created_us": "2026-02-25 12:07", "created_cn": "2026-02-26 01:07", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "TSLA", "exchange": "NASDAQ", "industry": "Motor Vehicles", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "OKE", "exchange": "NYSE", "industry": "Oil & Gas Pipelines", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "COP", "exchange": "NYSE", "industry": "Oil & Gas Production", "created_us": "2026-03-02 09:34", "created_cn": "2026-03-02 22:34", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VLO", "exchange": "NYSE", "industry": "Oil Refining/Marketing", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "CCJ", "exchange": "NYSE", "industry": "Other Metals/Minerals", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "FCX", "exchange": "NYSE", "industry": "Other Metals/Minerals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "RIO", "exchange": "NYSE", "industry": "Other Metals/Minerals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SCCO", "exchange": "NYSE", "industry": "Other Metals/Minerals", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "UBER", "exchange": "NYSE", "industry": "Other Transportation", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "APP", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "CRCL", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CRM", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CRWD", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "CRWV", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "INTU", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MSFT", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "NOW", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "ORCL", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "PANW", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "PLTR", "exchange": "NASDAQ", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "RBLX", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "SAP", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "SNOW", "exchange": "NYSE", "industry": "Packaged Software", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "LLY", "exchange": "NYSE", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NVO", "exchange": "NYSE", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "PFE", "exchange": "NYSE", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "ZTS", "exchange": "NYSE", "industry": "Pharmaceuticals: Major", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "AEM", "exchange": "NYSE", "industry": "Precious Metals", "created_us": "2026-03-02 09:32", "created_cn": "2026-03-02 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AU", "exchange": "NYSE", "industry": "Precious Metals", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "GOLD", "exchange": "NYSE", "industry": "Precious Metals", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "GFI", "exchange": "NYSE", "industry": "Precious Metals", "created_us": "2026-02-27 09:32", "created_cn": "2026-02-27 22:32", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NEM", "exchange": "NYSE", "industry": "Precious Metals", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "WPM", "exchange": "NYSE", "industry": "Precious Metals", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "WELL", "exchange": "NYSE", "industry": "Real Estate Investment Trusts", "created_us": "2026-03-02 09:34", "created_cn": "2026-03-02 22:34", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "NU", "exchange": "NYSE", "industry": "Regional Banks", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "AMD", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 12:01", "created_cn": "2026-02-26 01:01", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "ARM", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-03-02 09:31", "created_cn": "2026-03-02 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "ASX", "exchange": "NYSE", "industry": "Semiconductors", "created_us": "2026-02-25 11:59", "created_cn": "2026-02-26 00:59", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AVGO", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "COHR", "exchange": "NYSE", "industry": "Semiconductors", "created_us": "2026-02-27 09:31", "created_cn": "2026-02-27 22:31", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "INTC", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MRVL", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "MU", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "NVDA", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "QCOM", "exchange": "NASDAQ", "industry": "Semiconductors", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "TSM", "exchange": "NYSE", "industry": "Semiconductors", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "WMT", "exchange": "NASDAQ", "industry": "Specialty Stores", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "BHP", "exchange": "NYSE", "industry": "Steel", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VALE", "exchange": "NYSE", "industry": "Steel", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "AAPL", "exchange": "NASDAQ", "industry": "Telecommunications Equipment", "created_us": "2026-02-25 11:58", "created_cn": "2026-02-26 00:58", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "PCAR", "exchange": "NASDAQ", "industry": "Trucks/Construction/Farm Machinery", "created_us": "2026-03-02 09:39", "created_cn": "2026-03-02 22:39", "updated_us": "2026-03-03 09:58", "updated_cn": "2026-03-03 22:58"},
  {"ticker": "T", "exchange": "NYSE", "industry": "Wireless Telecommunications", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:56", "updated_cn": "2026-03-03 22:56"},
  {"ticker": "VZ", "exchange": "NYSE", "industry": "Wireless Telecommunications", "created_us": "2026-02-25 11:57", "created_cn": "2026-02-26 00:57", "updated_us": "2026-03-03 09:57", "updated_cn": "2026-03-03 22:57"},
  {"ticker": "VIX", "exchange": "CBOE", "industry": "INDEX", "symbol_role": "market_monitor", "created_us": "2026-04-30 04:47", "created_cn": "2026-04-30 16:47", "updated_us": "2026-04-30 04:47", "updated_cn": "2026-04-30 16:47"}
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
