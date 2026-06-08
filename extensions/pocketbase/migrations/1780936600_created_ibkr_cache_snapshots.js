/// <reference path="../pb_data/types.d.ts" />

const IBKR_CACHE_SNAPSHOTS_SCHEMA = {
  "id": "_pb_ibkr_cache_snapshots_",
  "listRule": "",
  "viewRule": "",
  "createRule": "",
  "updateRule": "",
  "deleteRule": "",
  "name": "ibkr_cache_snapshots",
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
      "id": "ics_cache_key",
      "max": 300,
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
      "id": "ics_scope",
      "max": 120,
      "min": 0,
      "name": "scope",
      "pattern": "",
      "presentable": true,
      "primaryKey": false,
      "required": true,
      "system": false,
      "type": "text"
    },
    {
      "hidden": false,
      "id": "ics_environment",
      "maxSelect": 1,
      "name": "environment",
      "presentable": true,
      "required": true,
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
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ics_market_date",
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
      "hidden": false,
      "id": "ics_payload",
      "maxSize": 8388608,
      "name": "payload",
      "presentable": false,
      "required": true,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "ics_computed_at_ms",
      "max": null,
      "min": null,
      "name": "computed_at_ms",
      "onlyInt": true,
      "presentable": false,
      "required": true,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ics_fresh_until_ms",
      "max": null,
      "min": null,
      "name": "fresh_until_ms",
      "onlyInt": true,
      "presentable": false,
      "required": true,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ics_stale_until_ms",
      "max": null,
      "min": null,
      "name": "stale_until_ms",
      "onlyInt": true,
      "presentable": false,
      "required": true,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "ics_status",
      "maxSelect": 1,
      "name": "status",
      "presentable": true,
      "required": true,
      "system": false,
      "type": "select",
      "values": [
        "fresh",
        "stale",
        "expired",
        "error"
      ]
    },
    {
      "autogeneratePattern": "",
      "hidden": false,
      "id": "ics_error",
      "max": 2000,
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
      "hidden": false,
      "id": "autodate_ics_created",
      "name": "created",
      "onCreate": true,
      "onUpdate": false,
      "presentable": false,
      "system": false,
      "type": "autodate"
    },
    {
      "hidden": false,
      "id": "autodate_ics_updated",
      "name": "updated",
      "onCreate": true,
      "onUpdate": true,
      "presentable": false,
      "system": false,
      "type": "autodate"
    }
  ],
  "indexes": [
    "CREATE UNIQUE INDEX idx_ibkr_cache_snapshots_cache_key ON ibkr_cache_snapshots (cache_key)",
    "CREATE INDEX idx_ibkr_cache_snapshots_scope_env_date ON ibkr_cache_snapshots (scope, environment, market_date)",
    "CREATE INDEX idx_ibkr_cache_snapshots_stale_until_ms ON ibkr_cache_snapshots (stale_until_ms)"
  ]
}

function findIbkrCacheSnapshotsCollection(app) {
  try {
    return app.findCollectionByNameOrId("_pb_ibkr_cache_snapshots_")
  } catch (e) {}
  try {
    return app.findCollectionByNameOrId("ibkr_cache_snapshots")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findIbkrCacheSnapshotsCollection(app)

  if (collection) {
    unmarshal(IBKR_CACHE_SNAPSHOTS_SCHEMA, collection)
  } else {
    collection = new Collection(IBKR_CACHE_SNAPSHOTS_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrCacheSnapshotsCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
