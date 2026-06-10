#!/bin/sh
set -e

echo "=== E2E: Waiting for database ==="
MAX_RETRIES=${DB_WAIT_RETRIES:-60}
RETRY=0

# Detect DB type from DATABASE_URL
case "${DATABASE_URL}" in
  mssql*)
    DB_CHECK_FILE=$(mktemp)
    # Wait for MSSQL server (connect to master — target DB may not exist yet)
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
    ;;
  *)
    DB_CHECK_FILE=$(mktemp)
    cat > "$DB_CHECK_FILE" << 'PYEOF'
import os, psycopg2
psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=5).close()
PYEOF
    ;;
esac

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
case "${DATABASE_URL}" in
  mssql*)
    # Create DB and user via sa account, then stamp+migrate
    echo "  Creating MSSQL database (if not exists)..."
    python << 'PYEOF'
import os, pyodbc, re, sys
url = os.environ.get("DATABASE_URL", "")
m = re.match(r"mssql\+pyodbc://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)", url)
if not m:
    sys.exit(f"Cannot parse MSSQL DATABASE_URL: {url}")
user, pwd, host, port, db = m.groups()
# Validate identifiers to prevent SQL injection accidents
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
# Escape single quotes in password for SQL literal
safe_pwd = pwd.replace("'", "''")
c.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name='{user}') CREATE LOGIN [{user}] WITH PASSWORD='{safe_pwd}'")
conn.close()
conn2 = pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};DATABASE={db};UID=sa;PWD={sa_pwd};Encrypt=no;TrustServerCertificate=yes", autocommit=True)
c2 = conn2.cursor()
c2.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name='{user}') BEGIN CREATE USER [{user}] FOR LOGIN [{user}]; ALTER ROLE db_owner ADD MEMBER [{user}]; END")
conn2.close()
print(f"  Database '{db}' ready.")
PYEOF
    flask db stamp head
    flask db migrate -m "e2e_mssql_auto"
    flask db upgrade
    ;;
  *)
    flask db upgrade
    ;;
esac

echo "=== E2E: Seeding test data ==="
python scripts/seed_dev.py

echo "=== E2E: Starting Flask dev server ==="
exec "$@"
