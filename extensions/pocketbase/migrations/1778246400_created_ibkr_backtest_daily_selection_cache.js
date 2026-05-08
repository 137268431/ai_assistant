/// <reference path="../pb_data/types.d.ts" />

const IBKR_BACKTEST_DAILY_SELECTION_CACHE_SCHEMA = {
  "id": "_pb_ibkr_bt_daily_sel_cache_",
  "listRule": "",
  "viewRule": "",
  "createRule": "",
  "updateRule": "",
  "deleteRule": "",
  "name": "ibkr_backtest_daily_selection_cache",
  "type": "base",
  "system": false,
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
      "autogeneratePattern": "",
      "hidden": false,
      "id": "btdsc_cache_key",
      "max": 80,
      "min": 0,
      "name": "cache_key",
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
      "id": "btdsc_market_date",
      "max": 10,
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
      "hidden": false,
      "id": "btdsc_source_env",
      "maxSelect": 1,
      "name": "source_environment",
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
      "id": "btdsc_algorithm_version",
      "max": 80,
      "min": 0,
      "name": "algorithm_version",
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
      "id": "btdsc_request_hash",
      "max": 80,
      "min": 0,
      "name": "request_hash",
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
      "id": "btdsc_universe_hash",
      "max": 80,
      "min": 0,
      "name": "universe_hash",
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
      "id": "btdsc_input_data_hash",
      "max": 80,
      "min": 0,
      "name": "input_data_hash",
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
      "id": "btdsc_scan_settings_hash",
      "max": 80,
      "min": 0,
      "name": "scan_settings_hash",
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
      "id": "btdsc_strategy_hash",
      "max": 80,
      "min": 0,
      "name": "strategy_hash",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "btdsc_status",
      "maxSelect": 1,
      "name": "status",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": [
        "valid",
        "stale",
        "failed",
        "partial"
      ]
    },
    {
      "hidden": false,
      "id": "btdsc_selected_count",
      "max": null,
      "min": null,
      "name": "selected_count",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "btdsc_target_rows",
      "maxSize": 2097152,
      "name": "target_rows",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "btdsc_selection_plan",
      "maxSize": 2097152,
      "name": "selection_plan",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "btdsc_daily_summary",
      "maxSize": 2097152,
      "name": "daily_summary",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "btdsc_fingerprint_extra",
      "maxSize": 2097152,
      "name": "fingerprint_extra",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "btdsc_error",
      "max": 1000,
      "min": 0,
      "name": "error",
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
      "id": "btdsc_last_built_at",
      "max": 32,
      "min": 0,
      "name": "last_built_at",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "autodate2990389176",
      "name": "created",
      "onCreate": true,
      "onUpdate": false,
      "presentable": false,
      "system": false,
      "type": "autodate"
    },
    {
      "hidden": false,
      "id": "autodate3332085495",
      "name": "updated",
      "onCreate": true,
      "onUpdate": true,
      "presentable": false,
      "system": false,
      "type": "autodate"
    }
  ],
  "indexes": [
    "CREATE UNIQUE INDEX idx_ibkr_btdsc_cache_key_date ON ibkr_backtest_daily_selection_cache (cache_key, market_date)",
    "CREATE INDEX idx_ibkr_btdsc_cache_key_date_status ON ibkr_backtest_daily_selection_cache (cache_key, market_date, status)",
    "CREATE INDEX idx_ibkr_btdsc_source_env_date ON ibkr_backtest_daily_selection_cache (source_environment, market_date)",
    "CREATE INDEX idx_ibkr_btdsc_updated ON ibkr_backtest_daily_selection_cache (updated)"
  ]
}

function findIbkrBacktestDailySelectionCacheCollection(app) {
  try {
    return app.findCollectionByNameOrId("_pb_ibkr_bt_daily_sel_cache_")
  } catch (e) {}
  try {
    return app.findCollectionByNameOrId("ibkr_backtest_daily_selection_cache")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findIbkrBacktestDailySelectionCacheCollection(app)

  if (collection) {
    unmarshal(IBKR_BACKTEST_DAILY_SELECTION_CACHE_SCHEMA, collection)
  } else {
    collection = new Collection(IBKR_BACKTEST_DAILY_SELECTION_CACHE_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrBacktestDailySelectionCacheCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
