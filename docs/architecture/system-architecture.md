# Production-Grade System Architecture Diagram

This document illustrates the networking, data, and observability flows across all containerized services running in the Voicebot ecosystem.

---

## 1. System Architecture Map (Mermaid)

The following diagram maps out client interaction boundaries, request gateway sorting, database clustering, and the unified observability loop.

```mermaid
flowchart TB
    %% Nodes
    subgraph ClientSpace ["Public Internet / Client Space"]
        Browser["User Web Browser"]
        APIClient["REST API Client"]
    end

    subgraph HostGateway ["Gateway Tier (Caddy Proxy)"]
        Caddy["Caddy Web Server (Port 80/443, SSL/TLS)"]
    end

    subgraph ApplicationTier ["Application & Cache Tier"]
        DEV["backend-dev Django Dev (Port 8000)"]
        WSGI["backend-wsgi Django REST (Port 8001)"]
        ASGI["backend-asgi Django Channels WS (Port 8002)"]
        Redis["redis Cache & Channels (Port 5563 Host / 6379 Int)"]
        Azurite["azurite Azure Storage (Port 10000-02)"]
    end

    subgraph DatabaseTier ["Database Tier"]
        PgBouncer["pgbouncer Pooler (Internal Port 5432)"]
        Postgres["postgres PostgreSQL Engine (Port 5579 Host / 5432 Int)"]
    end

    subgraph ObservabilityStack ["Observability & Monitoring Stack"]
        Alloy["alloy Log Collector (Internal Daemon)"]
        Loki["loki Log Aggregator (Port 3100)"]
        Prometheus["prometheus Metrics TSDB (Port 9090)"]
        Grafana["grafana UI Visualization (Port 3099)"]
        
        PgExporter["postgres-exporter (Port 9187)"]
        RedisExporter["redis-exporter (Port 9121)"]
    end

    subgraph AdminConsoles ["Admin & Management Interfaces"]
        PgAdmin["pgadmin DB Management (Port 5560)"]
        RedisInsight["redis-insight Cache GUI (Port 5561)"]
    end

    %% Flows
    Browser -->|HTTPS / WebSocket| Caddy
    APIClient -->|HTTPS API Requests| Caddy

    %% Gateway Routing
    Caddy -->|Route /api/v1/ & admin/ (Prod)| WSGI
    Caddy -->|Route /ws/ & /onboard/ (Prod)| ASGI
    Caddy -->|Route /api/v1/ & /ws/ (Local Dev)| DEV
    Caddy -->|Route pgadmin.domain| PgAdmin
    Caddy -->|Route redisinsight.domain| RedisInsight
    Caddy -->|Route grafana.domain| Grafana
    Caddy -->|Route prometheus.domain| Prometheus
    Caddy -->|Route loki.domain| Loki

    %% App Communications
    WSGI & ASGI & DEV -->|DB Queries| PgBouncer
    PgBouncer -->|Pooled Connections| Postgres
    WSGI & ASGI & DEV -->|Read/Write Session & PubSub| Redis
    WSGI & ASGI & DEV -->|Object Storage / Blobs| Azurite

    %% Observability Loop
    Alloy -->|Scrape Container Logs| WSGI & ASGI & DEV
    Alloy -->|Push Logs| Loki
    
    PgExporter -->|Scrape Database Metrics| Postgres
    RedisExporter -->|Scrape Cache Metrics| Redis
    
    Prometheus -->|Scrape Metrics /prom-metrics/| WSGI
    Prometheus -->|Scrape Metrics /prom-metrics/| ASGI
    Prometheus -->|Scrape Metrics /prom-metrics/| DEV
    Prometheus -->|Scrape DB Metrics| PgExporter
    Prometheus -->|Scrape Cache Metrics| RedisExporter
    
    Grafana -->|Query Metrics| Prometheus
    Grafana -->|Query Logs| Loki
    
    PgAdmin -->|Manage| Postgres
    RedisInsight -->|Inspect| Redis
```

---

## 2. Component Directory

| Tier | Service Name | Role & Functionality |
| :--- | :--- | :--- |
| **Gateway** | `caddy` | Standard entry-point for inbound traffic. Resolves SSL certs via Let's Encrypt, routes domains (`api.`, `ws.`), and restricts admin panels using Basic Auth. |
| **Application** | `backend-wsgi` | Django Gunicorn server. Handles synchronous REST requests, administration, and DB mutations. Exposes metrics at `/prom-metrics/`. |
| | `backend-asgi` | Django Daphne ASGI server. Manages asynchronous WebSockets connections and live session notifications. Exposes metrics at `/prom-metrics/`. |
| | `backend-dev` | Django local development server. Used during development to offer hot-reloading code execution and checks. Exposes metrics at `/prom-metrics/`. |
| **Caching** | `redis` | Caching store and Channels messaging broker. Holds active user sessions and routes pub/sub broadcast frames. |
| **Storage** | `azurite` | Azure storage emulator providing offline blob storage, queue tracking, and table indexing for asset processing. |
| **Database** | `pgbouncer` | Postgres connection pooler. Saves server CPU overhead by maintaining warm database sockets. |
| | `postgres` | Hardened transactional database holding application configurations, auditing logs, and tenant parameters. |
| **Observability** | `alloy` | Log daemon (Grafana Alloy; replaced Promtail, EOL 2026-03-02). Reads the Docker socket logs, tags them with service labels, and streams them to Loki. |
| | `loki` | Index-free log data store. Automatically deletes logs after 30 days via its compacting agent. |
| | `prometheus` | Scraping collector. Regularly polls Django endpoints and database/cache exporters to record timeseries health graphs. |
| | `grafana` | Single pane of glass UI. Pre-loaded with custom system dashboards to visualize logs and metrics. |
| **Exporters** | `postgres-exporter` | Queries internal PostgreSQL stats (`pg_stat_database`) and exposes them to Prometheus. |
| | `redis-exporter` | Queries Redis database memory configurations and active client counts for Prometheus collection. |
| **Management** | `pgadmin` | Visual DB workbench for executing SQL queries and inspecting table schemas. |
| | `redis-insight` | Diagnostic workspace to trace cache keys and channel traffic. |

---

## 3. Telemetry and Scrape Patterns
1. **Pull Metrics Model:** Prometheus regularly pulls timeseries variables from the WSGI/ASGI endpoints and exporters.
2. **Push Logs Model:** Alloy reads raw stderr/stdout containers streams and actively pushes formatted JSON lines up to Loki.
3. **Grafana Dashboards:** Queries Loki and Prometheus using LogQL and PromQL to render the system overview.

---

## 4. Visual Conceptual Infographic

Here is a conceptual illustration of the production-grade container stack and monitoring loop:

![System Architecture Illustration](system_architecture_diagram.png)

---

## 5. Endpoints & Access Control Directory

This section details how to access the services, the ports/subdomains used, and the authentication schemes required for each endpoint.

### A. Application & Core Entrypoints

| Endpoint Purpose | Access URL / Pattern | Protocol | Authentication & Credentials |
| :--- | :--- | :--- | :--- |
| **REST API Server** | `https://api.{$DOMAIN}/api/v1/` | HTTPS | Bearer Token / Session Cookie (set via onboarding / login flow). |
| **Django Admin Console** | `https://api.{$DOMAIN}/admin/` | HTTPS | Standard Django Superuser username & password. |
| **WebSocket Notifications** | `wss://ws.{$DOMAIN}/ws/` | WSS | Authenticated connection using token parameter inside query string. |
| **WebSocket Onboarding** | `wss://ws.{$DOMAIN}/onboard/` | WSS | Authenticated session token parameter. |

---

### B. Observability & Telemetry Dashboards

| Service | Access URL | Protocol | Authentication & Credentials |
| :--- | :--- | :--- | :--- |
| **Grafana UI** | `https://grafana.{$DOMAIN}` | HTTPS | Grafana admin dashboard login (`GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD`). Protected by Caddy Basic Auth gateway proxy if enabled. |
| **Prometheus Console** | `https://prometheus.{$DOMAIN}` | HTTPS | Protected at the Gateway by Caddy Basic Auth (`GATEWAY_BASIC_AUTH_USER` / `GATEWAY_BASIC_AUTH_PASSWORD_HASH`). |
| **Loki Endpoint** | `https://loki.{$DOMAIN}` | HTTPS | Protected at the Gateway by Caddy Basic Auth (`GATEWAY_BASIC_AUTH_USER` / `GATEWAY_BASIC_AUTH_PASSWORD_HASH`). |

---

### C. Admin & Management Interfaces

| Service | Access URL | Protocol | Authentication & Credentials |
| :--- | :--- | :--- | :--- |
| **PgAdmin console** | `https://pgadmin.{$DOMAIN}` | HTTPS | 1. Caddy Gateway Basic Auth (if enabled).<br>2. PgAdmin credentials (`PGADMIN_EMAIL` / `PGADMIN_PASSWORD`). |
| **RedisInsight UI** | `https://redisinsight.{$DOMAIN}` | HTTPS | Protected at the Gateway by Caddy Basic Auth (`GATEWAY_BASIC_AUTH_USER` / `GATEWAY_BASIC_AUTH_PASSWORD_HASH`). |

---

### D. Internal Metrics (Telemetry Scraping)

> [!NOTE]
> These endpoints are only accessible internally inside the Docker bridge network (`backend`) or locally on the host machine (`127.0.0.1`). External internet traffic trying to access these directly will be blocked.

| Metrics Provider | Scrape URL | Protocol | Authentication |
| :--- | :--- | :--- | :--- |
| **backend-wsgi** | `http://backend-wsgi:8001/prom-metrics/` | HTTP | None (Internal network only). |
| **backend-asgi** | `http://backend-asgi:8002/prom-metrics/` | HTTP | None (Internal network only). |
| **backend-dev** | `http://backend-dev:8000/prom-metrics/` | HTTP | None (Internal network only). |
| **PostgreSQL Exporter** | `http://postgres-exporter:9187/metrics` | HTTP | None (Internal network only). |
| **Redis Exporter** | `http://redis-exporter:9121/metrics` | HTTP | None (Internal network only). |
