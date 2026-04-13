/// <reference path="../pb_data/types.d.ts" />

const WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
const WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR = "market_monitor"
const DEFAULT_MARKET_MONITOR_SYMBOLS = ["SPY", "QQQ", "VIX"]

function watchlistFields(includeSymbolRole) {
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
      "values": ["global", "live", "paper", "backtest"]
    },
  ]
  if (includeSymbolRole) {
    fields.push({
      "hidden": false,
      "id": "wl_symbol_role",
      "maxSelect": 1,
      "name": "symbol_role",
      "presentable": true,
      "required": false,
      "system": false,
      "type": "select",
      "values": ["trade", "market_monitor"]
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

function buildWatchlistSchema(includeSymbolRole) {
  const indexes = [
    "CREATE UNIQUE INDEX idx_watchlist_symbol_environment ON watchlist (symbol, environment)",
    "CREATE INDEX idx_watchlist_environment ON watchlist (environment)",
  ]
  if (includeSymbolRole) {
    indexes.push("CREATE INDEX idx_watchlist_environment_role ON watchlist (environment, symbol_role)")
  }
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
    "fields": watchlistFields(includeSymbolRole),
    "indexes": indexes,
    "options": {},
  }
}

const WATCHLIST_SCHEMA_WITH_ROLE = buildWatchlistSchema(true)
const WATCHLIST_SCHEMA_WITHOUT_ROLE = buildWatchlistSchema(false)

function findCollection(app, nameOrId) {
  try {
    return app.findCollectionByNameOrId(nameOrId)
  } catch (e) {}
  return null
}

function parseSymbolList(rawValue) {
  const seen = {}
  return String(rawValue || "")
    .split(/[,\s;，；]+/)
    .map((item) => String(item || "").trim().toUpperCase())
    .filter((item) => {
      if (!item || seen[item]) return false
      seen[item] = true
      return true
    })
}

function normalizeWatchlistRole(value) {
  return String(value || "").trim().toLowerCase() === WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    ? WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    : WATCHLIST_SYMBOL_ROLE_TRADE
}

function defaultSymbolMeta(symbol) {
  const normalized = String(symbol || "").trim().toUpperCase()
  if (normalized === "SPY") return { exchange: "ARCA", industry: "ETF" }
  if (normalized === "QQQ") return { exchange: "NASDAQ", industry: "ETF" }
  if (normalized === "VIX") return { exchange: "CBOE", industry: "INDEX" }
  return { exchange: "SMART", industry: "INDEX" }
}

function loadAllRecords(app, collectionName, filter) {
  return app.findRecordsByFilter(collectionName, filter || "id != \"\"", "", 10000, 0, {}) || []
}

function upsertWatchlistRoleRecord(app, watchlistCollection, existingByKey, symbol, environment, role) {
  const normalizedSymbol = String(symbol || "").trim().toUpperCase()
  const normalizedEnvironment = String(environment || "global").trim().toLowerCase() || "global"
  if (!normalizedSymbol) return
  const key = `${normalizedEnvironment}::${normalizedSymbol}`
  let record = existingByKey[key] || null
  const fallbackMeta = defaultSymbolMeta(normalizedSymbol)
  if (!record) {
    record = new Record(watchlistCollection, {})
    record.set("symbol", normalizedSymbol)
    record.set("environment", normalizedEnvironment)
    record.set("exchange", fallbackMeta.exchange)
    record.set("industry", fallbackMeta.industry)
    record.set("note", "migrated_market_monitor")
  } else {
    if (!String(record.get("exchange") || "").trim()) {
      record.set("exchange", fallbackMeta.exchange)
    }
    if (!String(record.get("industry") || "").trim()) {
      record.set("industry", fallbackMeta.industry)
    }
  }
  record.set("symbol_role", normalizeWatchlistRole(role))
  app.save(record)
  existingByKey[key] = record
}

function deleteMarketIndexConfigRecords(app) {
  const rows = app.findRecordsByFilter(
    "config",
    "key = {:key}",
    "",
    1000,
    0,
    { key: "market_index_symbols" }
  ) || []
  rows.forEach((record) => {
    try {
      app.delete(record)
    } catch (e) {}
  })
  return rows
}

function ensureMarketIndexConfig(app) {
  const configCollection = findCollection(app, "config")
  if (!configCollection) return
  let record = null
  try {
    record = app.findFirstRecordByFilter(
      "config",
      "key = {:key} && environment = {:env}",
      { key: "market_index_symbols", env: "global" }
    )
  } catch (e) {}
  if (!record) {
    record = new Record(configCollection, {})
  }
  record.set("key", "market_index_symbols")
  record.set("value", DEFAULT_MARKET_MONITOR_SYMBOLS.join(","))
  record.set("default_value", DEFAULT_MARKET_MONITOR_SYMBOLS.join(","))
  record.set("display_name", "大盘指数标的")
  record.set("group_name", "市场分析")
  record.set("sort_order", 1100)
  record.set("description", "用于监控和关联分析的大盘指数代码，逗号分隔")
  record.set("environment", "global")
  app.save(record)
}

migrate((app) => {
  let watchlistCollection = findCollection(app, "_pb_watchlist_") || findCollection(app, "watchlist")
  if (watchlistCollection) {
    unmarshal(WATCHLIST_SCHEMA_WITH_ROLE, watchlistCollection)
  } else {
    watchlistCollection = new Collection(WATCHLIST_SCHEMA_WITH_ROLE)
  }
  app.save(watchlistCollection)

  const watchlistRows = loadAllRecords(app, "watchlist", "symbol != \"\"")
  const existingByKey = {}
  watchlistRows.forEach((record) => {
    const symbol = String(record.get("symbol") || "").trim().toUpperCase()
    const rawEnvironment = String(record.get("environment") || "").trim().toLowerCase()
    const environment = rawEnvironment || "live"
    if (!symbol) return
    if (rawEnvironment !== environment) {
      record.set("environment", environment)
    }
    record.set("symbol_role", WATCHLIST_SYMBOL_ROLE_TRADE)
    app.save(record)
    existingByKey[`${environment}::${symbol}`] = record
  })

  const configRows = app.findRecordsByFilter(
    "config",
    "key = {:key}",
    "",
    1000,
    0,
    { key: "market_index_symbols" }
  ) || []
  const monitorTargets = []
  configRows.forEach((record) => {
    const environment = String(record.get("environment") || "global").trim().toLowerCase() || "global"
    const rawValue = record.get("value") || record.get("default_value") || ""
    parseSymbolList(rawValue).forEach((symbol) => {
      monitorTargets.push({ symbol, environment })
    })
  })
  if (!monitorTargets.length) {
    DEFAULT_MARKET_MONITOR_SYMBOLS.forEach((symbol) => {
      monitorTargets.push({ symbol, environment: "global" })
    })
  }
  monitorTargets.forEach((item) => {
    upsertWatchlistRoleRecord(
      app,
      watchlistCollection,
      existingByKey,
      item.symbol,
      item.environment,
      WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR
    )
  })

  deleteMarketIndexConfigRecords(app)
  return null
}, (app) => {
  const watchlistCollection = findCollection(app, "_pb_watchlist_") || findCollection(app, "watchlist")
  if (watchlistCollection) {
    const monitorRows = app.findRecordsByFilter(
      "watchlist",
      "symbol_role = {:role}",
      "",
      10000,
      0,
      { role: WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR }
    ) || []
    monitorRows.forEach((record) => {
      record.set("symbol_role", WATCHLIST_SYMBOL_ROLE_TRADE)
      app.save(record)
    })
    unmarshal(WATCHLIST_SCHEMA_WITHOUT_ROLE, watchlistCollection)
    app.save(watchlistCollection)
  }

  ensureMarketIndexConfig(app)
  return null
})
