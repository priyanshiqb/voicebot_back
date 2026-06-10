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

Instead of multiple overridden files, the project uses a single `docker-compose.yml` configuration. Services are grouped using **Docker Compose Profiles** which are controlled dynamically via the `COMPOSE_PROFILES` environment variable inside your `.env.django.docker` file.

### Available Profiles:
1.  **`dev`**: Starts the Daphne development backend container (with local code mounting and hot-reloading).
2.  **`prod`**: Starts the production-grade split `backend-wsgi` and `backend-asgi` backend containers.
3.  **`databases`**: Runs PostgreSQL (with PgBouncer connection pooling and pgAdmin UI).
4.  **`caches`**: Runs Redis (for caching and channels layers, with RedisInsight dashboard).
5.  **`monitoring`**: Runs Prometheus (metrics), Loki (log aggregator), and Grafana (dashboards).
6.  **`emulators`**: Runs Azurite (local Azure Blob/Queue/Table storage emulator).
7.  **`gateway`**: Runs Caddy as the central reverse proxy.

---

## 3. Dual Environment Configuration Setup

To prevent port conflicts with local engines and handle standard resolution hostnames, the project splits environment configurations into two specialized files:

### A. Local Host Environment (`.env.django.local`)
Used when running Django **outside Docker** (e.g., standard debugging on your host machine via your local virtual environment). It maps connections to your forwarded host ports:
```env
# PostgreSQL URL pointing to 'localhost'
DATABASE_URL=postgres://voicebot_user:secure_password@localhost:25432/voicebot_db
REDIS_URL=redis://localhost:26379/0
```

### B. Container Environment (`.env.django.docker`)
Used when running Django **inside Docker containers**. It resolves backing databases using private bridge network container hosts:
```env
# PostgreSQL URL pointing to 'pgbouncer'
DATABASE_URL=postgres://postgres:secure_password@pgbouncer:5432/voicebot_db
REDIS_URL=redis://redis:6379/0
```

---

## 4. API Gateway Routing & Subdomains

When the `gateway` profile is active, Caddy exposes ports `80` and `443` on the host. By default, it routes the following domains (where `localhost` is the default value of the `DOMAIN` environment variable):

| Service | Address | Proxies To (Internal Network) |
| :--- | :--- | :--- |
| **Django WSGI / Dev** | `http://localhost` | `backend-wsgi:8000` (production HTTP) or `backend-dev:8000` (development) |
| **Django ASGI / Dev** | `http://localhost` (WebSocket headers) | `backend-asgi:8000` (production WS) or `backend-dev:8000` (development) |
| **pgAdmin** | `http://pgadmin.localhost` | `pgadmin:80` |
| **RedisInsight** | `http://redisinsight.localhost` | `redis-insight:5540` |
| **Grafana** | `http://grafana.localhost` | `grafana:3000` |
| **Prometheus** | `http://prometheus.localhost` | `prometheus:9090` |

*Note: For production, you can replace `localhost` by setting `DOMAIN=yourdomain.com` in your environment file, and Caddy will automatically manage Let's Encrypt SSL certificates using the configured `ACME_EMAIL`.*

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
docker compose --env-file .env.django.docker exec -u root backend-dev bash
```
