/// <reference path="../pb_data/types.d.ts" />

function watchlistFields(includeManualMember) {
  const fields = [
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
      "id": "wl_ticker",
      "max": 20,
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
      "autogeneratePattern": "",
      "hidden": false,
      "id": "wl_exchange",
      "max": 50,
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
      "id": "wl_industry",
      "max": 200,
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
      "id": "wl_created_us",
      "max": 50,
      "min": 0,
      "name": "created_us",
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
      "id": "wl_created_cn",
      "max": 50,
      "min": 0,
      "name": "created_cn",
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
      "id": "wl_updated_us",
      "max": 50,
      "min": 0,
      "name": "updated_us",
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
      "id": "wl_updated_cn",
      "max": 50,
      "min": 0,
      "name": "updated_cn",
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
      "id": "sig_note",
      "max": 500,
      "min": 0,
      "name": "note",
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
      "id": "sig_us_time",
      "max": 50,
      "min": 0,
      "name": "us_time",
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
      "id": "sig_cn_time",
      "max": 50,
      "min": 0,
      "name": "cn_time",
      "pattern": "",
      "presentable": false,
      "primaryKey": false,
      "required": false,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "sig_bar_time_ms",
      "max": null,
      "min": null,
      "name": "bar_time_ms",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "wl_environment",
      "maxSelect": 1,
      "name": "environment",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": [
        "global",
        "live",
        "paper",
        "backtest"
      ]
    },
    {
      "hidden": false,
      "id": "wl_symbol_role",
      "maxSelect": 1,
      "name": "symbol_role",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": [
        "trade",
        "market_monitor"
      ]
    }
  ]

  if (includeManualMember) {
    fields.push({
      "hidden": false,
      "id": "wl_manual_member",
      "name": "manual_member",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "bool"
    })
  }

  fields.push(
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
  )

  return fields
}

function buildWatchlistSchema(includeManualMember) {
  return {
    "id": "_pb_watchlist_",
    "name": "watchlist",
    "type": "base",
    "system": false,
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
    "fields": watchlistFields(includeManualMember),
    "indexes": [
      "CREATE UNIQUE INDEX idx_watchlist_symbol_environment ON watchlist (symbol, environment)",
      "CREATE INDEX idx_watchlist_environment ON watchlist (environment)",
      "CREATE INDEX idx_watchlist_environment_role ON watchlist (environment, symbol_role)"
    ],
    "options": {}
  }
}

const WATCHLIST_SCHEMA_WITH_MANUAL_MEMBER = buildWatchlistSchema(true)
const WATCHLIST_SCHEMA_WITHOUT_MANUAL_MEMBER = buildWatchlistSchema(false)

function findCollection(app, nameOrId) {
  try {
    return app.findCollectionByNameOrId(nameOrId)
  } catch (_) {}
  return null
}

migrate((app) => {
  let watchlistCollection = findCollection(app, "_pb_watchlist_") || findCollection(app, "watchlist")
  if (watchlistCollection) {
    unmarshal(WATCHLIST_SCHEMA_WITH_MANUAL_MEMBER, watchlistCollection)
  } else {
    watchlistCollection = new Collection(WATCHLIST_SCHEMA_WITH_MANUAL_MEMBER)
  }
  app.save(watchlistCollection)

  const rows = app.findRecordsByFilter("watchlist", "symbol != \"\"", "", 10000, 0) || []
  rows.forEach((record) => {
    record.set("manual_member", true)
    app.save(record)
  })

  return null
}, (app) => {
  const watchlistCollection = findCollection(app, "_pb_watchlist_") || findCollection(app, "watchlist")
  if (!watchlistCollection) {
    return null
  }
  unmarshal(WATCHLIST_SCHEMA_WITHOUT_MANUAL_MEMBER, watchlistCollection)
  app.save(watchlistCollection)
  return null
})
