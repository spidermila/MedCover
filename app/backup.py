"""
Backup and restore engine for MedCover.

Exports all application data (except app_settings and alembic_version) to a
JSON-in-zip archive, and restores from such an archive.

Backup file layout:
    medcover_backup_<YYYYMMDD_HHMMSS>.zip
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
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa

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
    "permission",
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
    "role_permissions",
    "user_roles",
    "user_qualifications",
    "qualification_parents",
    "spot_qualifications",
    "spot_template_qualifications",
    "event_equipment_plan",
    "event_equipment_assignment",
    "event_template_equipment_plan",
    # Leaf tables
    "assignment",
    "registration_invite",
    "outbox_email",
    "audit_log_entry",
    "user_feedback",
    "debriefing_record",
    "digest_schedule",
    "digest_block",
    "digest_metric_snapshot",
]


def _get_alembic_head() -> str:
    """Return the current alembic revision stored in the DB.

    Uses a dedicated connection so that a missing alembic_version table
    (e.g. in test worker DBs created via create_all) doesn't abort the
    ORM session's transaction.
    """
    try:
        with db.engine.connect() as conn:
            row = conn.execute(sa.text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
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
    import uuid
    if isinstance(val, uuid.UUID):
        return str(val)
    return val


def export_to_zip(backup_dir: str | Path, now: datetime | None = None) -> Path:
    """Export all application data to a timestamped zip file.

    Args:
        backup_dir: Directory where the zip will be written (created if absent).
        now:        Reference timestamp for the filename (default: current UTC time).

    Returns:
        Path to the created zip file.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    inspector = sa.inspect(db.engine)
    all_tables = [t for t in inspector.get_table_names() if t not in _EXCLUDED_TABLES]

    tables_data: dict[str, list[dict]] = {}
    for table_name in all_tables:
        rows = db.session.execute(sa.text(f'SELECT * FROM "{table_name}"')).fetchall()
        columns = [col["name"] for col in inspector.get_columns(table_name)]
        tables_data[table_name] = [
            {col: _serialize_value(val) for col, val in zip(columns, row)}
            for row in rows
        ]

    payload = {
        "version": "1.0",
        "schema_version": _get_alembic_head(),
        "exported_at": now.isoformat(),
        "tables": tables_data,
    }

    ts = now.strftime("%Y%m%d_%H%M%S_%f")
    zip_path = backup_path / f"medcover_backup_{ts}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup.json", json.dumps(payload, ensure_ascii=False, indent=2))
    zip_path.write_bytes(buf.getvalue())

    total_rows = sum(len(v) for v in tables_data.values())
    log.info("Backup written to %s (%d tables, %d rows)", zip_path, len(all_tables), total_rows)
    return zip_path


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

        # Pre-collect column info before TRUNCATE acquires AccessExclusiveLock.
        current_columns_map: dict[str, set[str]] = {
            t: {col["name"] for col in inspector.get_columns(t)}
            for t in all_table_names
            if t not in _EXCLUDED_TABLES
        }

        if tables_to_clear:
            quoted = ", ".join(f'"{t}"' for t in tables_to_clear)
            conn.execute(sa.text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))

        # Re-insert rows, skipping columns that no longer exist in the schema.
        for table_name in restore_sequence:
            rows = tables_data.get(table_name, [])
            if not rows:
                continue
            current_columns = current_columns_map.get(table_name)
            if current_columns is None:
                log.warning("Table %r in backup does not exist in current schema — skipping", table_name)
                continue
            for row in rows:
                filtered = {k: v for k, v in row.items() if k in current_columns}
                # sa.text() bypasses SQLAlchemy type coercion, so dict/list
                # values (JSON columns) must be serialized manually.
                filtered = {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in filtered.items()
                }
                if filtered:
                    col_list = ", ".join(f'"{c}"' for c in filtered)
                    val_list = ", ".join(f":{c}" for c in filtered)
                    conn.execute(
                        sa.text(f'INSERT INTO "{table_name}" ({col_list}) VALUES ({val_list})'),
                        filtered,
                    )

        conn.commit()

        # Reset sequences so future INSERTs don't collide with restored IDs.
        # Each table is in its own commit so a failure on one doesn't affect others.
        for table_name in tables_to_clear:
            try:
                pk_cols = inspector.get_pk_constraint(table_name).get("constrained_columns", [])
                for pk in pk_cols:
                    seq = conn.execute(sa.text(
                        f"SELECT pg_get_serial_sequence('{table_name}', '{pk}')"
                    )).scalar()
                    if seq:
                        next_id = conn.execute(sa.text(
                            f'SELECT COALESCE(MAX("{pk}"), 0) + 1 FROM "{table_name}"'
                        )).scalar()
                        if next_id is not None:
                            conn.execute(sa.text(f"SELECT setval('{seq}', {int(next_id)}, false)"))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                log.debug("Could not reset sequence for %s: %s", table_name, exc)

    # Expire the ORM session so subsequent queries see the freshly restored data.
    db.session.expire_all()
    log.info("Restore from %s complete", zip_path.name)


def prune_old_backups(backup_dir: str | Path, keep_count: int) -> list[Path]:
    """Delete oldest backup zip files, keeping at most *keep_count* files.

    Args:
        backup_dir: Directory containing backup zip files.
        keep_count: Maximum number of files to keep.

    Returns:
        List of deleted file paths.
    """
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return []
    files = sorted(backup_path.glob("medcover_backup_*.zip"), key=lambda p: p.stat().st_mtime)
    to_delete = files[: max(0, len(files) - keep_count)]
    for f in to_delete:
        f.unlink()
        log.info("Pruned old backup: %s", f.name)
    return to_delete


def list_backups(backup_dir: str | Path) -> list[dict]:
    """Return metadata for all backup files in *backup_dir*, newest first.

    Each entry: {name, path, size_bytes, created_at (datetime UTC)}
    """
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        return []
    files = sorted(backup_path.glob("medcover_backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        stat = f.stat()
        result.append({
            "name": f.name,
            "path": f,
            "size_bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        })
    return result
