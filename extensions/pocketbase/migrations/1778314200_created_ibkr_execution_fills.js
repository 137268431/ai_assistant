/// <reference path="../pb_data/types.d.ts" />

const IBKR_EXECUTION_FILLS_SCHEMA = {
  "id": "_pb_ibkr_execution_fills_",
  "name": "ibkr_execution_fills",
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
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ief_exec_id",
      "max": 120,
      "min": 0,
      "name": "exec_id",
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
      "id": "ief_order_id",
      "max": 80,
      "min": 0,
      "name": "order_id",
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
      "id": "ief_symbol",
      "max": 24,
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
      "hidden": false,
      "id": "ief_side",
      "maxSelect": 1,
      "name": "side",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": ["buy", "sell"]
    },
    {
      "hidden": false,
      "id": "ief_shares",
      "max": null,
      "min": null,
      "name": "shares",
      "onlyInt": false,
      "presentable": true,
      "required": true,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ief_price",
      "max": null,
      "min": null,
      "name": "price",
      "onlyInt": false,
      "presentable": true,
      "required": true,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ief_trade_value",
      "max": null,
      "min": null,
      "name": "trade_value",
      "onlyInt": false,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ief_commission",
      "max": null,
      "min": null,
      "name": "commission",
      "onlyInt": false,
      "presentable": true,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ief_commission_currency",
      "max": 12,
      "min": 0,
      "name": "commission_currency",
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
      "id": "ief_currency",
      "max": 12,
      "min": 0,
      "name": "currency",
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
      "id": "ief_trade_time",
      "max": 80,
      "min": 0,
      "name": "trade_time",
      "pattern": "",
      "presentable": true,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "ief_trade_time_ms",
      "max": null,
      "min": null,
      "name": "trade_time_ms",
      "onlyInt": true,
      "presentable": true,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ief_account",
      "max": 64,
      "min": 0,
      "name": "account",
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
      "id": "ief_source",
      "max": 40,
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
      "id": "ief_environment",
      "maxSelect": 1,
      "name": "environment",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "select",
      "values": ["live", "paper", "backtest"]
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ief_asset_category",
      "max": 40,
      "min": 0,
      "name": "asset_category",
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
      "id": "ief_exchange",
      "max": 40,
      "min": 0,
      "name": "exchange",
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
      "id": "ief_order_type",
      "max": 40,
      "min": 0,
      "name": "order_type",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "ief_reference_price",
      "max": null,
      "min": null,
      "name": "reference_price",
      "onlyInt": false,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ief_slippage_bps",
      "max": null,
      "min": null,
      "name": "slippage_bps",
      "onlyInt": false,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ief_raw",
      "maxSize": 2097152,
      "name": "raw",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
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
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ibkr_execution_fills_exec ON ibkr_execution_fills (environment, account, exec_id)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_execution_fills_symbol_time ON ibkr_execution_fills (environment, symbol, trade_time_ms)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_execution_fills_account_time ON ibkr_execution_fills (environment, account, trade_time_ms)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_execution_fills_source_time ON ibkr_execution_fills (environment, source, trade_time_ms)"
  ]
}

function findIbkrExecutionFillsCollection(app) {
  try {
    return app.findCollectionByNameOrId("_pb_ibkr_execution_fills_")
  } catch (e) {}
  try {
    return app.findCollectionByNameOrId("ibkr_execution_fills")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findIbkrExecutionFillsCollection(app)

  if (collection) {
    unmarshal(IBKR_EXECUTION_FILLS_SCHEMA, collection)
  } else {
    collection = new Collection(IBKR_EXECUTION_FILLS_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrExecutionFillsCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
