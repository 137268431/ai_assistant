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

`runtime/monitoring/grafana/dashboards/quant-monitoring-overview.json` is a cockpit-first phased dashboard for Grafana:

1. Top trading cockpit for Gateway/auth/WS, market data freshness, signal/order health, account guard, and host resources.
2. Core trends for account capital, subscription counts, market freshness, and error rates.
3. Detailed rows for service health, Gateway/session/WebSocket, market data subscriptions, account guard, signals/orders, Broker pressure, history/backfill, HTTP dependencies, host resources, and alert impact counts.
