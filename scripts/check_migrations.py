#!/usr/bin/env python3
"""Guard against rewriting the Alembic migration baseline.

MedCover ships a single squashed MSSQL baseline. That squash was a **one-time**
bootstrap performed before any durable database existed. Re-squashing — or
otherwise rewriting the baseline — after a long-lived DB (dev on zerver,
staging, prod) has been created from it is a breaking change: the DB's stored
``alembic_version`` then names a revision that no longer exists, ``flask db
upgrade`` aborts, and the ``web`` container fails its ``flask verify-schema``
health check, blocking the deploy. See DEVOPS.md → "Database Migrations".

This script enforces the invariants that make that mistake impossible to merge:

  1. Exactly one root revision (``down_revision is None``).
  2. The root revision id equals the frozen baseline id below — so rewriting or
     re-squashing the baseline (which mints a new id) fails CI.
  3. Exactly one head — no accidentally divergent/unmerged branches.

Re-squashing is *not* forbidden outright — when you genuinely need it, follow the
**Sanctioned re-baseline procedure** in DEVOPS.md (squash schema-neutrally,
re-stamp every durable DB in the deploy window, then bump
``EXPECTED_BASELINE_REVISION`` below in the same commit). Editing that constant is
step 4 of that procedure, not a shortcut around it.

Run: ``python scripts/check_migrations.py`` (also wired into CI and pre-commit).
"""

import ast
import pathlib
import sys

# The frozen root of the migration graph. Only change this as step 4 of the
# Sanctioned re-baseline procedure in DEVOPS.md (squash schema-neutrally + re-stamp
# every durable DB), never on its own.
EXPECTED_BASELINE_REVISION = "2c159bca01be"

VERSIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _module_assignment(tree: ast.Module, name: str) -> object:
    """Return the value of a module-level ``name = <literal>`` assignment."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if name in targets:
                return ast.literal_eval(node.value)
    raise KeyError(name)


def _collect_revisions() -> dict[str, object]:
    """Map each migration's ``revision`` id to its ``down_revision``."""
    revisions: dict[str, object] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        try:
            revision = _module_assignment(tree, "revision")
            down_revision = _module_assignment(tree, "down_revision")
        except (KeyError, ValueError) as exc:
            sys.exit(f"ERROR: {path.name}: could not parse revision metadata ({exc}).")
        revisions[str(revision)] = down_revision
    return revisions


def main() -> int:
    if not VERSIONS_DIR.is_dir():
        sys.exit(f"ERROR: migrations versions dir not found: {VERSIONS_DIR}")

    revisions = _collect_revisions()
    if not revisions:
        sys.exit("ERROR: no migration files found under migrations/versions/.")

    errors: list[str] = []

    # 1 + 2: exactly one root, and it is the frozen baseline.
    roots = [rev for rev, down in revisions.items() if down is None]
    if len(roots) != 1:
        errors.append(
            f"expected exactly one root migration (down_revision = None), found {len(roots)}: "
            f"{sorted(roots)}. Re-squashing or splitting the baseline is forbidden once a "
            f"durable DB exists — add a forward migration instead."
        )
    elif roots[0] != EXPECTED_BASELINE_REVISION:
        errors.append(
            f"baseline revision changed: found root '{roots[0]}', expected "
            f"'{EXPECTED_BASELINE_REVISION}'. Rewriting/re-squashing the baseline strands every "
            f"existing database (dangling alembic_version + missing columns). If this re-squash is "
            f"intentional, follow the Sanctioned re-baseline procedure in DEVOPS.md (schema-neutral "
            f"squash + re-stamp every durable DB), of which bumping EXPECTED_BASELINE_REVISION is "
            f"step 4."
        )

    # 3: exactly one head (no divergent branches).
    referenced = {d for down in revisions.values() for d in (down if isinstance(down, (list, tuple)) else [down]) if d}
    heads = [rev for rev in revisions if rev not in referenced]
    if len(heads) != 1:
        errors.append(
            f"expected exactly one head, found {len(heads)}: {sorted(heads)}. "
            f"Merge the divergent branches (flask db merge) so history stays linear."
        )

    if errors:
        print("Migration baseline guard FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  ✘ {err}", file=sys.stderr)
        return 1

    print(
        f"Migration baseline guard OK — {len(revisions)} revision(s), "
        f"baseline '{EXPECTED_BASELINE_REVISION}', single head."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
