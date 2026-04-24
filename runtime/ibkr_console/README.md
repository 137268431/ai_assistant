# IBKR Console

`runtime/ibkr_console/static` is the source-of-truth static console bundle for the standalone `ibkr-console` service.

Deployment ownership is now split cleanly:
- `ops/deploy/deploy_ibkr_console.sh` updates `/opt/ibkr_console/static`
- `ops/deploy/deploy_ibkr_public_proxy.sh` exposes the public entry at `https://quant.lzw-glory.top`
- `ops/deploy/deploy_pocketbase_runtime.sh --public-only` updates PocketBase's own minimal landing at `/opt/pocketbase/pb_public`

`runtime/pocketbase/pb_public` is no longer a console mirror.
It belongs to PocketBase and should only contain the PB landing/admin-facing public tree plus redirect-only compatibility shims for old console URLs.
The old `common.js` and `assets/**` console resource tree should not live there anymore.
New console edits should land in `runtime/ibkr_console/static`, not in `runtime/pocketbase/pb_public`.

`ibkr_console` only owns static assets.

- Control actions, webhook routes, startup/scheduler/system status APIs are owned by `ibkr_api`.
- Live runtime/session/gateway actions are owned by `ibkr_runtime`.
- Compute/backtest/history-driven data APIs are owned by `ibkr_compute`.
- Auth/data/admin pages under `pb.lzw-glory.top` are owned by `pocketbase`.
