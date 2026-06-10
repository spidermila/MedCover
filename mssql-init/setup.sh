#!/bin/bash
# mssql-init/setup.sh
#
# Wait for MSSQL to become ready, then create the dev database with
# Czech collation and enable RCSI (Read Committed Snapshot Isolation).
#
# This script is NOT run automatically by the MSSQL container (unlike
# PostgreSQL's docker-entrypoint-initdb.d). You need to run it manually
# after first startup:
#
#   docker compose -f docker-compose.mssql.yml exec mssql /docker-entrypoint-initdb.d/setup.sh

set -e

SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
SA_PASSWORD="DevPassword123!"

echo "Waiting for SQL Server to be ready..."
for i in $(seq 1 30); do
    if $SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -Q "SELECT 1" &>/dev/null; then
        echo "SQL Server is ready."
        break
    fi
    echo "  Attempt $i/30 — not ready yet..."
    sleep 2
done

echo "Creating database medcover_dev..."
$SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -Q "
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
$SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -Q "
IF NOT EXISTS (SELECT name FROM sys.server_principals WHERE name = 'medcover')
BEGIN
    CREATE LOGIN medcover WITH PASSWORD = 'Dev_Password1!';
    PRINT 'Login medcover created.';
END
ELSE
    PRINT 'Login medcover already exists.';
"

$SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -d medcover_dev -Q "
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
