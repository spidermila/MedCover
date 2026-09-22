"""
Backup and restore engine for MedCover.

Exports all application data (except app_settings and alembic_version) to a
JSON-in-zip archive, and restores from such an archive.

Archives are stored as blobs in Azure Blob Storage (Azurite locally), so the
web app and the scheduler — separate containers — see the same backups.

Backup blob layout:
    medcover_backup_<YYYYMMDD>_<HHMMSS>_<micros>_UTC.zip
        └── backup.json
              {
                "version": "1.0",
                "schema_version": "<alembic head revision>",
                "exported_at": "<ISO-8601 UTC>",
                "tables": {
                  "<table_name>": [ {col: val, ...}, ... ],
                  ...
                }
              }

Schema-version safety
---------------------
The JSON format stores rows as dicts keyed by column name.  On restore we
INSERT only the columns that exist in the *current* schema, ignoring any
extra columns from an older or newer backup.  This means:
- New nullable columns added by later migrations receive NULL (acceptable).
- Removed columns in the backup are silently skipped.
- Restoring to a schema that added NOT NULL columns without defaults will
  fail at the DB level — the restore routine surfaces this as an error.
"""

import functools
import json
import logging
import os
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContainerClient

from app.extensions import db

log = logging.getLogger(__name__)

# Tables excluded from backup.  app_settings holds server-specific config
# (SMTP creds, base URL, etc.) that must be set up fresh on every instance.
# alembic_version is managed by Flask-Migrate, not by the app.
_EXCLUDED_TABLES: frozenset[str] = frozenset({"app_settings", "alembic_version"})

# Tables that must be restored in a specific order to satisfy FK constraints.
# Tables not listed here are restored after these, in arbitrary order.
_RESTORE_ORDER: list[str] = [
    "role",
    "user_account",
    "master_event",
    "event",
    "event_spot",
    "event_template",
    "event_spot_template",
    "qualification",
    "equipment_type",
    "equipment_item",
    # M2M / association tables last
    "user_roles",
    "user_qualifications",
    "qualification_parents",
    "spot_qualifications",
    "spot_template_qualifications",
    "event_equipment_plan",
    "event_template_equipment_plan",
    # Leaf tables
    "assignment",
    "outbox_email",
    "registration_invite",
    "audit_log_entry",
    "user_feedback",
    "debriefing_record",
    "digest_schedule",
    "digest_block",
    "digest_metric_snapshot",
]


_BACKUP_PREFIX = "medcover_backup_"

# Small single-shot sizes keep uploads/downloads chunked (the SDK reads a
# single-put body fully into memory), and bounded timeouts/retries stop a
# hanging storage endpoint from stalling the single-threaded scheduler loop.
_CLIENT_OPTIONS: dict[str, Any] = {
    "max_single_put_size": 4 * 1024 * 1024,
    "max_single_get_size": 4 * 1024 * 1024,
    "connection_timeout": 10,
    "read_timeout": 60,
    "retry_total": 2,
}


@functools.cache
def _container() -> ContainerClient:
    """Return the blob container holding the backups.

    Production authenticates with the managed identity (``BACKUP_CONTAINER_URL``
    + ``AZURE_CLIENT_ID``, picked up by DefaultAzureCredential). Local dev, CI
    and e2e point ``BACKUP_STORAGE_CONNECTION_STRING`` at Azurite. Credential
    selection is the only difference between the two; everything else shares
    one code path.
    """
    conn_str = os.environ.get("BACKUP_STORAGE_CONNECTION_STRING")
    url = os.environ.get("BACKUP_CONTAINER_URL")
    if bool(conn_str) == bool(url):
        raise RuntimeError("Set exactly one of BACKUP_CONTAINER_URL or BACKUP_STORAGE_CONNECTION_STRING.")
    if url:
        from azure.identity import DefaultAzureCredential  # pylint: disable=import-outside-toplevel

        return ContainerClient.from_container_url(url, credential=DefaultAzureCredential(), **_CLIENT_OPTIONS)

    client = ContainerClient.from_connection_string(
        conn_str, os.environ.get("BACKUP_CONTAINER_NAME", "backups"), **_CLIENT_OPTIONS
    )
    # Only in connection-string (Azurite) mode: the production container is
    # provisioned by infrastructure and the identity may not create containers.
    try:
        client.create_container()
    except ResourceExistsError:
        pass
    return client


def _get_alembic_head() -> str:
    """Return the current alembic revision stored in the DB.

    Uses a dedicated connection so that a missing alembic_version table
    (e.g. in test worker DBs created via create_all) doesn't abort the
    ORM session's transaction.
    """
    try:
        with db.engine.connect() as conn:
            row = conn.execute(sa.text("SELECT TOP 1 version_num FROM alembic_version")).fetchone()
            return str(row[0]) if row else "unknown"
    except Exception:
        return "unknown"


def _serialize_value(val: Any) -> Any:
    """Convert non-JSON-serialisable types to strings."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if hasattr(val, "isoformat"):  # date
        return val.isoformat()
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    # UUID and other types with __str__ that aren't natively JSON-serialisable
    if isinstance(val, uuid.UUID):
        return str(val)
    return val


def export_to_zip(now: datetime | None = None) -> str:
    """Export all application data to a timestamped backup blob.

    Args:
        now: Reference timestamp for the blob name (default: current UTC time).

    Returns:
        Name of the created blob.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    inspector = sa.inspect(db.engine)
    all_tables = [t for t in inspector.get_table_names() if t not in _EXCLUDED_TABLES]

    tables_data: dict[str, list[dict]] = {}
    for table_name in all_tables:
        rows = db.session.execute(sa.text(f'SELECT * FROM "{table_name}"')).fetchall()
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        tables_data[table_name] = [{col: _serialize_value(val) for col, val in zip(columns, row)} for row in rows]

    payload = {
        "version": "1.0",
        "schema_version": _get_alembic_head(),
        "exported_at": now.isoformat(),
        "tables": tables_data,
    }

    # Timestamp is always UTC so names sort chronologically regardless of the
    # app's configured timezone. The explicit ``_UTC`` suffix makes the zone
    # unambiguous when archives are copied elsewhere.
    ts = now.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    name = f"{_BACKUP_PREFIX}{ts}_UTC.zip"

    # Compress into a temp file rather than memory so a large export stays out
    # of the gunicorn worker's RAM. The upload is a single commit, so a failure
    # never leaves a half-written blob visible; overwrite=False turns a name
    # collision into an error instead of silently replacing a backup.
    with tempfile.TemporaryFile() as f:
        with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("backup.json", json.dumps(payload, ensure_ascii=False, indent=2))
        f.seek(0)
        _container().upload_blob(name, f, overwrite=False)

    total_rows = sum(len(v) for v in tables_data.values())
    log.info("Backup written to %s (%d tables, %d rows)", name, len(all_tables), total_rows)
    return name


@contextmanager
def downloaded_backup(name: str) -> Iterator[Path]:
    """Download backup blob *name* to a temp file and yield its path.

    Raises azure.core.exceptions.ResourceNotFoundError when the blob is missing.
    """
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    try:
        with os.fdopen(fd, "wb") as f:
            _container().download_blob(name).readinto(f)
        yield Path(tmp)
    finally:
        os.unlink(tmp)


def delete_backup(name: str) -> None:
    """Delete backup blob *name*."""
    _container().delete_blob(name)


def restore_from_zip(zip_path: str | Path) -> None:
    """Restore the database from a backup zip file.

    This is a **destructive** operation: all rows in all non-excluded tables
    are deleted before the backup data is loaded.  Runs inside a single
    transaction; rolls back on any error.

    Args:
        zip_path: Path to the zip file produced by export_to_zip().

    Raises:
        ValueError: If the zip does not contain a valid backup.json.
        Exception:  Any DB error encountered during restore.
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        if "backup.json" not in zf.namelist():
            raise ValueError(f"{zip_path.name} does not contain backup.json")
        payload = json.loads(zf.read("backup.json").decode("utf-8"))

    tables_data: dict[str, list[dict]] = payload.get("tables", {})
    schema_version: str = payload.get("schema_version", "unknown")
    log.info(
        "Starting restore from %s (schema_version=%s, exported_at=%s)",
        zip_path.name,
        schema_version,
        payload.get("exported_at"),
    )

    current_schema_version = _get_alembic_head()
    if schema_version != current_schema_version:
        log.warning(
            "Schema version mismatch: backup=%s, current=%s — proceeding with best-effort restore",
            schema_version,
            current_schema_version,
        )

    # Determine restore order: prioritised tables first, remainder after.
    ordered = [t for t in _RESTORE_ORDER if t in tables_data]
    remainder = [t for t in tables_data if t not in set(ordered)]
    restore_sequence = ordered + remainder

    # Close any open session transaction to release AccessShareLocks before
    # TRUNCATE (which needs AccessExclusiveLock).
    db.session.commit()

    # Everything — schema inspection, TRUNCATE, and INSERTs — runs on a single
    # dedicated connection so that no second connection can be blocked by the
    # TRUNCATE's AccessExclusiveLock.
    with db.engine.connect() as conn:
        # Inspect via the same connection so schema reads share the transaction.
        inspector = sa.inspect(conn)

        all_table_names = inspector.get_table_names()
        tables_to_clear = [t for t in all_table_names if t not in _EXCLUDED_TABLES]

        # Pre-collect column info before clearing the tables.
        # Also track which tables have IDENTITY columns (MSSQL requires
        # SET IDENTITY_INSERT ON to insert explicit values into them).
        _col_cache = {t: sa.inspect(db.engine).get_columns(t) for t in all_table_names if t not in _EXCLUDED_TABLES}
        current_columns_map: dict[str, set[str]] = {
            t: {str(col["name"]) for col in cols} for t, cols in _col_cache.items()
        }
        identity_tables: set[str] = {
            t for t, cols in _col_cache.items() if any(col.get("autoincrement") for col in cols)
        }

        # Reflect table schemas so SQLAlchemy binds INSERT parameters with the
        # correct MSSQL column types (varchar(max) / nvarchar(max) for Text
        # columns).  Without this, sa.text() + raw dict hands pyodbc plain
        # Python strings and pyodbc infers the legacy 'text' type, which MSSQL
        # rejects when the database collation is _UTF8.
        sa_metadata = sa.MetaData()
        sa_metadata.reflect(bind=db.engine, only=tables_to_clear)

        if tables_to_clear:
            preparer = db.engine.dialect.identifier_preparer
            # Clear every table by toggling FK constraints off, DELETE-ing all
            # rows, then re-enabling the constraints — rather than relying on
            # ON DELETE CASCADE. Reasons:
            #   * The schema's FKs are not declared with ON DELETE CASCADE, so a
            #     plain DELETE on a referenced parent would fail. We want to wipe
            #     *all* tables regardless of their FK topology.
            #   * TRUNCATE can't be used on tables referenced by a FK in MSSQL.
            #   * NOCHECK CONSTRAINT ALL lets us DELETE in any order without
            #     having to topologically sort the dependency graph; CHECK
            #     CONSTRAINT ALL restores enforcement afterwards.
            for t in tables_to_clear:
                qt = preparer.quote(t)
                conn.execute(sa.text(f"ALTER TABLE {qt} NOCHECK CONSTRAINT ALL"))
            for t in tables_to_clear:
                qt = preparer.quote(t)
                conn.execute(sa.text(f"DELETE FROM {qt}"))
            for t in tables_to_clear:
                qt = preparer.quote(t)
                conn.execute(sa.text(f"ALTER TABLE {qt} CHECK CONSTRAINT ALL"))

        # Columns typed as LargeBinary were exported as hex strings by
        # _serialize_value; decode them back to bytes here so pyodbc binds
        # them as VARBINARY parameters.
        binary_columns_by_table: dict[str, set[str]] = {}
        for t, sa_table in sa_metadata.tables.items():
            bin_cols = {col.name for col in sa_table.columns if isinstance(col.type, sa.LargeBinary)}
            if bin_cols:
                binary_columns_by_table[t] = bin_cols

        # Re-insert rows, skipping columns that no longer exist in the schema.
        for table_name in restore_sequence:
            rows = tables_data.get(table_name, [])
            if not rows:
                continue
            current_columns = current_columns_map.get(table_name)
            if current_columns is None:
                log.warning("Table %r in backup does not exist in current schema — skipping", table_name)
                continue
            qt = preparer.quote(table_name)
            has_identity = table_name in identity_tables
            sa_table = sa_metadata.tables[table_name]
            binary_cols = binary_columns_by_table.get(table_name, set())
            if has_identity:
                conn.execute(sa.text(f"SET IDENTITY_INSERT {qt} ON"))
            for row in rows:
                filtered = {k: v for k, v in row.items() if k in current_columns}
                for col_name in binary_cols:
                    val = filtered.get(col_name)
                    if isinstance(val, str):
                        try:
                            filtered[col_name] = bytes.fromhex(val)
                        except ValueError as exc:
                            raise ValueError(
                                f"Malformed binary data for {table_name}.{col_name} in backup "
                                f"{zip_path.name!r}: not a valid hex string"
                            ) from exc
                if filtered:
                    conn.execute(sa_table.insert().values(filtered))
            if has_identity:
                conn.execute(sa.text(f"SET IDENTITY_INSERT {qt} OFF"))

        conn.commit()

        # Reset sequences so future INSERTs don't collide with restored IDs.
        # PostgreSQL uses sequences; MSSQL uses IDENTITY — reseed via DBCC.
        for table_name in tables_to_clear:
            try:
                qt = preparer.quote(table_name)
                # Reseed MSSQL IDENTITY columns to max(pk) so future INSERTs don't collide
                pk_cols = inspector.get_pk_constraint(table_name).get("constrained_columns", [])
                for pk in pk_cols:
                    col_info = next((c for c in inspector.get_columns(table_name) if c["name"] == pk), None)
                    if col_info and col_info.get("autoincrement", False):
                        qpk = preparer.quote(pk)
                        max_id = conn.execute(sa.text(f"SELECT COALESCE(MAX({qpk}), 0) FROM {qt}")).scalar()
                        # Only reseed when the table actually has restored rows.
                        # DBCC CHECKIDENT(..., RESEED, 0) on an EMPTY table makes the
                        # *next* insert use the reseed value directly (0) rather than
                        # reseed+increment — a MSSQL quirk — which would hand out an
                        # invalid id=0 PK. Empty tables have nothing to collide with,
                        # so skipping the reseed is both safe and correct.
                        if max_id is not None and int(max_id) > 0:
                            conn.execute(
                                sa.text(f"DBCC CHECKIDENT('{table_name}', RESEED, :max_id)"),
                                {"max_id": int(max_id)},
                            )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                log.debug("Could not reset sequence for %s: %s", table_name, exc)

    # Expire the ORM session so subsequent queries see the freshly restored data.
    db.session.expire_all()
    log.info("Restore from %s complete", zip_path.name)


def prune_old_backups(keep_count: int) -> list[str]:
    """Delete the oldest backup blobs, keeping at most *keep_count*.

    Returns:
        Names of the deleted blobs.
    """
    # Names embed a UTC timestamp, so name order is creation order — stable
    # even if a blob is re-uploaded (which would reset creation_time).
    names = sorted(b.name for b in _container().list_blobs(name_starts_with=_BACKUP_PREFIX))
    deleted = []
    for name in names[: max(0, len(names) - keep_count)]:
        # Web workers and the scheduler prune the same container; a concurrent
        # prune may already have removed this one.
        try:
            delete_backup(name)
        except ResourceNotFoundError:
            continue
        deleted.append(name)
        log.info("Pruned old backup: %s", name)
    return deleted


def list_backups() -> list[dict]:
    """Return metadata for all backup blobs, newest first.

    Each entry: {name, size_bytes, created_at (datetime UTC)}
    """
    blobs = sorted(_container().list_blobs(name_starts_with=_BACKUP_PREFIX), key=lambda b: b.name, reverse=True)
    return [
        {
            "name": b.name,
            "size_bytes": b.size,
            "created_at": b.creation_time.astimezone(timezone.utc),
        }
        for b in blobs
    ]
