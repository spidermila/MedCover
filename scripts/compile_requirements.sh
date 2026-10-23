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
#   Dependency resolution can vary by platform and Python version. Running
#   inside a matching Linux image keeps the resolved dependencies aligned
#   with CI and production.

set -euo pipefail

DOCKER="${DOCKER:-podman}"
IMAGE="python:3.14-slim"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Pulling ${IMAGE} ..."
"$DOCKER" pull "$IMAGE"

echo "==> Compiling all requirements files ..."
"$DOCKER" run --rm \
  --platform linux/amd64 \
  -v "${REPO_ROOT}:/app" \
  -w /app \
  "$IMAGE" \
  bash -euc '
    echo "==> Installing uv ..."
    pip install --quiet uv
    (
      echo "==> Compiling requirements.txt (production) ..."
      uv pip compile --upgrade --generate-hashes --output-file=requirements.txt requirements.in
      echo "==> Finished requirements.txt (production)"
    ) &
    production_pid=$!
    (
      echo "==> Compiling requirements-e2e.txt (E2E) ..."
      uv pip compile --upgrade --generate-hashes --output-file=requirements-e2e.txt requirements-e2e.in
      echo "==> Finished requirements-e2e.txt (E2E)"
    ) &
    e2e_pid=$!

    # Dev and telemetry must use the newly compiled production constraints.
    echo "==> Waiting for production constraints before starting dev and telemetry ..."
    wait "$production_pid"
    (
      echo "==> Compiling requirements-dev.txt (dev) ..."
      uv pip compile --upgrade --generate-hashes -c requirements.txt --output-file=requirements-dev.txt requirements-dev.in
      echo "==> Finished requirements-dev.txt (dev)"
    ) &
    dev_pid=$!
    (
      echo "==> Compiling requirements-telemetry.txt (telemetry) ..."
      uv pip compile --upgrade --generate-hashes -c requirements.txt --output-file=requirements-telemetry.txt requirements-telemetry.in
      echo "==> Finished requirements-telemetry.txt (telemetry)"
    ) &
    telemetry_pid=$!

    wait "$dev_pid"
    wait "$telemetry_pid"
    wait "$e2e_pid"
  '

echo ""
echo "Done. Review the changes with: git diff requirements*.txt"
