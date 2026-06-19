#!/bin/bash
# mssql-init/setup.sh
#
# Wait for MSSQL to become ready, then create the dev database with
# Czech collation and enable RCSI (Read Committed Snapshot Isolation),
# plus the application login/user.
#
# The MSSQL container has no auto-init directory (unlike PostgreSQL's
# docker-entrypoint-initdb.d), so this runs as the dedicated one-shot
# `db-init` service in docker-compose.yml — `docker compose up` invokes it
# automatically once the db container is healthy. No manual step required.
#
# Configurable via environment:
#   MSSQL_HOST         target server (default: localhost)
#   MSSQL_SA_PASSWORD  sa password   (default: DevPassword123!)
#   SQLCMD             sqlcmd path   (default: /opt/mssql-tools18/bin/sqlcmd)

set -e

SQLCMD="${SQLCMD:-/opt/mssql-tools18/bin/sqlcmd}"
MSSQL_HOST="${MSSQL_HOST:-localhost}"
SA_PASSWORD="${MSSQL_SA_PASSWORD:-DevPassword123!}"

echo "Waiting for SQL Server at ${MSSQL_HOST} to be ready..."
READY=false
for i in $(seq 1 30); do
    if $SQLCMD -S "$MSSQL_HOST" -U sa -P "$SA_PASSWORD" -C -Q "SELECT 1" &>/dev/null; then
        echo "SQL Server is ready."
        READY=true
        break
    fi
    echo "  Attempt $i/30 — not ready yet..."
    sleep 2
done

if [ "$READY" = "false" ]; then
    echo "ERROR: SQL Server did not become ready within 60 seconds. Exiting."
    exit 1
fi

echo "Creating database medcover_dev..."
$SQLCMD -S "$MSSQL_HOST" -U sa -P "$SA_PASSWORD" -C -Q "
IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'medcover_dev')
BEGIN
    CREATE DATABASE medcover_dev
        COLLATE Czech_100_CI_AS_SC_UTF8;
    ALTER DATABASE medcover_dev SET READ_COMMITTED_SNAPSHOT ON;
    PRINT 'Database medcover_dev created with RCSI enabled.';
END
ELSE
BEGIN
    PRINT 'Database medcover_dev already exists.';
END
"

echo "Creating login and user..."
$SQLCMD -S "$MSSQL_HOST" -U sa -P "$SA_PASSWORD" -C -Q "
IF NOT EXISTS (SELECT name FROM sys.server_principals WHERE name = 'medcover')
BEGIN
    CREATE LOGIN medcover WITH PASSWORD = 'Dev_Password1!';
    PRINT 'Login medcover created.';
END
ELSE
    PRINT 'Login medcover already exists.';
"

$SQLCMD -S "$MSSQL_HOST" -U sa -P "$SA_PASSWORD" -C -d medcover_dev -Q "
IF NOT EXISTS (SELECT name FROM sys.database_principals WHERE name = 'medcover')
BEGIN
    CREATE USER medcover FOR LOGIN medcover;
    ALTER ROLE db_owner ADD MEMBER medcover;
    PRINT 'User medcover created and added to db_owner.';
END
ELSE
    PRINT 'User medcover already exists.';
"

echo "✓ MSSQL setup complete."
echo "  Connection string for MedCover:"
echo "  DATABASE_URL=mssql+pyodbc://medcover:Dev_Password1!@127.0.0.1:1433/medcover_dev?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes"
echo ""
echo "  Note: Use 127.0.0.1 (not localhost) on macOS to avoid IPv6 issues."
