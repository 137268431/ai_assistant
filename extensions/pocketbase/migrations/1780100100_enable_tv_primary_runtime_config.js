/// <reference path="../pb_data/types.d.ts" />

const CONFIGS = [
  ["tv_webhook_ingest_enabled", "TRUE", "TV Webhook 入库开关", "行情链路", 702, "启用 TV-primary /webhook/tv 事件入口"],
  ["ibkr_signal_source", "tradingview", "信号来源", "信号与反转", 200, "TV-primary 迁移后只执行 TradingView webhook 写入的 ibkr_signals"],
]
const ENVIRONMENTS = ["global", "live", "paper"]

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
