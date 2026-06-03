/// <reference path="../pb_data/types.d.ts" />

const COLLECTION = "ibkr_execution_fills"

const FIELDS = [
  { "hidden": false, "id": "ief_commission_known", "name": "commission_known", "presentable": false, "required": false, "system": false, "type": "bool" },
  { "hidden": false, "id": "ief_realized_pnl", "max": null, "min": null, "name": "realized_pnl", "onlyInt": false, "presentable": false, "required": false, "system": false, "type": "number" },
  { "hidden": false, "id": "ief_realized_pnl_known", "name": "realized_pnl_known", "presentable": false, "required": false, "system": false, "type": "bool" },
  { "autogeneratePattern": "", "hidden": false, "id": "ief_perm_id", "max": 80, "min": 0, "name": "perm_id", "pattern": "", "presentable": false, "primaryKey": false, "required": false, "system": false, "type": "text" },
  { "hidden": false, "id": "ief_client_id", "max": null, "min": null, "name": "client_id", "onlyInt": true, "presentable": false, "required": false, "system": false, "type": "number" },
  { "autogeneratePattern": "", "hidden": false, "id": "ief_order_ref", "max": 160, "min": 0, "name": "order_ref", "pattern": "", "presentable": false, "primaryKey": false, "required": false, "system": false, "type": "text" },
  { "hidden": false, "id": "ief_contract_multiplier", "max": null, "min": null, "name": "contract_multiplier", "onlyInt": false, "presentable": false, "required": false, "system": false, "type": "number" },
]

function findCollection(app) {
  try { return app.findCollectionByNameOrId(COLLECTION) } catch (_) {}
  try { return app.findCollectionByNameOrId("_pb_ibkr_execution_fills_") } catch (_) {}
  return null
}

function fieldClass(field) {
  if (field.type === "bool") return new BoolField(field)
  if (field.type === "number") return new NumberField(field)
  if (field.type === "text") return new TextField(field)
  throw new Error(`unsupported field type ${field.type}`)
}

migrate((app) => {
  const collection = findCollection(app)
  if (!collection) throw new Error("ibkr_execution_fills collection not found")
  FIELDS.forEach((field) => {
    if (!collection.fields.getByName(field.name)) {
      collection.fields.add(fieldClass(field))
    }
  })
  return app.save(collection)
}, (app) => {
  const collection = findCollection(app)
  if (!collection) return null
  FIELDS.forEach((field) => {
    if (collection.fields.getByName(field.name)) {
      collection.fields.removeByName(field.name)
    }
  })
  return app.save(collection)
})
