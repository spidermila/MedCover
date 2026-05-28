#!/bin/sh
set -e

echo "=== E2E: Waiting for database ==="
MAX_RETRIES=${DB_WAIT_RETRIES:-60}
RETRY=0
until python -c "
import socket, sys, os
url = os.environ.get('DATABASE_URL', '')
# Extract host:port from postgresql://user:pass@host:port/db
host_part = url.split('@')[1].split('/')[0] if '@' in url else 'db-e2e:5432'
host, port = host_part.rsplit(':', 1)
s = socket.socket(); s.settimeout(2)
s.connect((host, int(port))); s.close()
" 2>/dev/null; do
  RETRY=$((RETRY+1))
  if [ "$RETRY" -ge "$MAX_RETRIES" ]; then
    echo "  ...database not ready after ${MAX_RETRIES} retries, exiting"
    exit 1
  fi
  echo "  ...database not ready, retrying in 2s (attempt $RETRY/$MAX_RETRIES)"
  sleep 2
done

echo "=== E2E: Running database migrations ==="
flask db upgrade

echo "=== E2E: Seeding test data ==="
python scripts/seed_dev.py

echo "=== E2E: Starting Flask dev server ==="
exec "$@"
