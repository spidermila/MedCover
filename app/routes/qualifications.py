"""
Qualifications admin blueprint.

Permissions:
  qualification.view    — list + detail
  qualification.create  — create
  qualification.edit    — edit (name, description, parent hierarchy)
  qualification.delete  — delete (only if no users or spots hold it)
"""
from __future__ import annotations

from flask import Blueprint
from flask import flash
from flask import redirect
from flask import render_template
from flask import request
from flask import Response
from flask import url_for
from flask_login import login_required
from sqlalchemy import collate

from app.extensions import db
from app.models.qualification import Qualification
from app.utils import audit
from app.utils import CS_COLLATION
from app.utils import diff_changes
from app.utils import get_or_404
from app.utils import require_permission

qualifications_bp = Blueprint("qualifications", __name__, url_prefix="/qualifications")


# ── List ──────────────────────────────────────────────────────────────────────

@qualifications_bp.get("/")
@login_required
def index() -> str:
    require_permission("qualification.view")
    qualifications = db.session.scalars(
        db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))
    ).all()
    return render_template("qualifications/index.html", qualifications=qualifications)


# ── Create ────────────────────────────────────────────────────────────────────

@qualifications_bp.route("/create", methods=["GET", "POST"])
@login_required
def create() -> str | Response:
    require_permission("qualification.create")

    all_qualifications = db.session.scalars(db.select(Qualification).where(Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))).all()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        parent_ids = request.form.getlist("parent_ids")

        if not name:
            flash("Název kvalifikace je povinný.", "danger")
            return render_template("qualifications/create.html", all_qualifications=all_qualifications)

        if db.session.scalar(db.select(Qualification).where(Qualification.name == name, Qualification.is_deleted.is_(False))):
            flash("Kvalifikace s tímto názvem již existuje.", "danger")
            return render_template("qualifications/create.html", all_qualifications=all_qualifications)

        cred = Qualification(name=name, description=description, can_be_rp="can_be_rp" in request.form)
        for pid in parent_ids:
            parent = db.session.get(Qualification, int(pid))
            if parent:
                cred.parents.append(parent)

        db.session.add(cred)
        db.session.flush()
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
        db.select(Qualification).where(Qualification.id != cred_id, Qualification.is_deleted.is_(False)).order_by(collate(Qualification.name, CS_COLLATION))
    ).all()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        parent_ids = {int(pid) for pid in request.form.getlist("parent_ids")}

        if not name:
            flash("Název kvalifikace je povinný.", "danger")
            return render_template("qualifications/edit.html", cred=cred, all_qualifications=all_qualifications)

        conflict = db.session.scalar(
            db.select(Qualification).where(Qualification.name == name, Qualification.id != cred_id, Qualification.is_deleted.is_(False))
        )
        if conflict:
            flash("Kvalifikace s tímto názvem již existuje.", "danger")
            return render_template("qualifications/edit.html", cred=cred, all_qualifications=all_qualifications)

        before = {"name": cred.name, "description": cred.description, "can_be_rp": cred.can_be_rp, "parents": str([p.id for p in cred.parents])}
        cred.name = name
        cred.description = description
        cred.can_be_rp = "can_be_rp" in request.form
        # Sync parents
        cred.parents = [c for pid in parent_ids if (c := db.session.get(Qualification, pid)) is not None]

        audit("edit", "Qualification", cred.id, f"Upravena kvalifikace '{cred.name}'", diff_changes(
            before,
            {"name": cred.name, "description": cred.description, "can_be_rp": cred.can_be_rp, "parents": str([p.id for p in cred.parents])},
        ))
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

    from app.models.event import (
        Event, EventSpot, EventStatus, EventTemplate, EventSpotTemplate,
    )

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

    # Templates referencing this qualification — will be unlinked
    affected_templates = db.session.scalars(
        db.select(EventTemplate)
        .join(EventTemplate.spot_templates)
        .join(EventSpotTemplate.required_qualifications)
        .where(Qualification.id == cred_id)
    ).unique().all()

    return render_template(
        "qualifications/delete_confirm.html",
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

    from app.models.qualification import user_qualifications
    from app.models.event import (
        spot_qualifications, spot_template_qualifications,
        Event, EventSpot, EventStatus,
    )

    _FIXED = (EventStatus.COMPLETED, EventStatus.CANCELLED)
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
        audit("qualification_unlinked", "Qualification", cred.id,
              f"Kvalifikace '{qual_name}' odebrána z {len(active_spot_ids)} aktivní(ch) pozice/pozic akcí")

    # ── Remove from event templates ────────────────────────────────────────────
    tmpl_count = db.session.scalar(
        db.select(db.func.count()).select_from(spot_template_qualifications).where(
            spot_template_qualifications.c.qualification_id == cred_id
        )
    ) or 0
    if tmpl_count:
        db.session.execute(
            spot_template_qualifications.delete().where(
                spot_template_qualifications.c.qualification_id == cred_id
            )
        )
        audit("qualification_unlinked", "Qualification", cred.id,
              f"Kvalifikace '{qual_name}' odebrána z {tmpl_count} šablony/šablon")

    # ── Remove from user qualifications ───────────────────────────────────────
    user_count = db.session.scalar(
        db.select(db.func.count()).select_from(user_qualifications).where(
            user_qualifications.c.qualification_id == cred_id
        )
    ) or 0
    if user_count:
        db.session.execute(
            user_qualifications.delete().where(
                user_qualifications.c.qualification_id == cred_id
            )
        )
        audit("qualification_unlinked", "Qualification", cred.id,
              f"Kvalifikace '{qual_name}' odebrána od {user_count} uživatele/uživatelů")

    # ── Soft-delete (fixed spots keep the FK as tombstone) ────────────────────
    cred.soft_delete()
    audit("delete", "Qualification", cred.id, f"Kvalifikace '{qual_name}' označena jako smazaná (tombstone zachován v dokončených/zrušených akcích)")
    db.session.commit()

    flash(f"Kvalifikace '{qual_name}' byla smazána.", "success")
    return redirect(url_for("qualifications.index"))
