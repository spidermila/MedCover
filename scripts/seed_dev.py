"""
Seed script for the development environment.

Creates roles, permissions, dev user accounts, qualifications, master events,
~20 events in various lifecycle states, assignments, equipment, and debriefings.
Safe to run multiple times — checks for existing data before inserting.

Usage:
    python scripts/seed_dev.py

Or from within the Docker web container:
    docker compose exec web python scripts/seed_dev.py
"""
import os
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env so DATABASE_URL / SECRET_KEY are available when running outside Docker
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed — env vars must be set manually

from app import create_app
from app.extensions import db
from app.models.user import UserAccount
from app.models.role import Role, Permission, ALL_PERMISSIONS, ROLE_PERMISSIONS
from app.models.master_event import MasterEvent
from app.models.event import Event, EventStatus, EventSpot
from app.models.qualification import Qualification
from app.models.assignment import Assignment, DebriefingRecord
from app.models.equipment import EquipmentType, EquipmentItem, EquipmentCategory
from app.models.settings import AppSettings
from app.routes.dev import DEV_ACCOUNTS


def _get_or_create_user(email: str) -> UserAccount | None:
    return db.session.scalar(db.select(UserAccount).where(UserAccount.email == email))


def _get_or_create_qual(name: str, description: str = "") -> Qualification:
    qual = db.session.scalar(db.select(Qualification).where(Qualification.name == name))
    if not qual:
        qual = Qualification(name=name, description=description)
        db.session.add(qual)
        db.session.flush()
    return qual


def _get_or_create_me(name: str, **kwargs: object) -> MasterEvent:
    me = db.session.scalar(db.select(MasterEvent).where(MasterEvent.name == name))
    if not me:
        me = MasterEvent(name=name, **kwargs)
        db.session.add(me)
        db.session.flush()
    return me


def _get_or_create_event(name: str, **kwargs: object) -> Event:
    ev = db.session.scalar(db.select(Event).where(Event.name == name))
    if not ev:
        ev = Event(name=name, **kwargs)
        db.session.add(ev)
        db.session.flush()
    return ev


def _get_or_create_equip_type(name: str, category: EquipmentCategory, description: str = "") -> EquipmentType:
    et = db.session.scalar(db.select(EquipmentType).where(EquipmentType.name == name))
    if not et:
        et = EquipmentType(name=name, category=category, description=description)
        db.session.add(et)
        db.session.flush()
    return et


def seed() -> None:
    app = create_app("development")

    with app.app_context():
        if not app.config.get("DEV_LOGIN_ENABLED"):
            print("ERROR: DEV_LOGIN_ENABLED is not set. Refusing to seed.")
            print("Add DEV_LOGIN_ENABLED=true to your .env file.")
            sys.exit(1)

        now = datetime.now(timezone.utc)

        # ── AppSettings ───────────────────────────────────────────────────────
        settings = db.session.get(AppSettings, 1)
        if not settings:
            print("Seeding AppSettings...")
            db.session.add(AppSettings(id=1, setup_complete=True, org_name="Czech Red Cross (Dev)"))
            db.session.flush()
        elif not settings.setup_complete:
            print("Marking AppSettings setup_complete=True...")
            settings.setup_complete = True
            db.session.flush()

        # ── Permissions & roles ───────────────────────────────────────────────
        print("Seeding permissions...")
        for perm_data in ALL_PERMISSIONS:
            if not db.session.scalar(db.select(Permission).where(Permission.code == perm_data["code"])):
                db.session.add(Permission(code=perm_data["code"], description=perm_data["description"]))
        db.session.flush()

        print("Seeding roles and assigning permissions...")
        for role_name, perm_codes in ROLE_PERMISSIONS.items():
            role = db.session.scalar(db.select(Role).where(Role.name == role_name))
            if not role:
                role = Role(name=role_name)
                db.session.add(role)
                db.session.flush()
            target_codes = set(perm_codes)
            existing_codes = {p.code for p in role.permissions}
            # Add missing permissions
            for code in target_codes - existing_codes:
                perm = db.session.scalar(db.select(Permission).where(Permission.code == code))
                if perm:
                    role.permissions.append(perm)
            # Remove stale permissions no longer in the role definition
            for perm in list(role.permissions):
                if perm.code not in target_codes:
                    role.permissions.remove(perm)
                    print(f"  Removed stale permission '{perm.code}' from role '{role_name}'")
        db.session.flush()

        # ── Dev user accounts ─────────────────────────────────────────────────
        print("Seeding dev user accounts...")
        role_map = {
            "admin": Role.ADMIN,
            "coordinator": Role.COORDINATOR,
            "member": Role.MEMBER,
            "viewer": Role.VIEWER,
            "debrief_manager": Role.DEBRIEFING_MANAGER,
            "inactive": Role.MEMBER,
        }
        for account_def in DEV_ACCOUNTS:
            email = account_def["email"]
            if db.session.scalar(db.select(UserAccount).where(UserAccount.email == email)):
                print(f"  {email} already exists, skipping.")
                continue
            user = UserAccount(
                email=email,
                name=account_def["name"],
                is_active=(account_def["role"] != "inactive"),
            )
            user.set_password("devpassword")
            role_name = role_map[account_def["role"]]
            role_obj = db.session.scalar(db.select(Role).where(Role.name == role_name))
            if role_obj:
                user.roles.append(role_obj)
            db.session.add(user)
            status = "inactive" if account_def["role"] == "inactive" else "active"
            print(f"  Created {email} ({status})")
        db.session.commit()

        # ── Convenience handles ───────────────────────────────────────────────
        admin_user = _get_or_create_user("admin@medcover.dev")
        coordinator_user = _get_or_create_user("coordinator@medcover.dev")
        member_user = _get_or_create_user("member@medcover.dev")

        # ── Credentials ───────────────────────────────────────────────────────
        print("Seeding qualifications...")
        c_zdravotnik = _get_or_create_qual(
            "Zdravotník zotavovacích akcí",
            "Základní zdravotnická způsobilost pro zotavovací akce."
        )
        c_zachranar = _get_or_create_qual(
            "Záchranář",
            "Zdravotní záchranář — rozšířená způsobilost."
        )
        c_lekar = _get_or_create_qual(
            "Lékař",
            "Absolvent lékařské fakulty, způsobilý k výkonu povolání lékaře."
        )
        c_ridic = _get_or_create_qual(
            "Řidič sanitky",
            "Oprávnění řídit sanitní vozidlo, řidičský průkaz sk. B+."
        )
        # Hierarchy: Záchranář can fill Zdravotník spot; Lékař can fill Záchranář spot
        if c_zachranar not in c_zdravotnik.parents:
            c_zdravotnik.parents.append(c_zachranar)
        if c_lekar not in c_zachranar.parents:
            c_zachranar.parents.append(c_lekar)
        # RP-eligible qualifications
        c_zdravotnik.can_be_rp = True
        c_zachranar.can_be_rp = True
        c_lekar.can_be_rp = True
        db.session.flush()

        # Assign qualifications to dev users
        if coordinator_user and c_zachranar not in coordinator_user.qualifications:
            coordinator_user.qualifications.append(c_zachranar)
        if member_user and c_zdravotnik not in member_user.qualifications:
            member_user.qualifications.append(c_zdravotnik)
        if member_user and c_ridic not in member_user.qualifications:
            member_user.qualifications.append(c_ridic)
        db.session.commit()

        # ── Master events ─────────────────────────────────────────────────────
        print("Seeding master events...")
        me_general = _get_or_create_me(
            "Obecné",
            description="Výchozí nadřazená akce pro akce bez specifického zařazení.",
            is_general=True,
        )
        me_festival = _get_or_create_me(
            "Letní festival 2026",
            description="Zdravotní zabezpečení letního hudebního festivalu.",
            coordinator_id=coordinator_user.id if coordinator_user else None,
        )
        me_sport = _get_or_create_me(
            "Sportovní závody 2026",
            description="Zajištění zdravotní péče při sportovních akcích.",
        )
        db.session.commit()

        # ── Equipment types & items ───────────────────────────────────────────
        print("Seeding equipment...")

        et_aed = _get_or_create_equip_type("AED", EquipmentCategory.SHARED, "AED")
        et_batoh_m = _get_or_create_equip_type("Batoh malý", EquipmentCategory.SHARED, "malá lékárnička")
        et_batoh_v = _get_or_create_equip_type("Batoh velký", EquipmentCategory.SHARED, "velká lékárnička")
        _get_or_create_equip_type("KPR figurína adolescent", EquipmentCategory.SHARED, "")
        _get_or_create_equip_type("KPR figurína dospělá standardní", EquipmentCategory.SHARED, "")
        _get_or_create_equip_type("KPR figurína dospělá tlustá", EquipmentCategory.SHARED, "")
        et_mimino = _get_or_create_equip_type("KPR figurína mimino", EquipmentCategory.SHARED, "")
        _get_or_create_equip_type("Mikina", EquipmentCategory.PERSONAL, "")
        _get_or_create_equip_type("Nosítka", EquipmentCategory.SHARED, "Skládací transportní nosítka.")
        _get_or_create_equip_type("Sanitka", EquipmentCategory.SHARED, "")
        et_stan = _get_or_create_equip_type("Stan", EquipmentCategory.SHARED, "")
        _get_or_create_equip_type("Uniforma blůza", EquipmentCategory.PERSONAL, "")
        _get_or_create_equip_type("Uniforma kalhoty", EquipmentCategory.PERSONAL, "")
        _get_or_create_equip_type("VR sada", EquipmentCategory.SHARED, "")

        def _add_item(
            equip_type: EquipmentType,
            name: str,
            serial: str | None,
            location: str,
            notes: str = "",
        ) -> EquipmentItem:
            existing = db.session.scalar(
                db.select(EquipmentItem).where(EquipmentItem.name == name)
            )
            if existing:
                return existing
            item = EquipmentItem(
                name=name,
                type_id=equip_type.id,
                serial_number=serial or None,
                home_location=location,
                notes=notes or None,
            )
            db.session.add(item)
            db.session.flush()
            return item

        _add_item(et_aed, "AED Phillips Sanitka", "123123", "Sanitka")
        _add_item(et_batoh_m, "Batoh 1", None, "spolek")
        _add_item(et_batoh_m, "Batoh 2", None, "spolek")
        _add_item(et_batoh_v, "Velký batoh 1", None, "spolek")
        _add_item(et_mimino, "mimino 1", None, "spolek")
        _add_item(et_stan, "Stan starý", None, "spolek")
        db.session.commit()

        # ── Events ────────────────────────────────────────────────────────────
        print("Seeding events...")

        def _spot(event: Event, label: str, creds: list[Qualification]) -> EventSpot:
            spot = db.session.scalar(
                db.select(EventSpot).where(EventSpot.event_id == event.id, EventSpot.description == label)
            )
            if not spot:
                spot = EventSpot(event_id=event.id, description=label)
                db.session.add(spot)
                db.session.flush()
            for c in creds:
                if c not in spot.required_qualifications:
                    spot.required_qualifications.append(c)
            db.session.flush()
            return spot

        def _assign(spot: EventSpot, user: UserAccount) -> Assignment | None:
            if spot.assignment:
                return spot.assignment
            a = Assignment(spot_id=spot.id, user_id=user.id)
            db.session.add(a)
            db.session.flush()
            return a

        # 1. Draft event (upcoming)
        e1 = _get_or_create_event(
            "Zdravotní dohled — fotbalový turnaj",
            master_event_id=me_sport.id,
            status=EventStatus.DRAFT,
            start_datetime=now + timedelta(days=30),
            end_datetime=now + timedelta(days=30, hours=6),
            address="Sportovní hřiště, Praha 10",
            paid=False,
            created_by_id=coordinator_user.id if coordinator_user else None,
        )
        _spot(e1, "Zdravotník 1", [c_zdravotnik])
        _spot(e1, "Zdravotník 2", [c_zdravotnik])

        # 2. Published (no assignments open yet)
        e2 = _get_or_create_event(
            "Zdravotní zabezpečení — maratón",
            master_event_id=me_sport.id,
            status=EventStatus.PUBLISHED,
            start_datetime=now + timedelta(days=20),
            end_datetime=now + timedelta(days=20, hours=8),
            address="Náměstí Míru, Praha 2",
            paid=True,
            description="Zdravotní zabezpečení městského maratónu. Trasa 42 km.",
            created_by_id=coordinator_user.id if coordinator_user else None,
        )
        _spot(e2, "Záchranář — start", [c_zachranar])
        _spot(e2, "Zdravotník — cíl", [c_zdravotnik])
        _spot(e2, "Řidič sanitky", [c_ridic])

        # 3. Assignments open — partially filled
        e3 = _get_or_create_event(
            "Letní festival — pátek",
            master_event_id=me_festival.id,
            status=EventStatus.ASSIGNMENTS_OPEN,
            start_datetime=now + timedelta(days=14),
            end_datetime=now + timedelta(days=14, hours=10),
            address="Výstaviště Praha, pavilon A",
            paid=True,
            description="Zdravotní stanoviště na letním festivalu, páteční směna.",
            responsible_person_id=coordinator_user.id if coordinator_user else None,
            created_by_id=coordinator_user.id if coordinator_user else None,
        )
        spot_e3_1 = _spot(e3, "Záchranář — stanoviště 1", [c_zachranar])
        spot_e3_2 = _spot(e3, "Zdravotník — stanoviště 2", [c_zdravotnik])
        _spot(e3, "Zdravotník — obchůzka", [c_zdravotnik])
        if coordinator_user:
            _assign(spot_e3_1, coordinator_user)
        if member_user:
            _assign(spot_e3_2, member_user)

        # 4. Assignments open — fully filled
        e4 = _get_or_create_event(
            "Letní festival — sobota",
            master_event_id=me_festival.id,
            status=EventStatus.ASSIGNMENTS_OPEN,
            start_datetime=now + timedelta(days=15),
            end_datetime=now + timedelta(days=15, hours=12),
            address="Výstaviště Praha, pavilon A",
            paid=True,
            description="Zdravotní stanoviště na letním festivalu, sobotní směna.",
            responsible_person_id=coordinator_user.id if coordinator_user else None,
            created_by_id=coordinator_user.id if coordinator_user else None,
        )
        spot_e4_1 = _spot(e4, "Záchranář — stanoviště 1", [c_zachranar])
        spot_e4_2 = _spot(e4, "Zdravotník — stanoviště 2", [c_zdravotnik])
        if coordinator_user:
            _assign(spot_e4_1, coordinator_user)
        if member_user:
            _assign(spot_e4_2, member_user)

        # 5. Assignments closed
        e5 = _get_or_create_event(
            "Cyklistický závod — zdravotní dohled",
            master_event_id=me_sport.id,
            status=EventStatus.ASSIGNMENTS_CLOSED,
            start_datetime=now + timedelta(days=7),
            end_datetime=now + timedelta(days=7, hours=5),
            address="Stromovka, Praha 7",
            paid=False,
            created_by_id=coordinator_user.id if coordinator_user else None,
        )
        spot_e5_1 = _spot(e5, "Záchranář", [c_zachranar])
        spot_e5_2 = _spot(e5, "Zdravotník", [c_zdravotnik])
        if coordinator_user:
            _assign(spot_e5_1, coordinator_user)
        if member_user:
            _assign(spot_e5_2, member_user)

        # 6–10. Completed events (past, with debriefings)
        completed_data = [
            ("Silvestrovský běh — zdravotní zabezpečení", me_general, now - timedelta(days=120), 5),
            ("Jarní maraton Praha", me_sport, now - timedelta(days=90), 8),
            ("Rock festival Hradec — pátek", me_festival, now - timedelta(days=60), 10),
            ("Rock festival Hradec — sobota", me_festival, now - timedelta(days=59), 10),
            ("Fotbalový pohár — finále", me_sport, now - timedelta(days=30), 3),
        ]
        for ev_name, me, start, hours in completed_data:
            ev = _get_or_create_event(
                ev_name,
                master_event_id=me.id,
                status=EventStatus.COMPLETED,
                start_datetime=start,
                end_datetime=start + timedelta(hours=hours),
                address="Praha",
                paid=True,
                created_by_id=coordinator_user.id if coordinator_user else None,
            )
            spot1 = _spot(ev, "Záchranář", [c_zachranar])
            spot2 = _spot(ev, "Zdravotník", [c_zdravotnik])
            a1 = _assign(spot1, coordinator_user) if coordinator_user else None
            a2 = _assign(spot2, member_user) if member_user else None
            # Add debriefings if not already present
            for a in [a1, a2]:
                if a and not a.debriefing:
                    submitted_by = coordinator_user or member_user
                    db.session.add(DebriefingRecord(
                        assignment_id=a.id,
                        submitted_by_id=submitted_by.id if submitted_by else a.user_id,
                        actual_hours=hours,
                        patients_treated=0,
                        feedback="Výjezd proběhl bez komplikací.",
                    ))

        # 11. Cancelled event
        _get_or_create_event(
            "Zrušená akce — letní kino",
            master_event_id=me_general.id,
            status=EventStatus.CANCELLED,
            start_datetime=now + timedelta(days=5),
            end_datetime=now + timedelta(days=5, hours=4),
            address="Letní kino, Praha 6",
            paid=False,
            created_by_id=coordinator_user.id if coordinator_user else None,
        )

        # 12–14. A few more upcoming drafts for variety
        for i, days in enumerate([45, 60, 75], start=1):
            _get_or_create_event(
                f"Připravovaná akce #{i}",
                master_event_id=me_general.id,
                status=EventStatus.DRAFT,
                start_datetime=now + timedelta(days=days),
                end_datetime=now + timedelta(days=days, hours=4),
                address="Praha",
                paid=False,
                created_by_id=admin_user.id if admin_user else None,
            )

        db.session.commit()
        print("  Created/updated events.")

        print("\nDone. Dev accounts (password: devpassword):")
        for a in DEV_ACCOUNTS:
            print(f"  {a['role']:12s}  {a['email']}")
        print("\nQualifications seeded: Zdravotník ZA, Záchranář, Lékař, Řidič sanitky")
        print("Master events: Obecné, Letní festival 2026, Sportovní závody 2026")
        print("Events: 14 total (draft, published, open, closed, completed, cancelled)")
        print("Equipment: 6 items across 3 types")


if __name__ == "__main__":
    seed()
