/// <reference path="../pb_data/types.d.ts" />

const INDEXES_BY_COLLECTION = {
  ibkr_targets: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_targets_env_date_status_updated ON ibkr_targets (environment, date, status, updated DESC)",
  ],
  orders: [
    "CREATE INDEX IF NOT EXISTS idx_orders_env_bar_time_created ON orders (environment, bar_time_ms DESC, created DESC)",
    "CREATE INDEX IF NOT EXISTS idx_orders_env_role_unique_id ON orders (environment, role, unique_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_env_role_trade_group_id ON orders (environment, role, trade_group_id)",
    "CREATE INDEX IF NOT EXISTS idx_orders_env_role_signal_id ON orders (environment, role, signal_id)",
  ],
  ibkr_order_details: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_order_details_env_symbol_time_created ON ibkr_order_details (environment, symbol, bar_time_ms, created)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_order_details_env_signal_time_created ON ibkr_order_details (environment, signal_id, bar_time_ms, created)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_order_details_env_trade_group_time_created ON ibkr_order_details (environment, trade_group_id, bar_time_ms, created)",
  ],
  ibkr_reverse_signals: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_reverse_signals_env_time_source ON ibkr_reverse_signals (environment, bar_time_ms, source)",
  ],
  ibkr_execution_fills: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_execution_fills_env_trade_time_created ON ibkr_execution_fills (environment, trade_time_ms, created)",
  ],
}

const INDEX_NAMES_BY_COLLECTION = {
  ibkr_targets: [
    "idx_ibkr_targets_env_date_status_updated",
  ],
  orders: [
    "idx_orders_env_bar_time_created",
    "idx_orders_env_role_unique_id",
    "idx_orders_env_role_trade_group_id",
    "idx_orders_env_role_signal_id",
  ],
  ibkr_order_details: [
    "idx_ibkr_order_details_env_symbol_time_created",
    "idx_ibkr_order_details_env_signal_time_created",
    "idx_ibkr_order_details_env_trade_group_time_created",
  ],
  ibkr_reverse_signals: [
    "idx_ibkr_reverse_signals_env_time_source",
  ],
  ibkr_execution_fills: [
    "idx_ibkr_execution_fills_env_trade_time_created",
  ],
}

function findCollection(app, name) {
  try {
    return app.findCollectionByNameOrId(name)
  } catch (e) {}
  return null
}

function hasIndex(indexSql, indexName) {
  return String(indexSql || "").indexOf(indexName) >= 0
}

function addIndexes(collection, indexSqlList, indexNames) {
  const indexes = Array.isArray(collection.indexes) ? collection.indexes.slice() : []
  for (let i = 0; i < indexSqlList.length; i++) {
    const indexSql = indexSqlList[i]
    const indexName = indexNames[i]
    if (!indexes.some((existing) => hasIndex(existing, indexName))) {
      indexes.push(indexSql)
    }
  }
  collection.indexes = indexes
}

function removeIndexes(collection, indexNames) {
  const indexes = Array.isArray(collection.indexes) ? collection.indexes.slice() : []
  collection.indexes = indexes.filter((indexSql) => {
    return !indexNames.some((indexName) => hasIndex(indexSql, indexName))
  })
}

migrate((app) => {
  Object.keys(INDEXES_BY_COLLECTION).forEach((collectionName) => {
    const collection = findCollection(app, collectionName)
    if (!collection) {
      throw new Error(`${collectionName} collection not found`)
    }
    addIndexes(collection, INDEXES_BY_COLLECTION[collectionName], INDEX_NAMES_BY_COLLECTION[collectionName])
    app.save(collection)
  })
}, (app) => {
  Object.keys(INDEX_NAMES_BY_COLLECTION).forEach((collectionName) => {
    const collection = findCollection(app, collectionName)
    if (!collection) return
    removeIndexes(collection, INDEX_NAMES_BY_COLLECTION[collectionName])
    app.save(collection)
  })
})
