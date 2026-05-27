# Quick-Copy Developer Commands Cheatsheet

This cheatsheet provides all standard developer commands in quick-copy format. **Every code block contains exactly one line**, allowing you to double-click or click "Copy" to instantly execute them.

---

## 1. Local Development (Outside Docker on Host Machine)

### Run Django System Check
Verify that all settings, middlewares, and dependencies compile cleanly:
```bash
uv run python manage.py check --settings=config.settings.local
```

### Make Django Migrations
Scan your models for updates and create database migration files:
```bash
uv run python manage.py makemigrations --settings=config.settings.local
```

### Run Django Migrations
Apply pending database migrations to your PostgreSQL engine:
```bash
uv run python manage.py migrate --settings=config.settings.local
```

### Create a Superuser
Create an administrative account to access the Django Admin console:
```bash
uv run python manage.py createsuperuser --settings=config.settings.local
```

### Start the Local Server (Daphne)
Launch the unified Daphne server locally for HTTP and WebSockets:
```bash
uv run daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

---

## 2. Docker Compose Commands

### Boot Local Development (Local DBs ON)
Start Daphne, PostgreSQL, Redis, pgAdmin, and RedisInsight collectively:
```bash
docker compose --env-file .env.django.docker up --build
```

### Boot Local Development in Background
Start all containers in detached mode to keep your terminal free:
```bash
docker compose --env-file .env.django.docker up -d --build
```

### Boot with Cloud/Remote DBs (Local DBs OFF)
Start only the Daphne server, bypassing local database containers:
```bash
docker compose --env-file .env.django.docker -f docker-compose.yml up --build
```

### Stop All Containers
Stop all active services:
```bash
docker compose --env-file .env.django.docker stop
```

### Tear Down and Delete Volumes
Stop all active services and completely wipe local database/Redis volumes:
```bash
docker compose --env-file .env.django.docker down -v
```

### View Live Logs
View real-time console print statements and server logs:
```bash
docker compose --env-file .env.django.docker logs -f
```

---

## 3. Operations Inside Running Docker Containers

### Run Database Migrations in Container
Apply database migrations inside the running API container:
```bash
docker compose --env-file .env.django.docker exec api python manage.py migrate
```

### Create a Django Superuser in Container
Create an admin user inside the running API container:
```bash
docker compose --env-file .env.django.docker exec api python manage.py createsuperuser
```

### Access PostgreSQL Shell (psql)
Open an interactive database command line inside the Postgres container:
```bash
docker compose --env-file .env.django.docker exec postgres psql -U voicebot_user -d voicebot_db
```

### Access Redis Command Line (redis-cli)
Open an interactive caching command line inside the Redis container:
```bash
docker compose --env-file .env.django.docker exec redis redis-cli
```

---

## 4. Troubleshooting & Root Access

### Gain ROOT Access Inside the API Container
Our containers run as a non-root system user (`django`) for production security. Use this command to bypass password prompts and enter the container as `root`:
```bash
docker compose --env-file .env.django.docker exec -u root api bash
```

