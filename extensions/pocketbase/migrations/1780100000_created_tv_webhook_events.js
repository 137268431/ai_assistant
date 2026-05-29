/// <reference path="../pb_data/types.d.ts" />

const TV_WEBHOOK_EVENTS_SCHEMA = {
  "id": "_pb_tv_webhook_events_",
  "listRule": "",
  "viewRule": "",
  "createRule": "",
  "updateRule": "",
  "deleteRule": "",
  "name": "tv_webhook_events",
  "type": "base",
  "system": false,
  "fields": [
    {
      "id": "text3208210256",
      "name": "id",
      "type": "text",
      "required": true,
      "presentable": false,
      "hidden": false,
      "system": true,
      "primaryKey": true,
      "min": 15,
      "max": 15,
      "pattern": "^[a-z0-9]+$",
      "autogeneratePattern": "[a-z0-9]{15}"
    },
    {
      "id": "tvwe_event_id",
      "name": "event_id",
      "type": "text",
      "required": true,
      "presentable": true,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_event_type",
      "name": "event_type",
      "type": "select",
      "required": true,
      "presentable": true,
      "hidden": false,
      "system": false,
      "maxSelect": 1,
      "values": [
        "pre_alert",
        "entry",
        "risk_update",
        "exit",
        "heartbeat"
      ]
    },
    {
      "id": "tvwe_symbol",
      "name": "symbol",
      "type": "text",
      "required": false,
      "presentable": true,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_direction",
      "name": "direction",
      "type": "select",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "maxSelect": 1,
      "values": [
        "long",
        "short",
        "neutral"
      ]
    },
    {
      "id": "tvwe_position_side",
      "name": "position_side",
      "type": "select",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "maxSelect": 1,
      "values": [
        "long",
        "short"
      ]
    },
    {
      "id": "tvwe_signal_id",
      "name": "signal_id",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_position_id",
      "name": "position_id",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_environment",
      "name": "environment",
      "type": "select",
      "required": false,
      "presentable": true,
      "hidden": false,
      "system": false,
      "maxSelect": 1,
      "values": [
        "live",
        "paper",
        "backtest"
      ]
    },
    {
      "id": "tvwe_broker_mode",
      "name": "broker_mode",
      "type": "select",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "maxSelect": 1,
      "values": [
        "live",
        "paper",
        "backtest"
      ]
    },
    {
      "id": "tvwe_date",
      "name": "date",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_bar_time_ms",
      "name": "bar_time_ms",
      "type": "number",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "onlyInt": true,
      "min": null,
      "max": null
    },
    {
      "id": "tvwe_script_tag",
      "name": "script_tag",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_strategy_version",
      "name": "strategy_version",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_timeframe_stack",
      "name": "timeframe_stack",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_chart_url",
      "name": "tv_chart_url",
      "type": "url",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "exceptDomains": [],
      "onlyDomains": []
    },
    {
      "id": "tvwe_status",
      "name": "status",
      "type": "select",
      "required": false,
      "presentable": true,
      "hidden": false,
      "system": false,
      "maxSelect": 1,
      "values": [
        "received",
        "routed",
        "skipped_duplicate",
        "rejected",
        "failed"
      ]
    },
    {
      "id": "tvwe_route_target",
      "name": "route_target",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_route_record_id",
      "name": "route_record_id",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_error_msg",
      "name": "error_msg",
      "type": "text",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "min": 0,
      "max": 0,
      "pattern": "",
      "autogeneratePattern": ""
    },
    {
      "id": "tvwe_payload",
      "name": "payload",
      "type": "json",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "maxSize": 2097152
    },
    {
      "id": "tvwe_extra",
      "name": "extra",
      "type": "json",
      "required": false,
      "presentable": false,
      "hidden": false,
      "system": false,
      "maxSize": 2097152
    },
    {
      "id": "autodate_tvwe_created",
      "name": "created",
      "type": "autodate",
      "hidden": false,
      "presentable": false,
      "required": false,
      "system": false,
      "onCreate": true,
      "onUpdate": false
    },
    {
      "id": "autodate_tvwe_updated",
      "name": "updated",
      "type": "autodate",
      "hidden": false,
      "presentable": false,
      "required": false,
      "system": false,
      "onCreate": true,
      "onUpdate": true
    }
  ],
  "indexes": [
    "CREATE UNIQUE INDEX idx_tv_webhook_events_event_env ON tv_webhook_events (event_id, environment)",
    "CREATE INDEX idx_tv_webhook_events_env_date_type ON tv_webhook_events (environment, date, event_type)",
    "CREATE INDEX idx_tv_webhook_events_env_symbol_created ON tv_webhook_events (environment, symbol, created)",
    "CREATE INDEX idx_tv_webhook_events_signal_id ON tv_webhook_events (signal_id)",
    "CREATE INDEX idx_tv_webhook_events_position_id ON tv_webhook_events (position_id)",
    "CREATE INDEX idx_tv_webhook_events_status ON tv_webhook_events (status)"
  ]
}


const REVERSE_EXTRA_SOURCES = ["signal", "indicator", "manual", "tradingview", "system", "risk_guard"]
const REVERSE_LEGACY_SOURCES = ["signal", "indicator", "manual"]
const REVERSE_ACTIONS_EXTENDED = ["close", "cancel", "adjust_sl", "adjust_tp", "adjust_bracket"]
const REVERSE_ACTIONS_LEGACY = ["close", "cancel", "adjust_sl", "adjust_tp"]
const SYSTEM_EVENT_TYPES_EXTENDED = ["heartbeat", "status_change", "alert", "compute_stats", "daily_report", "tv_webhook"]
const SYSTEM_EVENT_TYPES_LEGACY = ["heartbeat", "status_change", "alert", "compute_stats", "daily_report"]
const SYSTEM_SOURCES_EXTENDED = ["qc", "ibkr_compute", "ibkr_api", "pb", "manual", "tradingview"]
const SYSTEM_SOURCES_LEGACY = ["qc", "ibkr_compute", "ibkr_api", "pb", "manual"]
const TV_PRIMARY_CONFIGS = [
  ["tv_webhook_ingest_enabled", "TRUE", "TV Webhook 入库开关", "行情链路", 702, "控制 /webhook/tv 是否写入 TV-primary 事件并路由 pre_alert / entry / risk_update / exit"],
  ["tv_max_active_targets", "10", "TV 活跃标的上限", "行情链路", 703, "TradingView pre_alert 入池后，系统按 activity_score / quality_score 排序，每日最多激活多少个标的"],
  ["tv_max_same_direction_targets", "7", "TV 同方向上限", "行情链路", 7031, "TV 活跃标的排行中同一 direction_bias 最多保留多少个 active；0 表示不限制方向"],
  ["tv_entry_requires_active_target", "TRUE", "TV 入场要求 Active 标的", "行情链路", 705, "TRUE 时 entry webhook 必须命中当日 active ibkr_targets"],
  ["tv_entry_allow_self_activate", "TRUE", "TV Entry 自激活", "行情链路", 7051, "TRUE 时 entry payload 携带 qualified=true 可先写入 pre_alert 并参与排名"],
  ["tv_entry_window_enforce_enabled", "TRUE", "TV 入场窗口校验", "行情链路", 707, "TRUE 时系统兜底校验 RTH 入场时间；exit / risk_update 不受限制"],
  ["tv_entry_primary_start", "09:40", "TV 主入场开始", "行情链路", 7071, "美东时间，默认 09:40"],
  ["tv_entry_primary_end", "11:30", "TV 主入场结束", "行情链路", 7072, "美东时间，09:40-11:30 为主入场窗口"],
  ["tv_entry_quality_end", "14:30", "TV 高质量入场截止", "行情链路", 7073, "美东时间，11:30-14:30 只允许高质量 late entry"],
  ["tv_quality_window_max_rank", "5", "TV 午后最大排名", "行情链路", 7074, "质量窗口内 entry 允许的最大 activity_rank"],
  ["tv_quality_window_min_activity_score", "80", "TV 午后活跃分门槛", "行情链路", 7075, "质量窗口内 entry 需要的最低 activity_score"],
  ["tv_quality_window_min_signal_quality_score", "85", "TV 午后信号分门槛", "行情链路", 7076, "质量窗口内 entry 需要的最低 quality_score"],
]

function findCollection(app, name) {
  try { return app.findCollectionByNameOrId(name) } catch (_) {}
  return null
}

function setSelectValues(app, collectionName, fieldName, values) {
  const collection = findCollection(app, collectionName)
  if (!collection) return
  const field = collection.fields.getByName(fieldName)
  if (!field) return
  field.values = values
  app.save(collection)
}

function ensureConfig(app, item) {
  const collection = findCollection(app, "config")
  if (!collection) return
  const key = item[0]
  const defaultValue = item[1]
  let record = null
  try {
    record = app.findFirstRecordByFilter("config", "key = {:key} && environment = {:env}", { key, env: "global" })
  } catch (_) {}
  if (!record) {
    record = new Record(collection, {})
    record.set("value", defaultValue)
    record.set("environment", "global")
  }
  record.set("key", key)
  record.set("default_value", defaultValue)
  record.set("display_name", item[2])
  record.set("group_name", item[3])
  record.set("sort_order", item[4])
  record.set("description", item[5])
  app.save(record)
}

migrate((app) => {
  let collection = findCollection(app, "tv_webhook_events")
  if (collection) {
    unmarshal(TV_WEBHOOK_EVENTS_SCHEMA, collection)
  } else {
    collection = new Collection(TV_WEBHOOK_EVENTS_SCHEMA)
  }
  app.save(collection)
  setSelectValues(app, "ibkr_reverse_signals", "source", REVERSE_EXTRA_SOURCES)
  setSelectValues(app, "ibkr_reverse_signals", "action_type", REVERSE_ACTIONS_EXTENDED)
  setSelectValues(app, "system_events", "event_type", SYSTEM_EVENT_TYPES_EXTENDED)
  setSelectValues(app, "system_events", "source", SYSTEM_SOURCES_EXTENDED)
  TV_PRIMARY_CONFIGS.forEach((item) => ensureConfig(app, item))
  return null
}, (app) => {
  const collection = findCollection(app, "tv_webhook_events")
  if (collection) app.delete(collection)
  setSelectValues(app, "ibkr_reverse_signals", "source", REVERSE_LEGACY_SOURCES)
  setSelectValues(app, "ibkr_reverse_signals", "action_type", REVERSE_ACTIONS_LEGACY)
  setSelectValues(app, "system_events", "event_type", SYSTEM_EVENT_TYPES_LEGACY)
  setSelectValues(app, "system_events", "source", SYSTEM_SOURCES_LEGACY)
  return null
})
