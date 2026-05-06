/// <reference path="../pb_data/types.d.ts" />

const IBKR_BAR_COVERAGE_DAILY_SCHEMA = {
  "id": "_pb_ibkr_bar_coverage_daily_",
  "name": "ibkr_bar_coverage_daily",
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
    {
      "hidden": false,
      "id": "ibcd_environment",
      "maxSelect": 1,
      "name": "environment",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": [
        "live",
        "paper",
        "backtest"
      ]
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_market_date",
      "max": 40,
      "min": 0,
      "name": "market_date",
      "pattern": "",
      "presentable": true,
      "primaryKey": false,
      "required": true,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_symbol",
      "max": 20,
      "min": 0,
      "name": "symbol",
      "pattern": "",
      "presentable": true,
      "primaryKey": false,
      "required": true,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_interval",
      "max": 20,
      "min": 0,
      "name": "interval",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": true,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "ibcd_session_mode",
      "maxSelect": 1,
      "name": "session_mode",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": [
        "regular",
        "extended"
      ]
    },
    {
      "hidden": false,
      "id": "ibcd_status",
      "maxSelect": 1,
      "name": "status",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": [
        "ok",
        "soft_gap",
        "hard_gap",
        "repairing",
        "repaired",
        "repair_failed",
        "unverifiable"
      ]
    },
    {
      "hidden": false,
      "id": "ibcd_hard_gate",
      "name": "hard_gate",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "bool"
    },
    {
      "hidden": false,
      "id": "ibcd_needs_repair",
      "name": "needs_repair",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "bool"
    },
    {
      "hidden": false,
      "id": "ibcd_expected_count",
      "max": null,
      "min": null,
      "name": "expected_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_actual_count",
      "max": null,
      "min": null,
      "name": "actual_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_missing_count",
      "max": null,
      "min": null,
      "name": "missing_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_gap_count",
      "max": null,
      "min": null,
      "name": "gap_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_duplicate_count",
      "max": null,
      "min": null,
      "name": "duplicate_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_bad_ohlc_count",
      "max": null,
      "min": null,
      "name": "bad_ohlc_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_expected_start_ms",
      "max": null,
      "min": null,
      "name": "expected_start_ms",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_expected_end_ms",
      "max": null,
      "min": null,
      "name": "expected_end_ms",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_first_bar_ms",
      "max": null,
      "min": null,
      "name": "first_bar_ms",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ibcd_last_bar_ms",
      "max": null,
      "min": null,
      "name": "last_bar_ms",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_last_checked_at",
      "max": 80,
      "min": 0,
      "name": "last_checked_at",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_last_repair_at",
      "max": 80,
      "min": 0,
      "name": "last_repair_at",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "ibcd_missing_windows",
      "maxSize": 1048576,
      "name": "missing_windows",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "ibcd_missing_examples",
      "maxSize": 1048576,
      "name": "missing_examples",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "ibcd_repair_windows",
      "maxSize": 1048576,
      "name": "repair_windows",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_expected_mask_hex",
      "max": 8192,
      "min": 0,
      "name": "expected_mask_hex",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_actual_mask_hex",
      "max": 8192,
      "min": 0,
      "name": "actual_mask_hex",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_missing_mask_hex",
      "max": 8192,
      "min": 0,
      "name": "missing_mask_hex",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ibcd_source",
      "max": 80,
      "min": 0,
      "name": "source",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "ibcd_extra",
      "maxSize": 1048576,
      "name": "extra",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "autodate_ibcd_created",
      "name": "created",
      "onCreate": true,
      "onUpdate": false,
      "presentable": false,
      "system": false,
      "type": "autodate"
    },
    {
      "hidden": false,
      "id": "autodate_ibcd_updated",
      "name": "updated",
      "onCreate": true,
      "onUpdate": true,
      "presentable": false,
      "system": false,
      "type": "autodate"
    }
  ],
  "indexes": [
    "CREATE UNIQUE INDEX idx_ibkr_bar_coverage_daily_unique ON ibkr_bar_coverage_daily (environment, market_date, symbol, interval, session_mode)",
    "CREATE INDEX idx_ibkr_bar_coverage_daily_env_date_status ON ibkr_bar_coverage_daily (environment, market_date, status)",
    "CREATE INDEX idx_ibkr_bar_coverage_daily_env_needs_repair ON ibkr_bar_coverage_daily (environment, needs_repair)",
    "CREATE INDEX idx_ibkr_bar_coverage_daily_env_symbol_date ON ibkr_bar_coverage_daily (environment, symbol, market_date)",
    "CREATE INDEX idx_ibkr_bar_coverage_daily_env_hard_gate ON ibkr_bar_coverage_daily (environment, hard_gate)"
  ],
  "options": {}
}

function findIbkrBarCoverageDailyCollection(app) {
  try {
    return app.findCollectionByNameOrId("_pb_ibkr_bar_coverage_daily_")
  } catch (e) {}
  try {
    return app.findCollectionByNameOrId("ibkr_bar_coverage_daily")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findIbkrBarCoverageDailyCollection(app)

  if (collection) {
    unmarshal(IBKR_BAR_COVERAGE_DAILY_SCHEMA, collection)
  } else {
    collection = new Collection(IBKR_BAR_COVERAGE_DAILY_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrBarCoverageDailyCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
