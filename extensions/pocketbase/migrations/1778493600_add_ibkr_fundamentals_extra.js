/// <reference path="../pb_data/types.d.ts" />

const IBKR_FUNDAMENTALS_COLLECTION = "ibkr_fundamentals"

const EXTRA_FIELD = {
  "hidden": false,
  "id": "if_extra",
  "maxSize": 2097152,
  "name": "extra",
  "presentable": false,
  "required": false,
  "system": false,
  "type": "json",
}

function findIbkrFundamentalsCollection(app) {
  try {
    return app.findCollectionByNameOrId(IBKR_FUNDAMENTALS_COLLECTION)
  } catch (e) {}
  return null
}

migrate((app) => {
  const collection = findIbkrFundamentalsCollection(app)
  if (!collection) {
    throw new Error("ibkr_fundamentals collection not found")
  }

  if (!collection.fields.getByName("extra")) {
    collection.fields.add(new JSONField(EXTRA_FIELD))
  }

  return app.save(collection)
}, (app) => {
  const collection = findIbkrFundamentalsCollection(app)
  if (!collection) {
    return null
  }

  const field = collection.fields.getByName("extra")
  if (field) {
    collection.fields.removeByName("extra")
  }

  return app.save(collection)
})
