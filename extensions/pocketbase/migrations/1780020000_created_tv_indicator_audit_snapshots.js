/// <reference path="../pb_data/types.d.ts" />

const TV_INDICATOR_AUDIT_SNAPSHOTS_SCHEMA = {
  "id": "_pb_tv_indicator_audit_snapshots_",
  "listRule": "",
  "viewRule": "",
  "createRule": "",
  "updateRule": "",
  "deleteRule": "",
  "name": "tv_indicator_audit_snapshots",
  "type": "base",
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
      "id": "tias_symbol",
      "max": 0,
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
      "id": "tias_exchange",
      "max": 0,
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
      "id": "tias_interval",
      "max": 0,
      "min": 0,
      "name": "interval",
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
      "id": "tias_scripttag",
      "max": 0,
      "min": 0,
      "name": "script_tag",
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
      "id": "tias_us_time",
      "max": 0,
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
      "id": "tias_cn_time",
      "max": 0,
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
      "id": "tias_bartimems",
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
      "id": "tias_barindex",
      "max": null,
      "min": null,
      "name": "bar_index",
      "onlyInt": true,
      "presentable": false,
      "required": false,
      "system": false,
      "type": "number"
    },
    {
      "hidden": false,
      "id": "tias_extra",
      "maxSize": 2097152,
      "name": "extra",
      "presentable": false,
      "required": false,
      "system": false,
      "type": "json"
    },
    {
      "hidden": false,
      "id": "tias_environment",
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
      "hidden": false,
      "id": "autodate_tias_created",
      "name": "created",
      "onCreate": true,
      "onUpdate": false,
      "presentable": false,
      "system": false,
      "type": "autodate"
    },
    {
      "hidden": false,
      "id": "autodate_tias_updated",
      "name": "updated",
      "onCreate": true,
      "onUpdate": true,
      "presentable": false,
      "system": false,
      "type": "autodate"
    }
  ],
  "indexes": [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tv_indicator_audit_snapshots_sym_tf_ms_env_script ON tv_indicator_audit_snapshots (symbol, interval, bar_time_ms, environment, script_tag)",
    "CREATE INDEX IF NOT EXISTS idx_tv_indicator_audit_snapshots_symbol ON tv_indicator_audit_snapshots (symbol)",
    "CREATE INDEX IF NOT EXISTS idx_tv_indicator_audit_snapshots_bar_time_ms ON tv_indicator_audit_snapshots (bar_time_ms)",
    "CREATE INDEX IF NOT EXISTS idx_tv_indicator_audit_snapshots_created ON tv_indicator_audit_snapshots (created)",
    "CREATE INDEX IF NOT EXISTS idx_tv_indicator_audit_snapshots_symbol_created ON tv_indicator_audit_snapshots (symbol, created)",
    "CREATE INDEX IF NOT EXISTS idx_tv_indicator_audit_snapshots_environment ON tv_indicator_audit_snapshots (environment)"
  ],
  "system": false
}

function findTvIndicatorAuditSnapshotsCollection(app) {
  try {
    return app.findCollectionByNameOrId("tv_indicator_audit_snapshots")
  } catch (e) {}
  return null
}

migrate((app) => {
  let collection = findTvIndicatorAuditSnapshotsCollection(app)

  if (collection) {
    unmarshal(TV_INDICATOR_AUDIT_SNAPSHOTS_SCHEMA, collection)
  } else {
    collection = new Collection(TV_INDICATOR_AUDIT_SNAPSHOTS_SCHEMA)
  }

  return app.save(collection)
}, (app) => {
  const collection = findTvIndicatorAuditSnapshotsCollection(app)
  if (!collection) {
    return null
  }

  return app.delete(collection)
})
