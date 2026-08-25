"""Guardrail: Czech opening „ (U+201E) must always pair with “ (U+201C).

The straight ASCII " (U+0022) actually corrupts HTML attribute values when
it appears between „...“ inside a `data-confirm="..."` attribute (the stray
straight quote prematurely terminates the attribute value). The English
” (U+201D) is not a Czech closing quote and looks wrong in Czech UI copy.

This test scans user-facing sources for any „...(closer) sequence where the
first closer encountered is anything other than “, and fails with a listing
so the offender can be corrected before merge.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("app", "scripts", "scheduler")
SCAN_SUFFIXES = (".py", ".html", ".j2")

_BAD_CLOSERS = '"”'
_PATTERN = re.compile(rf"„([^„“]{{0,300}}?)([{_BAD_CLOSERS}])")


def _iter_sources():
    for rel in SCAN_DIRS:
        base = REPO_ROOT / rel
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix in SCAN_SUFFIXES and path.is_file():
                yield path


def test_czech_opener_pairs_with_correct_closer():
    violations: list[str] = []
    for path in _iter_sources():
        text = path.read_text(encoding="utf-8")
        if "„" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = _PATTERN.search(line)
            if m:
                closer = m.group(2)
                rel = path.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}:{lineno}: „ closed with U+{ord(closer):04X} "
                    f"instead of “ (U+201C) — {line.strip()[:160]}"
                )
    assert not violations, "Czech opening „ must close with “ (U+201C), not straight or English ”:\n  " + "\n  ".join(
        violations
    )
