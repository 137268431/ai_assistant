# AI Assistant Runtime Layout

This repo is split by responsibility instead of by product history.

## Service Ownership

- `ibkr_console`
  - Owns static pages and browser assets under `runtime/ibkr_console/static`.
- `ibkr_api`
  - Owns control-plane routes, webhook entrypoints, topology/status aggregation, startup progress, callback handling, and PocketBase-facing compatibility APIs under `runtime/ibkr_api/src/ibkr_api`.
- `ibkr_scheduler`
  - Owns job registry, persisted scheduler cursors, scheduler visibility, and scheduler health surfaces under `runtime/ibkr_scheduler/src/ibkr_scheduler`.
- `ibkr_runtime`
  - Owns broker session, gateway launch, live bar ingest, runtime-control execution, and runtime-owned service boot under `runtime/ibkr_runtime/src/ibkr_runtime`.
- `ibkr_compute`
  - Owns compute, recompute, scan, backtest, history rebuild, and shared compute libraries under `runtime/ibkr_compute/src/ibkr_compute`.
- `pocketbase`
  - Owns collections, auth, the minimal `pb_public` landing/admin surface, and temporary no-op `pb_hooks` shells; final target is storage/auth/admin only, with the redirect shims and shell files removable after cutover confidence is high.

## ibkr-api Feature Slices

- `runtime/ibkr_api/src/ibkr_api/compat`
  - Service shell routes plus explicit compatibility ownership (`/health`, `/status`, `/api/collections/*`, generic `/api/custom/*`, generic `/webhook/*`); unmatched custom/webhook paths now fail in `ibkr-api` instead of falling back to PocketBase.
- `runtime/ibkr_api/src/ibkr_api/runtime`
  - Runtime/status/2FA route registrars plus runtime payload-shaping helpers.
  - `status_support.py` is a compatibility barrel; the real runtime status shaping now lives in `status_fetch.py`, `status_compute.py`, `status_live_readiness.py`, `status_runtime_sections.py`, `status_runtime_warmup.py`, and `status_runtime_payload.py`.
- `runtime/ibkr_api/src/ibkr_api/two_factor`
  - Active 2FA request/result/respond/takeover/probe flows.
  - `request.py` is now a compatibility barrel; request flow internals live in `request_shared.py`, `request_approval.py`, `request_trigger.py`, and `request_response.py`.
- `runtime/ibkr_api/src/ibkr_api/system`
  - System summary, monitor, topology, system-event delivery, and PocketBase disk helpers.
  - `system/jobs/auth.py` is a compatibility barrel; the actual auth/session job logic is split across `auth_shared.py`, `auth_issue.py`, `auth_edge_guard.py`, `auth_pending_guard.py`, and `auth_reminders.py`.
- `runtime/ibkr_api/src/ibkr_api/startup`
  - Runtime startup progress, startup card payload helpers, and startup route registration.
- `runtime/ibkr_api/src/ibkr_api/integrations`
  - External service adapters such as Feishu delivery and runtime order-cancel bridging.
- `runtime/ibkr_api/src/ibkr_api/callbacks`
  - Feishu callback parsing/action dispatch builders plus callback route registration.
- `runtime/ibkr_api/src/ibkr_api/tradingview`
  - TradingView webhook normalization, `tv_indicators` / `tv_signals` ingestion helpers, and route registration.
- `runtime/ibkr_api/src/ibkr_api/signals`, `runtime/ibkr_api/src/ibkr_api/orders`, `runtime/ibkr_api/src/ibkr_api/reverse`, `runtime/ibkr_api/src/ibkr_api/webhooks`
  - API-owned domain slices and route registrars that should keep growing instead of `api_app.py`.
  - `reverse/common.py` is now a compatibility barrel; the live reverse-signal helpers are split across `reverse/shared.py`, `reverse/normalize.py`, `reverse/indicator_support.py`, `reverse/order_support.py`, and `reverse/repository.py`.
  - `signals/ingest.py` is now the signal-ingest orchestration shell; payload normalization, bar-dedupe helpers, lifecycle resolution, and record diff/upsert logic now live in `signals/ingest_payloads.py`, `signals/ingest_dedupe.py`, `signals/ingest_lifecycle.py`, and `signals/ingest_store.py`.
- `runtime/ibkr_api/src/ibkr_api/account`, `runtime/ibkr_api/src/ibkr_api/universe`
  - Feature slices that now also split large builders into smaller service-local modules such as `snapshot_live_orders.py`, `snapshot_relations.py`, `today_targets_shared.py`, and `today_targets_workflow.py`.
- `runtime/ibkr_api/src/ibkr_api/app_core`
  - Shared API composition helpers for config selection, request/response wrappers, value normalization, registrar assembly, presentation helpers, proxy forwarding, and state access so `api_app.py` can stay focused on assembly.
- Legacy root import paths like `ibkr_api.order_upsert` now resolve through `runtime/ibkr_api/src/ibkr_api/compat`, so the repo tree can stay folderized without keeping duplicate root files.

## Directories

- `runtime/`
  - Production files that are actually deployed and executed.
  - `runtime/ibkr_console/static`
  - `runtime/pocketbase/pb_public` (PocketBase landing + legacy redirect shims)
  - `runtime/pocketbase/pb_hooks`
  - `runtime/ibkr_compute/src`
  - `runtime/ibkr_api/src`
  - `runtime/ibkr_scheduler/src`
  - `runtime/ibkr_runtime/src`
  - `runtime/ibkr_compute/requirements.txt`
  - `runtime/ibkr_runtime/requirements.txt`
  - `runtime/ibkr_api/systemd/ibkr-api.service`
  - `runtime/ibkr_scheduler/systemd/ibkr-scheduler.service`
  - `runtime/ibkr_runtime/systemd/ibkr-runtime.service`
  - `runtime/ibkr_compute/systemd/ibkr-compute.service`
  - `runtime/ib_gateway/systemd/ibkr-gateway.service`
- `runtime/ibkr_console`
  - Standalone static console service and source-owned page bundle.
  - `runtime/ibkr_console/static`
  - `runtime/ibkr_console/systemd/ibkr-console.service`
- `runtime/ibkr_api`
  - Standalone control-plane API service source and systemd unit.
  - `runtime/ibkr_api/src/ibkr_api`
  - `runtime/ibkr_api/systemd/ibkr-api.service`
- `runtime/ibkr_scheduler`
  - Standalone scheduler service source and systemd unit.
  - `runtime/ibkr_scheduler/src/ibkr_scheduler`
  - `runtime/ibkr_scheduler/systemd/ibkr-scheduler.service`
- `runtime/ibkr_runtime`
  - Runtime-owned service bootstrap, runtime systemd unit, and runtime venv requirements.
  - `runtime/ibkr_runtime/src/ibkr_runtime`
  - `runtime/ibkr_runtime/requirements.txt`
  - `runtime/ibkr_runtime/systemd/ibkr-runtime.service`
- `ops/`
  - Deployment, health checks, database ops, validation tools, and IBKR operational scripts.
  - `ops/deploy`
  - `ops/ibkr_stack/health`
  - `ops/ibkr_console/ui/playwright`
  - `ops/ibkr_console/validate`
  - `ops/ib_gateway/install`
- `extensions/`
  - Non-runtime assets: migrations, schema bundles, seeds, tests, and maintenance scripts.
  - `extensions/ibkr_api/tests`
  - `extensions/ibkr_scheduler/tests`
  - `extensions/ibkr_runtime/tests`
  - `extensions/pocketbase/migrations`
  - `extensions/pocketbase/schema`
  - `extensions/pocketbase/seeds`
  - `extensions/pocketbase/scripts`
  - `extensions/pocketbase/tests`
  - `extensions/ibkr_compute/tests`

## Remote Mapping

- PocketBase runtime:
  - `runtime/pocketbase/pb_public` -> `/opt/pocketbase/pb_public`
  - `runtime/pocketbase/pb_hooks` -> `/opt/pocketbase/pb_hooks`
- IBKR Console runtime:
  - `runtime/ibkr_console/static` -> `/opt/ibkr_console/static`
  - `runtime/ibkr_console/systemd/ibkr-console.service` -> `/etc/systemd/system/ibkr-console.service`
- PocketBase extension migrations:
  - `extensions/pocketbase/migrations` -> `/opt/pocketbase/extensions/migrations`
- IBKR Compute runtime:
  - `runtime/ibkr_compute/src` -> `/opt/ibkr_compute/src`
  - `runtime/ibkr_compute/requirements.txt` -> `/opt/ibkr_compute/requirements.txt`
  - `runtime/ibkr_compute/systemd/ibkr-compute.service` -> `/etc/systemd/system/ibkr-compute.service`
- IBKR API runtime:
  - `runtime/ibkr_api/src` -> `/opt/ibkr_api/src`
  - `runtime/ibkr_api/systemd/ibkr-api.service` -> `/etc/systemd/system/ibkr-api.service`
- IBKR Scheduler runtime:
  - `runtime/ibkr_scheduler/src` -> `/opt/ibkr_scheduler/src`
  - `runtime/ibkr_scheduler/systemd/ibkr-scheduler.service` -> `/etc/systemd/system/ibkr-scheduler.service`
- IBKR Runtime runtime:
  - `runtime/ibkr_runtime/src` -> `/opt/ibkr_runtime/src`
  - `runtime/ibkr_runtime/requirements.txt` -> `/opt/ibkr_runtime/requirements.txt`
  - `runtime/ibkr_runtime/systemd/ibkr-runtime.service` -> `/etc/systemd/system/ibkr-runtime.service`
- IBKR Compute ops tools:
  - `ops/ibkr_compute/auth` -> `/opt/ibkr_compute/ops/auth`
  - `ops/ibkr_compute/monitor` -> `/opt/ibkr_compute/ops/monitor`
- IB Gateway:
  - `runtime/ib_gateway/systemd/ibkr-gateway.service` -> `/etc/systemd/system/ibkr-gateway.service`
  - Bootstrap IB Gateway + IBC on the server with `ops/ib_gateway/install/bootstrap_ib_gateway_remote.sh`

## Deploy Commands

From the parent directory of this repo:

```bash
bash ai_assistant/ops/deploy/deploy_pocketbase_runtime.sh
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh
bash ai_assistant/ops/deploy/deploy_runtime_all.sh
```

Deploy modes:

- `scope`: keep the existing directory-level runtime sync behavior.
- `files`: upload only the specified files and never apply `--delete`.
- `package`: build a staging tarball locally, upload once, validate remotely, then apply in batch.
- `auto`: pick `files` or `package` automatically from `--file` or `--diff`.

Optional extension scopes:

```bash
bash ai_assistant/ops/deploy/deploy_pocketbase_runtime.sh --migrations
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --ops-tools --gateway-service
```

Small change, file-level publish:

```bash
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode files --file runtime/ibkr_api/src/ibkr_api/signals/ingest.py
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode files --file runtime/ibkr_compute/src/ibkr_compute/api/app.py
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode files --file runtime/ibkr_api/src/ibkr_api/api_app.py
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode files --file runtime/ibkr_scheduler/src/ibkr_scheduler/scheduler_app.py
bash ai_assistant/ops/deploy/deploy_ibkr_runtime_service.sh --mode files --file runtime/ibkr_runtime/src/ibkr_runtime/server.py
```

Auto-pick mode from git diff:

```bash
bash ai_assistant/ops/deploy/deploy_runtime_all.sh --mode auto --diff HEAD~1..HEAD --plan-only
bash ai_assistant/ops/deploy/deploy_runtime_all.sh --mode auto --diff HEAD~1..HEAD
```

Large change, package publish:

```bash
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode package --diff HEAD~5..HEAD --ops-tools
bash ai_assistant/ops/deploy/deploy_pocketbase_runtime.sh --mode package --file runtime/ibkr_console/static/index.html --package-name pb-ui-refresh
bash ai_assistant/ops/deploy/deploy_ibkr_console.sh --mode package --file runtime/ibkr_console/static/index.html --package-name console-ui-refresh
```

Remote cleanup:

```bash
bash ai_assistant/ops/deploy/prune_remote_legacy.sh
```

Playwright smoke checks now live inside the repo:

```bash
bash ai_assistant/ops/ibkr_console/ui/run_console_playwright_smoke.sh
```

Console source compatibility helpers:

```bash
bash ai_assistant/ops/dev/sync_console_static.sh
python3 ai_assistant/ops/ibkr_console/validate/check_console_static_sync.py
```

## Rules

- Default deployment never uploads `extensions/`.
- `runtime/ibkr_console/static` is the source-of-truth static console bundle.
- PocketBase public deploys from `runtime/pocketbase/pb_public`, not from `runtime/ibkr_console/static`.
- `ops/dev/sync_console_static.sh` is now a boundary reminder only; it no longer mirrors console files into `runtime/pocketbase/pb_public`.
- `runtime/pocketbase/pb_public` is no longer a console mirror; it now contains the PB landing plus legacy redirect-only html shims.
- PocketBase runtime business logic should stay out of `pb_hooks`; the repo copy now only keeps no-op `*.pb.js` compatibility shells.
- `runtime/pocketbase/pb_hooks` cannot be deleted yet because deploy/docs still treat it as an explicit compatibility unit, even though its old JS internals are gone.
- `runtime/ibkr_api/src`, `runtime/ibkr_scheduler/src`, and `runtime/ibkr_runtime/src` are the source-of-truth service-owned split-stack entrypoints.
- `runtime/ibkr_compute/src` now holds shared compute/runtime libraries plus compatibility wrappers for legacy imports.
- `extensions/ibkr_api/tests`, `extensions/ibkr_scheduler/tests`, and `extensions/ibkr_runtime/tests` are the source-of-truth split-stack test homes; legacy `extensions/ibkr_compute/tests/*` wrappers stay only for compatibility.
- `ops/ibkr_stack/*`, `ops/ibkr_console/*`, and `ops/ib_gateway/*` are the source-of-truth service/function-specific ops homes; legacy `ops/health`, `ops/ui`, `ops/validate`, and `ops/ibkr_compute/install` entrypoints stay only as wrappers.
- Gateway binaries are not stored in this repo. Only the service contract is stored here.
- `--mode auto` requires `--file` or `--diff`.
- `--mode files` only supports add/modify changes. Delete or rename changes must use `--mode package`.
- `--plan-only` prints the resolved units, files, checks, and restart actions without touching the remote host.
