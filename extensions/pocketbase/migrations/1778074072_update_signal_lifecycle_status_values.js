/// <reference path="../pb_data/types.d.ts" />

const EXTENDED_SIGNAL_STATUSES = [
  "awaiting_confirm",
  "pending",
  "submitted",
  "protected_active",
  "protection_incomplete",
  "executed",
  "expired",
  "rejected",
  "closed",
]

const LEGACY_SIGNAL_STATUSES = [
  "awaiting_confirm",
  "pending",
  "executed",
  "expired",
  "rejected",
  "closed",
]

function findCollection(app, name) {
  try {
    return app.findCollectionByNameOrId(name)
  } catch (_) {}
  return null
}

function updateSignalStatusValues(app, values) {
  const collectionNames = ["ibkr_signals", "tv_signals"]
  collectionNames.forEach((name) => {
    const collection = findCollection(app, name)
    if (!collection) return
    const field = collection.fields.getByName("status")
    if (!field) return
    field.values = values
    app.save(collection)
  })
  return null
}

migrate((app) => {
  return updateSignalStatusValues(app, EXTENDED_SIGNAL_STATUSES)
}, (app) => {
  return updateSignalStatusValues(app, LEGACY_SIGNAL_STATUSES)
})
