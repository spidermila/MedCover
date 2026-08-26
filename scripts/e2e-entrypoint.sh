#!/bin/sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== E2E: Waiting for database ==="
MAX_RETRIES=${DB_WAIT_RETRIES:-60}
RETRY=0

# stderr is silenced during retries so operators aren't flooded with pyodbc
# tracebacks while the server is still booting; on the final failure we drop
# the redirect so the actual error (parse failure, auth error, ...) is shown.
while ! python "$SCRIPT_DIR/e2e_wait_db.py" 2>/dev/null; do
  RETRY=$((RETRY+1))
  if [ "$RETRY" -ge "$MAX_RETRIES" ]; then
    echo "  ...database not ready after ${MAX_RETRIES} retries; final error:"
    python "$SCRIPT_DIR/e2e_wait_db.py" || true
    exit 1
  fi
  echo "  ...database not ready, retrying in 2s (attempt $RETRY/$MAX_RETRIES)"
  sleep 2
done

echo "=== E2E: Running database migrations ==="
echo "  Creating MSSQL database (if not exists)..."
python "$SCRIPT_DIR/e2e_init_db.py"
# Bring the fresh database up to the latest schema: applies the MSSQL baseline
# plus every later migration, in order (today that's just the baseline; any
# migrations added in the future are applied here too). (Previously this did a
# "stamp head + autogenerate" dance to work around PG-only migration history;
# that history has been squashed into one MSSQL baseline, so a plain upgrade —
# same as the production entrypoint — now creates the full schema.)
flask db upgrade

echo "=== E2E: Seeding test data ==="
python scripts/seed_dev.py

echo "=== E2E: Starting Flask dev server ==="
exec "$@"
