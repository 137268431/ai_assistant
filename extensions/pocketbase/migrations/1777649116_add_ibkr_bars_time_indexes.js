/// <reference path="../pb_data/types.d.ts" />

const IBKR_BARS_COLLECTION = "ibkr_bars"

const IBKR_BARS_TIME_INDEXES = [
  "CREATE INDEX IF NOT EXISTS idx_ibkr_bars_env_interval_bar_time_ms ON ibkr_bars (environment, interval, bar_time_ms)",
  "CREATE INDEX IF NOT EXISTS idx_ibkr_bars_env_interval_us_time ON ibkr_bars (environment, interval, us_time)",
  "CREATE INDEX IF NOT EXISTS idx_ibkr_bars_env_created ON ibkr_bars (environment, created)",
]

const IBKR_BARS_TIME_INDEX_NAMES = [
  "idx_ibkr_bars_env_interval_bar_time_ms",
  "idx_ibkr_bars_env_interval_us_time",
  "idx_ibkr_bars_env_created",
]

function findIbkrBarsCollection(app) {
  try {
    return app.findCollectionByNameOrId(IBKR_BARS_COLLECTION)
  } catch (e) {}
  return null
}

function hasIndex(indexSql, indexName) {
  return String(indexSql || "").indexOf(indexName) >= 0
}

function addIndexes(collection) {
  const indexes = Array.isArray(collection.indexes) ? collection.indexes.slice() : []

  for (let i = 0; i < IBKR_BARS_TIME_INDEXES.length; i++) {
    const indexSql = IBKR_BARS_TIME_INDEXES[i]
    const indexName = IBKR_BARS_TIME_INDEX_NAMES[i]
    if (!indexes.some((existing) => hasIndex(existing, indexName))) {
      indexes.push(indexSql)
    }
  }

  collection.indexes = indexes
}

function removeIndexes(collection) {
  const indexes = Array.isArray(collection.indexes) ? collection.indexes.slice() : []
  collection.indexes = indexes.filter((indexSql) => {
    return !IBKR_BARS_TIME_INDEX_NAMES.some((indexName) => hasIndex(indexSql, indexName))
  })
}

migrate((app) => {
  const collection = findIbkrBarsCollection(app)
  if (!collection) {
    throw new Error("ibkr_bars collection not found")
  }

  addIndexes(collection)
  return app.save(collection)
}, (app) => {
  const collection = findIbkrBarsCollection(app)
  if (!collection) {
    return null
  }

  removeIndexes(collection)
  return app.save(collection)
})
