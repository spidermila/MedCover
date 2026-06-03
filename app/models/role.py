from sqlalchemy.orm import Mapped

from app.extensions import db

# Many-to-many: Role ↔ Permission
role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.Integer, db.ForeignKey("role.id"), primary_key=True),
    db.Column("permission_id", db.Integer, db.ForeignKey("permission.id"), primary_key=True),
)


class Role(db.Model):  # type: ignore[misc]
    __tablename__ = "role"

    ADMIN = "Admin"
    COORDINATOR = "Coordinator"
    MEMBER = "Member"
    VIEWER = "Viewer"
    DEBRIEFING_MANAGER = "Debriefing Manager"

    # Permissions that are intentionally withheld from Admin.
    # These are reserved for the Debriefing Manager role only — even
    # system administrators must not access confidential debriefing data.
    _ADMIN_EXCLUDED_PERMISSIONS: set[str] = {"debriefing.view_all", "debriefing.manage"}

    # Permissions only available to Admin (not in any other role's list).
    # Listed here for documentation; enforcement is via ROLE_PERMISSIONS below.
    _ADMIN_ONLY_PERMISSIONS: set[str] = {"user.archive", "user.view_archived"}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)

    permissions: Mapped[list[Permission]] = db.relationship(
        "Permission",
        secondary=role_permissions,
        back_populates="roles",
        lazy="selectin",
    )
    # Back-ref to UserAccount via string table name to avoid circular import
    users = db.relationship(
        "UserAccount",
        secondary="user_roles",
        back_populates="roles",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class Permission(db.Model):  # type: ignore[misc]
    __tablename__ = "permission"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)

    roles = db.relationship(
        "Role",
        secondary=role_permissions,
        back_populates="permissions",
        lazy="dynamic",
    )

    def __repr__(self) -> str:
        return f"<Permission {self.code}>"


# All permission codes defined in the RBAC table in architecture.md
ALL_PERMISSIONS: list[dict] = [
    # Users
    {"code": "user.view", "description": "View user profiles"},
    {"code": "user.create", "description": "Create a new user account manually"},
    {"code": "user.edit_own", "description": "Edit own profile"},
    {"code": "user.edit_any", "description": "Edit any user's profile"},
    {"code": "user.activate", "description": "Activate a user account"},
    {"code": "user.deactivate", "description": "Deactivate a user account"},
    {"code": "user.archive", "description": "Archive a user account (hides from all lists; also deactivates)"},
    {"code": "user.view_archived", "description": "View archived user accounts"},
    {"code": "user.assign_role", "description": "Assign/unassign roles to users"},
    {"code": "user.assign_qualification", "description": "Assign/unassign qualifications to users"},
    {"code": "invite.create", "description": "Create registration invites"},
    # Qualifications
    {"code": "qualification.view", "description": "View qualifications"},
    {"code": "qualification.create", "description": "Create qualifications"},
    {"code": "qualification.edit", "description": "Edit qualifications"},
    {"code": "qualification.delete", "description": "Delete qualifications"},
    # Master Events
    {"code": "master_event.view", "description": "View master events"},
    {"code": "master_event.create", "description": "Create master events"},
    {"code": "master_event.edit", "description": "Edit master events"},
    {"code": "master_event.archive", "description": "Archive master events"},
    {"code": "master_event.unarchive", "description": "Unarchive master events"},
    # Events
    {"code": "event.view", "description": "View published events"},
    {"code": "event.view_draft", "description": "View draft events"},
    {"code": "event.create", "description": "Create events"},
    {"code": "event.edit", "description": "Edit events"},
    {"code": "event.publish", "description": "Publish events"},
    {"code": "event.assignments.open", "description": "Open event assignments"},
    {"code": "event.assignments.close", "description": "Close event assignments"},
    {"code": "event.cancel", "description": "Cancel events"},
    {"code": "event.restore", "description": "Restore cancelled events"},
    {"code": "event.delete", "description": "Permanently delete archived events"},
    {"code": "event.delete_draft", "description": "Delete events in draft status"},
    {"code": "event.assign_own", "description": "Join/leave an event spot"},
    {"code": "event.assign_other", "description": "Assign another user to an event spot"},
    {"code": "event.set_responsible_person", "description": "Set the responsible person for an event"},
    {"code": "event.notification.send", "description": "Manually send event notifications"},
    # Event Templates
    {"code": "event_template.view", "description": "View event templates"},
    {"code": "event_template.create", "description": "Create event templates"},
    {"code": "event_template.edit", "description": "Edit event templates"},
    {"code": "event_template.delete", "description": "Delete event templates"},
    # Equipment
    {"code": "equipment.view", "description": "View equipment"},
    {"code": "equipment_type.create", "description": "Create equipment types"},
    {"code": "equipment_type.edit", "description": "Edit equipment types"},
    {"code": "equipment_type.delete", "description": "Delete equipment types"},
    {"code": "equipment_item.create", "description": "Create equipment items"},
    {"code": "equipment_item.edit", "description": "Edit equipment items"},
    {"code": "equipment_item.delete", "description": "Delete equipment items"},
    {"code": "equipment_item.issue_personal", "description": "Issue personal equipment to a member"},
    {"code": "equipment_item.report_own", "description": "Report status of own issued personal items"},
    {
        "code": "equipment_item.availability_modify",
        "description": "Set equipment item availability (available/unavailable)",
    },
    {"code": "event.equipment.plan", "description": "Plan required equipment for an event"},
    {"code": "event.equipment.assign", "description": "Assign shared equipment items to an event"},
    # Debriefing
    {"code": "debriefing.submit_own", "description": "Submit own debriefing record"},
    {"code": "debriefing.view_own", "description": "View own submitted debriefing record"},
    # The two permissions below are reserved for the Debriefing Manager role only.
    # They are intentionally excluded from Admin to protect participant confidentiality.
    {
        "code": "debriefing.view_all",
        "description": "View all confidential debriefing records (Debriefing Manager only)",
    },
    {
        "code": "debriefing.manage",
        "description": "Manage debriefing settings and pending requests (Debriefing Manager only)",
    },
    # Reports
    {"code": "report.view", "description": "View reports"},
    {"code": "work_report.generate", "description": "Generate own monthly work report (výkaz práce)"},
    # Audit
    {"code": "audit.view", "description": "View audit log"},
    # System / Admin
    {"code": "admin.view", "description": "Access the admin section"},
    {"code": "admin.manage_settings", "description": "View and edit system settings (SMTP, org name, timezone)"},
    {"code": "admin.manage_digest", "description": "View and configure the admin digest email"},
    # Backup / Restore
    {"code": "backup.run", "description": "Trigger an ad-hoc backup"},
    {"code": "backup.download", "description": "Download a backup file"},
    {"code": "backup.restore", "description": "Restore the application from a backup file"},
    {"code": "backup.delete", "description": "Delete a stored backup file"},
]

# Permissions per role (from RBAC table in architecture.md)
ROLE_PERMISSIONS: dict[str, list[str]] = {
    # Admin gets all permissions except those intentionally reserved for Debriefing Manager.
    # Even admins must not access confidential participant debriefing responses.
    Role.ADMIN: [p["code"] for p in ALL_PERMISSIONS if p["code"] not in Role._ADMIN_EXCLUDED_PERMISSIONS],
    Role.COORDINATOR: [
        "user.view",
        "user.edit_own",
        "qualification.view",
        "master_event.view",
        "master_event.create",
        "master_event.edit",
        "event.view",
        "event.view_draft",
        "event.create",
        "event.edit",
        "event.publish",
        "event.assignments.open",
        "event.assignments.close",
        "event.cancel",
        "event.restore",
        "event.delete_draft",
        "event.assign_own",
        "event.assign_other",
        "event.set_responsible_person",
        "event.notification.send",
        "event_template.view",
        "event_template.create",
        "event_template.edit",
        "event_template.delete",
        "equipment.view",
        "equipment_item.issue_personal",
        "equipment_item.report_own",
        "equipment_item.availability_modify",
        "event.equipment.plan",
        "event.equipment.assign",
        "debriefing.submit_own",
        "debriefing.view_own",
        "report.view",
        "work_report.generate",
    ],
    Role.MEMBER: [
        "user.view",
        "user.edit_own",
        "qualification.view",
        "master_event.view",
        "event.view",
        "event.view_draft",
        "event.assign_own",
        "event_template.view",
        "equipment.view",
        "equipment_item.issue_personal",
        "equipment_item.report_own",
        "debriefing.submit_own",
        "debriefing.view_own",
        "report.view",
        "work_report.generate",
    ],
    Role.VIEWER: [
        "user.view",
        "qualification.view",
        "master_event.view",
        "event.view",
        "event_template.view",
        "equipment.view",
        "report.view",
    ],
    Role.DEBRIEFING_MANAGER: [
        "debriefing.submit_own",
        "debriefing.view_own",
        "debriefing.view_all",
        "debriefing.manage",
    ],
}
