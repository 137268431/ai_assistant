/// <reference path="../pb_data/types.d.ts" />

const ENVIRONMENTS = ["global", "live", "paper"]

const CONFIGS = [
  ["tv_entry_window_enforce_enabled", "TRUE", "TV 入场窗口校验", "行情链路", 707, "TRUE 时系统兜底校验 RTH 入场时间；exit / risk_update 不受限制"],
  ["tv_entry_primary_start", "09:45", "TV 主入场开始", "行情链路", 7071, "美东时间，默认 09:45；避开开盘前 15 分钟噪声"],
  ["tv_entry_primary_end", "11:30", "TV 主入场结束", "行情链路", 7072, "美东时间，09:45-11:30 为主入场窗口"],
  ["tv_entry_closing_start", "14:00", "TV 尾盘高质量开始", "行情链路", 70725, "美东时间，14:00-15:15 为尾盘高质量窗口"],
  ["tv_entry_quality_end", "15:15", "TV 高质量入场截止", "行情链路", 7073, "美东时间，11:30-15:15 连续分层 quality/closing_quality；15:15 后不新开仓"],
  ["tv_quality_window_rank_enforce_enabled", "FALSE", "TV 午后排名门", "行情链路", 70735, "TV-primary 默认关闭午后 active rank 门槛，避免 100 标的池被 top-N 排名阻断；开启后才应用 tv_quality_window_max_rank"],
  ["tv_quality_window_min_activity_score", "80", "TV 午后活跃分门槛", "行情链路", 7075, "质量窗口内 entry 需要的最低 activity_score"],
  ["tv_quality_window_min_signal_quality_score", "85", "TV 午后信号分门槛", "行情链路", 7076, "质量窗口内 entry 需要的最低 quality_score"],
  ["tv_closing_quality_window_min_activity_score", "90", "TV 尾盘活跃分门槛", "行情链路", 7077, "尾盘高质量窗口内 entry 需要的最低 activity_score"],
  ["tv_closing_quality_window_min_signal_quality_score", "90", "TV 尾盘信号分门槛", "行情链路", 7078, "尾盘高质量窗口内 entry 需要的最低 quality_score"],
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
