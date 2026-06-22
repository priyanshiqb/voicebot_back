# 🐳 Docker Service to Profile Mapping

This document lists each Docker container service configured in the project alongside its corresponding Compose Profile.

| Docker Service Name | Compose Profile | Description / Role |
| :--- | :--- | :--- |
| **`backend-wsgi`** | `prod` | Django production WSGI server (Gunicorn) |
| **`backend-asgi`** | `prod` | Django production ASGI server (Daphne) |
| **`backend-dev`** | `dev` | Daphne development server (hot-reloading) |
| **`celery-dev`** | `dev` | Celery worker running in development mode |
| **`celery-prod`** | `prod` | Celery worker running in production mode |
| **`postgres`** | `database` | PostgreSQL Core Database service |
| **`pgbouncer`** | `pgbouncer` | PgBouncer Connection Pooler |
| **`postgres-exporter`**| `pgexporter` | Metrics exporter for PostgreSQL database |
| **`pgadmin`** | `pgadmin` | Database management UI |
| **`redis`** | `caches` | Redis Cache & Channels Layer |
| **`redis-insight`** | `redis-insight` | Redis Insight analytics web dashboard |
| **`redis-exporter`** | `redis-exporter` | Metrics exporter for Redis caching instance |
| **`grafana`** | `grafana` | Grafana visual observability dashboards |
| **`loki`** | `logs-monitoring` | Loki log aggregator |
| **`promtail`** | `logs-monitoring` | Promtail log shipper and scraper |
| **`prometheus`** | `matrics-monitoring` | Prometheus metrics and time-series DB |
| **`azurite`** | `emulators` | Azure Blob, Queue, and Table storage emulator |
| **`caddy`** | `gateway` | Caddy reverse proxy and SSL gateway |
| **`backend-docs`** | `backend-docs` | Nginx/Docsify developer portal & API/WS docs server |
