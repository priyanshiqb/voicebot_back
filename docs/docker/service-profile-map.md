# 🐳 Docker Service to Profile Mapping

This document lists each Docker container service configured in the project alongside its corresponding Compose Profile.

| Docker Service Name | Compose Profile | Description / Role |
| :--- | :--- | :--- |
| **`backend-wsgi`** | `prod` | Django production WSGI server (Gunicorn) |
| **`backend-asgi`** | `prod` | Django production ASGI server (Daphne) |
| **`backend-dev`** | `dev` | Daphne development server (hot-reloading) |
| **`celery-dev`** | `dev` | Celery worker (executes tasks) — development |
| **`celery-prod`** | `prod` | Celery worker (executes tasks) — production |
| **`celery-beat-dev`** | `dev` | Celery Beat scheduler — development. **Never scale: one instance only** |
| **`celery-beat-prod`** | `prod` | Celery Beat scheduler — production. **Never scale: one instance only** |
| **`postgres`** | `database` | PostgreSQL Core Database service |
| **`pgbouncer`** | `pgbouncer` | PgBouncer Connection Pooler |
| **`postgres-exporter`**| `pgexporter` | Metrics exporter for PostgreSQL database |
| **`pgadmin`** | `pgadmin` | Database management UI |
| **`redis`** | `caches` | Redis Cache & Channels Layer |
| **`redis-insight`** | `redis-insight` | Redis Insight analytics web dashboard |
| **`redis-exporter`** | `redis-exporter` | Metrics exporter for Redis caching instance (also reports the Celery queue length) |
| **`celery-exporter`** | `celery-exporter` | Celery task and worker metrics from the workers' event stream: tasks by state, runtime, workers online |
| **`grafana`** | `grafana` | Grafana visual observability dashboards |
| **`loki`** | `logs-monitoring` | Loki log aggregator |
| **`alloy`** | `logs-monitoring` | Grafana Alloy log collector (replaced Promtail, EOL 2026-03-02) |
| **`prometheus`** | `matrics-monitoring` | Prometheus metrics and time-series DB |
| **`alertmanager`** | `matrics-monitoring` | Sends Prometheus alerts by email and/or Telegram (channels set by `ALERT_*` in `.env`) |
| **`cadvisor`** | `host-metrics` | Per-container CPU, memory, network and disk metrics |
| **`node-exporter`** | `host-metrics` | Host CPU, memory, disk and network metrics; also serves docker-state's file |
| **`docker-state`** | `host-metrics` | Docker's per-container restart count, running state and healthcheck status, for the container alerts. Reads the Docker socket. |
| **`probe-tools`** | `logs-monitoring`, `redis-exporter` | One-shot: copies busybox for the healthchecks of shell-less images (loki, redis-exporter). Shows as `Exited (0)` |
| **`azurite`** | `emulators` | Azure Blob, Queue, and Table storage emulator |
| **`caddy`** | `gateway` | Caddy reverse proxy and SSL gateway |
| **`backend-docs`** | `backend-docs` | Nginx/Docsify developer portal & API/WS docs server |
