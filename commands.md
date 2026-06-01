# 🐳 Unified Developer Commands Cheatsheet

This cheatsheet provides all standard developer commands in a quick-copy format. Every code block is optimized to contain exactly one line for ease of copy-pasting, grouped logically by environment.

---

## 💡 Quick Overview of Docker Compose Files
* **`docker-compose.yml`**: Defines the single core unified Daphne backend application.
* **`docker-compose.override.yml`**: Extends the configuration for local development. By default, running `docker compose` loads **both** files together, spinning up the backend plus local PostgreSQL, PGAdmin, Redis, and RedisInsight.
* **To Bypass Override (Remote/Cloud DBs)**: Pass the `-f docker-compose.yml` flag to start *only* the Daphne application.

---

## 💻 1. Local Development (Outside Docker on Host Machine)
Use these commands when running Django locally directly on your system using Python/uv.

### Run Django System Check
```bash
uv run python manage.py check --settings=config.settings.local
```

### Make Django Migrations
```bash
uv run python manage.py makemigrations --settings=config.settings.local
```

### Run Django Migrations
```bash
uv run python manage.py migrate --settings=config.settings.local
```

### Create Django Superuser
```bash
uv run python manage.py createsuperuser --settings=config.settings.local
```

### Start Local ASGI Server (Daphne)
```bash
uv run daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

### Run Django Unit Tests Locally
```bash
uv run python manage.py test onboard --settings=config.settings.local
```

---

## 🚢 2. Docker Compose Commands

### Boot Local Development (Backend + Local PostgreSQL + Caching)
Loads both `docker-compose.yml` and `docker-compose.override.yml` collectively:
```bash
docker compose --env-file .env.django.docker up --build
```

### Boot Local Development in Background (Detached Mode)
```bash
docker compose --env-file .env.django.docker up -d --build
```

### Boot Daphne ONLY (Bypassing Local DBs / Using Remote Cloud DBs)
Loads *only* the primary compose file to disable Postgres/Redis containers:
```bash
docker compose --env-file .env.django.docker -f docker-compose.yml up --build
```

### Stop All Active Containers
```bash
docker compose --env-file .env.django.docker stop
```

### Tear Down Containers & Delete Local Volumes (Wipe Databases)
Stops containers and deletes local pgAdmin, Postgres, and Redis volumes to start fresh:
```bash
docker compose --env-file .env.django.docker down -v
```

### View Live Backend and DB Logs
```bash
docker compose --env-file .env.django.docker logs -f
```

---

## 📥 3. Operations Inside Running Docker Containers

### Scan for Model Changes inside Container (Makemigrations)
```bash
docker compose --env-file .env.django.docker exec backend python manage.py makemigrations
```

### Apply Database Migrations inside Container (Migrate)
```bash
docker compose --env-file .env.django.docker exec backend python manage.py migrate
```

### Create a Superuser inside Container
```bash
docker compose --env-file .env.django.docker exec backend python manage.py createsuperuser
```

### Run Django Unit Tests inside Container
```bash
docker compose --env-file .env.django.docker exec backend python manage.py test onboard
```

### Access PostgreSQL Shell (psql) inside Database Container
```bash
docker compose --env-file .env.django.docker exec postgres psql -U voicebot_user -d voicebot_db
```

### Access Redis Command Line (redis-cli) inside Redis Container
```bash
docker compose --env-file .env.django.docker exec redis redis-cli
```

---

## 🛠️ 4. Container Access & Diagnostics

### Gain Root Access inside the Daphne Container
Enter the backend container as `root` for administrative operations (installs, configs, etc.):
```bash
docker compose --env-file .env.django.docker exec -u root backend bash
```

### Gain Standard User Access inside the Daphne Container
```bash
docker compose --env-file .env.django.docker exec backend bash
```
