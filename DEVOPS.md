# MedCover — DevOps Reference

This document covers the development environment setup, repository structure, CI/CD pipeline, and deployment configuration for the MedCover application.

For architectural decisions behind these choices, see `architecture.md` (AD09, AD10, Deployment Model).

---

## Repository Structure

```
MedCover/
├── .github/
│   ├── dependabot.yml          # Weekly dependency update PRs (pip + GitHub Actions)
│   └── workflows/
│       └── ci.yml              # Run lint + tests + pip-audit on every PR and push
│
├── app/
│   ├── __init__.py             # Flask app factory: create_app(); CSP headers; custom filters
│   ├── config.py               # Config classes: DevelopmentConfig, ProductionConfig
│   ├── extensions.py           # Flask extensions (db, migrate, mail, login_manager, csrf)
│   ├── utils.py                # Shared helpers: require_permission, audit, diff_changes, …
│   ├── queries.py              # Reusable DB queries (active_master_events_list, …)
│   ├── mail.py                 # Email sending helpers (outbox-backed)
│   ├── scheduler_tasks.py      # Task implementations called by scheduler/main.py
│   ├── work_report_generator.py# Výkaz práce XLSX generator
│   ├── models/                 # SQLAlchemy models (one file per domain entity)
│   │   ├── __init__.py         # Imports all models so Alembic auto-detects them
│   │   ├── user.py             # UserAccount, has_permission(), has_any_permission()
│   │   ├── role.py             # Role enum, ALL_PERMISSIONS, ROLE_PERMISSIONS
│   │   ├── event.py            # Event, EventSpot, EventStatus, EventTemplate
│   │   ├── master_event.py     # MasterEvent (hierarchy for yearly reporting)
│   │   ├── assignment.py       # Assignment (user ↔ spot)
│   │   ├── equipment.py        # EquipmentType, EquipmentItem, plans, assignments
│   │   ├── qualification.py    # Qualification, UserQualification (credentials)
│   │   ├── audit.py            # AuditLogEntry
│   │   ├── settings.py         # AppSettings (SMTP, setup flag, Fernet-encrypted creds)
│   │   ├── invite.py           # Invite (invite-only registration tokens)
│   │   ├── outbox.py           # OutboxEmail (queued emails, retry logic, batched-notification fields)
│   │   ├── digest.py           # DigestSchedule, DigestBlock, DigestMetricSnapshot (admin digest email)
│   │   ├── debriefing.py       # DebriefingRecord, DebriefingQuestion
│   │   └── feedback.py         # UserFeedback
│   ├── routes/                 # Flask blueprints (one per feature area)
│   │   ├── __init__.py
│   │   ├── auth.py             # Login, logout, password reset, registration
│   │   ├── setup.py            # First-run setup wizard
│   │   ├── admin.py            # Dashboard, audit log, permissions overview
│   │   ├── admin_digest.py     # Weekly digest subscription management
│   │   ├── app_settings.py     # SMTP & app settings (admin)
│   │   ├── backup.py           # DB backup/restore (admin)
│   │   ├── users.py            # User management, invites, credentials
│   │   ├── master_events.py    # Master Event CRUD
│   │   ├── events.py           # Event CRUD, lifecycle, spot assignment, calendar feed
│   │   ├── assignments.py      # Assignment claim/release
│   │   ├── templates.py        # Event template CRUD
│   │   ├── qualifications.py   # Qualification (credential type) CRUD
│   │   ├── equipment.py        # Equipment types, items, issuance, event plans
│   │   ├── import_events.py    # Bulk event import from paste
│   │   ├── reports.py          # Reports (staffing, statistics, glossary)
│   │   ├── debriefing.py       # Post-event debriefing forms
│   │   ├── work_report.py      # Výkaz práce (monthly work-report XLSX)
│   │   ├── feedback.py         # User feedback submission
│   │   ├── main.py             # Dashboard, health check
│   │   └── dev.py              # Dev-only routes (disabled in production)
│   ├── templates/              # Jinja2 HTML templates
│   │   ├── base.html           # Base layout with nav, CSP-safe JS config
│   │   ├── macros/             # Reusable macros (help_icon, pagination, …)
│   │   ├── auth/
│   │   ├── events/
│   │   ├── equipment/
│   │   └── …
│   ├── static/
│   │   ├── css/main.css        # Custom utility classes (no inline styles — CSP)
│   │   ├── js/                 # FullCalendar, per-page JS modules
│   │   └── img/
│   └── email/                  # Email templates (Jinja2, plain-text + HTML)
│
├── scheduler/
│   └── main.py                 # Background task runner (schedule library)
│                               # Tasks: event auto-transitions, reminder emails,
│                               #        digest emails, work-report cleanup
│
├── migrations/                 # Flask-Migrate (Alembic) migration scripts
│   └── versions/
│
├── tests/
│   ├── conftest.py             # Fixtures: app, DB, client per role; AppSettings seed
│   ├── test_auth.py
│   ├── test_events.py
│   ├── test_assignments.py
│   ├── test_equipment.py
│   ├── test_admin.py
│   ├── test_admin_digest.py
│   ├── test_debriefing.py
│   ├── test_import_events.py
│   ├── test_master_events.py
│   ├── test_qualifications.py
│   ├── test_reports.py
│   ├── test_templates.py
│   ├── test_users.py
│   ├── test_work_report.py
│   └── …
│
├── scripts/
│   ├── seed_dev.py             # Populates DB with realistic mock data for local dev
│   ├── compile_requirements.sh # Recompiles .in → .txt in a Linux container (deterministic hashes)
│   └── e2e-entrypoint.sh       # Docker entrypoint for E2E web container
│
├── e2e_tests/                  # Playwright browser tests (NOT run by default pytest)
│   ├── conftest.py             # Fixtures: base_url, logged_in_page
│   ├── test_login_flow.py
│   ├── test_create_event.py
│   └── test_smoke_navigation.py
│
├── Dockerfile                  # Single image for both web and scheduler containers
├── docker-compose.yml          # Local dev: web + scheduler + MSSQL (hot reload)
├── docker-compose.e2e.yml      # E2E tests: db-e2e + web-e2e + playwright runner
├── .env.example                # Template for required env vars — COMMIT THIS
├── .env                        # Actual secrets — NEVER COMMIT (in .gitignore)
├── .dockerignore
├── requirements.txt            # Production dependencies (compiled from .in files)
├── requirements-dev.txt        # Dev/test extras (compiled from .in files)
├── requirements-e2e.txt        # E2E test deps: pytest-playwright
├── Makefile                    # Shortcuts: make e2e, make test
├── tox.ini                     # tox envs: py314 (unit), e2e (playwright)
├── architecture.md
└── DEVOPS.md                   # This file
```

---

## Container Architecture

Two containers share a single Docker image; they run different commands:

| Container | Dev command (docker-compose) | Prod command (Dockerfile CMD) | Purpose |
|---|---|---|---|
| `web` | `flask run --host=0.0.0.0 --debug` | `gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} "app:create_app()"` | Serves the Flask web application |
| `scheduler` | `python scheduler/main.py` | `python scheduler/main.py` | Background tasks: auto-transitions, reminders, digests, file cleanup |

Both containers share the same codebase and connect to the same MSSQL database via `DATABASE_URL`.
The `web` container uses `docker-entrypoint.sh` which runs `flask db upgrade` + `flask verify-schema` before starting.
The `scheduler` container uses `docker-entrypoint-scheduler.sh` (no migrations) and waits for `web` to be healthy before starting.

---

## Local Development

### Prerequisites
- Docker Desktop (or Docker Engine + Docker Compose)
- Git

### Setup

```bash
git clone https://github.com/spidermila/MedCover.git
cd MedCover
cp .env.example .env          # Fill in your local secrets
docker compose up --build     # Starts web + scheduler + MSSQL
```

The app will be available at `http://localhost:5000`.

### Seed mock data

```bash
docker compose exec web python scripts/seed_dev.py
```

This creates realistic test users, credentials, master events, events, assignments, and equipment. Running it multiple times is safe (idempotent).

### Run database migrations

```bash
# Create a new migration after model changes
docker compose exec web flask db migrate -m "describe the change"

# Apply pending migrations
docker compose exec web flask db upgrade
```

### Run tests

Tests run on the **host** in a local Python 3.14 virtualenv. The application
image (`Dockerfile`) installs only `requirements.txt` (production), so the
running `web`/`scheduler` containers do **not** contain pytest/tox — running
the suite inside them does not work. CI follows the same host-based approach
(see `.github/workflows/ci.yml`).

Because the only database driver is now `pyodbc`, the host needs the
**Microsoft ODBC Driver 18** and unixODBC installed once (pyodbc links against
`libodbc.so.2` at runtime — pip alone is not enough):

```bash
# Debian/Ubuntu — install the ODBC driver + unixODBC (one-time, needs sudo)
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
  | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -fsSL https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list \
  | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev
# macOS: brew install msodbcsql18 unixodbc
```

Then create a venv, install dev dependencies, and run the suite:

```bash
python3.14 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt

# Set TEST_DATABASE_URL to use an existing MSSQL DB (the dev db works fine —
# the suite uses an isolated medcover_test database), or leave it unset to let
# testcontainers auto-spin an MSSQL 2022 Express container.
export TEST_DATABASE_URL="mssql+pyodbc://SA:DevPassword123!@localhost:1433/medcover_test?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes"
pytest          # or: tox -e py314  (mirrors CI)
```

### Run E2E browser tests (Playwright)

End-to-end tests use real browsers (Chromium, Firefox, WebKit) driven by
[Playwright](https://playwright.dev/python/) to test rendered pages, JS
validation, form submission, and navigation. Everything runs in containers
(Docker or Podman) — nothing is installed on the host.

**Container runtime:** The Makefile auto-detects `podman` or `docker` (preferring
Podman). Override with `make e2e CONTAINER_ENGINE=docker` if needed. For tox,
set `CONTAINER_ENGINE=docker tox -e e2e`.

**Architecture:** `docker-compose.e2e.yml` spins up three containers:

| Container | Image | Purpose |
|-----------|-------|---------|
| `db-e2e` | `mcr.microsoft.com/mssql/server:2022-latest` | Fresh MSSQL on tmpfs (destroyed after each run) |
| `web-e2e` | App Dockerfile | Runs migrations, seeds data (`seed_dev.py`), serves Flask |
| `e2e` | `mcr.microsoft.com/playwright/python` | Runs Playwright tests against `http://web-e2e:5000` |

**How to run:**

```bash
# Using Make (recommended)
make e2e

# Or using tox
tox -e e2e

# Or directly with Docker Compose
docker compose -f docker-compose.e2e.yml up --build --abort-on-container-exit --exit-code-from e2e
docker compose -f docker-compose.e2e.yml down -v
```

**Cleanup after a failed run:**

```bash
make e2e-down
# or: docker compose -f docker-compose.e2e.yml down -v
```

**Test files** live in `e2e_tests/` (separate from `tests/`) and are never
included in the regular `pytest` or CI runs.

**HTML report:** After each run an HTML report with screenshots is saved to
`e2e-report/report.html`. To view it, run:

```bash
make e2e-report
# Opens http://localhost:9323/report.html (Ctrl+C to stop)
```

> **Note:** Opening `report.html` directly as a `file://` URL will fail due to
> browser security restrictions. Always use `make e2e-report` to serve it via HTTP.

**First run** pulls the Playwright Docker image (~1.5 GB) and builds the app
image. Subsequent runs are faster thanks to Docker layer caching.

**Adding new E2E tests:** create a `test_*.py` file in `e2e_tests/`. Use the
`logged_in_page` fixture from `e2e_tests/conftest.py` for tests that need an
authenticated session (logs in as the admin dev user automatically).

---

## docker-compose.yml

The embedded summary below reflects the actual file. Key points:

- `web` uses `flask run --debug` (hot reload) in dev; production uses gunicorn via `CMD` in the Dockerfile
- Both containers mount `.:/app` so local code changes reflect immediately
- Both containers have healthchecks; the scheduler checks a heartbeat file written every ~5 s
- `db` uses **MSSQL 2022 Express** (`mcr.microsoft.com/mssql/server:2022-latest`) with Czech collation and RCSI enabled

```yaml
services:
  web:
    build:
      context: .
      args:
        GIT_COMMIT: ${GIT_COMMIT:-dev}
    command: flask run --host=0.0.0.0 --debug
    restart: unless-stopped
    volumes:
      - .:/app          # Hot reload: local code changes reflect immediately
    env_file: .env
    ports:
      - "5000:5000"
    depends_on:
      db:
        condition: service_healthy
      db-init:
        condition: service_completed_successfully

  scheduler:
    build:
      context: .
      args:
        GIT_COMMIT: ${GIT_COMMIT:-dev}
    entrypoint: ["/docker-entrypoint-scheduler.sh"]
    command: python scheduler/main.py
    restart: unless-stopped
    volumes:
      - .:/app
    env_file: .env
    depends_on:
      web:
        condition: service_healthy

  db:
    image: mcr.microsoft.com/mssql/server:2022-latest
    restart: unless-stopped
    environment:
      ACCEPT_EULA: "Y"
      MSSQL_PID: "Express"
      MSSQL_SA_PASSWORD: "DevPassword123!"
      MSSQL_COLLATION: "Czech_100_CI_AS_SC_UTF8"
    ports:
      - "1433:1433"
    volumes:
      - mssql_data:/var/opt/mssql

  # One-shot initializer — the MSSQL image has no auto-init directory, so this
  # creates the dev database (Czech collation + RCSI) and the app login/user
  # once the db is healthy, then exits. web waits for it to complete.
  db-init:
    image: mcr.microsoft.com/mssql/server:2022-latest
    depends_on:
      db:
        condition: service_healthy
    environment:
      MSSQL_HOST: db
      MSSQL_SA_PASSWORD: "DevPassword123!"
    volumes:
      - ./mssql-init:/mssql-init:ro
    entrypoint: ["/bin/bash", "/mssql-init/setup.sh"]
    restart: "no"

volumes:
  mssql_data:
```

---

## Dockerfile

```dockerfile
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY . .

# Embed git commit hash at build time:
#   docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .
ARG GIT_COMMIT=dev
ENV GIT_COMMIT=${GIT_COMMIT}

COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY docker-entrypoint-scheduler.sh /docker-entrypoint-scheduler.sh
RUN chmod +x /docker-entrypoint.sh /docker-entrypoint-scheduler.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT:-5000} \"app:create_app()\""]
```

`docker-entrypoint.sh` runs `flask db upgrade` then `flask verify-schema` on every container start before handing off to the CMD process. If `verify-schema` detects missing tables/columns the container exits immediately rather than serving broken traffic.

`docker-entrypoint-scheduler.sh` is a lightweight entrypoint for the scheduler — it skips migrations and schema verification (the scheduler waits for `web` to be healthy first).

---

## Dependency Management

Dependencies are managed with **pip-tools** (`.in` → `.txt` compilation with hashes).

### Files

| File | Purpose |
|---|---|
| `requirements.in` | Top-level production dependencies |
| `requirements-dev.in` | Dev/test extras (extends production) |
| `requirements-e2e.in` | Playwright E2E test deps |
| `requirements.txt` | Compiled lock file with hashes (committed) |
| `requirements-dev.txt` | Compiled dev lock file with hashes (committed) |
| `requirements-e2e.txt` | Compiled E2E lock file with hashes (committed) |

### Adding or upgrading a dependency

1. Edit the relevant `.in` file (add/bump the package).
2. Run the compile script:
   ```bash
   ./scripts/compile_requirements.sh
   ```
   This uses Podman (or Docker) to compile inside a `python:3.14-slim` Linux/amd64 container — ensuring the generated hashes match CI and production.
3. Review the diff: `git diff requirements*.txt`
4. Commit both the `.in` and `.txt` files together.

> **Why a container?** pip-compile on macOS ARM produces hashes for macOS-only wheels. Some packages ship different wheels for Linux, causing hash mismatches in CI. The container guarantees Linux-compatible hashes.

### Dependabot

Dependabot submits weekly PRs for `pip` and `GitHub Actions` dependency updates (configured in `.github/dependabot.yml`). Review and merge these regularly.

---

## Environment Variables

Copy `.env.example` to `.env` for local development. Never commit `.env`.

| Variable | Description | Example |
|---|---|---|
| `FLASK_ENV` | `development` or `production` | `development` |
| `SECRET_KEY` | Flask session secret — generate a strong random value | `openssl rand -hex 32` |
| `DATABASE_URL` | MSSQL connection string | `mssql+pyodbc://medcover:Dev_Password1!@db:1433/medcover_dev?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes` |

> **Email / SMTP:** SMTP credentials are configured through the web UI setup wizard on first run and stored Fernet-encrypted in the `app_settings` database table. No `MAIL_*` environment variables are required.

---

## Production Deployment

MedCover runs on **Azure Container Apps** (France Central) with **Azure SQL Database** as the managed database service. The CI/CD pipeline (`.github/workflows/deploy-azure.yml`) builds and deploys on every version tag push.

See `azure-setup-guide.md` in the `medcover-infra` repo for the full Azure provisioning guide.

### What's ready

- **Docker image**: A single `Dockerfile` builds an image usable for both `web` and `scheduler` containers.
- **Database migrations**: Run automatically via `docker-entrypoint.sh` (`flask db upgrade`) on `web` container start. The scheduler uses a lightweight entrypoint without migrations.
- **First-run setup wizard**: After the web service is live, navigate to the app URL. The wizard appears on first visit — configure the application name, admin account, and SMTP settings there.
- **Production compose file**: `docker-compose.prod.yml` is available for self-hosted deployments (e.g. the zerver home-lab test server).

### Database schema on first deploy

The migration history is a single MSSQL-native baseline
(`migrations/versions/*_mssql_baseline_schema.py`, `down_revision = None`), so
no manual bootstrap is needed. The web container's entrypoint runs
`flask db upgrade` on every start, which creates the full schema on a fresh
Azure SQL database and applies any later migrations on subsequent deploys.

> **Historical note:** earlier revisions carried 45 PostgreSQL-era migrations
> that could not run on MSSQL and required a manual `stamp head` +
> autogenerate bootstrap. Those were squashed into the single baseline in
> [PR #381](https://github.com/spidermila/MedCover/pull/381); the manual step
> is gone.

### Subsequent deployments

Tag a version → GitHub Actions builds image → deploys to both Container Apps automatically:

```bash
git tag v1.2.3
git push origin v1.2.3
```

---

## Type Checking (mypy)

MedCover uses **mypy 2.0** for static type checking. All production code in `app/` and `scheduler/` is annotated and must pass mypy on every commit.

### Running mypy manually

```bash
source .venv/bin/activate
mypy app/ scheduler/
```

A clean run prints `Success: no issues found in N source files`.

### Configuration

mypy is configured in `pyproject.toml` under `[tool.mypy]`:

- `disallow_untyped_defs = true` — **hard requirement**: every function must have full parameter and return type annotations
- `check_untyped_defs = true` — bodies of annotated functions are fully type-checked
- `ignore_missing_imports = true` — suppresses errors for third-party packages without stubs (Flask, SQLAlchemy, etc.)
- `exclude` — migrations, tests, htmlcov, and .venv are excluded

#### Key overrides

| Override | Reason |
|---|---|
| `app.models.*` — disables `name-defined`, `misc`, `assignment` | `db.Model` base class is not resolvable without full SQLAlchemy stubs; `db.relationship()` returns `RelationshipProperty[Any]` at the type level |
| `app.routes.*` — disables `union-attr`, `return-value`, `attr-defined` | Flask's `redirect()` returns `werkzeug.wrappers.Response` (not `flask.wrappers.Response`); `current_user` is a `LocalProxy` without union narrowing |
| `scripts.*` — `ignore_errors = true` | Seed scripts are not production code |

### Pre-commit hook

mypy runs automatically on every commit via `.pre-commit-config.yaml`:

```yaml
- repo: local
  hooks:
    - id: mypy
      name: mypy
      entry: .venv/bin/mypy app/ scheduler/
      language: system
      pass_filenames: false
      always_run: true
```

It runs before pytest. A commit is rejected if mypy reports any errors.

### Model annotation pattern

SQLAlchemy models use the old-style `db.Column()` syntax (not `Mapped[]`-style declarative). To avoid converting models (which risks bugs), the pattern is:

1. Add `# type: ignore[misc]` to the class definition line: `class Event(db.Model):  # type: ignore[misc]`
2. Annotate relationship attributes with `Mapped[list[X]]` or `Mapped[X | None]` when they are iterated or accessed — **only the attribute declaration**, not the `db.relationship(...)` call
3. Import forward references under `TYPE_CHECKING` to avoid circular imports at runtime

---

## CI/CD Pipeline

### On every PR (`ci.yml`)

```
PR opened / updated
      ↓
GitHub Actions: ci.yml
  ├── lint job: pre-commit (flake8, mypy, pyupgrade, whitespace)
  ├── test job: MSSQL 2022 service → pytest --cov
  └── audit job: pip-audit → check dependencies for known CVEs
      ↓
Review, approve, merge
```

Dependabot submits weekly PRs for `pip` and `github-actions` dependency updates (configured in `.github/dependabot.yml`).

### On merge to main

Tag a version to trigger the deploy workflow (`deploy-azure.yml`) which builds the Docker image and deploys to Azure Container Apps.

### .github/workflows/ci.yml

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - name: Install pre-commit
        run: pip install pre-commit
      - name: Run pre-commit hooks
        run: pre-commit run --all-files

  test:
    runs-on: ubuntu-latest

    services:
      mssql:
        image: mcr.microsoft.com/mssql/server:2022-latest
        env:
          ACCEPT_EULA: "Y"
          MSSQL_SA_PASSWORD: "CiPassword123!"
          MSSQL_PID: "Express"
          MSSQL_COLLATION: "Czech_100_CI_AS_SC_UTF8"
        ports:
          - 1433:1433
        options: >-
          --health-cmd "/opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P 'CiPassword123!' -C -Q 'SELECT 1' -b"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 10

    env:
      TEST_DATABASE_URL: "mssql+pyodbc://SA:CiPassword123!@localhost:1433/medcover_test?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes"
      FLASK_ENV: testing
      SECRET_KEY: ci-test-secret-not-real

    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - name: Install ODBC Driver for SQL Server
        run: |
          sudo find /etc/apt/sources.list.d/ -name "*.list" -exec grep -l "packages.microsoft.com" {} \; | xargs sudo rm -f
          echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/$(lsb_release -rs)/prod $(lsb_release -cs) main" \
            | sudo tee /etc/apt/sources.list.d/mssql-release.list
          sudo apt-get update -q
          sudo ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev
      - name: Create test database
        run: |
          pip install pyodbc
          python - << 'EOF'
          import pyodbc, time
          conn_str = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost,1433;DATABASE=master;UID=SA;PWD=CiPassword123!;Encrypt=no;TrustServerCertificate=yes"
          for _ in range(30):
              try:
                  conn = pyodbc.connect(conn_str); conn.autocommit = True; break
              except Exception:
                  time.sleep(2)
          c = conn.cursor()
          c.execute("IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name='medcover_test') CREATE DATABASE medcover_test COLLATE Czech_100_CI_AS_SC_UTF8")
          c.execute("ALTER DATABASE medcover_test SET READ_COMMITTED_SNAPSHOT ON")
          conn.close()
          print("medcover_test ready")
          EOF
      - name: Install dependencies
        run: pip install --require-hashes -r requirements-dev.txt
      - name: Run tests with coverage
        run: pytest --cov=app --cov-report=term-missing --cov-report=xml
      - name: Upload coverage report
        uses: actions/upload-artifact@v7
        if: always()
        with:
          name: coverage-report
          path: htmlcov/

  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14"
      - name: Install pip-audit
        run: pip install pip-audit
      - name: Audit dependencies for known vulnerabilities
        run: pip-audit -r requirements.txt
```

---

## Versioning & Changelog

This project uses **[Semantic Versioning](https://semver.org/)** (`MAJOR.MINOR.PATCH`).

| Bump | When |
|---|---|
| `PATCH` | Bug fixes, small UI tweaks, no new features |
| `MINOR` | New features, backwards-compatible |
| `MAJOR` | Breaking changes or a major milestone (e.g. production launch) |

### Files

| File | Purpose |
|---|---|
| `VERSION` | Single source of truth — one line, e.g. `0.9.1` |
| `CHANGELOG.md` | English, [Keep a Changelog](https://keepachangelog.com) format — for developers and GitHub |
| `app/templates/main/changelog.html` | Czech *Změny ve verzích* — rendered in the app at `/changelog` for all logged-in users |

### APP_VERSION vs GIT_COMMIT

Both are available in `app.config` and in Jinja2 templates as `config.APP_VERSION` / `config.GIT_COMMIT`:

| Key | Value | Purpose |
|---|---|---|
| `APP_VERSION` | `0.9.0` (from `VERSION` file) | Human-readable semantic version; shown in admin dashboard; stored in `UserFeedback.app_version` |
| `GIT_COMMIT` | `abc1234` (from Docker build arg) | Exact commit; used for static file cache-busting in `app/__init__.py`; shown in admin dashboard as a GitHub link |

`GIT_COMMIT` defaults to `"dev"` outside Docker (local dev, tests).

### Release process

```
1. Create a feature branch (or use the last feature branch for the release)

2. Update VERSION
   echo "0.9.1" > VERSION

3. Update CHANGELOG.md (English)
   - Move items from [Unreleased] into a new [0.9.1] - YYYY-MM-DD section
   - Keep the [Unreleased] section at the top (empty for now)
   - Update the compare URLs at the bottom

4. Update app/templates/main/changelog.html (Czech)
   - Add a new card for version 0.9.1 above the previous release card
   - Keep the "Chystané změny" card at the top (empty)

5. Commit:
   git add VERSION CHANGELOG.md app/templates/main/changelog.html
   git commit -m "chore: release v0.9.1"

6. Open PR, merge to main

7. Tag the merge commit on main:
   git checkout main && git pull
   git tag v0.9.1
   git push origin v0.9.1
```

### Keeping changelogs in sync

Both the English `CHANGELOG.md` and the Czech `changelog.html` must be updated together on every release.

**Different audiences, different content:**

| File | Audience | What to include |
|---|---|---|
| `CHANGELOG.md` | Developers, GitHub | Everything: features, bug fixes, security changes, infra, refactors, migrations |
| `changelog.html` | End users (Czech) | **Only changes that affect the user's workflow or are visible in the UI** |

**Czech changelog rules** — include only if the user would notice or care:
- New features and screens they can interact with
- Changes to existing workflows (e.g. a form field added/removed, a step changed)
- Bug fixes that were visibly wrong to the user
- New or changed automatic emails they receive

**Never include** in the Czech changelog:
- Security hardening (CSRF, CSP, TLS, encryption algorithms) — implement silently
- Performance optimisations, caching, query improvements
- Refactors, code cleanup, constant extractions
- Database migrations, Alembic, infrastructure changes
- Developer tooling, CI, test additions
- Internal admin features invisible to regular members (audit log internals, outbox traceability)
- Version bumps, changelog metadata itself

---

## Notification Catalog

The app sends 13 types of email notifications.  The **authoritative source of truth** is
`NOTIFICATION_CATALOG` in `app/mail.py`.  The admin UI at `/admin/notifications/` renders
this list and exposes per-type toggles stored in `AppSettings`.

### Rule: always update the catalog when changing notifications

**Whenever you add, rename, remove, or change the recipients/trigger of any `send_*`
function in `app/mail.py`, you MUST:**

1. Update or add the corresponding entry in `NOTIFICATION_CATALOG` (same file).
2. If the new notification should be togglable: add a `notify_<code>` boolean column
   to `AppSettings` (model + Alembic migration, default `True`) and set `settings_field`
   in the catalog entry accordingly.
3. Call `_is_notify_enabled("notify_<code>")` at the top of the new `send_*` function.
4. Pass `notification_type="<code>"` to `_enqueue()`.
5. Update `CHANGELOG.md` and `app/templates/main/changelog.html`.

Failure to update the catalog means the admin page will be out of sync with the actual
behaviour of the application.

### Notification toggles

Nine toggle fields are stored in `AppSettings` (one per togglable catalog entry; each
maps 1:1 to a `notify_<code>` column, per the rule above):

| Field | Controls |
|---|---|
| `notify_assignment` | `send_assignment_confirmed`, `send_assignment_released` |
| `notify_event_published` | `send_event_published` |
| `notify_assignments_opened` | `send_assignments_opened` |
| `notify_event_cancelled` | `send_event_cancelled` |
| `notify_event_archived` | `send_event_archived` |
| `notify_event_unarchived` | `send_event_unarchived` |
| `notify_event_changed` | `send_event_changed` |
| `notify_unfilled_reminder` | `send_unfilled_spots_reminder` (scheduler) |
| `notify_debriefing` | `send_debriefing_invitation` |

Auth-related notifications (`account_activated`, invite/password reset (`auth`), `admin_digest`)
are always-on and cannot be toggled.

---


## Database Migrations

This project uses **Flask-Migrate** (Alembic wrapper for Flask-SQLAlchemy).

```bash
# After changing a model:
flask db migrate -m "add preferred_calendar_view to user"

# Before committing: review the generated migration in migrations/versions/
# Then apply:
flask db upgrade

# Rollback one step:
flask db downgrade
```

Migrations run automatically on `web` container start via `docker-entrypoint.sh` (`flask db upgrade`). The scheduler container skips migrations and waits for web to be healthy.

### ⚠️ Do NOT re-squash the baseline once a DB exists

The migration history is a single MSSQL baseline (`*_mssql_baseline_schema.py`,
`down_revision = None`). That squash was a **one-time bootstrap**. Once any
long-lived database (dev on zerver, staging, or prod) has been created from a
baseline, **never re-squash or rewrite that baseline** — always add **forward
migrations** for schema changes instead:

```bash
flask db migrate -m "add color to event"   # new revision, down_revision = current head
```

**Why:** re-squashing rewrites the baseline's revision id and folds new columns
into it. An existing DB then (1) has an `alembic_version` pointing at a revision
that no longer exists (`Can't locate revision identified by '<old>'`), and
(2) is missing any columns the new baseline added — and because a single
baseline has no incremental step, `flask db upgrade` can never add them. The web
container fails its `flask verify-schema` health check and the deploy aborts.
This exact failure hit the zerver dev deploy of `feat/mssql-support` (the
re-squash that "included color" left dev DBs without `event.color`).

**This is not a dev-only risk — production is more exposed.** The mechanism is
environment-independent: it triggers for *any* database stamped at the old
baseline, including staging and prod. In fact, a re-squash that produces a
schema **identical** to the old one still breaks prod, because the squash mints
a *new* revision id and the dangling-`alembic_version` failure (1) fires
regardless. Prod is the worst place for it to land:

- The auto-run entrypoint (`flask db upgrade` → `flask verify-schema`) aborts on
  deploy, so the new version never becomes healthy — a **failed/blocked
  production release**, possibly mid-rollout.
- There is no disposable-volume escape hatch with real data: recovery is the
  manual path only — stamp `alembic_version` to head, then hand-write
  `ALTER TABLE`s to reconcile every divergence with the baseline, verifying with
  `flask verify-schema`. Error-prone under release pressure.

**CI / pre-commit guard.** `scripts/check_migrations.py` enforces this
mechanically and runs both in the CI `lint` job and as a pre-commit hook
(triggered when any `migrations/versions/*.py` changes). It fails the build if
the baseline (root) revision id ever changes from the frozen
`EXPECTED_BASELINE_REVISION`, if there is more than one root, or if history has
more than one head. The guard is a **forcing function, not an absolute ban** —
bumping `EXPECTED_BASELINE_REVISION` is allowed, but only as the documented step
4 of the procedure below, so a re-baseline can't merge by accident.

### Sanctioned re-baseline (squash) procedure

A re-squash *is* allowed when migration history has grown unwieldy — but it must
be done so that every durable DB survives it. The two rules that make it safe:

- **It must be schema-neutral.** The new baseline must reproduce the *current*
  head schema **exactly** — do not fold any new or changed columns into it. Any
  real schema change ships separately as a normal forward migration, either
  before or after the squash, never baked into it. (The `event.color` incident
  happened because a schema change rode along inside the squash.)
- **Every durable DB must be re-stamped** to the new baseline id during the
  deploy window — because the entrypoint's `flask db upgrade` reads the old,
  now-deleted revision and aborts before anything else runs.

Steps:

1. **Snapshot the current schema** of a representative up-to-date DB (dev is
   fine): `flask verify-schema` should pass on it first, so you know it matches
   today's head.
2. **Squash** the history into a single new baseline whose `down_revision = None`.
   Apply it to a **fresh, empty** DB and run `flask verify-schema` against it —
   it must report the *same* objects as the snapshot in step 1. If anything
   differs, the squash is not schema-neutral; fix it before proceeding.
3. **Record the new baseline revision id** (call it `NEW_ID`).
4. **Bump the guard:** set `EXPECTED_BASELINE_REVISION = "<NEW_ID>"` in
   `scripts/check_migrations.py` in the *same commit* as the squash. CI now
   passes; the diff is the audit trail.
5. **Re-stamp every durable DB** (dev on zerver, staging, prod) in the deploy
   window, *before* the new app image starts its auto-`upgrade`. Since the old
   code can't reach `NEW_ID`, do it with a plain SQL update against
   `alembic_version`:
   ```sql
   UPDATE alembic_version SET version_num = '<NEW_ID>';
   ```
   (Equivalent to `flask db stamp <NEW_ID>` once you can run the new code with
   auto-upgrade disabled. Take a DB backup first for prod.)
6. **Deploy** the new image normally. `flask db upgrade` now finds `NEW_ID` as
   head and is a no-op; `flask verify-schema` passes; the container goes healthy.
7. **Verify** each environment: container healthy + `flask verify-schema` OK.

If step 2 ever shows a schema difference, stop — that is exactly the failure this
whole section exists to prevent.

**Recovering a DB already stranded** by an *un*sanctioned past re-squash: stamp
`alembic_version` to the current head, then `ALTER TABLE` in the missing columns
to match the baseline (confirm with `flask verify-schema`) — or, **for dev only,
if the data is disposable**, drop the DB volume so the baseline applies fresh.

---

## Security Notes

### Content Security Policy (CSP)

The app sets a CSP header in all non-dev environments via `@app.after_request` in `app/__init__.py`:

```
default-src 'self';
script-src  'self' https://cdn.jsdelivr.net;
style-src   'self' https://cdn.jsdelivr.net 'unsafe-inline';
font-src    'self' https://cdn.jsdelivr.net;
img-src     'self' data:;
connect-src 'self' https://cdn.jsdelivr.net;
```

**Why `style-src` includes `'unsafe-inline'`:** FullCalendar v6 injects inline styles at runtime to render its calendar grid. There is no practical workaround without abandoning FullCalendar or adding per-request nonces. CSS `'unsafe-inline'` does not enable script execution, so the security impact is limited.

**Why `script-src` does NOT include `'unsafe-inline'`:** All JS is in external files. There are no `onclick`/`onchange`/`onsubmit` attributes in any template — inline handlers were removed in PR #93 and kept clean thereafter. This is the more important constraint to maintain.

**Why `https://` is explicit:** The scheme-free `cdn.jsdelivr.net` form is interpreted as the current page's scheme. Over HTTP it works, but the app is served over HTTPS in production, and an HTTP CDN resource would be blocked as mixed content. Always use `https://cdn.jsdelivr.net` in the CSP.

---

## Known Issues & Mitigations

### MSSQL on WSL2

MSSQL Server is a significantly heavier container than the PostgreSQL it replaced (approx. 1.5 GB image). On WSL2, allow extra time for the container to start and become healthy. The MSSQL health check retries up to 10 times with 10 s intervals, giving 100 s total — this is sufficient in practice.

If the container fails to start, check available memory: MSSQL Express requires at least 1 GB RAM. The compose file caps it at 512 MB buffer pool via `MSSQL_MEMORY_LIMIT_MB`; the OS-level limit should be at least 1.5 GB.

> **Historical note:** The dev stack originally used PostgreSQL 17. PostgreSQL was removed and replaced with MSSQL in [PR #381](https://github.com/spidermila/MedCover/pull/381).

---

## Dev Data Seeding

`scripts/seed_dev.py` creates a realistic dataset. Safe to run multiple times — idempotent.

**Dev accounts** (password: `devpassword`, email format: `dev.<role>@medcover.local`):

| Role | Email | Description |
|---|---|---|
| Admin | `dev.admin@medcover.local` | Full system access |
| Coordinator | `dev.coordinator@medcover.local` | Create/manage events |
| Member | `dev.member@medcover.local` | Join events, submit debriefings |
| Viewer | `dev.viewer@medcover.local` | Read-only access |
| Debrief Manager | `dev.debrief@medcover.local` | View/manage confidential debriefing records |
| Inactive | `dev.inactive@medcover.local` | Registered but not yet activated |

**Also seeded:**
- All Roles, Permissions (synced to `ROLE_PERMISSIONS` in `role.py`)
- Standard credential hierarchy (Záchranář, Zdravotník, Řidič, etc.)
- 2 named Master Events + the default General ME
- ~10 Events in various lifecycle states (planned, published, completed, cancelled)
- Assignments, equipment types, personal and shared items
- Completed events with DebriefingRecords
- AppSettings (id=1, setup_complete=True)

**After changing role permissions in `role.py`,** re-run the seeder to sync:

```bash
docker compose exec web python scripts/seed_dev.py
```

Or on the test server:
```bash
ssh <user>@<host> "cd /path/to/MedCover && docker compose exec web python scripts/seed_dev.py"
```

---

## Temporary File Storage

### Výkaz práce xlsx files

Generated monthly work-report files are stored in the Flask `instance/` directory:

```
instance/
  work_report/
    <user-uuid>/
      <year>-<MM>.xlsx   (e.g. 2026-05.xlsx)
```

- Each user has their own subdirectory; generating a new report for the same month overwrites the previous file.
- Files are **automatically deleted after 1 day** by the `cleanup_work_report` scheduler task (runs hourly in the `scheduler` container).
- **Do not commit these files** — the `instance/` directory is gitignored.
- The `holidays` Python package (Czech locale) is used to detect Czech public holidays for correct cell colouring. It is declared in `requirements.txt`.

---

## Secrets Management

| Secret | Where stored |
|---|---|
| `.env` local secrets | Local only — in `.gitignore`, never committed |
| `.env.prod` production secrets | Production server only — never committed |
| GitHub Actions secrets | GitHub repo → Settings → Secrets and variables → Actions |

The `.env.example` file is committed and documents every required variable with a description but no real values.

---

## Frontend Assets

### Help Icons — Standard Pattern

All user-facing labels, filters, buttons, and page section titles must include a help icon
whenever the concept or behaviour might not be immediately obvious to a new user.

**Macro:** `help_icon(text, title="Nápověda")` in `app/templates/macros/help.html`

```jinja
{% from 'macros/help.html' import help_icon %}

{# On a form label #}
<label class="form-label">Název {{ help_icon("Celý název akce, jak se zobrazí v přehledech.") }}</label>

{# On a page title #}
<h2 class="mb-0">Akce {{ help_icon("Vysvětlení konceptu...", "Nadpis nápovědy") }}</h2>

{# On a section header inside a card #}
<span class="fw-semibold">Moje akce {{ help_icon("Akce, na které jste přihlášeni...") }}</span>
```

The icon renders as a small `ⓘ` button that opens a Bootstrap popover on click/tap (works on
both desktop and mobile). Popovers are auto-initialized in `app-init.js`.

**When to add a help icon:**
- Every form field label that describes a non-trivial concept
- Page `<h2>` titles for main sections (Akce, Nadřazené akce, Vybavení, …)
- Dashboard section headings
- Filter controls that aren't self-explanatory
- Buttons with non-obvious side effects (e.g. status transitions)

**Text guidelines:**
- Write in Czech (all UI text is Czech)
- Be concise but complete — explain *why*, not just *what*
- For multi-line content use `\n•` bullet points within the string
- Keep under ~300 characters so the popover stays readable on mobile

**Do not add a help icon to:**
- Self-explanatory fields like "E-mail" or "Datum"
- Action buttons where the label is already fully descriptive ("Uložit", "Zrušit")

### Bootstrap

Bootstrap is loaded via CDN — no npm or build pipeline required.

| Asset | Version | CDN |
|---|---|---|
| `bootstrap.min.css` | 5.3.8 | jsDelivr |
| `bootstrap.bundle.min.js` | 5.3.8 | jsDelivr (includes Popper) |

SRI hashes in `app/templates/base.html` were generated directly from jsDelivr at the time of setup:

```
CSS sha384: sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB
JS  sha384: FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI
```

When upgrading Bootstrap, regenerate the hashes:
```bash
curl -s "https://cdn.jsdelivr.net/npm/bootstrap@VERSION/dist/css/bootstrap.min.css" \
  | openssl dgst -sha384 -binary | openssl base64 -A

curl -s "https://cdn.jsdelivr.net/npm/bootstrap@VERSION/dist/js/bootstrap.bundle.min.js" \
  | openssl dgst -sha384 -binary | openssl base64 -A
```
Then update the `integrity` attributes in `base.html`.

### Jinja2 Custom Filters

#### `localdt` — datetime formatting
Converts a UTC `datetime` to Europe/Prague local time.
```jinja
{{ event.start_datetime | localdt }}          {# default: "23.04.2025 14:00" #}
{{ event.start_datetime | localdt("%d.%m.%Y") }}   {# date only #}
```

#### `cznum` — Czech decimal formatting
Czech locale uses a **comma** as the decimal separator, not a dot.
All decimal numbers displayed in templates **must** use this filter.

```jinja
{{ value | cznum }}        {# 1 decimal place → "3,5" #}
{{ value | cznum(2) }}     {# 2 decimal places → "3,50" #}
```

- Registered in `app/__init__.py` alongside `localdt`.
- **Never** use `"%.1f"|format(x)` — that produces an English dot separator.
- Handles `None` gracefully (returns `—`).
