/// <reference path="../pb_data/types.d.ts" />

const ENVIRONMENTS = ["global", "live", "paper"]

const CONFIGS = [
  ["storage_cleanup_profile", "tv_primary_lean", "存储治理 Profile", "存储治理", 781, "TV-primary 默认策略：指标缓存可清空，bars/TV webhook 保留 90 天，质量/审计/系统事件保留 30 天，回测仅保留保护项和最近 5 个 run/batch"],
  ["storage_cleanup_backtest_recent_limit", "5", "回测保留数量", "存储治理", 782, "除保护的最佳 batch/run 外，默认仅保留最近 5 个 backtest batch/run；不影响实盘订单、信号或目标表"],
  ["pb_cron_ibkr_history_retention_enabled", "FALSE", "历史数据留存", "Scheduler 调度(兼容 key)", 140, "TV-primary 默认关闭 legacy 每小时留存 Cron，避免旧策略触碰核心信号/目标表；低峰 storage_cleanup 接管安全清理"],
  ["ibkr_history_retention_enabled", "FALSE", "历史留存清理", "行情链路", 760, "TV-primary 默认关闭 legacy 每小时留存；即使手动开启，代码策略也会跳过订单、信号、目标、watchlist、config 与关键 ibkr_state"],
  ["pb_cron_ibkr_storage_governor_enabled", "TRUE", "PocketBase 存储治理", "Scheduler 调度(兼容 key)", 1405, "每日美东 03:20 执行 tv_primary_lean 存储治理；只删除可重建或过期的非核心数据，不自动 VACUUM"],
]

function findCollection(app, name) {
  try { return app.findCollectionByNameOrId(name) } catch (_) {}
  return null
}

function upsertConfig(app, item, environment) {
  const collection = findCollection(app, "config")
  if (!collection) return
  const key = item[0]
  const value = item[1]
  let record = null
  try {
    record = app.findFirstRecordByFilter("config", "key = {:key} && environment = {:env}", { key, env: environment })
  } catch (_) {}
  if (!record) record = new Record(collection, {})
  record.set("key", key)
  record.set("value", value)
  record.set("default_value", value)
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
