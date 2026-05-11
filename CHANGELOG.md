# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- User archiving: admins can archive departed users, hiding them from all lists and dropdowns while preserving their historical data (closes #123)
- Minimum test coverage enforced at 83%; CI and local test runs now fail if coverage drops below this threshold (closes #47)
- Archived users cannot log in and are excluded from all live assignment/notification queries
- Archived users are blocked from requesting a password reset (UI shows same message to prevent enumeration)
- New permissions: `user.archive` (archive/unarchive) and `user.view_archived` (see archived list) — Admin role only
- Archived user list accessible via `?archived=1` on the users page (Admin only)
- Import: new users in the import preview can be marked as archived at creation time (for departed volunteers in historical data)
- Import: archived users are assignable to imported event spots (historical events may reference people who have since left)
- Report link on user detail page: users with `report.view` permission now have a direct "Přehled akcí" button linking to the user's event report (closes #117)
- Events table: scheduled duration now shown in the Začátek column, e.g. "pá 10:00 (2 h)"; Nadřazená akce column moved to the end (closes #121)
- Calendar view: current month/week is now remembered across filter changes and page reloads; applying a status, ME, or event-type filter no longer resets the calendar to today (closes #111)

### Fixed
- User report: planned hours sum cell in the "Celkem (dokončené akce)" footer row now shows "—" instead of a meaningless scheduled-hours total (closes #108)
- Hour values consistently rounded to 1 decimal place in both HTML views and CSV export; previously HTML showed 1 dp while CSV showed 2 dp for the same value (closes #115)
- Dashboard "Moje akce" events now reliably sorted by start date; previously they could appear in creation order (closes #113)
- Pending-activation user names on dashboard are now hyperlinks to the user profile page (closes #105)
- Creating or editing an event with a responsible person selected caused a server error (ValueError); fixed (closes #137)
- Navbar title "MedCover" hidden on mobile screens; logo remains visible at all sizes (closes #138)
- Zodpovědný zdravotník picker in event create/edit now shows only users with an RP-eligible qualification (closes #138)
- Debriefing form: actual start/end datetime pickers now use the same flatpickr component (Czech locale, dd.mm.yyyy HH:MM format, "Teď" button) as the event edit form; also fixed the displayed default times to use local (Europe/Prague) time instead of UTC (closes #111)

## [0.11.0] - 2026-05-11

### Added
- Event types: events now have a type — `Zdravotní dozor` (medical cover), `Školení` (training), or `Prezentační akce` (presentation) (closes #69)
- Training events: new optional `planned_participants_count` field (planned audience size); debriefing RP section has optional actual times and participant count, with "Lektor" title
- Presentation events: no unique fields; no RP section in debriefing
- `post_event_count` column (renamed from `patients_count`): shared post-event metric whose label is driven by event type (patients for medical cover, actual participants for training; not shown for presentations)
- Event type filter buttons on the events list page (server-side, like the status filter); deselecting all types shows no events
- Event type badge shown in the events table for non-medical-cover types
- Event type selector in event create/edit forms with JS-toggled training-specific fields
- Event type selector in event template create/edit form
- `Neplacená` badge (blue) shown on unpaid events in the event list, event detail, and dashboard — paid events keep the green `Placená` badge

### Changed
- Debriefing: section heading changes to "Lektor" for training events; "ZZ" remains for medical cover
- Reports: "Pacienti" column header renamed to "Ošetřených / účastníků" in user and ME reports
- Event template form: event type field added alongside existing fields
- Events table: day-of-week abbreviation moved to the second line (left of the time) to keep the start-date column narrow

## [0.10.0] - 2026-05-10

### Added
- Notification catalog: admin page (`/admin/notifications/`) listing all 10 email notification types with trigger, recipient scope, and email template names
- Per-type notification toggles: admins can enable/disable 5 operational notification groups (assignment, event lifecycle, event cancelled, unfilled spots reminder, debriefing invitation) directly from the catalog page
- `OutboxEmail.notification_type` field: every enqueued email now records which `send_*` function created it, enabling outbox filtering by notification type
- Welcome email on registration: `send_account_activated` is now called automatically when a user completes invite-link registration (previously only sent on manual admin activation)
- Catalog rule: added documentation requiring the notification catalog to be updated whenever any email notification is added, changed, or removed (DEVOPS.md + copilot-instructions)
- Event change notification (closes #103): assigned users now receive an email when any event detail (name, time, location, description, etc.) is changed; includes old and new values with Czech field labels; controllable via the notification catalog toggle
- Czech two-letter weekday abbreviation (po/út/st/čt/pá/so/ne) shown next to the date in the events table for quick day-of-week recognition

### Fixed
- Backup timestamps displayed in CET (Europe/Prague) instead of UTC in both the backup management page and the admin digest email (closes #110)
- Completed events now appear in the table view; status and ME filters and table sorting all moved fully server-side (before pagination) so all pages are correctly filtered and sorted — previously only the calendar showed completed events (closes #120)
- Master Event filter in the events table now correctly counts and paginates only the filtered results
- Table sorting now covers all pages, not just the current page
- Default events table view shows only active/upcoming events (excludes Draft, Cancelled, Completed) sorted by start date ascending
- "Pro mě" toggle button no longer stays visually stuck active after tapping on mobile

## [0.9.0] - 2026-05-10

### Added
- Authentication: invite-only registration, password reset, auto-activation on invite-link completion
- Brute-force login protection: account lockout after 5 failed attempts (15-minute cooldown)
- User management: roles (Admin, Coordinator, Member, Viewer), 53-code permission system, activate/deactivate
- User profile: dark mode toggle, dashboard horizon setting
- Events: full CRUD, status machine (Draft → Published → Assignments Open → In Progress → Completed / Cancelled)
- Master Events with hierarchy for yearly reporting
- Spot management and assignments with pessimistic row-level locking (no race conditions)
- Responsible Person (RP): assignment, dashboard warning for upcoming events without RP
- Optional spots on events and templates
- Qualifications: CRUD, self-referential hierarchy (`can_be_filled_by`), soft-delete with tombstone
- Event templates: save spot structure for recurring events
- Debriefing: two-stage form (quick + final), Debriefing Manager role, auto-trigger on event completion
- Work report (Výkaz práce): pre-filled xlsx export per month, 24-hour file retention
- User feedback: submit from any page, admin management list
- Admin digest email: configurable block-based content, scheduled delivery
- Equipment management: types and items
- Import: events and users from xlsx/CSV, idempotent, preview + confirm flow
- Reports & statistics: per-user, per-master-event, date-range, CSV export
- Database backup and restore
- Permission matrix page in admin
- Audit log: every create/edit/delete on every entity recorded
- FullCalendar integration: calendar view of events
- Dark mode: full Bootstrap 5.3 CSS-variable support, safe utility class conventions documented
- Badge macros (`macros/badges.html`): centralised, dark-mode-safe badge patterns
- Mobile navigation and responsive layout
- Version and changelog page ("Změny ve verzích") visible to all logged-in users
- App version (`APP_VERSION`) read from `VERSION` file; stored with each feedback submission
- Semantic version shown in admin dashboard alongside git commit hash

### Security
- CSRF protection on all forms (Flask-WTF) and AJAX requests (`X-CSRFToken` header)
- Content Security Policy response headers (production)
- SMTP password stored Fernet-encrypted in `AppSettings`
- Open redirect protection on all `next=` parameters
- `sslmode=require` enforced for production `DATABASE_URL`
- Feedback deletion blocked when `DEV_LOGIN_ENABLED=True` (test environment guard)

[Unreleased]: https://github.com/spidermila/MedCover/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/spidermila/MedCover/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/spidermila/MedCover/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/spidermila/MedCover/releases/tag/v0.9.0
