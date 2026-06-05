#!/bin/sh
set -e

# Wait for database using Python socket check to avoid netcat/nc dependency
echo "Waiting for database..."
python - <<EOF
import socket
import time
import os
import sys

host = os.environ.get("POSTGRES_HOST", "localhost")
try:
    port = int(os.environ.get("POSTGRES_PORT", 5432))
except ValueError:
    port = 5432

start_time = time.time()
while True:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect((host, port))
            break
    except Exception:
        if time.time() - start_time > 60:
            print("Timeout waiting for database.")
            sys.exit(1)
        time.sleep(0.2)
print("Database is ready!")
EOF

# Run database migrations
echo "Running migrations..."
python manage.py migrate --noinput

# Collect static files (only for production environments where DJANGO_DEBUG is False)
if [ "$DJANGO_DEBUG" != "True" ] && [ "$DJANGO_DEBUG" != "1" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
else
    echo "Local development: skipping collectstatic (DJANGO_DEBUG=True)."
fi

# Change media file ownership to current user 
chmod 775 /app/media



# Start server / execute CMD
exec "$@"
