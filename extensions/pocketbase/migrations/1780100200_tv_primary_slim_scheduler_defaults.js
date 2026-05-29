/// <reference path="../pb_data/types.d.ts" />

const ENVIRONMENTS = ["global", "live", "paper"]

const CONFIGS = [
  ["ibkr_runtime_technical_pipeline_enabled", "FALSE", "Runtime 技术指标链路", "运行总控", 122, "TV-primary 瘦身默认关闭 legacy bars/indicators/signals 技术链路调度；需要恢复 compute/scan/数据质量技术任务时先显式开启"],
  ["ibkr_warmup_indicator_backfill_enabled", "FALSE", "Warmup 指标回补", "运行总控", 124, "TV-primary 瘦身默认关闭本地指标 warmup/backfill"],
  ["ibkr_tv_primary_runtime_slim_enabled", "TRUE", "TV-primary Runtime 瘦身", "运行总控", 123, "标记当前运行态采用 TV-primary 瘦身默认值：TradingView 作为信号主源，legacy 技术链路 cron 默认关闭，执行保护类任务保留"],
  ["pb_cron_ibkr_compute_runtime_enabled", "FALSE", "Compute + 状态摘要", "Scheduler 调度(兼容 key)", 132, "TV-primary 瘦身默认关闭 legacy indicator/signal compute 调度；系统状态摘要由 status/monitor/日报等保护任务继续承担"],
  ["pb_cron_system_heartbeat_enabled", "FALSE", "系统心跳", "Scheduler 调度(兼容 key)", 1321, "TV-primary 瘦身默认关闭独立心跳 cron，减少与 summary/monitor 重复的噪声"],
  ["pb_cron_ibkr_scan_runtime_enabled", "FALSE", "信号窗口预筛", "Scheduler 调度(兼容 key)", 133, "TV-primary 瘦身默认关闭 legacy daily scan / target universe 技术预筛 cron"],
  ["pb_cron_ibkr_fundamentals_refresh_enabled", "FALSE", "Fundamentals 刷新", "Scheduler 调度(兼容 key)", 13305, "TV-primary 瘦身默认关闭 legacy 盘前 fundamentals 刷新 cron，避免为旧扫描链路预热"],
  ["pb_cron_ibkr_active_window_progress_status_enabled", "FALSE", "标的/信号窗口动态卡", "Scheduler 调度(兼容 key)", 13315, "TV-primary 瘦身默认关闭 legacy active target / signal-window progress 动态卡 cron"],
  ["pb_cron_ibkr_early_expansion_topup_enabled", "FALSE", "信号窗口入池补充", "Scheduler 调度(兼容 key)", 13320, "TV-primary 瘦身默认关闭 legacy 信号窗口增量入池 cron"],
  ["pb_cron_ibkr_intraday_window_admission_enabled", "FALSE", "信号窗口入池", "Scheduler 调度(兼容 key)", 13325, "TV-primary 瘦身默认关闭 legacy 盘中 bars 驱动入池 cron"],
  ["pb_cron_system_data_gap_guard_enabled", "FALSE", "数据缺口巡检", "Scheduler 调度(兼容 key)", 136, "TV-primary 瘦身默认关闭 legacy bars/indicators 数据缺口告警 cron"],
  ["pb_cron_ibkr_data_quality_repair_sweep_enabled", "FALSE", "全观察池 Sweep", "Scheduler 调度(兼容 key)", 13620, "TV-primary 瘦身默认关闭 legacy bars/indicators 全观察池一致性 sweep cron"],
  ["pb_cron_ibkr_data_quality_open_sweep_enabled", "FALSE", "开盘前 Sweep 旧别名", "Scheduler 调度(Deprecated)", 13621, "旧开盘前 sweep 开关；TV-primary 瘦身默认关闭"],
  ["pb_cron_ibkr_data_quality_close_sweep_enabled", "FALSE", "收盘后 Sweep 旧别名", "Scheduler 调度(Deprecated)", 13622, "旧收盘后 sweep 开关；TV-primary 瘦身默认关闭"],
  ["pb_cron_ibkr_data_quality_premarket_truth_audit_enabled", "FALSE", "盘前真值审计旧别名", "Scheduler 调度(Deprecated)", 13635, "旧盘前 truth audit 开关；TV-primary 瘦身默认关闭"],
  ["pb_cron_ibkr_data_quality_truth_audit_enabled", "FALSE", "IBKR 真值审计", "Scheduler 调度(兼容 key)", 13640, "TV-primary 瘦身默认关闭 legacy bars/indicators/signals 真值审计 cron"],
  ["pb_cron_ibkr_tv_indicator_audit_enabled", "FALSE", "TV 指标审计", "Scheduler 调度(兼容 key)", 13645, "TV indicator_audit 快照真值审计默认关闭；仅在 TV 已配置对应 alert 后手动开启"],
  ["ibkr_order_flow_enabled", "FALSE", "订单流观察开关", "信号与反转", 225, "TV-primary 瘦身默认关闭本地订单流确认，保留 pre-submit quote guard 和订单保护链路"],
  ["ibkr_watchlist_idle_topup_enabled", "FALSE", "底池空闲动态回补", "行情链路", 706, "TV-primary 瘦身默认关闭 watchlist bars idle topup"],
  ["ibkr_history_repair_enabled", "FALSE", "历史缺口修复", "行情链路", 70605, "TV-primary 瘦身默认关闭 legacy bars 历史修复"],
  ["ibkr_official_5m_enabled", "FALSE", "官方 5m Close 拉取", "行情链路", 707079, "TV-primary 瘦身默认关闭本地 official 5m bars 追新线程"],
]

function findCollection(app, name) {
  try { return app.findCollectionByNameOrId(name) } catch (_) {}
  return null
}

function upsertConfig(app, item, environment) {
  const collection = findCollection(app, "config")
  if (!collection) return
  const key = item[0]
  let record = null
  try {
    record = app.findFirstRecordByFilter("config", "key = {:key} && environment = {:env}", { key, env: environment })
  } catch (_) {}
  if (!record) record = new Record(collection, {})
  record.set("key", key)
  record.set("value", item[1])
  record.set("default_value", item[1])
  record.set("display_name", item[2])
  record.set("group_name", item[3])
  record.set("sort_order", item[4])
  record.set("description", item[5])
  record.set("environment", environment)
  app.save(record)
}

migrate((app) => {
  CONFIGS.forEach((item) => {
    ENVIRONMENTS.forEach((environment) => upsertConfig(app, item, environment))
  })
  return null
}, (app) => {
  return null
})
