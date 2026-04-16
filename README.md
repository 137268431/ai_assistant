# AI Assistant Runtime Layout

This repo is split by responsibility instead of by product history.

## Directories

- `runtime/`
  - Production files that are actually deployed and executed.
  - `runtime/pocketbase/pb_public`
  - `runtime/pocketbase/pb_hooks`
  - `runtime/ibkr_compute/src`
  - `runtime/ibkr_compute/requirements.txt`
  - `runtime/ibkr_compute/systemd/ibkr-compute.service`
  - `runtime/ib_gateway/systemd/ibkr-gateway.service`
- `ops/`
  - Deployment, health checks, database ops, validation tools, and IBKR operational scripts.
  - `ops/deploy`
  - `ops/ibkr_compute/auth`
  - `ops/ibkr_compute/monitor`
  - `ops/ui/playwright`
- `extensions/`
  - Non-runtime assets: migrations, schema bundles, seeds, tests, and maintenance scripts.
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
- PocketBase extension migrations:
  - `extensions/pocketbase/migrations` -> `/opt/pocketbase/extensions/migrations`
- IBKR Compute runtime:
  - `runtime/ibkr_compute/src` -> `/opt/ibkr_compute/src`
  - `runtime/ibkr_compute/requirements.txt` -> `/opt/ibkr_compute/requirements.txt`
  - `runtime/ibkr_compute/systemd/ibkr-compute.service` -> `/etc/systemd/system/ibkr-compute.service`
- IBKR Compute ops tools:
  - `ops/ibkr_compute/auth` -> `/opt/ibkr_compute/ops/auth`
  - `ops/ibkr_compute/monitor` -> `/opt/ibkr_compute/ops/monitor`
- IB Gateway:
  - `runtime/ib_gateway/systemd/ibkr-gateway.service` -> `/etc/systemd/system/ibkr-gateway.service`
  - Bootstrap IB Gateway + IBC on the server with `ops/ibkr_compute/install/bootstrap_ib_gateway_remote.sh`

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
bash ai_assistant/ops/deploy/deploy_pocketbase_runtime.sh --mode files --file runtime/pocketbase/pb_hooks/ibkr_actions.pb.js
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode files --file runtime/ibkr_compute/src/ibkr_compute/api/app.py
```

Auto-pick mode from git diff:

```bash
bash ai_assistant/ops/deploy/deploy_runtime_all.sh --mode auto --diff HEAD~1..HEAD --plan-only
bash ai_assistant/ops/deploy/deploy_runtime_all.sh --mode auto --diff HEAD~1..HEAD
```

Large change, package publish:

```bash
bash ai_assistant/ops/deploy/deploy_ibkr_compute_runtime.sh --mode package --diff HEAD~5..HEAD --ops-tools
bash ai_assistant/ops/deploy/deploy_pocketbase_runtime.sh --mode package --file runtime/pocketbase/pb_public/index.html --package-name pb-ui-refresh
```

Remote cleanup:

```bash
bash ai_assistant/ops/deploy/prune_remote_legacy.sh
```

Playwright smoke checks now live inside the repo:

```bash
bash ai_assistant/ops/ui/run_pb_playwright_smoke.sh
```

## Rules

- Default deployment never uploads `extensions/`.
- PocketBase runtime is only `pb_public` and `pb_hooks`.
- IBKR Compute runtime is only `src`, `requirements.txt`, and the compute systemd unit.
- Gateway binaries are not stored in this repo. Only the service contract is stored here.
- `--mode auto` requires `--file` or `--diff`.
- `--mode files` only supports add/modify changes. Delete or rename changes must use `--mode package`.
- `--plan-only` prints the resolved units, files, checks, and restart actions without touching the remote host.
