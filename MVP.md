# MedCover — MVP Checklist

This file tracks the implementation status of all planned MVP features.
For the reasoning behind each feature, see [architecture.md](architecture.md).

Legend: ✅ Done · 🔲 To do · ⏳ In progress

---

## Phase 1 — Foundation ✅

- ✅ SQLAlchemy models: UserAccount, Role, Permission, MasterEvent, Event, EventSpot, Credential, Assignment, DebriefingRecord, EquipmentType, EquipmentItem, AuditLog, AppSettings, OutboxEmail
- ✅ Alembic migration baseline
- ✅ Auth flow: login, logout, forgot password, password reset via email
- ✅ Invite-only registration: admin creates invite link, user sets own password
- ✅ Admin account activation / deactivation
- ✅ Role-based permission system (Admin, Coordinator, Member, Viewer — 53 permission codes)
- ✅ First-run setup wizard (SMTP, admin account, organisation name)
- ✅ AppSettings model with Fernet-encrypted SMTP password
- ✅ Application settings admin page (edit SMTP and other settings post-wizard)
- ✅ Docker Compose: web + scheduler + db containers
- ✅ Container healthchecks (web, scheduler)
- ✅ GitHub Actions CI (lint + test)
- ✅ Dev seed script (`scripts/seed_dev.py`)

---

## Phase 2 — Event Management ✅

- ✅ Master Event CRUD (create, edit, archive, delete)
- ✅ Master Event hierarchy (parent/child MEs for yearly reporting)
- ✅ Event CRUD (create, edit, cancel, restore, delete)
- ✅ Event lifecycle: Draft → Published → Assignments Open → Assignments Closed → Completed / Cancelled
- ✅ Event list view (table, with status filtering and archived toggle)
- ✅ FullCalendar integration (monthly/weekly/daily/list views)
- ✅ Calendar: colour coding by status; cancelled events shown in grey
- ✅ Calendar: event tooltip with name, ME, responsible person, dates, staffing, status
- ✅ Calendar legend
- ✅ Spot definition: required credentials per spot
- ✅ Credential hierarchy (`can_be_filled_by()` logic)
- ✅ Member self-assignment (claim / release spot)
- ✅ Admin/coordinator assign-other
- ✅ Responsible person (`Zodpovědná osoba`) tag on event
- ✅ Pessimistic locking for spot assignment (row-level `WITH FOR UPDATE`)
- ✅ Optimistic locking for general entity edits (`version` column)

---

## Phase 3 — Notifications & Debriefing ✅

- ✅ Outbox email queue (`OutboxEmail` model, scheduler flushes queue)
- ✅ Email: event published notification to eligible members
- ✅ Email: event cancelled notification to assigned members
- ✅ Email: assignment confirmed / cancelled
- ✅ Email: password reset
- ✅ Email: account activation
- ✅ Scheduler container (`schedule` lib): auto-transition events, auto-close, reminder emails, admin digest
- ✅ Debriefing form (post-event report: hours, patients, materials, notes)
- ✅ Admin debriefing view (list all submissions per event)
- ✅ Admin dashboard with service health overview (DB, scheduler, SMTP, stats)
- ✅ Admin link in top navigation bar

---

## Testing Framework ✅

- ✅ pre-commit hooks: trailing-whitespace, end-of-file-fixer, check-yaml, check-merge-conflict, check-added-large-files, debug-statements, flake8, pyupgrade
- ✅ pytest hook in pre-commit (runs full suite on every commit)
- ✅ 68 tests across 6 files (health, auth, models, events, assignments, admin)
- ✅ Test isolation: per-test `TRUNCATE CASCADE` against `medcover_test` DB
- ✅ Coverage reporting (59% — HTML report at `htmlcov/`)
- ✅ CI: lint job (pre-commit) + test job (pytest + coverage artifact)
- ✅ tox: multi-Python-version test runner (py314 baseline; add py315+ as releases arrive)

---

## Phase 4 — Audit, Equipment & Reports ✅

### Audit Log UI
- ✅ Audit log list page (admin only): paginated table, filter by entity type / actor / date range
- ✅ Audit log detail view: before/after diff for edit actions

### Equipment Inventory
- ✅ EquipmentType CRUD (name, description, category: personal / shared)
- ✅ EquipmentItem CRUD (name, type, home location, issued_to for personal items)
- ✅ Personal equipment: issue item to member, return item
- ✅ User profile: display personal equipment currently issued to logged-in user
- ✅ Shared equipment: assign items to event, return after event
- ✅ Event equipment planning: specify required quantities per type when creating/editing event
- ✅ Event equipment assignment: assign specific physical items from inventory to event

### Event Templates
- ✅ Event template CRUD (admin / coordinator only)
- ✅ Template properties: default spots with credential requirements, paid/unpaid flag, reminder schedule
- ✅ Create event from template (pre-fills form, all values editable)
- ✅ Reminder schedule inherited from template

### Reports
- ✅ Per-user report: events attended, hours, credentials used
- ✅ Per-ME report: all events under a master event, staffing summary
- ✅ Date-range report: all events in a configurable date range
- ✅ Export to CSV (`?format=csv` on all report routes)

---

## Phase 5 — Polish & Admin Panel ✅

### User Profile
- ✅ View and edit own profile (name, email, phone, preferred calendar view)
- ✅ Preferred calendar view stored per user (month / week / day / list)
- ✅ Change own password
- ✅ Dark mode toggle in user settings

### Admin Panel Improvements
- ✅ Credential management UI (create, edit, delete credentials and hierarchy)
- ✅ Role assignment per user (in user detail page)
- ✅ Permission matrix page (read-only reference showing all roles and permission codes)
- ✅ User list with search/filter + activate/deactivate
- ✅ Full dev seed (`scripts/seed_dev.py` with events, assignments, equipment, debriefings)

### Mobile Polish
- ✅ Responsive layout helpers (`table-responsive-stack`, `btn-toolbar-mobile`)
- ✅ Touch-friendly toggle switch for paid/unpaid flag
- ✅ Mobile-default calendar view via per-user `preferred_calendar_view` setting

### Miscellaneous
- ✅ Client-side form validation (`validate.js`)
- ✅ CSRF protection on all forms (Flask-WTF `CSRFProtect`)
- ✅ Service status health checks on admin dashboard (DB latency, SMTP timing, scheduler heartbeat)
- ✅ Post-migration schema verification (`flask verify-schema` in docker-entrypoint.sh)
- 🔲 REST API foundation (read-only, authenticated via token) — post-MVP

---

## Phase 6 — Import & Bulk Operations ✅

### Google Sheets Event Import
- ✅ Extraction script `scripts/import_events.py` — reads `.xlsx` export, filters past events, maps GS columns, disambiguates duplicate names by appending date, outputs JSON
- ✅ Script documentation `scripts/README_import.md` — step-by-step usage guide, column mapping table, format reference
- ✅ Admin-only import page `/import/events/` — paste JSON, step-by-step instructions, help popovers
- ✅ Validation + preview: server-side validation of each row, fuzzy RP name matching (exact → case-insensitive → reversed "Lastname Firstname"), duplicate detection (same name + date in DB)
- ✅ Editable preview table — per-row overrides for name, start/end time, location, paid flag, master event, responsible person; global default qualifications for spot creation
- ✅ Confirm import: all-or-nothing transaction, 3 spots per event (1 mandatory Zdravotník, 1 mandatory Zelenáč, 1 optional Zelenáč), events created as DRAFT, audit log entry per event
- ✅ "Import akcí" link added to Administrace dropdown (admin/coordinator only)

### Bulk Lifecycle Actions
- ✅ Multi-select checkboxes in events list table (check-all + per-row)
- ✅ Bulk action toolbar (shown when ≥1 row selected): Zveřejnit, Otevřít přihlášky, Zrušit
- ✅ Server-side `POST /events/bulk` — skips events in wrong state, reports changed vs skipped count
- ✅ No email notifications on bulk actions (prevents notification storms on large import batches)

---

## Future / Post-MVP Ideas

- In-app notification inbox (bell icon in navbar)
- Create new event from a cancelled/completed event (copy/reuse)
- Custom user roles (currently only the 4 predefined roles)
- REST API write access for third-party integrations
- Advanced reporting/statistics dashboard
- Medical training event type (specific requirements differ from medical cover)
