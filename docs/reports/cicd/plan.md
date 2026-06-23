# Technical Implementation Plan: CI/CD Pipeline for Dockerized Django on VPS

This document outlines the complete configuration blueprint for a continuous integration (CI) and continuous deployment (CD) pipeline using **GitHub Actions** to automatically test, build, and deploy the Django backend to a target VPS.

---

## 1. Directory Structure of Configurations

```text
.github/
└── workflows/
    └── deploy.yml          # GitHub Actions pipeline workflow definition
deploy/
└── scripts/
    ├── backup_db.sh        # Pre-deployment database backup script
    └── setup_vps.sh        # Initial VPS user & directory setup script
reports/
└── cicd/
    └── plan.md             # This design document
```

---

## 2. CI/CD Workflow: GitHub Actions Configuration

This workflow file will reside in `.github/workflows/deploy.yml`. It runs linting and Django unit tests on every Pull Request to `main`. When a PR is merged, it triggers deployment to the VPS via SSH.

```yaml
name: Django CI/CD Pipeline

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  # ----------------------------------------------------
  # Job 1: Test & Quality Checks (CI)
  # ----------------------------------------------------
  test:
    name: Run Django Test Suite
    runs-on: ubuntu-latest
    
    services:
      postgres:
        image: postgres:15-alpine
        env:
          POSTGRES_DB: voicebot_test_db
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgrespassword
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - name: Checkout Code
        uses: actions/checkout@v3

      - name: Install Python & Setup UV
        uses: astral-sh/setup-uv@v2
        with:
          python-version: "3.12"
          enable-cache: true

      - name: Install Dependencies
        run: |
          uv pip install -r pyproject.toml --system

      - name: Run Code Linters
        run: |
          uv pip install ruff
          uv run ruff check .

      - name: Run Django Tests
        env:
          POSTGRES_DB: voicebot_test_db
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgrespassword
          POSTGRES_HOST: localhost
          POSTGRES_PORT: 5432
          REDIS_HOST: localhost
          REDIS_PORT: 6379
          SECRET_KEY: test_secret_key_for_ci
          DEBUG: "False"
        run: |
          uv run python manage.py test --settings=config.settings.local

  # ----------------------------------------------------
  # Job 2: Build & Deploy via SSH (CD)
  # ----------------------------------------------------
  deploy:
    name: Deploy to Production VPS
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'

    steps:
      - name: Checkout Code
        uses: actions/checkout@v3

      - name: Executing remote SSH commands to deploy
        uses: appleboy/ssh-action@v0.1.10
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          port: ${{ secrets.VPS_SSH_PORT || 22 }}
          script: |
            # Navigate to deployment directory
            cd /var/www/voicebot_back

            # Backup DB before deploying new changes
            if [ -f ./deploy/scripts/backup_db.sh ]; then
              chmod +x ./deploy/scripts/backup_db.sh
              ./deploy/scripts/backup_db.sh
            fi

            # Pull latest changes
            git pull origin main

            # Build and deploy new containers in detached mode
            # NOTE: Migrations and collectstatic are handled automatically inside entrypoint.sh!
            docker compose up -d --build

            # Clean up dangling images to save disk space
            docker image prune -f
```

---

## 3. VPS Script Implementation

### A. Pre-Deployment Database Backup Script (`deploy/scripts/backup_db.sh`)
This script takes an automated dump of PostgreSQL before the deployment changes are pulled down.

```bash
#!/bin/bash
set -e

# Configuration
BACKUP_DIR="/var/backups/voicebot_db"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/voicebot_backup_${TIMESTAMP}.sql"

echo "Starting automated database backup..."
mkdir -p "${BACKUP_DIR}"

# Run pg_dump within the docker postgres container
docker compose exec -T db pg_dump -U postgres voicebot_db > "${BACKUP_FILE}"

# Compress backup
gzip "${BACKUP_FILE}"

# Keep only the last 7 backups to prevent filling up disk space
find "${BACKUP_DIR}" -type f -name "*.sql.gz" -mtime +7 -delete

echo "Backup created successfully: ${BACKUP_FILE}.gz"
```

### B. Initial VPS Environment Setup Script (`deploy/scripts/setup_vps.sh`)
Run this script manually once on your VPS to set directories, install Docker, and configure permissions.

```bash
#!/bin/bash
set -e

echo "Updating system packages..."
sudo apt-get update && sudo apt-get upgrade -y

echo "Installing Docker & git..."
sudo apt-get install -y docker.io docker-compose git-all

# Create deploy group and add active user
sudo groupadd -f deploy
sudo usermod -aG docker $USER
sudo usermod -aG deploy $USER

# Set up project deployment folder
sudo mkdir -p /var/www/voicebot_back
sudo chown -R $USER:deploy /var/www/voicebot_back
sudo chmod -R 775 /var/www/voicebot_back

echo "VPS directory permissions configured successfully."
```

---

## 4. Port Forwarding & Direct Container Routing

In this architecture, the Master Load Balancer (Master VM) is managed externally and we do not have access to it. It is pre-configured to forward all domain-mapped requests directly to our VPS IP address on a designated port (e.g., port `8000`).

### A. Docker Compose Port Binding
Our application container must bind directly to the port that is being forwarded to our VPS host. Add the port mapping directly to your base `docker-compose.yml` file:

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile
    command: daphne -b 0.0.0.0 -p 8000 config.asgi:application
    ports:
      - "8000:8000" # Expose container port 8000 to host port 8000 (forwarded port)
    env_file:
      - .env
```

### B. Self-Contained Static File Serving (WhiteNoise)
Since we do not have directory-sharing access to the Master VM, the Django application container must serve its own static files natively using WhiteNoise:
1. Ensure `whitenoise` is configured in `MIDDLEWARE` immediately below the Security middleware:
   ```python
   MIDDLEWARE = [
       "django.middleware.security.SecurityMiddleware",
       "whitenoise.middleware.WhiteNoiseMiddleware",
       # ...
   ]
   ```
2. Enable manifest storage in settings to manage cache-busting:
   ```python
   STORAGES = {
       "staticfiles": {
           "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
       },
   }
   ```

---

## 5. Security & Network Checklist for Production VPS

1. **Firewall Rules (`ufw`)**:
   - Enable UFW and restrict access to the designated forwarded port (e.g., `8000`) only to the IP address of the Master Load Balancer / Master VM.
   - Allow port `22` (SSH) only for deployment keys/authorized administrators.
2. **Django SSL Trust Header**:
   - Ensure the Master VM forwards the `X-Forwarded-Proto` header.
   - Configure Django in production (`settings/production.py`) to recognize this header to prevent redirect loops:
     ```python
     SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
     ```


