"""Tests for the qualifications blueprint (CRUD + permission enforcement)."""
from __future__ import annotations

from app.extensions import db
from app.models.qualification import Qualification
from app.models.audit import AuditLogEntry
from app.models.role import Role
from app.models.user import UserAccount


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_qual(name: str, description: str | None = None) -> Qualification:
    q = Qualification(name=name, description=description)
    db.session.add(q)
    db.session.commit()
    return q


def _make_user_with_qual(app, qual: Qualification) -> UserAccount:
    role = db.session.scalar(db.select(Role).where(Role.name == Role.MEMBER))
    user = UserAccount(email="holder@test.com", name="Holder", is_active=True)
    user.set_password("testpass")
    user.roles = [role]
    user.qualifications.append(qual)
    db.session.add(user)
    db.session.commit()
    return user


def _make_event_spot_with_qual(qual: Qualification) -> int:
    """Create a minimal MasterEvent → Event → EventSpot and attach qual. Returns spot id."""
    from app.models.master_event import MasterEvent
    from app.models.event import Event, EventStatus, EventSpot
    from datetime import datetime, timezone, timedelta
    me = MasterEvent(name="Test ME for spot")
    db.session.add(me)
    db.session.flush()
    now = datetime.now(timezone.utc)
    ev = Event(
        name="Test Event for spot",
        master_event_id=me.id,
        status=EventStatus.DRAFT,
        start_datetime=now,
        end_datetime=now + timedelta(hours=2),
        version=1,
    )
    db.session.add(ev)
    db.session.flush()
    spot = EventSpot(event_id=ev.id)
    spot.required_qualifications.append(qual)
    db.session.add(spot)
    db.session.commit()
    return spot.id


# ── Index ─────────────────────────────────────────────────────────────────────

class TestQualificationIndex:
    def test_requires_login(self, client):
        resp = client.get("/qualifications/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_member_can_view_list(self, member_client):
        # Members have qualification.view permission
        resp = member_client.get("/qualifications/")
        assert resp.status_code == 200

    def test_admin_can_view_list(self, admin_client):
        resp = admin_client.get("/qualifications/")
        assert resp.status_code == 200

    def test_coordinator_can_view_list(self, coordinator_client):
        resp = coordinator_client.get("/qualifications/")
        assert resp.status_code == 200

    def test_member_sees_nav_link(self, member_client):
        """Issue #73: Members must see the Kvalifikace nav link (was hidden behind admin.view)."""
        resp = member_client.get("/qualifications/")
        assert b"Kvalifikace" in resp.data

    def test_coordinator_sees_nav_link(self, coordinator_client):
        """Issue #73: Coordinators must see the Kvalifikace nav link."""
        resp = coordinator_client.get("/qualifications/")
        assert b"Kvalifikace" in resp.data

    def test_list_shows_qualifications(self, app, admin_client):
        with app.app_context():
            _make_qual("Zdravotník")
            _make_qual("Řidič")
        resp = admin_client.get("/qualifications/")
        assert "Zdravotník".encode() in resp.data
        assert "Řidič".encode() in resp.data

    def test_empty_list_renders(self, admin_client):
        resp = admin_client.get("/qualifications/")
        assert resp.status_code == 200


# ── Create ────────────────────────────────────────────────────────────────────

class TestQualificationCreate:
    def test_get_create_page_admin(self, admin_client):
        resp = admin_client.get("/qualifications/create")
        assert resp.status_code == 200

    def test_member_cannot_access_create(self, member_client):
        resp = member_client.get("/qualifications/create")
        assert resp.status_code == 403

    def test_create_qualification(self, app, admin_client):
        resp = admin_client.post(
            "/qualifications/create",
            data={"name": "Zdravotník", "description": "Základní zdravotník"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Zdravotník".encode() in resp.data
        with app.app_context():
            q = db.session.scalar(db.select(Qualification).where(Qualification.name == "Zdravotník"))
            assert q is not None
            assert q.description == "Základní zdravotník"

    def test_create_writes_audit_log(self, app, admin_client):
        admin_client.post(
            "/qualifications/create",
            data={"name": "AuditTest"},
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "Qualification",
                    AuditLogEntry.action_type == "create",
                )
            )
            assert entry is not None
            assert "AuditTest" in entry.summary

    def test_create_empty_name_rejected(self, admin_client):
        resp = admin_client.post(
            "/qualifications/create",
            data={"name": ""},
            follow_redirects=True,
        )
        assert "povinný".encode() in resp.data

    def test_create_duplicate_name_rejected(self, app, admin_client):
        with app.app_context():
            _make_qual("Duplikát")
        resp = admin_client.post(
            "/qualifications/create",
            data={"name": "Duplikát"},
            follow_redirects=True,
        )
        assert "již existuje".encode() in resp.data

    def test_create_with_parent(self, app, admin_client):
        with app.app_context():
            parent = _make_qual("Rodič")
            parent_id = parent.id
        resp = admin_client.post(
            "/qualifications/create",
            data={"name": "Dítě", "parent_ids": [str(parent_id)]},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            child = db.session.scalar(db.select(Qualification).where(Qualification.name == "Dítě"))
            assert child is not None
            assert len(child.parents) == 1
            assert child.parents[0].name == "Rodič"

    def test_coordinator_cannot_create(self, coordinator_client):
        # Coordinators only have qualification.view, not qualification.create
        resp = coordinator_client.post(
            "/qualifications/create",
            data={"name": "CoordQual"},
            follow_redirects=True,
        )
        assert resp.status_code == 403


# ── Edit ──────────────────────────────────────────────────────────────────────

class TestQualificationEdit:
    def test_get_edit_page(self, app, admin_client):
        with app.app_context():
            q = _make_qual("Editovatelná")
            qid = q.id
        resp = admin_client.get(f"/qualifications/{qid}/edit")
        assert resp.status_code == 200
        assert "Editovatelná".encode() in resp.data

    def test_edit_404_for_missing(self, admin_client):
        resp = admin_client.get("/qualifications/99999/edit")
        assert resp.status_code == 404

    def test_member_cannot_edit(self, app, member_client):
        with app.app_context():
            q = _make_qual("MemberEdit")
            qid = q.id
        resp = member_client.get(f"/qualifications/{qid}/edit")
        assert resp.status_code == 403

    def test_edit_updates_name(self, app, admin_client):
        with app.app_context():
            q = _make_qual("Starý název")
            qid = q.id
        resp = admin_client.post(
            f"/qualifications/{qid}/edit",
            data={"name": "Nový název"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            updated = db.session.get(Qualification, qid)
            assert updated is not None
            assert updated.name == "Nový název"

    def test_edit_writes_audit_log(self, app, admin_client):
        with app.app_context():
            q = _make_qual("Auditovaná")
            qid = q.id
        admin_client.post(
            f"/qualifications/{qid}/edit",
            data={"name": "Auditovaná Upravená"},
            follow_redirects=True,
        )
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "Qualification",
                    AuditLogEntry.action_type == "edit",
                )
            )
            assert entry is not None

    def test_edit_empty_name_rejected(self, app, admin_client):
        with app.app_context():
            q = _make_qual("NePrázdná")
            qid = q.id
        resp = admin_client.post(
            f"/qualifications/{qid}/edit",
            data={"name": ""},
            follow_redirects=True,
        )
        assert "povinný".encode() in resp.data

    def test_edit_duplicate_name_rejected(self, app, admin_client):
        with app.app_context():
            _make_qual("Existující")
            q2 = _make_qual("Cíl")
            qid = q2.id
        resp = admin_client.post(
            f"/qualifications/{qid}/edit",
            data={"name": "Existující"},
            follow_redirects=True,
        )
        assert "již existuje".encode() in resp.data

    def test_edit_updates_parents(self, app, admin_client):
        with app.app_context():
            parent = _make_qual("NovýRodič")
            child = _make_qual("Potomek")
            pid = parent.id
            cid = child.id
        admin_client.post(
            f"/qualifications/{cid}/edit",
            data={"name": "Potomek", "parent_ids": [str(pid)]},
            follow_redirects=True,
        )
        with app.app_context():
            updated = db.session.get(Qualification, cid)
            assert updated is not None
            assert any(p.id == pid for p in updated.parents)

    def test_edit_clears_parents_when_none_submitted(self, app, admin_client):
        with app.app_context():
            parent = _make_qual("OldParent")
            child = _make_qual("ChildClear")
            child.parents.append(parent)
            db.session.commit()
            cid = child.id
        admin_client.post(
            f"/qualifications/{cid}/edit",
            data={"name": "ChildClear"},  # no parent_ids
            follow_redirects=True,
        )
        with app.app_context():
            updated = db.session.get(Qualification, cid)
            assert updated is not None
            assert updated.parents == []


# ── Delete ────────────────────────────────────────────────────────────────────

class TestQualificationDelete:
    def test_delete_qualification(self, app, admin_client):
        """Deleting an unreferenced qualification soft-deletes it."""
        with app.app_context():
            q = _make_qual("KeSmazání")
            qid = q.id
        resp = admin_client.post(f"/qualifications/{qid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            q = db.session.get(Qualification, qid)
            assert q is not None
            assert q.is_deleted is True
            assert q.deleted_at is not None

    def test_delete_writes_audit_log(self, app, admin_client):
        with app.app_context():
            q = _make_qual("AuditDelete")
            qid = q.id
        admin_client.post(f"/qualifications/{qid}/delete", follow_redirects=True)
        with app.app_context():
            entry = db.session.scalar(
                db.select(AuditLogEntry).where(
                    AuditLogEntry.entity_type == "Qualification",
                    AuditLogEntry.action_type == "delete",
                )
            )
            assert entry is not None

    def test_delete_cascades_user_qualification(self, app, admin_client):
        """Deleting a qualification removes it from users and soft-deletes it."""
        with app.app_context():
            q = _make_qual("Přiřazená")
            user = _make_user_with_qual(app, q)
            qid = q.id
            uid = user.id
        resp = admin_client.post(f"/qualifications/{qid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            q = db.session.get(Qualification, qid)
            assert q.is_deleted is True
            user = db.session.get(UserAccount, uid)
            # qualification removed from user
            assert all(uq.id != qid for uq in user.qualifications)

    def test_delete_cascades_active_spot(self, app, admin_client):
        """Qualification is removed from active (DRAFT) event spots on delete."""
        from app.models.event import EventSpot
        with app.app_context():
            q = _make_qual("SpotQual")
            qid = q.id
            q_ref = db.session.get(Qualification, qid)
            spot_id = _make_event_spot_with_qual(q_ref)
        resp = admin_client.post(f"/qualifications/{qid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            q = db.session.get(Qualification, qid)
            assert q.is_deleted is True
            spot = db.session.get(EventSpot, spot_id)
            # qualification unlinked from the DRAFT spot
            assert all(rq.id != qid for rq in spot.required_qualifications)

    def test_delete_keeps_tombstone_on_completed_spot(self, app, admin_client):
        """Qualification stays linked (tombstone) on completed/cancelled event spots."""
        from app.models.event import EventSpot, Event, EventStatus
        from datetime import datetime, timezone, timedelta
        from app.models.master_event import MasterEvent
        with app.app_context():
            q = _make_qual("CompletedSpotQual")
            qid = q.id
            # Build a completed event with this qualification
            me = MasterEvent(name="Completed ME")
            db.session.add(me)
            db.session.flush()
            now = datetime.now(timezone.utc)
            ev = Event(
                name="Completed Event",
                master_event_id=me.id,
                status=EventStatus.COMPLETED,
                start_datetime=now - timedelta(days=1),
                end_datetime=now,
                version=1,
            )
            db.session.add(ev)
            db.session.flush()
            spot = EventSpot(event_id=ev.id)
            spot.required_qualifications.append(db.session.get(Qualification, qid))
            db.session.add(spot)
            db.session.commit()
            spot_id = spot.id
        resp = admin_client.post(f"/qualifications/{qid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        with app.app_context():
            q = db.session.get(Qualification, qid)
            assert q.is_deleted is True
            spot = db.session.get(EventSpot, spot_id)
            # tombstone: qualification still linked on the completed spot
            assert any(rq.id == qid for rq in spot.required_qualifications)

    def test_delete_404_for_missing(self, admin_client):
        resp = admin_client.post("/qualifications/99999/delete")
        assert resp.status_code == 404

    def test_member_cannot_delete(self, app, member_client):
        with app.app_context():
            q = _make_qual("MemberDel")
            qid = q.id
        resp = member_client.post(f"/qualifications/{qid}/delete")
        assert resp.status_code == 403

    def test_deleted_qualification_not_shown_on_user_page(self, app, admin_client):
        """Soft-deleted qualifications must not appear in the user detail qualification list."""
        with app.app_context():
            q = _make_qual("SoftDeletedQual")
            qid = q.id
            user = _make_user_with_qual(app, q)
            uid = user.id
        # Delete the qualification
        admin_client.post(f"/qualifications/{qid}/delete", follow_redirects=True)
        # The user detail page must not show the deleted qualification
        resp = admin_client.get(f"/users/{uid}")
        assert resp.status_code == 200
        assert b"SoftDeletedQual" not in resp.data


# ── Model: can_be_filled_by ───────────────────────────────────────────────────

class TestCanBeFilledBy:
    def test_same_qualification_fills_itself(self, app):
        with app.app_context():
            q = _make_qual("Same")
            assert q.can_be_filled_by(q)

    def test_parent_fills_child_spot(self, app):
        """A more advanced (parent) qualification can fill a less advanced (child) spot."""
        with app.app_context():
            parent = _make_qual("Parent")
            child = _make_qual("Child")
            child.parents.append(parent)
            db.session.commit()
            # child.can_be_filled_by(parent) = "can holder of parent fill spot requiring child?" → True
            assert child.can_be_filled_by(parent)

    def test_child_cannot_fill_parent_spot(self, app):
        """A less advanced (child) qualification cannot fill a more advanced (parent) spot."""
        with app.app_context():
            parent = _make_qual("ParentOnly")
            child = _make_qual("ChildOnly")
            child.parents.append(parent)
            db.session.commit()
            # parent.can_be_filled_by(child) = "can holder of child fill spot requiring parent?" → False
            assert not parent.can_be_filled_by(child)

    def test_grandparent_fills_grandchild_spot(self, app):
        """Multi-level: most advanced qualification can fill spots at any level below it."""
        with app.app_context():
            gp = _make_qual("GrandParent")
            p = _make_qual("Parent2")
            c = _make_qual("GrandChild")
            p.parents.append(gp)   # gp can fill p's spots
            c.parents.append(p)    # p can fill c's spots → gp can fill c's spots transitively
            db.session.commit()
            # c.can_be_filled_by(gp) → True (transitively)
            assert c.can_be_filled_by(gp)

    def test_unrelated_does_not_fill(self, app):
        with app.app_context():
            a = _make_qual("QualA")
            b = _make_qual("QualB")
            assert not a.can_be_filled_by(b)
            assert not b.can_be_filled_by(a)

    def test_cycle_does_not_crash(self, app):
        """Cyclic parent relationships must not cause infinite recursion."""
        with app.app_context():
            a = _make_qual("CycleA")
            b = _make_qual("CycleB")
            a.parents.append(b)
            b.parents.append(a)
            db.session.commit()
            # Should complete without RecursionError
            result = a.can_be_filled_by(b)
            assert isinstance(result, bool)
