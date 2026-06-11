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

const CONFIG_ALIAS_FALLBACKS = {
  pb_cron_system_market_open_reminder_enabled: ['pb_cron_system_scan_summary_enabled'],
  pb_cron_ibkr_data_quality_repair_sweep_enabled: [
    'pb_cron_ibkr_data_quality_open_sweep_enabled',
    'pb_cron_ibkr_data_quality_close_sweep_enabled'
  ],
  pb_cron_ibkr_data_quality_truth_audit_enabled: ['pb_cron_ibkr_data_quality_premarket_truth_audit_enabled'],
  ibkr_order_flow_execution_pool_size: ['ibkr_order_flow_active_limit']
};

const configData = [
  cfg('ibkr_trading_enabled', 'TRUE', 'TRUE', '交易总开关', '运行总控', 100, 'OFF 时不下新单；仍执行信号拉取、取消确认、手动操作轮询与状态同步'),
  cfg('ibkr_compute_enabled', 'TRUE', 'TRUE', 'Compute 调度开关', '运行总控', 110, '控制自动 compute / scan 调度；关闭后不再自动计算指标和执行盘前扫描'),
  cfg('pb_scheduler_enabled', 'TRUE', 'TRUE', 'Scheduler 总开关', '运行总控', 120, '控制 ibkr-scheduler 读取的兼容调度总开关；关闭后信号过期、订单过期、健康巡检、状态提醒等定时任务都会停止'),
  cfg('ibkr_runtime_technical_pipeline_enabled', 'FALSE', 'FALSE', 'Runtime 技术指标链路', '运行总控', 122, 'TV-primary 瘦身默认关闭 legacy bars/indicators/signals 技术链路调度；需要恢复 compute/scan/数据质量技术任务时先显式开启'),
  cfg('ibkr_warmup_indicator_backfill_enabled', 'FALSE', 'FALSE', 'Warmup 指标回补', '运行总控', 122.1, 'TV-primary 瘦身默认关闭本地指标 warmup/backfill'),
  cfg('ibkr_tv_primary_runtime_slim_enabled', 'TRUE', 'TRUE', 'TV-primary Runtime 瘦身', '运行总控', 123, '标记当前运行态采用 TV-primary 瘦身默认值：TradingView 作为信号主源，legacy 技术链路 cron 默认关闭，执行保护类任务保留'),
  cfg('pb_cron_signal_expiry_enabled', 'TRUE', 'TRUE', '信号过期清理', 'Scheduler 调度(兼容 key)', 130, '扫描 pending / awaiting_confirm 信号，超时后自动标记为 expired。Cron: */5 * * * *；周期: 每 5 分钟；时间窗口: 全天。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_order_expiry_enabled', 'TRUE', 'TRUE', '订单过期取消', 'Scheduler 调度(兼容 key)', 131, '扫描 Init / Submitted 订单，超时后自动标记为 Canceled。Cron: */5 * * * *；周期: 每 5 分钟；时间窗口: 全天。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_order_detail_integrity_guard_enabled', 'TRUE', 'TRUE', '订单明细自愈', 'Scheduler 调度(兼容 key)', 131.5, '巡检 orders 与 ibkr_order_details 的当前状态是否一致，缺失时自动回补当前状态明细。Cron: */10 * * * *；周期: 每 10 分钟；时间窗口: 全天。受 Scheduler 总开关和本开关共同控制；默认静默补齐，不发送订单通知卡片。'),
  cfg('pb_cron_ibkr_compute_runtime_enabled', 'FALSE', 'FALSE', 'Compute + 状态摘要', 'Scheduler 调度(兼容 key)', 132, '触发 compute 调度。Cron: */5 * * * *；周期: 每 5 分钟检查一次；执行条件: 仅当已落库 5m bar ingest cursor 超过 compute dispatch cursor 时才调用 compute；空转时只比较 PB state 游标，覆盖盘前、盘中、盘后和 DST 切换。'),
  cfg('pb_cron_system_heartbeat_enabled', 'FALSE', 'FALSE', '系统心跳', 'Scheduler 调度(兼容 key)', 132.1, '基于 ibkr-api 的 summaryz / monitorz 生成原生系统心跳与恢复通知。Cron: */5 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:55 每 5 分钟。'),
  cfg('pb_cron_system_monitor_alert_guard_enabled', 'TRUE', 'TRUE', '系统监控告警', 'Scheduler 调度(兼容 key)', 132.2, '读取 monitorz flags 和故障域状态，生成系统监控告警。Cron: */5 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:55 每 5 分钟。'),
  cfg('pb_cron_system_status_reminder_enabled', 'TRUE', 'TRUE', '系统状态摘要', 'Scheduler 调度(兼容 key)', 132.3, '定时发送 split stack 的系统状态摘要。Cron: 0,30 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:30 每 30 分钟。'),
  cfg('pb_cron_ibkr_scan_runtime_enabled', 'FALSE', 'FALSE', '信号窗口预筛', 'Scheduler 调度(兼容 key)', 133, '按 08:20-09:20 ET 每 5 分钟触发信号窗口 daily scan，覆盖刷新当天 candidate / active 目标池。Cron: 20,25,30,35,40,45,50,55 8 * * 1-5 与 0,5,10,15,20 9 * * 1-5；时区: America/New_York。受 Scheduler 总开关、Compute 开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_fundamentals_refresh_enabled', 'FALSE', 'FALSE', 'Fundamentals 刷新', 'Scheduler 调度(兼容 key)', 133.05, '盘前小批量刷新 trade watchlist 的股票基础数据。Cron: 5 7 * * 1-5；时区: America/New_York；周期: 工作日 ET 07:05。'),
  cfg('pb_cron_ibkr_active_window_progress_status_enabled', 'FALSE', 'FALSE', '标的/信号窗口动态卡', 'Scheduler 调度(兼容 key)', 133.15, 'TV-primary 瘦身默认关闭 legacy active target / signal-window progress 动态卡 cron。'),
  cfg('pb_cron_system_scan_summary_enabled', 'TRUE', 'TRUE', '09:30 开盘交易摘要旧别名', 'Scheduler 调度(Deprecated)', 133.1, '旧日筛摘要开关；已并入 pb_cron_system_market_open_reminder_enabled，仅作为兼容 alias 保留。'),
  cfg('pb_cron_ibkr_early_expansion_topup_enabled', 'FALSE', 'FALSE', '信号窗口入池补充', 'Scheduler 调度(兼容 key)', 133.2, '09:20 预筛最终轮后，09:25-11:00 ET 每 5 分钟执行增量扩池；只增加新可操作标的，不移除已有 active。Cron: 25,30,35,40,45,50,55 9 * * 1-5 与 0,5,10,15,20,25,30,35,40,45,50,55 10 * * 1-5 与 0 11 * * 1-5；时区: America/New_York。受 Scheduler 总开关、本开关和 status_notify_enabled 共同控制。'),
  cfg('pb_cron_ibkr_intraday_window_admission_enabled', 'FALSE', 'FALSE', '信号窗口入池', 'Scheduler 调度(兼容 key)', 133.25, '扫描 trade 观察池中已有新鲜 5m bars 的非 active 标的，按窗口与质量门槛自动加入 active 目标池。Cron: */5 9-15 * * 1-5；时区: America/New_York。'),
  cfg('pb_cron_ibkr_auth_edge_guard_enabled', 'TRUE', 'TRUE', '2FA 即时巡检', 'Scheduler 调度(兼容 key)', 134, '巡检 Session / 2FA 的边沿变化，并在会话失效、401 或进入待验证状态时立即告警。Cron: */1 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:59 每 1 分钟；时间窗口: 盘前到盘后。受 Scheduler 总开关和本开关共同控制；长时间未恢复仍由 2FA 长时间未恢复巡检继续补报。'),
  cfg('pb_cron_ibkr_auth_pending_guard_enabled', 'TRUE', 'TRUE', '2FA 长时间未恢复巡检', 'Scheduler 调度(兼容 key)', 135, '巡检 Session / 2FA 长时间未恢复状态，并在需要时发出系统告警。Cron: */10 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:50 每 10 分钟；时间窗口: 盘前到盘后。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_system_data_gap_guard_enabled', 'FALSE', 'FALSE', '数据缺口巡检', 'Scheduler 调度(兼容 key)', 136, '巡检 bars / indicators / 序列缺口，并在检测到市场活动异常时发出告警。Cron: */10 4-20 * * 1-5；周期: 工作日 UTC 04:00-20:50 每 10 分钟；时间窗口: 盘前到盘后。受 Scheduler 总开关、Compute 开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_data_quality_repair_sweep_enabled', 'FALSE', 'FALSE', '全观察池 Sweep', 'Scheduler 调度(兼容 key)', 136.2, '统一控制开盘前和收盘后两次全观察池 5m 一致性 sweep。Cron: 40 9 * * 1-5 与 10 20 * * 1-5；时区: UTC。'),
  cfg('pb_cron_ibkr_data_quality_open_sweep_enabled', 'FALSE', 'FALSE', '开盘前 Sweep 旧别名', 'Scheduler 调度(Deprecated)', 136.21, '旧开盘前 sweep 开关；已并入 pb_cron_ibkr_data_quality_repair_sweep_enabled，仅作为兼容 alias 保留。'),
  cfg('pb_cron_ibkr_data_quality_close_sweep_enabled', 'FALSE', 'FALSE', '收盘后 Sweep 旧别名', 'Scheduler 调度(Deprecated)', 136.22, '旧收盘后 sweep 开关；已并入 pb_cron_ibkr_data_quality_repair_sweep_enabled，仅作为兼容 alias 保留。'),
  cfg('pb_cron_ibkr_data_quality_premarket_truth_audit_enabled', 'FALSE', 'FALSE', '盘前真值审计旧别名', 'Scheduler 调度(Deprecated)', 136.35, '旧盘前 truth audit 开关；已并入 pb_cron_ibkr_data_quality_truth_audit_enabled，仅作为兼容 alias 保留。'),
  cfg('pb_cron_ibkr_data_quality_truth_audit_enabled', 'FALSE', 'FALSE', 'IBKR 真值审计', 'Scheduler 调度(兼容 key)', 136.4, '统一控制盘前上一交易日和盘后当天 IBKR authoritative history 真值审计。Cron: 20 8 * * 1-5 与 20 16 * * 1-5；时区: America/New_York。'),
  cfg('pb_cron_ibkr_tv_indicator_audit_enabled', 'FALSE', 'FALSE', 'TV 指标审计', 'Scheduler 调度(兼容 key)', 136.45, 'TV indicator_audit 快照真值审计默认关闭；仅在 TV 已配置对应 alert 后手动开启。'),
  cfg('pb_cron_ibkr_weekly_reauth_reminder_enabled', 'TRUE', 'TRUE', '周验证提醒', 'Scheduler 调度(兼容 key)', 137, '每周发送一张周验证提醒卡片；只提醒，不自动触发 Gateway 登录。Cron: 0 5 * * 1；周期: 每周一 UTC 05:00；时间窗口: 北京时间周一 13:00 / 美东周一 01:00(EDT) 或 00:00(EST)。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_weekly_reauth_followup_enabled', 'TRUE', 'TRUE', '周验证补提醒', 'Scheduler 调度(兼容 key)', 137.5, '若周验证仍停在待手动触发阶段，则补发一张飞书验证卡片提醒。Cron: 30 7 * * 1；周期: 每周一 UTC 07:30；时间窗口: 北京时间周一 15:30 / 美东周一 03:30(EDT) 或 02:30(EST)。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_ibkr_2fa_hourly_check_enabled', 'TRUE', 'TRUE', '2FA 每小时提醒', 'Scheduler 调度(兼容 key)', 138, '若 2FA 仍未恢复，则按小时补发飞书验证卡片提醒。Cron: 5 4-20 * * 1-5；周期: 工作日 UTC 每小时 05 分；时间窗口: 盘前到盘后。受 Scheduler 总开关和本开关共同控制。'),
  cfg('pb_cron_system_market_open_reminder_enabled', 'TRUE', 'TRUE', '09:30 开盘交易摘要', 'Scheduler 调度(兼容 key)', 139, '交易日 09:30 发送开盘交易摘要；闭市日同窗口发送闭市与下次开盘提醒。Cron: */5 * * * *；周期: 每 5 分钟轮询一次；时间窗口: 内部按 ET 09:30-09:39 仅发送一次。受 Scheduler 总开关、本开关、status_notify_enabled 与 market_closed_notify_enabled 共同控制。'),
  cfg('pb_cron_system_daily_report_enabled', 'TRUE', 'TRUE', '系统日报', 'Scheduler 调度(兼容 key)', 139, '仅 NYSE 交易日汇总当日信号、订单、bars、targets 和系统事件，并在收盘后发送日报；休息日跳过。Cron: */5 * * * *；周期: 每 5 分钟轮询一次；时间窗口: 内部按 ET 16:05 起 30 分钟内仅发送一次。日报是否真正发送，还受 daily_summary_notify_enabled 控制。'),
  cfg('pb_cron_ibkr_history_retention_enabled', 'FALSE', 'FALSE', '历史数据留存', 'Scheduler 调度(兼容 key)', 140, 'TV-primary 默认关闭 legacy 每小时留存 Cron；存储治理由 storage_cleanup 低峰任务接管。若临时开启，仅允许清理 bars / indicators / bar quality 等非核心表，不删除订单、信号、目标、watchlist、config 或 ibkr_state。'),
  cfg('pb_cron_ibkr_storage_governor_enabled', 'TRUE', 'TRUE', 'PocketBase 存储治理', 'Scheduler 调度(兼容 key)', 140.5, '按 tv_primary_lean 策略清理可重建指标、旧日志、TV 兼容数据和旧回测产物。Cron: 20 3 * * *；时区: America/New_York；周期: 每日美东 03:20。受 Scheduler 总开关和 storage_cleanup_enabled 控制；只删除安全过期数据，不自动 VACUUM。'),

  cfg('ibkr_signal_source', 'tradingview', 'tradingview', '信号来源', '信号与反转', 200, 'both=接收全部 pending 信号；tradingview=只处理 TV webhook；ibkr_compute=只处理 compute 生成信号'),
  cfg('signal_manual_confirm_enabled', 'FALSE', 'FALSE', '手动确认信号', '信号与反转', 205, '默认关闭，paper/live 使用同一套订单流自动确认链路；开启后新信号会先进入 awaiting_confirm，由人工确认后再进入正式下单链路'),
  cfg('signal_poll_interval_sec', '5', '5', '信号轮询秒数', '信号与反转', 210, 'IBKR Compute 拉取 pending 信号并处理反转请求的兜底轮询间隔秒数'),
  cfg('signal_validity_minutes', '30', '30', '信号有效期', '信号与反转', 220, '超过此时间的 pending / awaiting_confirm 信号将被自动标记为 expired'),
  cfg('signal_window_max_bars', '12', '12', '信号窗口最大K线数', '信号与反转', 222, 'SD 窗口开启后最多保留多少根 5m K线；过期后清空组件，避免陈旧信号'),
  cfg('signal_strategy_profile', 'core_two_setup_v1', 'core_two_setup_v1', '信号策略配置', '信号与反转', 222.2, 'core_two_setup_v1=只保留 VWAP 回踩多 + SD 均值回归空；intraday_sd_v1 仅保留为旧版兼容 profile'),
  cfg('exit_policy_profile', 'setup_aware_hybrid_v1', 'setup_aware_hybrid_v1', '退出策略 Profile', '信号与反转', 222.25, 'setup_aware_hybrid_v1=按 setup 区分出场：MR 固定止盈，trend/breakout 使用软目标+追踪止盈+安全TP；fixed_atr_rr 为旧版固定 ATR/RR'),
  cfg('exit_policy_overrides', '', '', '退出策略覆盖JSON', '信号与反转', 222.26, '可选 JSON，例如 {"mr_reversion":{"tp_rr":1.5},"trend_pullback":{"chandelier_atr_mult":2.0}}；留空使用内置信号模式策略映射'),
  cfg('intraday_signal_validity_minutes', '15', '15', '日内信号有效分钟', '信号与反转', 222.4, 'core_two_setup_v1 setup 的 pending/awaiting_confirm 有效期；比普通信号更短，避免突破信号滞后成交'),
  cfg('entry_limit_mode', 'passive_limit_dynamic', 'passive_limit_dynamic', '入场价格模式', '信号与反转', 222.55, 'passive_limit_dynamic=按 5m ATR 与 bps clamp 生成被动 limit，多头 close-offset，空头 close+offset；marketable_limit_dynamic 作为旧别名兼容；marketable_limit_bps=旧版固定 bp 偏移'),
  cfg('entry_limit_atr_mult', '0.30', '0.30', '被动入场 ATR 倍数', '信号与反转', 222.56, '被动动态入场 offset 的 ATR 倍数；默认使用 5m ATR*0.30'),
  cfg('entry_limit_floor_bps', '15', '15', '被动入场最小bp', '信号与反转', 222.57, '被动动态入场 offset 下限，避免 ATR 太小时挂单离 close 过近'),
  cfg('entry_limit_cap_bps', '30', '30', '被动入场最大bp', '信号与反转', 222.58, '被动动态入场 offset 上限，避免挂单离 close 过远'),
  cfg('marketable_limit_bps', '10', '10', '旧版 Marketable Limit 偏移bp', '信号与反转', 222.6, '旧版 fixed bps 入场模式使用；passive_limit_dynamic 默认使用 ATR clamp 参数'),
  cfg('intraday_min_signal_quality_score', '70', '70', '日内最小信号质量分', '信号与反转', 222.61, '信号窗口 v1 质量门槛；低于该分不生成正式交易信号，避免只有波动/热度但确认不足'),
  cfg('intraday_candidate_observation_min_quality_score', '60', '60', '日内候选观察质量分', '信号与反转', 222.62, '60-69 默认仅观察/候选，不报警不下单；正式下单仍看最小信号质量分'),
  cfg('entry_plan_version', 'entry_plan_v2', 'entry_plan_v2', '入场计划版本', '信号与反转', 222.63, 'entry_plan_v2 输出 entry_anchor、timeout、reprice_policy 与价格改善 bps，用于优化入场价格'),
  cfg('entry_breakout_marketable_quality_min', '80', '80', '突破可吃单质量分', '信号与反转', 222.64, '突破 setup 质量分达到该值才允许标记 marketable_limit 倾向；默认仍通过 LMT 保护价格'),
  cfg('entry_reprice_policy', 'single_reprice_then_cancel', 'single_reprice_then_cancel', '入场改价策略', '信号与反转', 222.645, '未成交入场单最多一次向可成交方向改价，之后过期撤单，避免追价'),
  cfg('intraday_min_rvol_20', '1.2', '1.2', '日内最小 RVOL20', '信号与反转', 222.65, 'core_two_setup_v1 过滤：rvol_20 低于该值时不生成突破/回踩新 setup；少而精默认 1.2，0 表示关闭'),
  cfg('intraday_min_atr_pct', '0.08', '0.08', '日内最小 ATR%', '信号与反转', 222.66, 'core_two_setup_v1 过滤：atr_pct 低于该值时不生成突破/回踩新 setup；0 表示关闭'),
  cfg('intraday_max_atr_pct', '1.20', '1.20', '日内最大 ATR%', '信号与反转', 222.67, 'core_two_setup_v1 过滤：atr_pct 高于该值时不生成突破/回踩新 setup；0 表示关闭'),
  cfg('intraday_max_directional_day_change_pct', '4.0', '4.0', '日内顺方向涨跌幅上限', '信号与反转', 222.68, 'core_two_setup_v1 过滤：多头用 day_change_pct、空头用 -day_change_pct，超过该百分比时不追单；0 表示关闭'),
  cfg('intraday_trend_mismatch_max_abs_day_change_pct', '0', '0', '日内趋势不一致波动保护', '信号与反转', 222.69, 'core_two_setup_v1 可选过滤：SD trend 与方向不一致且 |day_change_pct| 超过该值时过滤；0 表示关闭'),
  cfg('intraday_vwap_pullback_atr_mult', '0.15', '0.15', 'VWAP 回踩 ATR 容差', '信号与反转', 222.692, 'VWAP 回踩必须触及 VWAP 附近；容差取 ATR 倍数与最大 bp 容差的较小值'),
  cfg('intraday_vwap_pullback_max_bps', '10', '10', 'VWAP 回踩最大bp', '信号与反转', 222.693, 'VWAP 回踩容差上限，默认 10bp，避免离 VWAP 太远也被算作回踩'),
  cfg('intraday_vwap_pullback_long_require_trend_walk', 'true', 'true', 'VWAP 回踩需 Trend Walk', '信号与反转', 222.695, '开启后 VWAP trend-pullback 多/空都必须处于对应 trend_walk regime，避免 breakout 初段假回踩'),
  cfg('intraday_setup_daily_limit', '1', '1', '日内 setup 每日上限', '信号与反转', 222.696, '同一标的同一 setup+方向每天最多确认次数；0 表示关闭'),
  cfg('intraday_setup_cooldown_bars', '6', '6', '日内 setup 冷却K线', '信号与反转', 222.6965, '同一标的同一 setup+方向确认后至少等待多少根 5m K；0 表示关闭'),
  cfg('intraday_reentry_policy', 'controlled', 'controlled', '日内重复入场策略', '信号与反转', 222.6966, 'controlled=同一标的允许平仓+冷却后的高质量二次入场；single_setup=保持单 setup 每日一次'),
  cfg('intraday_symbol_daily_entry_limit', '3', '3', '标的每日入场上限', '信号与反转', 222.6967, 'controlled 策略下同一标的每天最多实际入场次数；0 表示关闭'),
  cfg('intraday_include_legacy_signals', 'false', 'false', '日内策略包含 legacy SD', '信号与反转', 222.697, 'core_two_setup_v1 默认不并入 legacy SD；旧版 intraday_sd_v1 可显式开启 legacy 兼容'),
  cfg('ibkr_require_target_direction_alignment', 'FALSE', 'FALSE', '要求目标方向一致', '信号与反转', 222.698, '开启后 compute 信号下单前必须与当日 active target 的 direction_bias 一致；neutral 或缺失会阻止交易；TV-primary 默认关闭以允许授权标的直接执行'),
  cfg('intraday_entry_window_start_time', '09:35', '09:35', '日内策略开始时间', '信号与反转', 222.8, 'core_two_setup_v1 setup 的最早生成时间，ET；默认 09:35，避开第一根 5m 噪音'),
  cfg('intraday_entry_window_end_time', '10:30', '10:30', '日内策略截止时间', '信号与反转', 222.9, 'core_two_setup_v1 setup 的最晚生成时间，ET；默认少而精窗口 09:35-10:30'),
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
  cfg('ibkr_order_flow_enabled', 'FALSE', 'FALSE', '订单流观察开关', '信号与反转', 225.0, 'TV-primary 瘦身默认关闭；开启后 paper/live 使用订单流 candidate queue / execution pool / CVD 链路'),
  cfg('ibkr_order_flow_mode', 'enforce', 'enforce', '订单流模式', '信号与反转', 225.1, 'enforce=订单流参与自动开仓、提前平仓和只收紧止损；shadow=只记录与订阅执行池，不阻断交易'),
  cfg('ibkr_order_flow_active_limit', '3', '3', '订单流执行池上限', '信号与反转', 225.2, '最多同时订阅 tick-by-tick 的 execution pool 标的数；4核8G 默认 3'),
  cfg('ibkr_order_flow_execution_pool_size', '3', '3', '订单流执行池大小', '信号与反转', 225.205, 'execution_pool 的标准大小配置；兼容旧 key ibkr_order_flow_active_limit，默认 3 个 shadow 观察槽位'),
  cfg('ibkr_order_flow_max_position_slots', '1', '1', '持仓订单流槽位', '信号与反转', 225.21, 'execution pool 中最多保留多少个持仓风险监控槽位，避免持仓永久占满新开仓名额'),
  cfg('ibkr_order_flow_tick_types', 'Last', 'Last', '订单流 Tick 类型', '信号与反转', 225.3, '默认只订阅 reqTickByTickData Last，BidAsk 暂复用 L1 quote 以节省 tick 额度'),
  cfg('ibkr_order_flow_confirm_window_sec', '60', '60', '订单流确认窗口秒', '信号与反转', 225.4, 'CVD/delta ratio 的默认确认窗口；v1 shadow 统计使用'),
  cfg('ibkr_order_flow_tbt_freshness_sec', '120', '120', 'TBT订单流新鲜度秒', '信号与反转', 225.45, '订单流确认只接受 tick-by-tick 成交数据；超过该秒数无 TBT 则 fail-closed'),
  cfg('ibkr_order_flow_min_delta_ratio', '0.12', '0.12', '最小 Delta Ratio', '信号与反转', 225.5, '订单流方向确认阈值；多头要求大于该值，空头要求小于负该值'),
  cfg('ibkr_order_flow_max_spread_bps', '12', '12', '订单流最大点差bp', '信号与反转', 225.6, '订单流确认时允许的最大 spread bps；超过则等待或超时拒绝'),
  cfg('ibkr_order_flow_auto_entry_enabled', 'TRUE', 'TRUE', '订单流自动开仓', '信号与反转', 225.7, '开启后 enforce 模式会用订单流确认 pending signal，确认后提交 marketable LMT bracket'),
  cfg('ibkr_order_flow_auto_exit_enabled', 'TRUE', 'TRUE', '订单流提前平仓', '信号与反转', 225.8, '开启后 enforce 模式会在强反向 CVD/delta 且价格失效时用 marketable LMT 提前平仓'),
  cfg('ibkr_order_flow_stop_tighten_enabled', 'TRUE', 'TRUE', '订单流收紧止损', '信号与反转', 225.9, '开启后 enforce 模式允许订单流只收紧止损；never_widen_stop_by_order_flow 仍保持硬保护'),
  cfg('ibkr_order_flow_entry_timeout_sec', '60', '60', '订单流开仓等待秒', '信号与反转', 225.91, 'pending signal 等待订单流确认的最长秒数，超时后不下单'),
  cfg('ibkr_order_flow_exit_poll_sec', '2', '2', '订单流退出轮询秒', '信号与反转', 225.92, '订单流持仓风险评估的目标轮询秒数；当前随 lifecycle 循环执行'),
  cfg('ibkr_order_flow_marketable_limit_bps', '8', '8', '订单流可成交限价bp', '信号与反转', 225.93, '订单流确认后 marketable LMT 的保护价偏移 bp；long=ask+offset，short=bid-offset'),
  cfg('ibkr_order_flow_exit_delta_ratio', '0.18', '0.18', '订单流平仓Delta阈值', '信号与反转', 225.94, '反向 delta ratio 达到该阈值且价格失效时触发提前平仓'),
  cfg('ibkr_order_flow_stop_delta_ratio', '0.12', '0.12', '订单流止损Delta阈值', '信号与反转', 225.95, '反向 delta ratio 达到该阈值时尝试收紧止损'),
  cfg('ibkr_order_flow_close_fill_timeout_sec', '5', '5', '订单流平仓成交等待秒', '信号与反转', 225.96, '提前平仓 marketable LMT 提交后等待确认成交的秒数；未确认成交则保持 Closing 状态继续跟踪'),
  cfg('candidate_queue_max', '10', '10', '候选队列上限', '信号与反转', 226.0, '5m setup candidate queue 最大数量；同标的同向合并、反向冲突阻断'),
  cfg('candidate_breakout_ttl_sec', '120', '120', '突破候选TTL秒', '信号与反转', 226.1, 'breakout / squeeze setup 的订单流观察有效期'),
  cfg('candidate_pullback_ttl_sec', '300', '300', '回踩候选TTL秒', '信号与反转', 226.2, 'VWAP / trend pullback setup 的订单流观察有效期'),
  cfg('candidate_reversal_ttl_sec', '600', '600', '反转候选TTL秒', '信号与反转', 226.3, 'mean reversion / reversal setup 的订单流观察有效期'),
  cfg('quality_auto_full_min', '80', '80', 'A级质量分', '信号与反转', 227.0, 'A/A+ paper 自动化门槛参考；v1 shadow 统计使用'),
  cfg('quality_auto_small_min', '75', '75', 'A-质量分', '信号与反转', 227.1, 'A- 小仓/paper/观察门槛参考；v1 shadow 统计使用'),
  cfg('quality_shadow_min', '70', '70', '订单流Shadow质量分', '信号与反转', 227.2, '低于该质量分的候选仅保留普通信号，不进入订单流重点观察'),
  cfg('entry_breakout_order_timeout_sec', '15', '15', '突破订单超时秒', '信号与反转', 228.0, '未来 breakout marketable limit 的等待时间；当前版本不改变订单行为'),
  cfg('entry_pullback_order_timeout_sec', '90', '90', '回踩订单超时秒', '信号与反转', 228.1, '未来 pullback/reversal limit 的等待时间；当前版本不改变订单行为'),
  cfg('entry_watch_after_fill_sec', '180', '180', '成交后订单流观察秒', '信号与反转', 228.2, '成交后短暂保留持仓风险监控槽位，默认 180 秒后释放'),
  cfg('partial_take_profit_r', '1.0', '1.0', '核心止盈R', '信号与反转', 229.0, 'core 仓位默认止盈 R；当前版本作为配置占位与复盘字段'),
  cfg('partial_take_profit_fraction', '0.6', '0.6', '核心止盈比例', '信号与反转', 229.1, 'core 仓位默认比例；当前版本作为配置占位与复盘字段'),
  cfg('breakeven_trigger_r', '0.6', '0.6', '保本触发R', '信号与反转', 229.2, '达到该 R 后可推 breakeven；当前版本不放宽止损'),
  cfg('runner_enabled', 'TRUE', 'TRUE', 'Runner 开关', '信号与反转', 229.3, '趋势/突破可保留 runner；当前版本作为配置占位'),
  cfg('runner_fraction', '0.4', '0.4', 'Runner 比例', '信号与反转', 229.4, 'runner 默认仓位比例；mean reversion 默认不启用 runner'),
  cfg('mean_reversion_runner_enabled', 'FALSE', 'FALSE', '均值回归Runner', '信号与反转', 229.5, '均值回归默认不跑 runner'),
  cfg('breakout_runner_enabled', 'TRUE', 'TRUE', '突破Runner', '信号与反转', 229.6, '突破 setup 允许 runner'),
  cfg('trend_pullback_runner_enabled', 'TRUE', 'TRUE', '趋势回踩Runner', '信号与反转', 229.7, '趋势回踩 setup 允许 runner'),
  cfg('new_entry_cutoff_time', '14:45', '14:45', '新开仓截止时间', '信号与反转', 229.8, '订单流增强策略建议的新开仓截止时间，ET'),
  cfg('force_flat_time', '15:45', '15:45', '强制日内平仓时间', '信号与反转', 229.9, '订单流增强策略建议的日内强制平仓时间，ET'),
  cfg('never_widen_stop_by_order_flow', 'TRUE', 'TRUE', '订单流不放宽止损', '信号与反转', 229.91, '硬保护：订单流只允许提前退出、减仓或收紧止损，不能放宽止损'),
  cfg('cvd_flip_exit_enabled', 'TRUE', 'TRUE', 'CVD翻转退出', '信号与反转', 229.92, '未来用于 CVD 翻转提前减仓/退出；当前版本先记录'),
  cfg('cvd_divergence_take_profit_enabled', 'TRUE', 'TRUE', 'CVD背离止盈', '信号与反转', 229.93, '未来用于 CVD 背离提前止盈；当前版本先记录'),
  cfg('reverse_signal_threshold', '6', '6', '反转通知阈值', '信号与反转', 230, '仅当反转评分达到该阈值时发送反转卡片通知'),
  cfg('reverse_flip_enabled', 'FALSE', 'FALSE', '反向信号反手', '信号与反转', 235, '默认关闭；反向信号只用于取消/平仓/风控，不立即开反向新仓'),

  cfg('trade_window_start_time', '09:35', '09:35', '交易开始时间', '交易窗口', 300, '信号允许进入交易校验的开始时间，ET 时区'),
  cfg('trade_window_end_time', '15:30', '15:30', '交易结束时间', '交易窗口', 310, '超过该时间后不再接受新交易信号，ET 时区'),
  cfg('order_window_end_time', '15:00', '15:00', '下单截止时间', '交易窗口', 320, '超过该时间后新信号不再进入下单环节，ET 时区'),

  cfg('position_limit_max', '36', '36', '当日交易次数上限', '交易风控', 400, '当日成功下单计数达到该上限后，新的交易信号将被拒绝；0 表示不限制每日交易次数'),
  cfg('max_strategy_open_positions', '0', '0', '策略同时持仓上限', '交易风控', 402, '策略持仓与已提交未成交策略入场单合计达到该上限时，新信号保持 pending 等待容量；0 表示主要按动态购买力限制'),
  cfg('ibkr_order_symbol_queue_enabled', 'TRUE', 'TRUE', '标的级订单队列', '交易风控', 402.01, '开启后同一标的的开仓、平仓、撤单、改单按队列顺序执行，不同标的允许并发执行'),
  cfg('ibkr_order_symbol_queue_max_active_symbols', '12', '12', '并发活跃标的数', '交易风控', 402.02, '标的级订单队列允许同时执行的最大标的数；实际 Gateway 写入仍会短暂串行保护 orderId/ibapi 写安全'),
  cfg('ibkr_gateway_order_serial_enabled', 'TRUE', 'TRUE', 'Gateway写入安全门', '交易风控', 402.03, '开启后仅 Gateway 写入瞬间共用进程内安全门，避免 IB API 下单、撤单、改单请求互相穿插；订单确认可跨标的并行等待'),
  cfg('ibkr_gateway_order_serial_timeout_sec', '900', '900', 'Gateway写入排队超时', '交易风控', 402.04, 'Gateway 写入安全门的最长等待秒数；超时后返回 gateway_order_queue_timeout 且不发送 broker 请求'),
  cfg('ibkr_order_tracker_callback_cache_during_activity', 'TRUE', 'TRUE', '订单回调缓存优先', '交易风控', 402.05, '近期有订单回调时，订单 tracker 优先使用 Gateway callback cache，避免压力期间频繁 reqOpenOrders 抢占账户数据队列'),
  cfg('ibkr_order_tracker_skip_live_fetch_during_order_pressure', 'TRUE', 'TRUE', '下单压力跳过OpenOrders', '交易风控', 402.06, '存在本地下单购买力预留时，订单 tracker 不主动 reqOpenOrders，优先使用 callback/PB 状态，避免抢占账户数据队列'),
  cfg('ibkr_account_data_skip_during_order_pressure', 'TRUE', 'TRUE', '下单压力跳过账户拉取', '交易风控', 402.07, '存在本地下单购买力预留时，lifecycle 暂停 positions/account summary/account snapshot 拉取，避免影响下单确认'),
  cfg('ibkr_buying_power_guard_enabled', 'TRUE', 'TRUE', '购买力阈值保护', '交易风控', 402.1, '开启后新开仓会基于 IBKR BuyingPower 计算下单后剩余购买力；低于禁止阈值时拒绝下单'),
  cfg('ibkr_buying_power_warn_usd', '25000', '25000', '购买力预警金额', '交易风控', 402.2, '下单后剩余 BuyingPower 低于 max(该金额, NetLiq 百分比阈值) 时发送预警但允许继续'),
  cfg('ibkr_buying_power_warn_pct_net_liq', '20', '20', '购买力预警净值%', '交易风控', 402.3, '预警阈值的 NetLiq 百分比部分；默认 20%'),
  cfg('ibkr_buying_power_block_usd', '10000', '10000', '购买力禁止金额', '交易风控', 402.4, '下单后剩余 BuyingPower 低于 max(该金额, NetLiq 百分比阈值) 时禁止交易'),
  cfg('ibkr_buying_power_block_pct_net_liq', '10', '10', '购买力禁止净值%', '交易风控', 402.5, '禁止阈值的 NetLiq 百分比部分；默认 10%'),
  cfg('ibkr_buying_power_notify_enabled', 'TRUE', 'TRUE', '购买力通知', '交易风控', 402.6, '开启后手动/自动开仓会记录剩余购买力，预警或禁止时发送告警'),
  cfg('ibkr_buying_power_notify_cooldown_sec', '1800', '1800', '购买力预警冷却秒数', '交易风控', 402.7, '同一标的同一购买力状态重复预警的最短间隔；成功开仓通知不受此冷却限制'),
  cfg('ibkr_buying_power_stale_safe_enabled', 'TRUE', 'TRUE', '购买力安全降级', '交易风控', 402.71, '账户快照过期但剩余购买力非常充足时允许降级为 warning，不因轻微过期直接 fail-closed'),
  cfg('ibkr_buying_power_stale_safe_max_age_sec', '1800', '1800', '购买力安全降级最长秒数', '交易风控', 402.72, 'stale account snapshot/baseline 允许降级的最大年龄；超过仍暂停自动开仓'),
  cfg('ibkr_buying_power_stale_safe_min_usd', '50000', '50000', '购买力安全降级最低余额', '交易风控', 402.73, '降级允许时下单后剩余购买力至少需要达到该美元金额'),
  cfg('ibkr_buying_power_stale_safe_block_multiple', '5', '5', '购买力安全降级禁止倍数', '交易风控', 402.74, '降级允许时 remaining_after 至少为 block_floor 的该倍数'),
  cfg('ibkr_buying_power_stale_safe_exposure_multiple', '3', '3', '购买力安全降级下单倍数', '交易风控', 402.75, '降级允许时 remaining_after 至少为本次预估占用的该倍数'),
  cfg('fixed_position_symbols', 'BOXX,IBKR', 'BOXX,IBKR', '固定持仓标的', '交易风控', 403, '这些标的不允许策略开仓，也不占用策略同时持仓容量；逗号分隔'),
  cfg('cooldown_bars_after_sl', '6', '6', '止损后冷却K线数', '交易风控', 405, '同标的止损后冷却多少根 5m K线，冷却期间不再开新仓'),
  cfg('cooldown_bars_after_reverse', '3', '3', '反向退出后冷却K线数', '交易风控', 406, '同标的反向信号平仓后冷却多少根 5m K线，冷却期间不再开新仓'),
  cfg('atr_dynamic_stop_enabled', 'TRUE', 'TRUE', 'ATR动态止损', '交易风控', 407, '开启后仅允许按 ATR 收紧止损，不放宽风险，不调整 TP'),
  cfg('live_exit_policy_stop_update_enabled', 'FALSE', 'FALSE', '实盘追踪止盈调止损', '交易风控', 407.5, '开启后实盘按完成的 5m bar 使用共享 exit policy 计算，只允许收紧 stop，不放宽风险'),
  cfg('atr_stop_min_profit_r', '0.3', '0.3', 'ATR调止损最小盈利R', '交易风控', 408, '持仓至少达到该 R 倍盈利后才允许 ATR 动态收紧止损'),
  cfg('atr_stop_deviation_threshold', '0.30', '0.30', 'ATR调止损变化阈值', '交易风控', 408.2, '当前 ATR 相对上次记录 ATR 变化超过该比例才触发收紧评估'),
  cfg('atr_stop_min_change', '0.01', '0.01', 'ATR调止损最小价差', '交易风控', 408.4, '新旧止损价差至少达到该值才尝试改单'),
  cfg('order_validity_minutes', '30', '30', '订单有效期', '交易风控', 410, 'Init / Submitted 状态的订单超过此时间自动标记为 Canceled'),

  cfg('eod_close_time', '15:55', '15:55', 'EOD 平仓时间', '日终规则', 500, '到达该 ET 时间后自动执行日终平仓'),
  cfg('eod_keep_symbols', 'BOXX,IBKR', 'BOXX,IBKR', 'EOD 保留标的', '日终规则', 510, '日终平仓时跳过这些标的，逗号分隔；默认保留 BOXX 与 IBKR'),

  cfg('watchlist_interval_min', '5', '5', '标的同步间隔', '标的订阅', 600, '联动 sync_*: 同步窗口内按该间隔刷新 watchlist 股票池'),
  cfg('ibkr_target_refresh_sec', '60', '60', '目标订阅刷新秒数', '标的订阅', 610, '按 ibkr_targets 刷新当日实时订阅列表'),
  cfg('ibkr_scan_schedule', '08:20-09:20', '08:20-09:20', '盘前扫描窗口', '标的订阅', 611, '盘前自动扫描允许触发的 ET 时间窗口；默认 08:20-09:20 每 5 分钟覆盖预筛，供 scan 调度与页面展示使用'),
  cfg('ibkr_daily_scan_time_et', '08:20', '08:20', '日筛起始时间', '标的订阅', 612, '每日自动预筛起始时间，ET 时区；当前设计为 08:20-09:20 每 5 分钟覆盖预筛'),
  cfg('ibkr_daily_scan_min_avg_10d_volume', '100000', '100000', '日筛最小10D均量', '标的订阅', 614, '自动日筛硬门槛：10 日平均成交量至少达到该值'),
  cfg('ibkr_daily_scan_min_premarket_volume', '5000', '5000', '日筛最小盘前量', '标的订阅', 616, '自动日筛硬门槛：盘前累计成交量至少达到该值'),
  cfg('ibkr_target_activity_gate_stages_json', '[{"id":"preopen_early","start_et":"08:20","end_et":"08:55","any_of":{"premarket_volume_gte":3000}},{"id":"preopen_final","start_et":"09:00","end_et":"09:25","any_of":{"premarket_volume_gte":5000}},{"id":"open_discovery","start_et":"09:30","end_et":"09:45","any_of":{"premarket_volume_gte":5000,"regular_volume_gte":10000,"elapsed_rvol_gte":1.5}},{"id":"open_followthrough","start_et":"09:50","end_et":"10:30","any_of":{"premarket_volume_gte":10000,"regular_volume_gte":30000,"elapsed_rvol_gte":1.2}},{"id":"late_morning","start_et":"10:35","end_et":"11:00","any_of":{"regular_volume_gte":60000,"elapsed_rvol_gte":1.0}}]', '[{"id":"preopen_early","start_et":"08:20","end_et":"08:55","any_of":{"premarket_volume_gte":3000}},{"id":"preopen_final","start_et":"09:00","end_et":"09:25","any_of":{"premarket_volume_gte":5000}},{"id":"open_discovery","start_et":"09:30","end_et":"09:45","any_of":{"premarket_volume_gte":5000,"regular_volume_gte":10000,"elapsed_rvol_gte":1.5}},{"id":"open_followthrough","start_et":"09:50","end_et":"10:30","any_of":{"premarket_volume_gte":10000,"regular_volume_gte":30000,"elapsed_rvol_gte":1.2}},{"id":"late_morning","start_et":"10:35","end_et":"11:00","any_of":{"regular_volume_gte":60000,"elapsed_rvol_gte":1.0}}]', '分阶段成交活跃门槛', '标的订阅', 617, 'JSON 配置 08:20-11:00 不同阶段的 activity any_of 门槛；盘前看 premarket_volume，开盘后看 regular_volume / elapsed_rvol'),
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
  cfg('ibkr_open_report_target_capture_enabled', 'TRUE', 'TRUE', '开盘摘要空池补采集', '标的订阅', 619.72, '09:30 开盘摘要发现今日 active/candidate 目标为空时，检查或触发 topup scan，等待完成后再重读目标池。'),
  cfg('ibkr_open_report_target_wait_sec', '45', '45', '开盘摘要等待补池秒数', '标的订阅', 619.74, '开盘摘要等待 09:25/open-report topup 完成的最大秒数；超时后仍发送摘要并标注开盘采集状态。'),
  cfg('ibkr_open_report_target_poll_sec', '3', '3', '开盘摘要补池轮询秒数', '标的订阅', 619.76, '开盘摘要等待补池时轮询 /scan/status 的间隔秒数。'),
  cfg('ibkr_daily_scan_indicator_snapshot_enabled', 'TRUE', 'TRUE', '日筛指标快照', '标的订阅', 619.8, '开启后日筛会保存多周期指标快照，供目标原因、图表和信号上下文复用'),
  cfg('ibkr_daily_scan_indicator_snapshot_intervals', '5m,15m,30m,1h,4h,1d', '5m,15m,30m,1h,4h,1d', '日筛指标快照周期', '标的订阅', 619.81, '日筛保存指标快照的周期列表；默认覆盖 5m/15m/30m/1h/4h/1d'),
  cfg('ibkr_daily_scan_rollup_enabled', 'TRUE', 'TRUE', '日筛多周期 Rollup', '标的订阅', 619.82, '开启后日筛先基于 5m official close bars 生成 15m/30m/1h/4h/1d 周期数据，降低高周期直接拉取压力'),
  cfg('ibkr_daily_scan_rollup_incremental', 'TRUE', 'TRUE', '日筛增量 Rollup', '标的订阅', 619.83, '开启后只补齐缺失或过期的 rollup 周期，避免每次扫描全量重算'),
  cfg('ibkr_daily_scan_rollup_intervals', '15m,30m,1h,4h,1d', '15m,30m,1h,4h,1d', '日筛 Rollup 周期', '标的订阅', 619.84, '日筛从 5m bars 生成的目标周期列表；默认生成 15m/30m/1h/4h/1d'),
  cfg('ibkr_rollup_parallel_enabled', 'TRUE', 'TRUE', 'Rollup 周期并行', '标的订阅', 619.845, '开启后多周期 rollup 会从同一批 5m bars 按目标周期并行生成；单周期仍串行'),
  cfg('ibkr_rollup_max_workers', '5', '5', 'Rollup 最大周期并发', '标的订阅', 619.846, '多周期 rollup 的最大 worker 数；默认 5，对应 15m/30m/1h/4h/1d 一一并行'),
  cfg('ibkr_daily_scan_materialize_enabled', 'TRUE', 'TRUE', '日筛指标物化', '标的订阅', 619.85, '开启后日筛会物化交易所需的近期 bars 和指标，减少开盘后实时链路临时计算压力'),
  cfg('ibkr_daily_scan_materialize_intervals', '5m,15m,30m,1h', '5m,15m,30m,1h', '日筛指标物化周期', '标的订阅', 619.86, '日筛物化 bars/指标的周期列表；默认覆盖 5m/15m/30m/1h，高周期保留为趋势快照上下文'),
  cfg('ibkr_timeframe_param_profiles_json', '{"5m":{"signal_strategy_profile":"core_two_setup_v1","intraday_include_legacy_signals":false},"15m":{"sd_length":96,"dtp_sma_length":80,"dtp_atr_length":160,"crsi_domcycle":24,"signal_window_max_bars":8},"30m":{"sd_length":80,"dtp_sma_length":70,"dtp_atr_length":140,"ema_slope_lookback":10},"1h":{"sd_length":80,"dtp_sma_length":60,"dtp_atr_length":120,"ema_slope_lookback":8},"4h":{"sd_length":60,"dtp_sma_length":50,"dtp_atr_length":100,"ema_slope_lookback":6},"1d":{"sd_length":50,"dtp_sma_length":40,"dtp_atr_length":80,"ema_slope_lookback":5}}', '{"5m":{"signal_strategy_profile":"core_two_setup_v1","intraday_include_legacy_signals":false},"15m":{"sd_length":96,"dtp_sma_length":80,"dtp_atr_length":160,"crsi_domcycle":24,"signal_window_max_bars":8},"30m":{"sd_length":80,"dtp_sma_length":70,"dtp_atr_length":140,"ema_slope_lookback":10},"1h":{"sd_length":80,"dtp_sma_length":60,"dtp_atr_length":120,"ema_slope_lookback":8},"4h":{"sd_length":60,"dtp_sma_length":50,"dtp_atr_length":100,"ema_slope_lookback":6},"1d":{"sd_length":50,"dtp_sma_length":40,"dtp_atr_length":80,"ema_slope_lookback":5}}', '多周期指标参数', '标的订阅', 619.87, '按周期覆盖 SD / DTP / CRSI / slope 等指标参数；避免 5m/15m/30m/1h/4h/1d 共用同一套技术指标窗口'),
  cfg('ibkr_trade_target_persistent_quote_enabled', 'FALSE', 'FALSE', 'Trade 目标常驻 Quote', '标的订阅', 619.9, '默认关闭：active trade target 不再常驻订阅 quote，只在下单前临时订阅/快照；market monitor 仍常驻'),
  cfg('ibkr_target_subscription_limit', '80', '80', 'Trade 订阅上限', '标的订阅', 620, 'trade 标的单独上限；实际 trade 可用预算会再与总订阅上限扣除 monitor 预留后的余额取更小值'),
  cfg('ibkr_total_subscription_limit', '80', '80', '总订阅上限', '标的订阅', 625, 'WS 总订阅上限，包含 trade targets 与 market monitor 订阅'),
  cfg('entry_pre_submit_temp_subscription_limit', '8', '8', '下单前临时报价预留', '标的订阅', 625.5, '从 trade 订阅预算中预留的临时 quote 槽位，保证 entry guard 可为新信号临时订阅或拉取报价'),
  cfg('ibkr_realtime_quote_stale_resubscribe_sec', '600', '600', 'Quote 自动重订阅阈值', '标的订阅', 627, '实时 quote 超过该秒数未更新时，运行态会强制 unsubscribe/subscribe 修复僵尸订阅'),
  cfg('ibkr_realtime_quote_resubscribe_cooldown_sec', '300', '300', 'Quote 重订阅冷却秒数', '标的订阅', 628, '同一标的自动重订阅后的最小冷却秒数，避免 IBKR streaming 频繁抖动'),
  cfg('ibkr_market_ws_symbols', 'SPY,QQQ,VIX', 'SPY,QQQ,VIX', '市场监控标的', '标的订阅', 626, '系统级 WS 市场监控默认订阅标的；用于 monitor / runtime 状态页和行情链路基准观测'),
  cfg('ibkr_market_calendar_symbol', 'SPY', 'SPY', '交易日历代表标的', '标的订阅', 626.2, '用于 IBKR ContractDetails 交易时间拉取的代表合约 symbol；默认 SPY 代表美股常规日历'),
  cfg('ibkr_market_calendar_exchange', 'SMART', 'SMART', '交易日历交易所', '标的订阅', 626.3, '用于 IBKR ContractDetails 交易时间拉取的 exchange；默认 SMART'),
  cfg('ibkr_market_calendar_sec_type', 'STK', 'STK', '交易日历证券类型', '标的订阅', 626.4, '用于 IBKR ContractDetails 交易时间拉取的 secType；默认 STK'),

  cfg('ibkr_bar_publish_enabled', 'TRUE', 'TRUE', 'K线发布开关', '行情链路', 700, '控制实时 / 回补 bars 是否写入 PocketBase；关闭后页面与指标链路不会收到新 OHLCV'),
  cfg('tv_webhook_ingest_enabled', 'TRUE', 'TRUE', 'TV Webhook 入库开关', '行情链路', 702, '控制 /webhook/tv 是否写入 TV-primary 事件并路由 pre_alert / entry / risk_update / exit；关闭后直接返回 skipped'),
  cfg('tv_max_active_targets', '100', '100', 'TV 活跃标的上限', '行情链路', 703, 'TradingView pre_alert 入池后，系统按 activity_score / quality_score 排序，每日最多激活多少个标的'),
  cfg('tv_max_same_direction_targets', '0', '0', 'TV 同方向上限', '行情链路', 7031, 'TV 活跃标的排行中同一 direction_bias 最多保留多少个 active；0 表示不限制方向'),
  cfg('tv_entry_requires_active_target', 'FALSE', 'FALSE', 'TV 入场要求 Active 标的', '行情链路', 705, 'TRUE 时 entry webhook 必须命中当日 active ibkr_targets；TV-primary 默认关闭，改由授权标的保护'),
  cfg('tv_entry_requires_authorized_symbol', 'TRUE', 'TRUE', 'TV 入场要求授权标的', '行情链路', 7050, 'TRUE 时 entry webhook 必须命中授权交易宇宙或 watchlist 交易标的，避免非授权 TradingView 标的直接下单'),
  cfg('tv_primary_trade_universe_symbols', '', '', 'TV Primary 授权标的覆盖', '行情链路', 70505, '逗号分隔的 TV-primary 授权交易标的覆盖；留空时使用现有交易 watchlist / active universe'),
  cfg('tv_entry_allow_self_activate', 'TRUE', 'TRUE', 'TV Entry 自激活', '行情链路', 7051, 'TRUE 时 entry payload 携带 qualified=true 可先写入 pre_alert 并参与排名；排名不足仍拒绝'),
  cfg('tv_webhook_async_route_enabled', 'TRUE', 'TRUE', 'TV Webhook 异步路由', '行情链路', 706.8, 'TRUE 时 /webhook/tv 先持久化 received 事件再异步路由，降低 TradingView webhook 响应延迟；PB 落库失败时尝试本地 fsync spool'),
  cfg('tv_command_reconcile_enabled', 'TRUE', 'TRUE', 'TV Pending Command 自愈', '行情链路', 706.81, '开启后巡检统一 TV pending command，并对可确认或可重试的异步执行状态做保守自愈'),
  cfg('tv_command_reconcile_lookback_min', '1440', '1440', 'TV Command 自愈回看分钟', '行情链路', 706.82, '自愈巡检 pending command 的最大回看窗口；默认 1440 分钟'),
  cfg('tv_command_reconcile_max_wait_sec', '900', '900', 'TV Command 最大等待秒数', '行情链路', 706.83, 'pending command 进入异步等待态后允许自愈确认或重试前的最大等待秒数；默认 900 秒'),
  cfg('tv_command_reconcile_pending_warn_min', '15', '15', 'TV Command Pending 告警分钟', '行情链路', 706.84, '监控中 TV 执行动作 pending 超过该分钟数后告警；默认 15 分钟，与异步自愈等待窗口一致'),
  cfg('tv_command_reconcile_conservative_resubmit', 'TRUE', 'TRUE', 'TV Command 保守重提', '行情链路', 706.85, '开启后自愈只在缺少可用 broker 执行痕迹且幂等条件满足时才重提执行请求'),
  cfg('tv_entry_window_enforce_enabled', 'TRUE', 'TRUE', 'TV 入场窗口校验', '行情链路', 707, 'TRUE 时系统兜底校验 RTH 入场时间；exit / risk_update 不受限制'),
  cfg('tv_entry_limit_freshness_sec', '240', '240', 'TV 限价入场有效秒数', '行情链路', 707.1, 'TV direct bounded limit/cap 入场信号的最大年龄；限价帽已限制成交容错，默认 240 秒'),
  cfg('tv_entry_primary_start', '09:45', '09:45', 'TV 主入场开始', '行情链路', 7071, '美东时间，默认 09:45；避开开盘前 15 分钟噪声'),
  cfg('tv_entry_primary_end', '11:30', '11:30', 'TV 主入场结束', '行情链路', 7072, '美东时间，09:45-11:30 为主入场窗口'),
  cfg('tv_entry_closing_start', '14:00', '14:00', 'TV 尾盘高质量开始', '行情链路', 70725, '美东时间，14:00-15:15 为尾盘高质量窗口'),
  cfg('tv_entry_quality_end', '15:15', '15:15', 'TV 高质量入场截止', '行情链路', 7073, '美东时间，11:30-15:15 连续分层 quality/closing_quality；15:15 后不新开仓'),
  cfg('tv_quality_window_rank_enforce_enabled', 'FALSE', 'FALSE', 'TV 午后排名门', '行情链路', 70735, 'TV-primary 默认关闭午后 active rank 门槛，避免 100 标的池被 top-N 排名阻断；开启后才应用 tv_quality_window_max_rank'),
  cfg('tv_quality_window_max_rank', '5', '5', 'TV 午后最大排名', '行情链路', 7074, '质量窗口内 entry 允许的最大 activity_rank'),
  cfg('tv_quality_window_min_activity_score', '80', '80', 'TV 午后活跃分门槛', '行情链路', 7075, '质量窗口内 entry 需要的最低 activity_score'),
  cfg('tv_quality_window_min_signal_quality_score', '85', '85', 'TV 午后信号分门槛', '行情链路', 7076, '质量窗口内 entry 需要的最低 quality_score'),
  cfg('tv_closing_quality_window_min_activity_score', '90', '90', 'TV 尾盘活跃分门槛', '行情链路', 7077, '尾盘高质量窗口内 entry 需要的最低 activity_score'),
  cfg('tv_closing_quality_window_min_signal_quality_score', '90', '90', 'TV 尾盘信号分门槛', '行情链路', 7078, '尾盘高质量窗口内 entry 需要的最低 quality_score'),
  cfg('tv_risk_update_seq_guard_enabled', 'TRUE', 'TRUE', 'TV Risk Update 序号保护', '行情链路', 708, 'TRUE 时 compute 对 risk_update_seq 做单调保护；已处理的较旧或重复序号不会再次改保护单'),
  cfg('tv_risk_update_require_monotonic_seq', 'TRUE', 'TRUE', 'TV Risk Update 单调序号别名', '行情链路', 708.05, 'tv_risk_update_seq_guard_enabled 的兼容别名；TRUE 表示 risk_update_seq 必须单调递增'),
  cfg('tv_risk_update_never_widen_stop', 'TRUE', 'TRUE', 'TV Risk Update 不放宽止损', '行情链路', 708.1, 'TRUE 时 TV risk_update 只能收紧止损；相对 previous_stop_loss 放宽的更新会被阻止'),
  cfg('tv_risk_update_missing_child_order_retry_pending', 'TRUE', 'TRUE', 'TV Risk Update 缺保护单等待', '行情链路', 708.2, 'TRUE 时缺少 TP/SL 子订单 ID 的 risk_update 保持 pending 以便保护单回补后重试'),
  cfg('tv_risk_update_retry_missing_child_orders', 'TRUE', 'TRUE', 'TV Risk Update 缺保护单重试别名', '行情链路', 708.25, 'tv_risk_update_missing_child_order_retry_pending 的兼容别名；TRUE 表示缺少子订单 ID 时不丢弃 risk_update'),
  cfg('ibkr_active_repair_interval_min', '5', '5', '活跃修复间隔', '行情链路', 705, '当前实时订阅标的的缺口 / rollup 异常巡检间隔，按 5m 链路优先修复'),
  cfg('ibkr_watchlist_idle_topup_enabled', 'FALSE', 'FALSE', '底池空闲动态回补', '行情链路', 706, 'TV-primary 瘦身默认关闭；开启后 Runtime 空闲时为 watchlist 非活跃标的补齐 5m bars'),
  cfg('ibkr_history_repair_enabled', 'FALSE', 'FALSE', '历史缺口修复', '行情链路', 706.05, 'TV-primary 瘦身默认关闭 legacy bars 历史修复；需要恢复本地 bars 链路时再开启'),
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
  cfg('ibkr_watchlist_idle_topup_no_data_cooldown_min', '15', '15', '无数据冷却分钟', '行情链路', 707.001, '盘前/盘后/闭市时 watchlist 标的返回 HMDS no data 后的跳过冷却，避免不活跃标的被高频重复拉取'),
  cfg('ibkr_watchlist_idle_topup_regular_no_data_cooldown_min', '5', '5', '盘中无数据冷却分钟', '行情链路', 707.002, 'regular session 中返回 HMDS no data 后的较短冷却；仍会在状态中标记，避免误判为系统故障'),
  cfg('ibkr_watchlist_inactive_no_data_count_threshold', '3', '3', '无数据治理次数阈值', '行情链路', 707.003, '同一 watchlist 标的连续多次 HMDS no data 后标记为 inactive/removal candidate，仅告警建议不自动移除'),
  cfg('ibkr_watchlist_inactive_days_threshold', '3', '3', '无数据治理天数阈值', '行情链路', 707.004, '同一 watchlist 标的跨多个交易日 HMDS no data 后标记为 removal candidate，供人工确认是否移出底池'),
  cfg('ibkr_watchlist_hygiene_notify_cooldown_hours', '24', '24', '底池治理告警冷却小时', '行情链路', 707.005, 'watchlist inactive/removal candidate 治理建议告警的最小重复间隔，避免同一批标的刷屏'),
  cfg('ibkr_history_max_concurrency', '16', '16', '历史请求最大并发', '行情链路', 707.04, 'Runtime 历史 bars 请求最大并发；默认 16，保护 official 5m close，避免 historical 请求雪崩'),
  cfg('ibkr_history_request_spacing', '0.05', '0.05', '历史请求排队间隔秒数', '行情链路', 707.05, 'Runtime 历史 bars 请求之间的全局排队间隔；watchlist 追新默认 0.05 秒，降低 5m close 后全池落库延迟'),
  cfg('ibkr_history_repair_intraday_max_symbols_per_run', '8', '8', '盘中修复 Symbol 上限', '行情链路', 707.055, '盘中 data-quality repair 每轮最多修复多少个 symbol；保护 official 5m close 和实时信号链路'),
  cfg('ibkr_history_repair_intraday_time_budget_s', '60', '60', '盘中修复时间预算', '行情链路', 707.056, '盘中 data-quality repair 单轮最多运行秒数；超出后剩余 symbol 延期'),
  cfg('ibkr_history_repair_offhours_max_symbols_per_run', '25', '25', '盘后修复 Symbol 上限', '行情链路', 707.057, '非盘中 data-quality repair 每轮最多修复多少个 symbol'),
  cfg('ibkr_history_repair_offhours_time_budget_s', '240', '240', '盘后修复时间预算', '行情链路', 707.058, '非盘中 data-quality repair 单轮最多运行秒数'),
  cfg('ibkr_large_operation_alert_enabled', 'true', 'true', '大规模操作告警', '行情链路', 707.059, '开启后，批量回填、全池修复、长时间历史请求等大规模操作会发送开始、进度和结束告警'),
  cfg('ibkr_large_operation_alert_min_symbols', '25', '25', '大规模 Symbol 阈值', '行情链路', 707.0591, '单次操作 symbol 数达到该值时触发大规模操作告警'),
  cfg('ibkr_large_operation_alert_min_tasks', '50', '50', '大规模任务阈值', '行情链路', 707.0592, '单次操作 symbol * interval 数达到该值时触发大规模操作告警'),
  cfg('ibkr_large_operation_alert_min_period_days', '120', '120', '大规模周期天数阈值', '行情链路', 707.0593, '历史请求周期达到该天数时触发大规模操作告警'),
  cfg('ibkr_large_operation_alert_min_duration_s', '120', '120', '大规模耗时阈值', '行情链路', 707.0594, '操作运行超过该秒数时触发进行中告警'),
  cfg('ibkr_large_operation_alert_min_written', '10000', '10000', '大规模写入阈值', '行情链路', 707.0595, '单次操作写入 bars 达到该值时触发完成告警'),
  cfg('ibkr_large_operation_alert_min_requests', '100', '100', '大规模请求阈值', '行情链路', 707.0596, '单次操作历史请求数达到该值时触发告警'),
  cfg('ibkr_large_operation_alert_min_retry', '10', '10', '大规模 Retry 阈值', '行情链路', 707.0597, '单次操作历史请求 retry 数达到该值时触发告警'),
  cfg('ibkr_large_operation_alert_min_throttle', '50', '50', '大规模 Throttle 阈值', '行情链路', 707.0598, '单次操作 throttle 数达到该值时触发告警'),
  cfg('ibkr_large_operation_alert_min_time_budget_s', '120', '120', '大规模预算阈值', '行情链路', 707.0599, '计划运行时间预算达到该秒数时触发大规模操作告警'),
  cfg('ibkr_large_operation_progress_cooldown_s', '300', '300', '大规模进度告警冷却', '行情链路', 707.05991, '同一大规模操作的进行中告警最小间隔秒数，避免刷屏'),
  cfg('ibkr_large_operation_duration_gate_sources', 'watchlist_idle_topup,runtime_direct_topup,runtime_direct_topup_parallel,bar_repair,active_repair,official_5m_close', 'watchlist_idle_topup,runtime_direct_topup,runtime_direct_topup_parallel,bar_repair,active_repair,official_5m_close', '耗时门控任务来源', '行情链路', 707.059915, '这些自动回补来源的大规模操作不按 symbol 数立即报警，只有超过大规模耗时阈值或失败/延期时才报警'),
  cfg('ibkr_large_operation_requires_ack', 'false', 'false', '大规模操作需确认', '行情链路', 707.05992, '预留开关；当前默认只告警不阻断，开启后可扩展为执行前必须人工确认'),
  cfg('ibkr_official_5m_enabled', 'FALSE', 'FALSE', '官方 5m Close 拉取', '行情链路', 707.079, 'TV-primary 瘦身默认关闭本地 official 5m bars 追新线程'),
  cfg('ibkr_official_5m_close_delay_sec', '3', '3', '5m Close 安全等待秒数', '行情链路', 707.08, '5m bar close 后等待多少秒再拉 IBKR 官方历史 bars；越小越快，但过小可能遇到 IBKR 尚未产出该 bar'),
  cfg('ibkr_runtime_direct_topup_close_delay_sec', '8', '8', '高周期 Close 安全等待秒数', '行情链路', 707.09, '15m/30m/1h/4h/1d direct topup 在周期 close 后等待多少秒再拉 IBKR 历史 bars'),
  cfg('ibkr_runtime_direct_topup_loop_interval_sec', '1', '1', '高周期回补循环秒数', '行情链路', 707.091, '高周期 direct topup 空闲检查间隔；默认 1 秒，减少 close 后等待下一轮循环的延迟'),
  cfg('ibkr_runtime_direct_topup_enabled', 'false', 'false', '高周期 Direct Topup', '行情链路', 707.089, '盘中默认关闭高周期直接拉 IBKR historical；高周期主路径由 official 5m rollup 生成'),
  cfg('ibkr_runtime_direct_topup_parallel_enabled', 'false', 'false', '高周期批量追新', '行情链路', 707.092, '开启后同一轮把所有已到期高周期按优先级批量拉取；默认关闭，避免抢占 official 5m historical 通道'),
  cfg('ibkr_runtime_direct_topup_interval_priority', '4h,1h,30m,15m,1d', '4h,1h,30m,15m,1d', '高周期追新优先级', '行情链路', 707.093, '多个高周期同时到期时的拉取顺序；默认先处理原先最容易延后的 4h/1h'),
  cfg('ibkr_watchlist_idle_topup_materialize_5m', 'TRUE', 'TRUE', '回补后物化 5m', '行情链路', 707.1, '回补后默认物化 5m 指标；同一批可仅为 active targets 持久化信号，普通 watchlist 只补 bars/指标'),
  cfg('ibkr_watchlist_idle_topup_progress_warn_sec', '7200', '7200', '回补进度提醒秒数', '行情链路', 707.2, '长时间无底池回补进展时用于状态提示，便于发现 IBKR 历史链路卡住'),
  cfg('ibkr_watchlist_active_due_guard_sec', '45', '45', 'Active 5m Due Guard 秒数', '行情链路', 707.3, '距离 active 目标下一根 5m close 少于该秒数时暂停底池回补，保护实时信号链路'),
  cfg('ibkr_peak_shedding_enabled', 'TRUE', 'TRUE', '高峰削峰开关', '行情链路', 707.31, '开启后 Runtime 根据主机资源进入 green/warning/shedding/critical 模式，优先保护 active targets、official 5m close、信号和订单链路'),
  cfg('ibkr_peak_cpu_shedding_pct', '78', '78', '削峰 CPU 阈值%', '行情链路', 707.32, 'CPU 5m 平均达到该值时进入 shedding，暂停或显著压低 watchlist/batch 工作，避免 compute_busy 雪崩'),
  cfg('ibkr_peak_cpu_recovery_pct', '60', '60', '削峰恢复 CPU 阈值%', '行情链路', 707.33, '预留的恢复阈值；用于后续 hysteresis，当前作为状态观测和配置基线'),
  cfg('ibkr_peak_watchlist_warning_max_symbols', '40', '40', 'Warning 回补 Symbol 上限', '行情链路', 707.34, 'warning 模式下 watchlist idle topup 每轮最多处理的 symbol 数，降低历史请求峰值'),
  cfg('ibkr_peak_watchlist_warning_batch_size', '40', '40', 'Warning 回补批次', '行情链路', 707.35, 'warning 模式下静态 watchlist idle topup 批次大小'),
  cfg('ibkr_peak_watchlist_warning_history_concurrency', '6', '6', 'Warning 历史并发', '行情链路', 707.36, 'warning 模式下 watchlist idle topup 历史请求并发上限'),
  cfg('ibkr_peak_watchlist_warning_request_spacing', '0.15', '0.15', 'Warning 请求间隔秒', '行情链路', 707.37, 'warning 模式下 watchlist idle topup 历史请求排队间隔'),
  cfg('ibkr_peak_watchlist_shedding_max_symbols', '8', '8', 'Shedding 回补 Symbol 上限', '行情链路', 707.38, 'shedding 模式下保留的极小 watchlist 预算；默认会暂停 idle topup，仅作为观测/手动放开时上限'),
  cfg('ibkr_peak_watchlist_shedding_batch_size', '8', '8', 'Shedding 回补批次', '行情链路', 707.39, 'shedding 模式下静态 watchlist idle topup 批次大小上限'),
  cfg('ibkr_peak_watchlist_shedding_history_concurrency', '2', '2', 'Shedding 历史并发', '行情链路', 707.391, 'shedding 模式下 watchlist idle topup 历史请求并发上限'),
  cfg('ibkr_peak_watchlist_shedding_request_spacing', '0.3', '0.3', 'Shedding 请求间隔秒', '行情链路', 707.392, 'shedding 模式下 watchlist idle topup 历史请求排队间隔'),
  cfg('ibkr_peak_watchlist_critical_history_concurrency', '1', '1', 'Critical 历史并发', '行情链路', 707.393, 'critical 模式下仅保留最小历史请求并发，idle topup 默认关闭'),
  cfg('ibkr_peak_watchlist_critical_request_spacing', '0.5', '0.5', 'Critical 请求间隔秒', '行情链路', 707.394, 'critical 模式下历史请求最小排队间隔，保护 L0/L1 链路恢复'),
  cfg('ibkr_compute_busy_defer_sec', '60', '60', 'Compute Busy 退避秒', '行情链路', 707.395, 'Scheduler 遇到 compute_busy 后首轮延迟多久再重试，避免高峰期反复 POST /compute 放大 CPU'),
  cfg('ibkr_compute_busy_defer_cap_sec', '300', '300', 'Compute Busy 退避上限秒', '行情链路', 707.396, 'Scheduler compute_busy 指数退避的最大等待秒数'),
  cfg('ibkr_compute_cursor_seed_from_indicators_enabled', 'TRUE', 'TRUE', 'Compute 游标指标回退', '行情链路', 707.397, '持久化游标为空时允许从 indicators 回退生成游标；若 PB 读取失败会跳过该重扫描以保护实时链路'),
  cfg('ibkr_watchlist_backfill_interval_min', '30', '30', '底池回补间隔', '行情链路', 710, '非目标标的按批次执行 5m 增量回补的间隔'),
  cfg('ibkr_watchlist_backfill_batch_size', '12', '12', '底池回补批次', '行情链路', 720, '每轮底池回补最多处理多少个非目标标的'),
  cfg('ibkr_watchlist_backfill_stale_min', '20', '20', '底池回补滞后阈值', '行情链路', 730, '仅当最近 5m bar 超过该阈值未更新时才触发回补'),
  cfg('ibkr_watchlist_integrity_enabled', 'TRUE', 'TRUE', '底池完整性巡检', '行情链路', 740, '启用后按批次巡检非目标标的的 5m bars 完整性，并将结果写入 ibkr_bar_integrity，同时增量更新 ibkr_bar_coverage_daily 日级覆盖账本'),
  cfg('ibkr_watchlist_integrity_batch_size', '8', '8', '底池巡检批次', '行情链路', 750, '每轮底池完整性巡检最多处理多少个非目标标的'),
  cfg('ibkr_history_retention_enabled', 'FALSE', 'FALSE', '历史留存清理', '行情链路', 760, 'TV-primary 默认关闭 legacy 每小时留存；低峰 storage_cleanup 负责安全清理。即使手动开启，代码策略也会跳过订单、信号、目标、watchlist、config 与关键 ibkr_state。'),
  cfg('ibkr_history_retention_days', '365', '365', '历史留存天数', '行情链路', 770, 'legacy 每小时留存窗口；仅适用于 bars / indicators / bar quality 等非核心表，核心审计和交易表不参与自动留存删除'),
  cfg('storage_cleanup_enabled', 'TRUE', 'TRUE', '存储治理开关', '存储治理', 780, '开启后，ibkr-scheduler 每日低峰触发 PocketBase 存储治理；只清理可重建或可归档数据，不删除 config / watchlist / 活跃信号订单 / bars 核心窗口'),
  cfg('storage_cleanup_profile', 'tv_primary_lean', 'tv_primary_lean', '存储治理 Profile', '存储治理', 781, '默认 tv_primary_lean：指标表清空可重建数据，bars/TV webhook 保留 90 天，质量/审计/系统事件保留 30 天，回测仅保留保护项和最近 5 个 run/batch'),
  cfg('storage_cleanup_backtest_recent_limit', '5', '5', '回测保留数量', '存储治理', 782, '除保护的最佳 batch/run 外，保留最近多少个 backtest batch/run；旧 run 的回测子表 rows 会被同步清理，不影响实盘订单、信号或目标表'),
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
  cfg('trade_ledger_chat_id', 'oc_c5f7f750a38692b48220b8f6c58e0ac9', 'oc_c5f7f750a38692b48220b8f6c58e0ac9', '交易流水群 Chat ID', '通知路由', 635, 'IBKR socket 实时订单回调流水发送到这里，用于确认真实开仓、保护单和退出单状态'),
  cfg('reverse_chat_id', 'oc_2931e2b8501df3a9d869d7aebceb8fe2', 'oc_2931e2b8501df3a9d869d7aebceb8fe2', '反转群 Chat ID', '通知路由', 640, '反转信号与反转执行卡片默认发送到这里'),

  cfg('status_notify_enabled', 'TRUE', 'TRUE', '状态摘要通知', '系统通知', 900, 'NYSE 交易日 09:30 开盘交易摘要、重启和定时系统状态摘要通知开关；09:30 由开盘摘要接管，:00 / :30 会合并同轮心跳与健康检查结果，不再额外发送重复卡片'),
  cfg('market_closed_notify_enabled', 'TRUE', 'TRUE', '闭市提醒通知', '系统通知', 905, '非交易日 09:30 ET 发送一次闭市与下次开盘提醒；实际交易日历优先从 IBKR 合约交易时间拉取，失败时回退本地 NYSE 日历'),
  cfg('market_closed_notify_weekends', 'TRUE', 'TRUE', '周末闭市提醒', '系统通知', 906, '闭市提醒是否覆盖周末；默认开启，保证每天 09:30 ET 都有明确开闭市状态'),
  cfg('daily_summary_notify_enabled', 'TRUE', 'TRUE', '日报通知', '系统通知', 910, '仅 NYSE 交易日收盘后发送当日交易汇总；周末或非交易日跳过'),
  cfg('manual_stop_notify_enabled', 'TRUE', 'TRUE', '手动停止通知', '系统通知', 920, '手动停止算法时发送通知'),
  cfg('health_check_notify_enabled', 'TRUE', 'TRUE', '兜底心跳通知', '系统通知', 930, '仅在状态摘要关闭时，用于发送独立 ok 心跳兜底；warning / error 级别仍走 inspection_notify_enabled'),
  cfg('inspection_notify_enabled', 'TRUE', 'TRUE', '巡检告警', '系统通知', 940, '孤立持仓、恢复异常、数据缺口等巡检告警'),
  cfg('system_data_gap_bar_lag_alert_min', '20', '20', 'Bars 缺口告警分钟', '系统通知', 941, 'regular 5m bars 相对全局最新 regular bar 落后达到该分钟数时才触发数据缺口告警；watchlist 可继续回补，但告警不要过早打扰'),
  cfg('system_data_gap_indicator_lag_alert_min', '30', '30', '指标缺口告警分钟', '系统通知', 942, 'active/candidate 标的的指标相对最新 5m bar 落后超过该分钟数时才触发数据缺口告警'),
  cfg('system_data_gap_alert_cooldown_min', '30', '30', '数据缺口冷却分钟', '系统通知', 943, '同一数据缺口 fingerprint 重复通知的最短间隔分钟数'),
  cfg('system_data_gap_indicator_requires_targets', 'TRUE', 'TRUE', '指标缺口仅看目标', '系统通知', 944, 'TRUE 时 active/candidate 数为 0 不因 watchlist 普通标的缺少指标而告警；watchlist bars 缺口仍会巡检'),
  cfg('system_monitor_host_load_consecutive_count', '2', '2', 'Load 连续命中次数', '系统通知', 945, '仅对 host_load_high / host_load_critical 生效；Monitor Cron 每 5 分钟检查一次，默认需连续 2 次命中才发告警'),
  cfg('system_monitor_alert_error_cooldown_min', '15', '15', 'Monitor 严重冷却分钟', '系统通知', 945.1, 'IBKR Monitor error 级别同一 fingerprint 重复通知的最短间隔分钟数'),
  cfg('system_monitor_alert_warning_cooldown_min', '60', '60', 'Monitor 告警冷却分钟', '系统通知', 945.2, 'IBKR Monitor warning 级别同一 fingerprint 重复通知的最短间隔分钟数'),
  cfg('system_monitor_account_snapshot_warning_consecutive_count', '2', '2', '账户快照连续告警次数', '系统通知', 945.3, 'Account snapshot 探针类 warning 连续命中达到该次数后才发送 IBKR Monitor 告警'),
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
      if (collection === 'config' && existing) {
        payload.value = existing.value ?? payload.value;
      } else if (collection === 'config') {
        const aliases = CONFIG_ALIAS_FALLBACKS[payload.key] || [];
        for (const aliasKey of aliases) {
          const aliasRecord = await findExistingRecord(collection, resolvedUniqueField, aliasKey);
          if (aliasRecord) {
            payload.value = aliasRecord.value ?? payload.value;
            break;
          }
        }
      }
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
