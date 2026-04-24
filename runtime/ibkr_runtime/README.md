# ibkr-runtime

- Runtime service-owned entrypoints live under `runtime/ibkr_runtime/src/ibkr_runtime`.
- The runtime-owned gateway launcher now lives at `runtime/ibkr_runtime/src/ibkr_runtime/broker/launch_ib_gateway.sh`.
- Runtime still reuses shared `ibkr_compute` orchestration libraries during this compatibility phase.
- On a fresh host, deploy `ibkr_compute` before `ibkr_runtime`; the runtime entrypoint still imports shared modules from `/opt/ibkr_compute/src`.
- The goal of this directory is to keep runtime boot/service ownership out of `runtime/ibkr_compute`.
- `ibkr_runtime` owns broker session recovery, gateway launch, live bars, and runtime-control execution.
- Startup presentation/state shaping and system/topology summary surfaces now belong under `runtime/ibkr_api/src/ibkr_api/startup` and `runtime/ibkr_api/src/ibkr_api/system`.
