# Developer Guide: B2B Multi-Tenant Docker Orchestration

This guide details the high-performance containerization architecture of the Voicebot backend, featuring a unified single-file Compose configuration with profiles and a Caddy Reverse Proxy / API Gateway acting as a routing gate for the Split WSGI/ASGI services.

---

## 1. Core Architecture Overview

To simplify development, routing, and deployment workflows, all incoming application traffic goes through a single, high-performance gateway service:

*   **`caddy` (API Gateway & Reverse Proxy)**: Binds to ports `80` and `443`. It terminates SSL/TLS (in production) and forwards requests dynamically:
    *   Subdomain routing maps to administration tools (pgAdmin, RedisInsight) and observability suites (Grafana, Prometheus).
    *   The primary domain (e.g., `localhost` or your server's domain/IP) routes all HTTP/WebSocket requests to the backend Django servers.
*   **Django Application Servers**:
    *   **`backend-wsgi` (Production - HTTP)**: Runs Gunicorn (WSGI) on port `8000` for high-performance multi-process HTTP (REST APIs, Admin panel) request handling.
    *   **`backend-asgi` (Production - WebSockets)**: Runs Daphne (ASGI) on port `8000` dedicated to persistent WebSocket traffic.
    *   **`backend-dev` (Development - Unified)**: Runs Daphne on port `8000` handling both HTTP and WebSockets with volume mounts and hot-reloading for rapid local code iteration.

---

## 2. Unified Profile-Based Orchestration

Instead of multiple overridden files, the project uses a single `docker-compose.yml` configuration. Services are grouped using **Docker Compose Profiles** which are controlled dynamically via the `COMPOSE_PROFILES` environment variable inside your `.env` file.

### Available Profiles:
1.  **`dev`**: Starts the Daphne development backend container (with local code mounting and hot-reloading).
2.  **`prod`**: Starts the production-grade split `backend-wsgi` and `backend-asgi` backend containers.
3.  **`database`**: Runs PostgreSQL database container.
4.  **`pgbouncer`**: Runs PgBouncer connection pooler container.
5.  **`pgadmin`**: Runs pgAdmin web UI.
6.  **`pgexporter`**: Runs PostgreSQL metrics exporter.
7.  **`caches`**: Runs Redis cache layer container.
8.  **`redis-insight`**: Runs RedisInsight web UI.
9.  **`redis-exporter`**: Runs Redis metrics exporter.
10. **`grafana`**: Runs Grafana dashboards.
11. **`logs-monitoring`**: Runs Loki and Promtail collectors.
12. **`matrics-monitoring`**: Runs Prometheus metrics database.
13. **`emulators`**: Runs Azurite (local Azure storage emulator).
14. **`gateway`**: Runs Caddy as the central reverse proxy.
15. **`backend-docs`**: Runs Nginx/Docsify to serve the developer portal and formatted API/WS docs.

---

## 3. Dual Environment Configuration Setup

To prevent port conflicts with local engines and handle standard resolution hostnames, the project splits environment configurations into two specialized files:

### A. Local Host Environment (`.env.local`)
Used when running Django **outside Docker** (e.g., standard debugging on your host machine via your local virtual environment). It maps connections to your forwarded host ports:
```env
# PostgreSQL pointing to localhost exposed port
POSTGRES_HOST=localhost
POSTGRES_PORT=5579
REDIS_HOST=localhost
```

### B. Container Environment (`.env`)
Used when running Django **inside Docker containers**. It resolves backing databases using private bridge network container hosts:
```env
# PostgreSQL pointing to private network bridge hosts
POSTGRES_HOST=pgbouncer
POSTGRES_PORT=5432
REDIS_HOST=redis
```

---

## 4. API Gateway Routing & Subdomains

When the `gateway` profile is active, Caddy exposes ports `80` and `443` on the host. By default, it routes the following domains (where `localhost` is the default value of the `DOMAIN` environment variable):

| Service | Address | Proxies To (Internal Network) |
| :--- | :--- | :--- |
| **Django WSGI / Dev** | `{$SCHEME}api{$ENV_NAME}.localhost` | `backend-wsgi:8000` (production HTTP) or `backend-dev:8000` (development) |
| **Django ASGI / Dev** | `{$SCHEME}ws{$ENV_NAME}.localhost` (WebSocket headers) | `backend-asgi:8000` (production WS) or `backend-dev:8000` (development) |
| **pgAdmin** | `{$SCHEME}pgadmin{$ENV_NAME}.localhost` | `pgadmin:80` |
| **RedisInsight** | `{$SCHEME}redisinsight{$ENV_NAME}.localhost` | `redis-insight:5540` |
| **Grafana** | `{$SCHEME}grafana{$ENV_NAME}.localhost` | `grafana:3000` |
| **Prometheus** | `{$SCHEME}prometheus{$ENV_NAME}.localhost` | `prometheus:9090` |
| **Loki** | `{$SCHEME}loki{$ENV_NAME}.localhost` | `loki:3100` |
| **MinIO API** | `{$SCHEME}minio{$ENV_NAME}.localhost` | `minio:9000` |
| **MinIO Console** | `{$SCHEME}minio-console{$ENV_NAME}.localhost` | `minio:9001` |

*Note: For production/QA, set `ENV_NAME=qa` or `ENV_NAME=prod` to dynamically prefix your subdomains. Set `SCHEME="http://"` if you are placing this Caddy behind a Master Load Balancer that already handles SSL. Set `SCHEME="https://"` (or leave it blank) if you want this local Caddy to handle Let's Encrypt SSL certificates automatically. You can replace `localhost` by setting `DOMAIN=yourdomain.com` in your `.env`.*

---

## 5. Production Security Hardening

Our production-ready `Dockerfile` implements standard-compliant enterprise security features out-of-the-box:

1.  **Non-Privileged User (`USER django`)**: The container processes do **not** run as root. They execute inside a highly restricted, sandboxed system user profile to prevent container-breakout attacks.
2.  **Instant Logging (`PYTHONUNBUFFERED=1`)**: Disables Python logging buffers, ensuring that print statement outputs and server errors write instantly to your cloud console dashboard.
3.  **Build-Tool Purging**: Standard compilers (`gcc`, `libc-dev`) are utilized only during the image build stage to compile wheels, and are **cleanly uninstalled** right after to minimize the image's security attack surface.

---

## 6. Troubleshooting & Root Access

Because our backend container is hardened and runs as the non-root user `django` for security, running standard user switching (`su`) directly inside the container will prompt for a password that is intentionally unset.

To bypass password prompts and enter the container as **`root`** for hot-fixes, debugging, or custom logs, simply pass the `-u root` flag when connecting:

```bash
docker compose exec -u root backend-dev bash
```
