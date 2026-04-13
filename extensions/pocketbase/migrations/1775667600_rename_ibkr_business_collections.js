/// <reference path="../pb_data/types.d.ts" />

const ORDER_DETAILS_ID = "_pb_order_details_"
const ORDER_DETAILS_OLD_NAME = "order_details"
const ORDER_DETAILS_NEW_NAME = "ibkr_order_details"
const REVERSE_SIGNALS_ID = "_pb_reverse_signals_"
const REVERSE_SIGNALS_OLD_NAME = "reverse_signals"
const REVERSE_SIGNALS_NEW_NAME = "ibkr_reverse_signals"

const ORDER_DETAILS_INDEXES_NEW = [
  "CREATE INDEX idx_ibkr_order_details_order_id ON ibkr_order_details (order_id)",
  "CREATE INDEX idx_ibkr_order_details_symbol ON ibkr_order_details (symbol)",
  "CREATE INDEX idx_ibkr_order_details_order_type ON ibkr_order_details (order_type)",
  "CREATE INDEX idx_ibkr_order_details_trade_group_id ON ibkr_order_details (trade_group_id)",
  "CREATE INDEX idx_ibkr_order_details_entry_order_unique_id ON ibkr_order_details (entry_order_unique_id)",
  "CREATE INDEX idx_ibkr_order_details_environment ON ibkr_order_details (environment)",
  "CREATE INDEX idx_ibkr_order_details_signal_environment ON ibkr_order_details (signal_id, environment)",
]

const ORDER_DETAILS_INDEXES_OLD = [
  "CREATE INDEX idx_order_details_order_id ON order_details (order_id)",
  "CREATE INDEX idx_order_details_symbol ON order_details (symbol)",
  "CREATE INDEX idx_order_details_order_type ON order_details (order_type)",
  "CREATE INDEX idx_order_details_trade_group_id ON order_details (trade_group_id)",
  "CREATE INDEX idx_order_details_entry_order_unique_id ON order_details (entry_order_unique_id)",
  "CREATE INDEX idx_order_details_environment ON order_details (environment)",
  "CREATE INDEX idx_order_details_signal_environment ON order_details (signal_id, environment)",
]

const REVERSE_SIGNALS_INDEXES_NEW = [
  "CREATE INDEX idx_ibkr_reverse_signals_symbol ON ibkr_reverse_signals (symbol)",
  "CREATE INDEX idx_ibkr_reverse_signals_status ON ibkr_reverse_signals (status)",
  "CREATE INDEX idx_ibkr_reverse_signals_bar_time_ms ON ibkr_reverse_signals (bar_time_ms)",
  "CREATE INDEX idx_ibkr_reverse_signals_priority ON ibkr_reverse_signals (priority)",
  "CREATE INDEX idx_ibkr_reverse_signals_source ON ibkr_reverse_signals (source)",
  "CREATE INDEX idx_ibkr_reverse_signals_environment ON ibkr_reverse_signals (environment)",
]

const REVERSE_SIGNALS_INDEXES_OLD = [
  "CREATE INDEX idx_reverse_signals_symbol ON reverse_signals (symbol)",
  "CREATE INDEX idx_reverse_signals_status ON reverse_signals (status)",
  "CREATE INDEX idx_reverse_signals_bar_time_ms ON reverse_signals (bar_time_ms)",
  "CREATE INDEX idx_reverse_signals_priority ON reverse_signals (priority)",
  "CREATE INDEX idx_reverse_signals_source ON reverse_signals (source)",
  "CREATE INDEX idx_reverse_signals_environment ON reverse_signals (environment)",
]

function findCollection(app, candidates) {
  for (let i = 0; i < candidates.length; i++) {
    try {
      const collection = app.findCollectionByNameOrId(candidates[i])
      if (collection) {
        return collection
      }
    } catch (e) {}
  }
  return null
}

function renameCollection(app, options) {
  const collection = findCollection(app, options.findBy)
  if (!collection) {
    return null
  }

  collection.name = options.name
  collection.indexes = options.indexes.slice()
  return app.save(collection)
}

migrate((app) => {
  renameCollection(app, {
    findBy: [ORDER_DETAILS_ID, ORDER_DETAILS_NEW_NAME, ORDER_DETAILS_OLD_NAME],
    name: ORDER_DETAILS_NEW_NAME,
    indexes: ORDER_DETAILS_INDEXES_NEW,
  })

  return renameCollection(app, {
    findBy: [REVERSE_SIGNALS_ID, REVERSE_SIGNALS_NEW_NAME, REVERSE_SIGNALS_OLD_NAME],
    name: REVERSE_SIGNALS_NEW_NAME,
    indexes: REVERSE_SIGNALS_INDEXES_NEW,
  })
}, (app) => {
  renameCollection(app, {
    findBy: [ORDER_DETAILS_ID, ORDER_DETAILS_NEW_NAME, ORDER_DETAILS_OLD_NAME],
    name: ORDER_DETAILS_OLD_NAME,
    indexes: ORDER_DETAILS_INDEXES_OLD,
  })

  return renameCollection(app, {
    findBy: [REVERSE_SIGNALS_ID, REVERSE_SIGNALS_NEW_NAME, REVERSE_SIGNALS_OLD_NAME],
    name: REVERSE_SIGNALS_OLD_NAME,
    indexes: REVERSE_SIGNALS_INDEXES_OLD,
  })
})
