# ibkr-scheduler

- Source-owned scheduler code lives under `runtime/ibkr_scheduler/src/ibkr_scheduler`.
- Job registry visibility, persisted cursors, and scheduler health endpoints belong here.
- Legacy `ibkr_compute.control_plane.*` scheduler modules remain only as compatibility wrappers.
- `ibkr_scheduler` owns scheduler visibility pages/data and restart-safe cursor recovery, while `ibkr_api` owns control/webhook/topology routes and `ibkr_console` owns the static pages that render them.
