# MedCover

[![CI](https://github.com/spidermila/MedCover/actions/workflows/ci.yml/badge.svg)](https://github.com/spidermila/MedCover/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-715%20passed-brightgreen)](https://github.com/spidermila/MedCover/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-84%25-green)](https://github.com/spidermila/MedCover/actions/workflows/ci.yml)
![CodeRabbit Pull Request Reviews](https://img.shields.io/coderabbit/prs/github/spidermila/MedCover?utm_source=oss&utm_medium=github&utm_campaign=spidermila%2FMedCover&labelColor=171717&color=FF570A&link=https%3A%2F%2Fcoderabbit.ai&label=CodeRabbit+Reviews)

MedCover is a web application for the **Czech Red Cross** that replaces a Google Sheets–based medical cover planning solution.
It manages events, spot assignments, user roles, equipment, and reporting — all in Czech, tailored to the organisation's workflows.

**For architecture and design decisions, see [architecture.md](architecture.md).**
**For DevOps, local setup, CI/CD, and deployment, see [DEVOPS.md](DEVOPS.md).**
**For the MVP feature checklist and progress tracker, see [MVP.md](MVP.md).**

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.14 |
| Web framework | Flask 3 |
| Database | PostgreSQL 17 |
| ORM / migrations | SQLAlchemy · Flask-Migrate (Alembic) |
| Auth | Flask-Login · Flask-Mail |
| Frontend | Jinja2 · Bootstrap 5.3 · FullCalendar |
| Infrastructure | Docker Compose (web + scheduler + db) |
| CI | GitHub Actions (lint + test) |
| Hosting (target) | Render.com |

---

## Quick Start

> Full setup instructions are in [DEVOPS.md](DEVOPS.md).

```bash
# 1. Clone and create virtual environment
git clone git@github.com:spidermila/MedCover.git && cd MedCover
python3.14 -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 3. Configure environment
cp .env.example .env  # then fill in your values

# 4. Start containers
docker compose up -d

# 5. Seed dev users (admin, coordinator, member, viewer + reference data)
docker compose exec web python scripts/seed_dev.py

# 6. Install pre-commit hooks
pre-commit install

# 7. Run tests
pytest tests/
```

---

## Running Tests

Tests run automatically on every commit via the pre-commit `pytest` hook.
To run manually:

```bash
# Run against the current Python interpreter (fastest for day-to-day dev)
pytest tests/

# Run via tox (recommended — mirrors CI; uses pinned deps from requirements-dev.txt)
tox -e py314
```

Environment variables (`FLASK_ENV`, `SECRET_KEY`) are injected automatically by
`pytest-env` via `pyproject.toml`.

**`TEST_DATABASE_URL` is managed automatically** by `testcontainers`:
- If `TEST_DATABASE_URL` is **not set**, a temporary `postgres:17` Docker container
  is started at the beginning of the test session and stopped at the end.
  The only requirement is a running Docker daemon.
- If `TEST_DATABASE_URL` **is set** (e.g. in CI or by a developer with a local
  Postgres), testcontainers skips the container and uses the provided URL.

Coverage report is written to `htmlcov/` after each run.

### Adding support for a new Python version

1. Install the new Python interpreter on the host.
2. Add `py3XX` to `envlist` in `[tool.tox]` in `pyproject.toml`.
3. Recompile deps if needed: `pip-compile requirements-dev.in --generate-hashes -o requirements-dev.txt`.
4. Run `tox -e py3XX` to verify.
