/// <reference path="../pb_data/types.d.ts" />

const INDEXES_BY_COLLECTION = {
  ibkr_indicators: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_indicators_env_interval_bar_time_ms_symbol ON ibkr_indicators (environment, interval, bar_time_ms DESC, symbol)",
  ],
  ibkr_backtest_runs: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_backtest_runs_source_env_created ON ibkr_backtest_runs (source_environment, created DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_backtest_runs_source_env_updated ON ibkr_backtest_runs (source_environment, updated DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_backtest_runs_env_updated ON ibkr_backtest_runs (environment, updated DESC)",
  ],
  ibkr_backtest_batches: [
    "CREATE INDEX IF NOT EXISTS idx_ibkr_backtest_batches_source_env_created ON ibkr_backtest_batches (source_environment, created DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ibkr_backtest_batches_env_updated ON ibkr_backtest_batches (environment, updated DESC)",
  ],
}

const INDEX_NAMES_BY_COLLECTION = {
  ibkr_indicators: [
    "idx_ibkr_indicators_env_interval_bar_time_ms_symbol",
  ],
  ibkr_backtest_runs: [
    "idx_ibkr_backtest_runs_source_env_created",
    "idx_ibkr_backtest_runs_source_env_updated",
    "idx_ibkr_backtest_runs_env_updated",
  ],
  ibkr_backtest_batches: [
    "idx_ibkr_backtest_batches_source_env_created",
    "idx_ibkr_backtest_batches_env_updated",
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
