# ibkr-scheduler extensions

- Service-owned scheduler validation lives under `extensions/ibkr_scheduler/tests`.
- This test home covers cron matching, slot dedupe, persisted job state, native API/compute dispatch, and scheduler service routes.
- Keep scheduler timing/state tests here instead of piling them into `extensions/ibkr_api/tests` or `extensions/ibkr_compute/tests`.
