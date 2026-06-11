# 🐳 Unified Developer Commands Cheatsheet

This cheatsheet provides all standard developer commands in a quick-copy format. Every code block is optimized to contain exactly one line for ease of copy-pasting, grouped logically by environment.

---

## 💡 Quick Overview of Docker Compose Files
* **`docker-compose.yml`**: The single, unified configuration file managing all environments (development, production, databases, caches, monitoring, and storage emulation).
* **Docker Compose Profiles**: Controls which services spin up. Profiles are managed inside `.env.django.docker` using the `COMPOSE_PROFILES` environment variable:
  - `dev`: Daphne development server (with code hot-reloading).
  - `prod`: Gunicorn production workers (no local mounts).
  - `databases`: Runs PgBouncer, PostgreSQL, and pgAdmin.
  - `caches`: Runs Redis and RedisInsight.
  - `monitoring`: Runs Loki, Prometheus, and Grafana.
  - `emulators`: Runs Azurite.
  - `gateway`: Runs Caddy reverse proxy for subdomain/domain routing.

---

## 💻 1. Local Development (Outside Docker on Host Machine)
Use these commands when running Django locally directly on your system using Python/uv.

### Run Django System Check
```bash
uv run python manage.py check
```

### Make Django Migrations
```bash
uv run python manage.py makemigrations
```

### Run Django Migrations
```bash
uv run python manage.py migrate
```

### Create Django Superuser
```bash
uv run python manage.py createsuperuser
```

### Start Local ASGI Server (Daphne)
```bash
uv run daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

### Run Django Unit Tests Locally
```bash
uv run python manage.py test onboard
```

### Seed Database (All Fixtures) Locally
```bash
uv run python manage.py load_data all
```

### Seed Specific Model Fixture Locally
```bash
uv run python manage.py load_data plan
```

### Generate Secure Production Credentials & Hashes
```bash
python docker-config/generate_secrets.py
```

---

## 🚢 2. Docker Compose Commands

### Boot Local Development (Backend + Local PostgreSQL + Caching + Monitoring + Emulators + Gateway)
Ensure `COMPOSE_PROFILES=dev,databases,caches,monitoring,emulators,gateway` is set in `.env.django.docker`:
```bash
docker compose --env-file .env.django.docker --env-file .env.shared up --build
```

### Boot Local Development in Background (Detached Mode)
```bash
docker compose --env-file .env.django.docker --env-file .env.shared up -d --build
```

### Boot Daphne ONLY (Bypassing Local DBs / Caches / Monitoring / Gateway)
Change `COMPOSE_PROFILES` to `dev` in `.env.django.docker` or run explicitly:
```bash
docker compose --env-file .env.django.docker --env-file .env.shared --profile dev up --build
```

### Boot Production Environment (Split WSGI Gunicorn + ASGI Daphne + Gateway)
Change `COMPOSE_PROFILES` to `prod,databases,caches,monitoring,gateway` (or omitting local DB profiles if using cloud DBs) and run:
```bash
docker compose --env-file .env.django.docker --env-file .env.shared up --build
```

### Stop All Active Containers
```bash
docker compose --env-file .env.django.docker --env-file .env.shared stop
```

### Tear Down Containers & Delete Local Volumes (Wipe Databases)
Stops containers and deletes local pgAdmin, Postgres, and Redis volumes to start fresh:
```bash
docker compose --env-file .env.django.docker --env-file .env.shared down -v
```

### View Live Backend and DB Logs
```bash
docker compose --env-file .env.django.docker --env-file .env.shared logs -f
```

---

## 📥 3. Operations Inside Running Docker Containers

> [!NOTE]
> Replace `backend-dev` with `backend-wsgi` (or `backend-asgi` as needed) if you are executing commands inside the production environment container.

### Scan for Model Changes inside Container (Makemigrations)
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev python manage.py makemigrations
```

### Apply Database Migrations inside Container (Migrate)
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev python manage.py migrate
```

### Create a Superuser inside Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev python manage.py createsuperuser
```

### Run Django Unit Tests inside Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev python manage.py test onboard
```

### Seed Database (All Fixtures) inside Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev python manage.py load_data all
```

### Seed Specific Model Fixture inside Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev python manage.py load_data plan
```


### Access PostgreSQL Shell (psql) inside Database Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec postgres psql -U postgres -d voicebot_db
```

### Access Redis Command Line (redis-cli) inside Redis Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec redis redis-cli
```

---

## 🛠️ 4. Container Access & Diagnostics

### Gain Root Access inside the Daphne Container
Enter the backend container as `root` for administrative operations (installs, configs, etc.):
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec -u root backend-dev bash
```

### Gain Standard User Access inside the Daphne Container
```bash
docker compose --env-file .env.django.docker --env-file .env.shared exec backend-dev bash
```
