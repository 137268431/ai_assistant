# ibkr-console ops

- Console/UI smoke and split-boundary validation tooling lives here.
- `ops/ui/*` and `ops/validate/check_console_static_sync.py` remain compatibility wrappers.
- `check_console_static_sync.py` now validates that `runtime/ibkr_console/static` and `runtime/pocketbase/pb_public` stay intentionally decoupled; PB keeps only its own landing page plus redirect-only shims for old console URLs, not the old `common.js` / `assets/**` bundle.
- Page smoke failures can still originate from `ibkr_api`, `ibkr_runtime`, or `ibkr_compute` backends; do not assume every console failure is a static-asset regression.
