# ibkr-gateway ops

- Gateway/bootstrap operational tooling lives here.
- The runtime-owned launcher is deployed through `runtime/ibkr_runtime/src/ibkr_runtime/broker/launch_ib_gateway.sh`.
- Legacy `ops/ibkr_compute/install/*` wrappers remain for compatibility.
- `ibkr_api` may expose gateway status in topology or monitor payloads, but gateway/session failures remain `ibkr_runtime` ownership.
