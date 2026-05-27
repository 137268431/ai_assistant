/// <reference path="../pb_data/types.d.ts" />

const IBKR_TARGET_DECISIONS_SCHEMA = {
  "id": "_pb_ibkr_target_decisions_",
  "name": "ibkr_target_decisions",
  "type": "base",
  "system": false,
  "listRule": "",
  "viewRule": "",
  "createRule": "",
  "updateRule": "",
  "deleteRule": "",
  "fields": [
    {
      "autogeneratePattern": "[a-z0-9]{15}",
      "hidden": false,
      "id": "text3208210256",
      "max": 15,
      "min": 15,
      "name": "id",
      "pattern": "^[a-z0-9]+$",
      "presentable": false,
      "primaryKey": true,
      "required": true,
      "system": true,
      "type": "text"
    },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_decision_key", "max": 220, "min": 0, "name": "decision_key", "pattern": "", "presentable": true, "primaryKey": false, "required": true, "system": false, "type": "text" },
    { "hidden": false, "id": "itd_environment", "maxSelect": 1, "name": "environment", "presentable": true, "required": true, "system": false, "type": "select", "values": ["live", "paper", "backtest"] },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_market_date", "max": 10, "min": 0, "name": "market_date", "pattern": "", "presentable": true, "primaryKey": false, "required": true, "system": false, "type": "text" },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_source", "max": 80, "min": 0, "name": "source", "pattern": "", "presentable": true, "primaryKey": false, "required": false, "system": false, "type": "text" },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_scan_run_id", "max": 160, "min": 0, "name": "scan_run_id", "pattern": "", "presentable": false, "primaryKey": false, "required": false, "system": false, "type": "text" },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_symbol", "max": 24, "min": 0, "name": "symbol", "pattern": "", "presentable": true, "primaryKey": false, "required": true, "system": false, "type": "text" },
    { "hidden": false, "id": "itd_decision", "maxSelect": 1, "name": "decision", "presentable": true, "required": true, "system": false, "type": "select", "values": ["selected", "rejected", "deferred", "not_selected", "error", "unknown"] },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_reason_code", "max": 120, "min": 0, "name": "reason_code", "pattern": "", "presentable": true, "primaryKey": false, "required": false, "system": false, "type": "text" },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_reason_text", "max": 1000, "min": 0, "name": "reason_text", "pattern": "", "presentable": true, "primaryKey": false, "required": false, "system": false, "type": "text" },
    { "hidden": false, "id": "itd_metrics", "maxSize": 2097152, "name": "metrics", "presentable": false, "required": false, "system": false, "type": "json" },
    { "hidden": false, "id": "itd_thresholds", "maxSize": 2097152, "name": "thresholds", "presentable": false, "required": false, "system": false, "type": "json" },
    { "hidden": false, "id": "itd_rank", "max": null, "min": null, "name": "rank", "onlyInt": true, "presentable": true, "required": false, "system": false, "type": "number" },
    { "hidden": false, "id": "itd_active_gate", "name": "active_gate_passed", "presentable": false, "required": false, "system": false, "type": "bool" },
    { "hidden": false, "id": "itd_context_gate", "name": "context_gate_passed", "presentable": false, "required": false, "system": false, "type": "bool" },
    { "autogeneratePattern": "", "hidden": false, "id": "itd_related_target_id", "max": 80, "min": 0, "name": "related_target_id", "pattern": "", "presentable": false, "primaryKey": false, "required": false, "system": false, "type": "text" },
    { "hidden": false, "id": "itd_created_ms", "max": null, "min": null, "name": "created_ms", "onlyInt": true, "presentable": false, "required": false, "system": false, "type": "number" },
    { "hidden": false, "id": "itd_extra", "maxSize": 2097152, "name": "extra", "presentable": false, "required": false, "system": false, "type": "json" },
    { "hidden": false, "id": "autodate2990389176", "name": "created", "onCreate": true, "onUpdate": false, "presentable": false, "system": false, "type": "autodate" },
    { "hidden": false, "id": "autodate3332085495", "name": "updated", "onCreate": true, "onUpdate": true, "presentable": false, "system": false, "type": "autodate" }
  ],
  "indexes": [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ibkr_target_decisions_key ON ibkr_target_decisions (decision_key)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_target_decisions_day_symbol ON ibkr_target_decisions (environment, market_date, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_target_decisions_decision ON ibkr_target_decisions (environment, market_date, decision)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_target_decisions_source ON ibkr_target_decisions (environment, market_date, source)"
  ]
}

function findIbkrTargetDecisionsCollection(app) {
  try {
    return app.findCollectionByNameOrId("_pb_ibkr_target_decisions_")
  } catch (e) {}
  try {
    return app.findCollectionByNameOrId("ibkr_target_decisions")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findIbkrTargetDecisionsCollection(app)

  if (collection) {
    unmarshal(IBKR_TARGET_DECISIONS_SCHEMA, collection)
  } else {
    collection = new Collection(IBKR_TARGET_DECISIONS_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrTargetDecisionsCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
