#!/usr/bin/env bash
# Compile all requirements*.in files inside a Linux container that matches
# the production and CI environment (python:3.14-slim / x86_64).
#
# Usage:
#   ./scripts/compile_requirements.sh
#
# Requirements:
#   - Podman (or Docker — set DOCKER=docker to override)
#   - The compiled .txt files are written back to the repo root on the host.
#
# Why run in a container?
#   pip-compile on macOS (ARM) generates hashes only for macOS-compatible
#   wheels. Some packages (e.g. greenlet) have platform-conditional
#   dependencies and ship separate Linux wheels whose hashes would be missing
#   from a macOS-compiled lock file, causing pip-audit and pip --require-hashes
#   to fail in CI. Running inside a matching Linux image guarantees that all
#   Linux-specific wheel hashes are captured.

set -euo pipefail

DOCKER="${DOCKER:-podman}"
IMAGE="python:3.14-slim"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Pulling ${IMAGE} ..."
"$DOCKER" pull "$IMAGE"

echo "==> Compiling all requirements files ..."
"$DOCKER" run --rm \
  -v "${REPO_ROOT}:/app" \
  -w /app \
  "$IMAGE" \
  bash -c "
    pip install --quiet pip-tools &&
    pip-compile --upgrade --generate-hashes --output-file=requirements.txt requirements.in &&
    pip-compile --upgrade --generate-hashes --output-file=requirements-dev.txt requirements-dev.in &&
    pip-compile --upgrade --generate-hashes --output-file=requirements-e2e.txt requirements-e2e.in
  "

echo ""
echo "Done. Review the changes with: git diff requirements*.txt"
