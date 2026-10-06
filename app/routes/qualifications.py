"""
Qualifications admin blueprint.

Permissions:
  qualification.view    — list + detail
  qualification.create  — create
  qualification.edit    — edit (name, description, parent hierarchy)
  qualification.delete  — delete (only if no users or spots hold it)
"""

import sqlalchemy as sa
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import collate

from app.extensions import db
from app.models.event import (
    Event,
    EventQualificationRequirement,
    EventSpot,
    EventSpotTemplate,
    EventStatus,
    EventTemplate,
    EventTemplateQualificationRequirement,
    StaffingMode,
    spot_qualifications,
)
from app.models.qualification import Qualification, user_qualifications
from app.responsible_person import refresh_responsible_people
from app.staffing import qualification_graph
from app.utils import CS_COLLATION, audit, diff_changes, get_or_404, require_permission

qualifications_bp = Blueprint("qualifications", __name__, url_prefix="/qualifications")


def _set_parents(cred: Qualification, parent_ids: list[str]) -> None:
    # ponytail: serialize rare hierarchy edits; use a dedicated graph lock if this table grows large.
    with db.session.no_autoflush:
        db.session.execute(db.select(Qualification.id).with_hint(Qualification, "WITH (TABLOCKX, HOLDLOCK)")).all()
    try:
        ids = {int(value) for value in parent_ids}
    except ValueError:
        raise ValueError("Vyberte platné rodičovské kvalifikace.") from None
    parents = db.session.scalars(
        db.select(Qualification).where(Qualification.id.in_(ids), Qualification.is_deleted == sa.false())
    ).all()
    if len(parents) != len(ids):
        raise ValueError("Vyberte aktivní rodičovské kvalifikace.")
    cred.parents = list(parents)
    db.session.add(cred)
    db.session.flush()
    qualification_graph()  # The flush invalidates the cache; this rejects cycles before commit.


def _condition_references(cred_id: int) -> tuple[list[Event], list[EventTemplate]]:
    graph = qualification_graph()
    # Include requirements whose active substitution chain depends on this node.
    dependent_ids = [qid for qid in graph.parents if cred_id in graph.fillers(qid)]
    events = (
        db.session.scalars(
            db.select(Event)
            .join(Event.qualification_requirements)
            .where(
                EventQualificationRequirement.qualification_id.in_(dependent_ids),
                Event.staffing_mode == StaffingMode.CONDITIONS,
                Event.status.not_in((EventStatus.COMPLETED, EventStatus.CANCELLED)),
            )
        )
        .unique()
        .all()
    )
    templates = (
        db.session.scalars(
            db.select(EventTemplate)
            .join(EventTemplate.qualification_requirements)
            .where(EventTemplateQualificationRequirement.qualification_id.in_(dependent_ids))
        )
        .unique()
        .all()
    )
    return list(events), list(templates)


# ── List ──────────────────────────────────────────────────────────────────────


@qualifications_bp.get("/")
@login_required
def index() -> str:
    require_permission("qualification.view")
    qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    return render_template("qualifications/index.html", qualifications=qualifications)


# ── Create ────────────────────────────────────────────────────────────────────


@qualifications_bp.route("/create", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    require_permission("qualification.create")

    all_qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        parent_ids = request.form.getlist("parent_ids")

        if not name:
            flash("Název kvalifikace je povinný.", "danger")
            return render_template("qualifications/create.html", all_qualifications=all_qualifications)

        if db.session.scalar(
            db.select(Qualification).where(Qualification.name == name, Qualification.is_deleted == sa.false())
        ):
            flash("Kvalifikace s tímto názvem již existuje.", "danger")
            return render_template("qualifications/create.html", all_qualifications=all_qualifications)

        cred = Qualification(name=name, description=description, can_be_rp="can_be_rp" in request.form)
        try:
            _set_parents(cred, parent_ids)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return render_template("qualifications/create.html", all_qualifications=all_qualifications)
        audit("create", "Qualification", cred.id, f"Vytvořena kvalifikace '{cred.name}'")
        db.session.commit()

        flash(f"Kvalifikace '{cred.name}' byla vytvořena.", "success")
        return redirect(url_for("qualifications.index"))

    return render_template("qualifications/create.html", all_qualifications=all_qualifications)


# ── Edit ──────────────────────────────────────────────────────────────────────


@qualifications_bp.route("/<int:cred_id>/edit", methods=["GET", "POST"])
@login_required
def edit(cred_id: int) -> str | Response:
    require_permission("qualification.edit")

    cred = get_or_404(Qualification, cred_id)

    all_qualifications = db.session.scalars(
        db.select(Qualification)
        .where(Qualification.id != cred_id, Qualification.is_deleted == sa.false())
        .order_by(collate(Qualification.name, CS_COLLATION))
    ).all()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        parent_ids = request.form.getlist("parent_ids")

        if not name:
            flash("Název kvalifikace je povinný.", "danger")
            return render_template("qualifications/edit.html", cred=cred, all_qualifications=all_qualifications)

        conflict = db.session.scalar(
            db.select(Qualification).where(
                Qualification.name == name, Qualification.id != cred_id, Qualification.is_deleted == sa.false()
            )
        )
        if conflict:
            flash("Kvalifikace s tímto názvem již existuje.", "danger")
            return render_template("qualifications/edit.html", cred=cred, all_qualifications=all_qualifications)

        before = {
            "name": cred.name,
            "description": cred.description,
            "can_be_rp": cred.can_be_rp,
            "parents": str([p.id for p in cred.parents]),
        }
        cred.name = name
        cred.description = description
        cred.can_be_rp = "can_be_rp" in request.form
        try:
            _set_parents(cred, parent_ids)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return render_template("qualifications/edit.html", cred=cred, all_qualifications=all_qualifications)
        if before["can_be_rp"] != cred.can_be_rp:
            refresh_responsible_people()

        audit(
            "edit",
            "Qualification",
            cred.id,
            f"Upravena kvalifikace '{cred.name}'",
            diff_changes(
                before,
                {
                    "name": cred.name,
                    "description": cred.description,
                    "can_be_rp": cred.can_be_rp,
                    "parents": str([p.id for p in cred.parents]),
                },
            ),
        )
        db.session.commit()

        flash(f"Kvalifikace '{cred.name}' byla uložena.", "success")
        return redirect(url_for("qualifications.index"))

    return render_template("qualifications/edit.html", cred=cred, all_qualifications=all_qualifications)


# ── Delete ────────────────────────────────────────────────────────────────────


@qualifications_bp.get("/<int:cred_id>/delete")
@login_required
def delete_confirm(cred_id: int) -> str | Response:
    require_permission("qualification.delete")

    cred = get_or_404(Qualification, cred_id)
    if cred.is_deleted:
        flash("Tato kvalifikace již byla smazána.", "warning")
        return redirect(url_for("qualifications.index"))

    _FIXED = (EventStatus.COMPLETED, EventStatus.CANCELLED)

    # Active spots (editable events) — will be unlinked
    active_spots = db.session.scalars(
        db.select(EventSpot)
        .join(EventSpot.event)
        .join(EventSpot.required_qualifications)
        .where(Qualification.id == cred_id)
        .where(Event.status.not_in(_FIXED))
    ).all()

    # Fixed spots (completed/cancelled events) — will keep link as tombstone
    fixed_spots = db.session.scalars(
        db.select(EventSpot)
        .join(EventSpot.event)
        .join(EventSpot.required_qualifications)
        .where(Qualification.id == cred_id)
        .where(Event.status.in_(_FIXED))
    ).all()

    # Users holding this qualification — will be unlinked
    affected_users = list(cred.holders.all())

    # Legacy templates retain the qualification as a historical reference
    affected_templates = (
        db.session.scalars(
            db.select(EventTemplate)
            .join(EventTemplate.spot_templates)
            .join(EventSpotTemplate.required_qualifications)
            .where(Qualification.id == cred_id)
        )
        .unique()
        .all()
    )

    blocking_events, blocking_templates = _condition_references(cred_id)
    return render_template(
        "qualifications/delete_confirm.html",
        blocking_events=blocking_events,
        blocking_templates=blocking_templates,
        cred=cred,
        active_spots=active_spots,
        fixed_spots=fixed_spots,
        affected_users=affected_users,
        affected_templates=affected_templates,
    )


@qualifications_bp.post("/<int:cred_id>/delete")
@login_required
def delete(cred_id: int) -> Response:
    require_permission("qualification.delete")

    cred = get_or_404(Qualification, cred_id)
    if cred.is_deleted:
        flash("Tato kvalifikace již byla smazána.", "warning")
        return redirect(url_for("qualifications.index"))

    _FIXED = (EventStatus.COMPLETED, EventStatus.CANCELLED)
    blocking_events, blocking_templates = _condition_references(cred_id)
    if blocking_events or blocking_templates:
        flash("Kvalifikaci používají podmínky aktivních akcí nebo šablon. Nejprve upravte jejich plán.", "danger")
        return redirect(url_for("qualifications.delete_confirm", cred_id=cred_id))
    qual_name = cred.name

    # ── Remove from active event spots ────────────────────────────────────────
    active_spot_ids = db.session.scalars(
        db.select(EventSpot.id)
        .join(EventSpot.event)
        .join(EventSpot.required_qualifications)
        .where(Qualification.id == cred_id)
        .where(Event.status.not_in(_FIXED))
    ).all()

    if active_spot_ids:
        db.session.execute(
            spot_qualifications.delete().where(
                spot_qualifications.c.qualification_id == cred_id,
                spot_qualifications.c.spot_id.in_(active_spot_ids),
            )
        )
        audit(
            "qualification_unlinked",
            "Qualification",
            cred.id,
            f"Kvalifikace '{qual_name}' odebrána z {len(active_spot_ids)} aktivní(ch) pozice/pozic akcí",
        )

    # Legacy template references retain qualification tombstones for manual recreation.

    # ── Remove from user qualifications ───────────────────────────────────────
    user_count = (
        db.session.scalar(
            db.select(db.func.count())
            .select_from(user_qualifications)
            .where(user_qualifications.c.qualification_id == cred_id)
        )
        or 0
    )
    if user_count:
        db.session.execute(user_qualifications.delete().where(user_qualifications.c.qualification_id == cred_id))
        audit(
            "qualification_unlinked",
            "Qualification",
            cred.id,
            f"Kvalifikace '{qual_name}' odebrána od {user_count} uživatele/uživatelů",
        )

    # ── Soft-delete (fixed spots keep the FK as tombstone) ────────────────────
    cred.soft_delete()
    refresh_responsible_people()
    audit(
        "delete",
        "Qualification",
        cred.id,
        f"Kvalifikace '{qual_name}' označena jako smazaná (tombstone zachován v dokončených/zrušených akcích)",
    )
    db.session.commit()

    flash(f"Kvalifikace '{qual_name}' byla smazána.", "success")
    return redirect(url_for("qualifications.index"))
