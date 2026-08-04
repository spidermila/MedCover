#!/bin/sh
set -e

echo "=== E2E: Waiting for database ==="
MAX_RETRIES=${DB_WAIT_RETRIES:-60}
RETRY=0

DB_CHECK_FILE=$(mktemp)
cat > "$DB_CHECK_FILE" << 'PYEOF'
import os, pyodbc, re, sys
url = os.environ.get("DATABASE_URL", "")
m = re.match(r"mssql\+pyodbc://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)", url)
if not m:
    sys.exit(f"Cannot parse MSSQL DATABASE_URL: {url}")
user, pwd, host, port, db = m.groups()
sa_pwd = os.environ.get("MSSQL_SA_PASSWORD", pwd)
pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};DATABASE=master;UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes", timeout=5).close()
PYEOF

until python "$DB_CHECK_FILE" 2>/dev/null; do
  RETRY=$((RETRY+1))
  if [ "$RETRY" -ge "$MAX_RETRIES" ]; then
    echo "  ...database not ready after ${MAX_RETRIES} retries, exiting"
    exit 1
  fi
  echo "  ...database not ready, retrying in 2s (attempt $RETRY/$MAX_RETRIES)"
  sleep 2
done
rm -f "$DB_CHECK_FILE"

echo "=== E2E: Running database migrations ==="
echo "  Creating MSSQL database (if not exists)..."
python << 'PYEOF'
import os, pyodbc, re, sys, time
url = os.environ.get("DATABASE_URL", "")
m = re.match(r"mssql\+pyodbc://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)", url)
if not m:
    sys.exit(f"Cannot parse MSSQL DATABASE_URL: {url}")
user, pwd, host, port, db = m.groups()
ident_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
if not ident_re.fullmatch(db):
    sys.exit(f"Invalid database name: {db}")
if not ident_re.fullmatch(user):
    sys.exit(f"Invalid username: {user}")
sa_pwd = os.environ.get("MSSQL_SA_PASSWORD", pwd)
conn = pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};DATABASE=master;UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes", autocommit=True)
c = conn.cursor()
c.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name='{db}') CREATE DATABASE [{db}] COLLATE Czech_100_CI_AS_SC_UTF8")
c.execute(f"ALTER DATABASE [{db}] SET READ_COMMITTED_SNAPSHOT ON")
safe_pwd = pwd.replace("'", "''")
c.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name='{user}') CREATE LOGIN [{user}] WITH PASSWORD='{safe_pwd}'")
conn.close()
# SET READ_COMMITTED_SNAPSHOT ON forces the database briefly offline while
# it waits for existing connections to drain; a race with our immediate
# reconnect surfaces as "Cannot open database ... The login failed. (4060)".
last_err = None
for attempt in range(30):
    try:
        conn2 = pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};DATABASE={db};UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes", autocommit=True, timeout=5)
        break
    except pyodbc.Error as e:
        last_err = e
        time.sleep(1)
else:
    sys.exit(f"Database '{db}' never became reachable after ALTER: {last_err}")
c2 = conn2.cursor()
c2.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name='{user}') BEGIN CREATE USER [{user}] FOR LOGIN [{user}]; ALTER ROLE db_owner ADD MEMBER [{user}]; END")
conn2.close()
print(f"  Database '{db}' ready.")
PYEOF
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
