# Developer Guide: B2B Multi-Tenant Docker Orchestration

This guide details the high-performance containerization architecture of the Voicebot backend, featuring **Unified Daphne Orchestration** for handling both HTTP and WebSockets collectively.

---

## 1. Core Architecture Overview

To simplify development and deployment workflows, all incoming application traffic goes through a single, high-performance service cluster:

*   **`api` (Unified Daphne Server)**: Runs **Daphne** on port `8000`. Because Daphne natively supports both standard HTTP protocol (REST APIs, Admin panels) and persistent real-time WebSocket protocol concurrently, a single port handles all operations seamlessly.

---

## 2. Multi-File Compose Setup (The "On/Off" DB Design)

We split Compose configurations into two files to allow easy toggling of local database containers.

*   **`docker-compose.yml` (Base File)**: Defines *only* the application server (`api` running Daphne). This is the exact configuration used in production environments.
*   **`docker-compose.override.yml` (Local Dev Override)**: Merged automatically during local runs. It spins up local Postgres and Redis databases, maps ports, and mounts volumes for hot-reloading.

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
# PostgreSQL URL pointing to 'postgres'
DATABASE_URL=postgres://voicebot_user:secure_password@postgres:5432/voicebot_db
REDIS_URL=redis://redis:6379/0
```

---

## 4. Terminal Commands Cheatsheet

### A. Working INSIDE this Backend Repository (`voicebot_back/`)

Use these commands when developing exclusively on the backend:

| Scenario | Command | Result |
| :--- | :--- | :--- |
| **Local Development**<br>*(Local Postgres & Redis ON)* | `docker compose --env-file .env.django.docker up --build` | Boots Daphne (port 8000), local Postgres, and local Redis collectively with **code hot-reloading**. |
| **Testing Cloud DBs**<br>*(Local Postgres & Redis OFF)* | `docker compose --env-file .env.django.docker -f docker-compose.yml up --build` | Boots Daphne only. Bypasses local DB containers and connects straight to your remote database. |
| **Tear Down Everything** | `docker compose --env-file .env.django.docker down -v` | Safely stops containers and clears local volumes. |

---

### B. Working at the MASTER Repository Level

When this repo is integrated as a submodule alongside the Frontend inside your Master repository:

| Scenario | Command | Result |
| :--- | :--- | :--- |
| **Local Unified Dev**<br>*(Local DBs ON)* | `docker compose --env-file .env.django.docker up --build` | Boots Frontend, Daphne (port 8000), Caddy, local Postgres, and local Redis together with **hot-reloading**. |
| **Production / Staging**<br>*(Local DBs OFF)* | `docker compose --env-file .env.django.docker -f docker-compose.yml up -d --build` | Boots Frontend, Daphne (port 8000), and Caddy. Connects cleanly to cloud-managed database instances. |

---

## 5. Production Security Hardening

Our production-ready `Dockerfile` implements standard-compliant enterprise security features out-of-the-box:

1.  **Non-Privileged User (`USER django`)**: The container processes do **not** run as root. They execute inside a highly restricted, sandboxed system user profile to prevent container-breakout attacks.
2.  **Instant Logging (`PYTHONUNBUFFERED=1`)**: Disables Python logging buffers, ensuring that print statement outputs and server errors write instantly to your cloud console dashboard.
3.  **Build-Tool Purging**: Standard compilers (`gcc`, `libc-dev`) are utilized only during the image build stage to compile wheels, and are **cleanly uninstalled** right after to minimize the image's security attack surface.

---

## 6. Troubleshooting & Root Access

Because our container is hardened and runs as the non-root user `django` for security, running standard user switching (`su`) directly inside the container will prompt for a password that is intentionally unset.

To bypass password prompts and enter the container as **`root`** for hot-fixes, debugging, or custom logs, simply pass the `-u root` flag when connecting:

```bash
docker compose --env-file .env.django.docker exec -u root api bash
```

