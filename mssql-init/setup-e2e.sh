#!/bin/bash
# mssql-init/setup-e2e.sh
#
# Create the e2e test database in the MSSQL container.
# Run after the container is healthy:
#   podman exec <container> /docker-entrypoint-initdb.d/setup-e2e.sh

set -e

SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
SA_PASSWORD="E2e_Password1!"

echo "Waiting for SQL Server to be ready..."
for i in $(seq 1 30); do
    if $SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -Q "SELECT 1" &>/dev/null; then
        echo "SQL Server is ready."
        break
    fi
    echo "  Attempt $i/30 — not ready yet..."
    sleep 2
done

echo "Creating database medcover_e2e..."
$SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -Q "
IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'medcover_e2e')
BEGIN
    CREATE DATABASE medcover_e2e
        COLLATE Czech_100_CI_AS_SC_UTF8;
    ALTER DATABASE medcover_e2e SET READ_COMMITTED_SNAPSHOT ON;
    PRINT 'Database medcover_e2e created with RCSI enabled.';
END
ELSE
BEGIN
    PRINT 'Database medcover_e2e already exists.';
END
"

echo "Creating login and user..."
$SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -Q "
IF NOT EXISTS (SELECT name FROM sys.server_principals WHERE name = 'medcover')
BEGIN
    CREATE LOGIN medcover WITH PASSWORD = 'E2e_Password1!';
    PRINT 'Login medcover created.';
END
ELSE
    PRINT 'Login medcover already exists.';
"

$SQLCMD -S localhost -U sa -P "$SA_PASSWORD" -C -d medcover_e2e -Q "
IF NOT EXISTS (SELECT name FROM sys.database_principals WHERE name = 'medcover')
BEGIN
    CREATE USER medcover FOR LOGIN medcover;
    ALTER ROLE db_owner ADD MEMBER medcover;
    PRINT 'User medcover created and added to db_owner.';
END
ELSE
    PRINT 'User medcover already exists.';
"

echo "✓ MSSQL e2e setup complete."
