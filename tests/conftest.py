import os
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

from app import create_app
from app.extensions import db as _db
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.qualification import Qualification
from app.models.role import ALL_PERMISSIONS, ROLE_PERMISSIONS, Permission, Role
from app.models.settings import AppSettings
from app.models.user import UserAccount

if TYPE_CHECKING:
    pass

# All mutable tables — reference data (role, permission, role_permissions,
# app_settings, alembic_version) is preserved across the suite.
_MUTABLE_TABLES_LIST = [
    "event_equipment_assignment",
    "event_equipment_plan",
    "equipment_item",
    "equipment_type",
    "debriefing_record",
    "assignment",
    "event_spot",
    "spot_qualifications",
    "spot_template_qualifications",
    "event_spot_template",
    "event_template",
    "event",
    "master_event",
    "user_qualifications",
    "qualification_parents",
    "qualification",
    "registration_invite",
    "digest_metric_snapshot",
    "digest_block",
    "digest_schedule",
    "outbox_email",
    "audit_log_entry",
    "user_feedback",
    "user_roles",
    "user_account",
]

# ── Testcontainers: automatic MSSQL container lifecycle ───────────────────────
# The controller starts one container; xdist workers receive the URL via
# workerinput.  If TEST_DATABASE_URL is already set (CI service, local MSSQL)
# the container is skipped entirely.

_tc_mssql: object | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Start an MSSQL container when TEST_DATABASE_URL is not pre-set."""
    global _tc_mssql
    worker_input = getattr(config, "workerinput", None)
    if worker_input is not None:
        if "test_db_url" in worker_input:
            os.environ["TEST_DATABASE_URL"] = worker_input["test_db_url"]
        return

    if os.environ.get("TEST_DATABASE_URL"):
        url = os.environ["TEST_DATABASE_URL"]
        host, port, db_name, _, password = _parse_mssql_url(url)
        _wait_for_mssql(host, port, password)
        _create_mssql_db(host, port, password, db_name)
        _check_db_reachable(url)
        return

    from testcontainers.mssql import SqlServerContainer  # pylint: disable=import-outside-toplevel

    container = SqlServerContainer(
        image="mcr.microsoft.com/mssql/server:2022-latest",
        password="DevPassword123!",
        dbname="master",
        dialect="mssql+pyodbc",
    )
    container.with_env("MSSQL_PID", "Express")
    container.with_env("MSSQL_COLLATION", "Czech_100_CI_AS_SC_UTF8")
    container.start()

    host = container.get_container_host_ip()
    port = container.get_exposed_port(1433)

    # Wait for external connectivity — the internal sqlcmd health check can pass
    # before the TCP port is ready for external pyodbc connections.
    _wait_for_mssql(host, port, "DevPassword123!")
    _create_mssql_db(host, port, "DevPassword123!", "medcover_test")

    base_url = (
        f"mssql+pyodbc://SA:DevPassword123!@{host}:{port}/medcover_test"
        "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes"
    )
    os.environ["TEST_DATABASE_URL"] = base_url
    _tc_mssql = container
    config._testcontainer_url = base_url  # type: ignore[attr-defined]


def _check_db_reachable(url: str) -> None:
    """Exit immediately with a clear message if the DB is not reachable."""
    from sqlalchemy.exc import OperationalError  # pylint: disable=import-outside-toplevel

    try:
        engine = create_engine(url, connect_args={"connect_timeout": 5})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except OperationalError as exc:
        pytest.exit(
            f"\n\nERROR: Cannot connect to the test database.\n"
            f"  URL: {url}\n"
            f"  Reason: {exc.orig}\n\n"
            "Make sure the database container is running before running tests.\n"
            "  Local:  docker compose up -d db\n"
            "  Then:   pytest tests/\n",
            returncode=3,
        )


def pytest_configure_node(node: object) -> None:  # type: ignore[type-arg]
    """Pass the container URL to each xdist worker (controller side)."""
    url = getattr(node.config, "_testcontainer_url", None) or os.environ.get(  # type: ignore[attr-defined]
        "TEST_DATABASE_URL"
    )
    if url:
        node.workerinput["test_db_url"] = url  # type: ignore[attr-defined]


def pytest_unconfigure(config: pytest.Config) -> None:
    """Stop the MSSQL container once all tests have finished."""
    global _tc_mssql
    if _tc_mssql is not None:
        _tc_mssql.stop()  # type: ignore[attr-defined]
        _tc_mssql = None


# ── DB URL helpers ─────────────────────────────────────────────────────────────
# Read lazily (via function) so that pytest_configure can set os.environ
# before the URL is consumed by the app fixture.


def _base_test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "mssql+pyodbc://SA:DevPassword123!@localhost:1433/medcover_test"
        "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no&TrustServerCertificate=yes",
    )


def _worker_db_url(worker_id: str) -> str:
    """Return a worker-specific DB URL for xdist parallelism.

    Each xdist worker (gw0, gw1, …) gets its own database so that
    concurrent DELETE operations never conflict.  Non-parallel runs
    (worker_id == 'master') use the base URL unchanged.
    """
    base_url = _base_test_db_url()
    if worker_id == "master":
        return base_url
    # Parse URL to replace only the database name segment before the query string
    # e.g. mssql+pyodbc://SA:pwd@host:1433/medcover_test?driver=... →
    #      mssql+pyodbc://SA:pwd@host:1433/medcover_test_gw0?driver=...
    parts = urlsplit(base_url)
    db_name = parts.path.lstrip("/")
    new_path = f"/{db_name}_{worker_id}"
    return urlunsplit(parts._replace(path=new_path))


def _parse_mssql_url(url: str) -> tuple[str, int, str, str, str]:
    """Extract (host, port, db_name, user, password) from an MSSQL URL."""
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    port = parts.port or 1433
    db_name = parts.path.lstrip("/")
    user = parts.username or "SA"
    password = parts.password or ""
    return host, port, db_name, user, password


def _wait_for_mssql(host: str, port: int, sa_password: str, timeout: int = 60) -> None:
    """Wait until MSSQL accepts external pyodbc connections."""
    import time  # pylint: disable=import-outside-toplevel

    import pyodbc  # pylint: disable=import-outside-toplevel

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE=master;UID=SA;PWD={sa_password};Encrypt=no;TrustServerCertificate=yes"
    )
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pyodbc.connect(conn_str, timeout=3).close()
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(2)
    raise RuntimeError(f"MSSQL not ready after {timeout}s: {last_exc}") from last_exc


def _create_mssql_db(host: str, port: int, sa_password: str, db_name: str) -> None:
    """Create an MSSQL database with Czech collation and RCSI enabled."""
    import pyodbc  # pylint: disable=import-outside-toplevel

    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE=master;UID=SA;PWD={sa_password};Encrypt=no;TrustServerCertificate=yes"
    )
    conn = pyodbc.connect(conn_str)
    conn.autocommit = True
    c = conn.cursor()
    c.execute(
        f"IF NOT EXISTS (SELECT 1 FROM sys.databases WHERE name='{db_name}') "
        f"CREATE DATABASE [{db_name}] COLLATE Czech_100_CI_AS_SC_UTF8"
    )
    c.execute(f"ALTER DATABASE [{db_name}] SET READ_COMMITTED_SNAPSHOT ON")
    conn.close()


def _ensure_db_exists(db_url: str) -> None:
    """Create the worker database if it does not already exist."""
    host, port, db_name, _, password = _parse_mssql_url(db_url)
    _create_mssql_db(host, port, password, db_name)


def _drop_db(db_url: str) -> None:
    """Drop the worker database (only called for worker-specific DBs)."""
    import pyodbc  # pylint: disable=import-outside-toplevel

    host, port, db_name, _, password = _parse_mssql_url(db_url)
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={host},{port};"
        f"DATABASE=master;UID=SA;PWD={password};Encrypt=no;TrustServerCertificate=yes"
    )
    conn = pyodbc.connect(conn_str)
    conn.autocommit = True
    c = conn.cursor()
    # Force disconnect all active connections before dropping
    c.execute(
        f"IF EXISTS (SELECT 1 FROM sys.databases WHERE name='{db_name}') "
        f"ALTER DATABASE [{db_name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE"
    )
    c.execute(f"DROP DATABASE IF EXISTS [{db_name}]")
    conn.close()


@pytest.fixture(scope="session")
def app(worker_id: str):
    """Create Flask test application with a worker-specific DB.

    With pytest-xdist each worker gets its own database (medcover_test_gw0,
    medcover_test_gw1, …) so parallel TRUNCATE operations never conflict.
    Without xdist the plain medcover_test DB is used.
    """
    db_url = _worker_db_url(worker_id)
    _ensure_db_exists(db_url)

    # Use a pool of size 1 per worker: avoids per-query TCP connection overhead
    # (which caused MSSQL RCSI snapshot gaps in CI) while still ensuring each
    # worker has its own isolated connection. pool_reset_on_return="rollback"
    # keeps the connection clean between uses.
    flask_app = create_app(
        "testing",
        db_url=db_url,
        engine_options={
            "pool_size": 2,
            "max_overflow": 0,
            "pool_pre_ping": True,
            "pool_reset_on_return": "rollback",
        },
    )

    with flask_app.app_context():
        _db.drop_all()  # clear leftover types/tables from previous runs
        _db.create_all()
        _seed_reference_data()
        _db.session.remove()

    yield flask_app

    with flask_app.app_context():
        _db.drop_all()

    # Clean up worker-specific DB; leave the base medcover_test intact
    if worker_id != "master":
        _drop_db(db_url)


@pytest.fixture(scope="session")
def worker_id(request: pytest.FixtureRequest) -> str:
    """Return the xdist worker ID ('gw0', 'gw1', …) or 'master'."""
    return getattr(request.config, "workerinput", {}).get("workerid", "master")


def _seed_reference_data() -> None:
    """Seed roles, permissions and AppSettings — stable data tests depend on."""
    if not _db.session.get(AppSettings, 1):
        _db.session.add(AppSettings(id=1, org_name="Test Org", setup_complete=True))

    for perm_data in ALL_PERMISSIONS:
        if not _db.session.scalar(_db.select(Permission).where(Permission.code == perm_data["code"])):
            _db.session.add(Permission(code=perm_data["code"], description=perm_data["description"]))
    _db.session.flush()

    for role_name, perm_codes in ROLE_PERMISSIONS.items():
        role = _db.session.scalar(_db.select(Role).where(Role.name == role_name))
        if not role:
            role = Role(name=role_name)
            _db.session.add(role)
            _db.session.flush()
        target_codes = set(perm_codes)
        existing_codes = {p.code for p in role.permissions}
        for code in target_codes - existing_codes:
            perm = _db.session.scalar(_db.select(Permission).where(Permission.code == code))
            if perm:
                role.permissions.append(perm)
        for perm in list(role.permissions):
            if perm.code not in target_codes:
                role.permissions.remove(perm)

    _db.session.commit()


@pytest.fixture(autouse=True)
def clean_db(app):
    """Delete all mutable rows after every test to keep tests isolated.

    For MSSQL we disable FK constraints, delete all rows in each mutable
    table, then re-enable constraints. Identity columns are left as-is
    (tests don't depend on specific ID values).

    AppSettings is NOT cleared (it is reference data seeded once) but any
    fields that tests may mutate are explicitly reset to their defaults so that
    test order does not matter.
    """
    # Ensure a completely fresh session at the start of every test — eliminates
    # any lingering identity-map state or open transactions from fixture setup.
    with app.app_context():
        _db.session.remove()
    yield
    with app.app_context():
        _db.session.remove()
        with _db.engine.connect() as conn:
            preparer = _db.engine.dialect.identifier_preparer
            for t in _MUTABLE_TABLES_LIST:
                qt = preparer.quote(t)
                conn.execute(_db.text(f"ALTER TABLE {qt} NOCHECK CONSTRAINT ALL"))
            for t in _MUTABLE_TABLES_LIST:
                qt = preparer.quote(t)
                conn.execute(_db.text(f"DELETE FROM {qt}"))
            for t in _MUTABLE_TABLES_LIST:
                qt = preparer.quote(t)
                conn.execute(_db.text(f"ALTER TABLE {qt} CHECK CONSTRAINT ALL"))
            conn.commit()
        # Reset mutable AppSettings fields to their defaults
        settings = _db.session.get(AppSettings, 1)
        if settings:
            settings.dev_email_block = False
            settings.dev_email_allowlist = None
            settings.feedback_enabled = True
            settings.app_base_url = None
            settings.notify_assignment = True
            settings.notify_event_published = True
            settings.notify_assignments_opened = True
            settings.notify_event_cancelled = True
            settings.notify_event_changed = True
            settings.notify_unfilled_reminder = True
            settings.notify_debriefing = True
            _db.session.commit()
        _db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_client(app, client):
    """Test client pre-logged in as an activated admin user."""
    with app.app_context():
        _make_user("admin@test.com", "Test Admin", Role.ADMIN)
    _login(client, "admin@test.com")
    return client


@pytest.fixture
def coordinator_client(app, client):
    """Test client pre-logged in as an activated coordinator user."""
    with app.app_context():
        _make_user("coordinator@test.com", "Test Coordinator", Role.COORDINATOR)
    _login(client, "coordinator@test.com")
    return client


@pytest.fixture
def member_client(app, client):
    """Test client pre-logged in as an activated member user."""
    with app.app_context():
        _make_user("member@test.com", "Test Member", Role.MEMBER)
    _login(client, "member@test.com")
    return client


def _make_user(
    email: str,
    name: str,
    role_name: str,
    password: str = "testpass123",
) -> UserAccount:
    """Create a user in the current app context and return it."""
    role = _db.session.scalar(_db.select(Role).where(Role.name == role_name))
    user = UserAccount(email=email, name=name, is_active=True)
    user.set_password(password)
    user.roles = [role]
    _db.session.add(user)
    _db.session.commit()
    return user


def _get_csrf(client, url: str) -> str:
    """Fetch a page and extract the CSRF token from a hidden input."""
    resp = client.get(url)
    m = re.search(rb'name="csrf_token" value="([^"]+)"', resp.data)
    return m.group(1).decode() if m else ""


def _make_master_event(app, name: str = "Test ME", **kwargs) -> int:
    """Create a MasterEvent and return its ID."""
    with app.app_context():
        me = MasterEvent(name=name, **kwargs)
        _db.session.add(me)
        _db.session.commit()
        return me.id


def _make_event_with_spot(
    app,
    status: EventStatus = EventStatus.ASSIGNMENTS_OPEN,
    name: str = "Test Event",
    me_id: int | None = None,
    address: str | None = None,
) -> tuple[int, int]:
    """Create ME → Event → EventSpot and return (event_id, spot_id)."""
    with app.app_context():
        if me_id is None:
            me = MasterEvent(name=f"ME for {name}")
            _db.session.add(me)
            _db.session.flush()
            me_id = me.id
        event = Event(
            name=name,
            master_event_id=me_id,
            status=status,
            start_datetime=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            address=address,
        )
        _db.session.add(event)
        _db.session.flush()
        spot = EventSpot(event_id=event.id)
        _db.session.add(spot)
        _db.session.commit()
        return event.id, spot.id


def _make_event_in_status(
    app,
    status: EventStatus = EventStatus.DRAFT,
    name: str = "Test Event",
    start: datetime | None = None,
    end: datetime | None = None,
    address: str | None = None,
    me_id: int | None = None,
) -> int:
    """Create ME → Event (no spot) and return event_id."""
    with app.app_context():
        if me_id is None:
            me = MasterEvent(name=f"ME for {name}")
            _db.session.add(me)
            _db.session.flush()
            me_id = me.id
        event = Event(
            name=name,
            master_event_id=me_id,
            status=status,
            start_datetime=start or datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
            end_datetime=end or datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc),
            address=address,
        )
        _db.session.add(event)
        _db.session.commit()
        return event.id


def _make_user_with_qual(app, email: str, qual_id: int) -> str:
    """Create a Member user with the given qualification; return str(user.id)."""

    with app.app_context():
        qual = _db.session.get(Qualification, qual_id)
        u = _make_user(email, "Test User", Role.MEMBER)
        u.qualifications = [qual]
        _db.session.commit()
        return str(u.id)


def _login(client, email: str, password: str = "testpass123") -> None:
    """Log in via the auth endpoint."""
    client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=True,
    )
