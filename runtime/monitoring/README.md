# Monitoring Runtime

Lightweight systemd-native monitoring for the split IBKR host.

Services:

- `prometheus.service` binds to `127.0.0.1:9090` and stores data in `/var/lib/prometheus`.
- `node-exporter.service` binds to `127.0.0.1:9100`.
- `grafana-server.service` binds to `127.0.0.1:3000` and uses provisioning from `/opt/monitoring/grafana`.

The public Grafana proxy is expected to be protected by a Caddy import at
`/etc/caddy/secrets/quant-monitor.lzw-glory.top.auth.caddy`. The deploy script
checks for that file before reloading Caddy and does not create a default public
password.


## Dashboard

`runtime/monitoring/grafana/dashboards/quant-monitoring-overview.json` is a phased request-flow dashboard for Grafana:

1. Service health and scrape readiness.
2. Inbound HTTP request rate, p95 latency, in-flight requests, and exceptions.
3. Outbound HTTP dependency rate, p95 latency, in-flight requests, and errors.
4. Gateway socket and IB API connect/error/disconnect status.
5. Broker request pressure, pending requests, and error-code table.
6. History/backfill activity and row throughput.
7. Signal validation, submission, reverse action, and order tracker outcomes.
8. Host CPU, memory, filesystem, and systemd state.
9. Account capital, buying power floors, guard state, and Gateway/account runtime state.
