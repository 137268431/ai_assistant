# ibkr-stack ops

- Cross-service stack health and topology tooling lives here.
- `ops/health/*` remains as a compatibility entrypoint only.
- Triage ownership:
  - `ibkr_console` = static UI bundle and page asset issues
  - `ibkr_api` = control-plane routes, webhook entrypoints, topology/status/system summary surfaces
  - `ibkr_scheduler` = scheduler registry, persisted cursors, scheduler health/visibility
  - `ibkr_runtime` = broker session, gateway, auth recovery, live runtime state
  - `ibkr_compute` = compute, scan, recompute, backtest, history rebuild
