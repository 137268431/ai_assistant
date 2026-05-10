/// <reference path="../pb_data/types.d.ts" />

const IBKR_FUNDAMENTALS_SCHEMA = {
  "id": "_pb_ibkr_fundamentals_",
  "name": "ibkr_fundamentals",
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
      "id": "if_symbol",
      "max": 24,
      "min": 1,
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
      "id": "if_provider",
      "maxSelect": 1,
      "name": "provider",
      "presentable": true,
      "required": true,
      "system": false,
      "type": "select",
      "values": ["finnhub"]
    },
    {
      "hidden": false,
      "id": "if_status",
      "maxSelect": 1,
      "name": "status",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": ["fresh", "stale", "failed"]
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "if_profile",
      "max": 40,
      "min": 0,
      "name": "profile",
      "pattern": "",
      "presentable": true,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "if_company_name",
      "max": 200,
      "min": 0,
      "name": "company_name",
      "pattern": "",
      "presentable": true,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "if_exchange",
      "max": 80,
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
      "id": "if_industry",
      "max": 160,
      "min": 0,
      "name": "industry",
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
      "id": "if_currency",
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
      "id": "if_ipo",
      "max": 20,
      "min": 0,
      "name": "ipo",
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
      "id": "if_web_url",
      "max": 500,
      "min": 0,
      "name": "web_url",
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
      "id": "if_logo_url",
      "max": 500,
      "min": 0,
      "name": "logo_url",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "if_market_cap_usd",
      "max": null,
      "min": null,
      "name": "market_cap_usd",
      "onlyInt": false,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "if_market_cap_mil",
      "max": null,
      "min": null,
      "name": "market_cap_millions",
      "onlyInt": false,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "if_share_out_mil",
      "max": null,
      "min": null,
      "name": "share_outstanding_millions",
      "onlyInt": false,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "if_raw_profile",
      "maxSize": 2097152,
      "name": "raw_profile",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "if_error",
      "max": 500,
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
      "id": "if_fetched_at",
      "max": 40,
      "min": 0,
      "name": "fetched_at",
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
      "id": "if_expires_at",
      "max": 40,
      "min": 0,
      "name": "expires_at",
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
    "CREATE UNIQUE INDEX idx_ibkr_fundamentals_symbol_provider ON ibkr_fundamentals (symbol, provider)",
    "CREATE INDEX idx_ibkr_fundamentals_provider_status ON ibkr_fundamentals (provider, status)",
    "CREATE INDEX idx_ibkr_fundamentals_profile ON ibkr_fundamentals (profile)",
    "CREATE INDEX idx_ibkr_fundamentals_updated ON ibkr_fundamentals (updated)"
  ]
}

function findIbkrFundamentalsCollection(app) {
  try {
    return app.findCollectionByNameOrId("_pb_ibkr_fundamentals_")
  } catch (e) {}
  try {
    return app.findCollectionByNameOrId("ibkr_fundamentals")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findIbkrFundamentalsCollection(app)

  if (collection) {
    unmarshal(IBKR_FUNDAMENTALS_SCHEMA, collection)
  } else {
    collection = new Collection(IBKR_FUNDAMENTALS_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrFundamentalsCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
