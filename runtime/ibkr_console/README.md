# IBKR Console

`runtime/ibkr_console/static` is the source-of-truth static console bundle.

Deployment fans this directory out to:
- `/opt/ibkr_console/static` for the standalone `ibkr-console` service
- `/opt/pocketbase/pb_public` as a PocketBase compatibility public tree

Deploy responsibility is split:
- `ops/deploy/deploy_ibkr_console.sh` updates `/opt/ibkr_console/static`
- `ops/deploy/deploy_pocketbase_runtime.sh --public-only` updates `/opt/pocketbase/pb_public`

`runtime/pocketbase/pb_public` remains as a legacy compatibility workspace during the migration,
but new console edits should land in `runtime/ibkr_console/static`.

`ibkr_console` only owns static assets.

- Control actions, webhook routes, startup/scheduler/system status APIs are owned by `ibkr_api`.
- Live runtime/session/gateway actions are owned by `ibkr_runtime`.
- Compute/backtest/history-driven data APIs are owned by `ibkr_compute`.
