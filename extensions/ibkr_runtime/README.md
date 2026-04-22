# ibkr-runtime extensions

- Runtime/data-plane validation lives under `extensions/ibkr_runtime/tests`.
- Queue persistence, PocketBase batch upsert, and remote-runtime split tests should land here first.
- Signal/order/reverse/system/startup control-plane tests should land in `extensions/ibkr_api/tests`, not here.
