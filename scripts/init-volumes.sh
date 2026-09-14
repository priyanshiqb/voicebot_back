#!/usr/bin/env bash

set -euo pipefail

# Require script to be run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (or use sudo)"
  exit 1
fi

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Initializing Docker volume directories..."

mkdir -p "$BASE_DIR/.data/postgres"
mkdir -p "$BASE_DIR/.data/pgadmin"
mkdir -p "$BASE_DIR/.data/grafana"
mkdir -p "$BASE_DIR/.data/loki"
mkdir -p "$BASE_DIR/.data/redis"
mkdir -p "$BASE_DIR/.data/redisinsight"
mkdir -p "$BASE_DIR/.data/prometheus"
mkdir -p "$BASE_DIR/.data/caddy/data"
mkdir -p "$BASE_DIR/.data/caddy/config"
mkdir -p "$BASE_DIR/.data/azurite"
mkdir -p "$BASE_DIR/.data/alloy"

echo "Setting ownership..."

# PostgreSQL (official image)
chown -R 999:999 "$BASE_DIR/.data/postgres" || true

# pgAdmin (UID 5050 for dpage/pgadmin4)
chown -R 5050:5050 "$BASE_DIR/.data/pgadmin" || true

# Grafana (UID 472)
chown -R 472:472 "$BASE_DIR/.data/grafana" || true

# Loki (UID 10001)
chown -R 10001:10001 "$BASE_DIR/.data/loki" || true

# RedisInsight (Commonly UID 1000)
chown -R 1000:1000 "$BASE_DIR/.data/redisinsight" || true

# Prometheus (UID 65534 nobody)
chown -R 65534:65534 "$BASE_DIR/.data/prometheus" || true

# Azurite (node image, runs as root by default; 1000 keeps it writable if that changes)
chown -R 1000:1000 "$BASE_DIR/.data/azurite" || true

echo "Setting permissions..."

# Base read/execute for all
chmod -R 755 "$BASE_DIR/.data"

# These services frequently need wide write access for their internal non-root users
chmod -R 775 "$BASE_DIR/.data/grafana"
chmod -R 775 "$BASE_DIR/.data/loki"
chmod -R 775 "$BASE_DIR/.data/pgadmin"
chmod -R 775 "$BASE_DIR/.data/prometheus"
chmod -R 775 "$BASE_DIR/.data/azurite"

echo ""
echo "Directory status:"
ls -ld "$BASE_DIR/.data"/*

echo ""
echo "Initialization complete. You can now start the services:"
echo "docker compose --profile prod --profile gateway up -d"
