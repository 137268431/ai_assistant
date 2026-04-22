# ibkr-console ops

- Console/UI smoke, static-sync validation, and related tooling lives here.
- `ops/ui/*` and `ops/validate/check_console_static_sync.py` remain compatibility wrappers.
- Page smoke failures can still originate from `ibkr_api`, `ibkr_runtime`, or `ibkr_compute` backends; do not assume every console failure is a static-asset regression.
