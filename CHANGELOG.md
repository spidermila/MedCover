# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- iCal profile page: step-by-step guide for adding the MedCover calendar feed to Google Calendar, linked from the profile iCal card
- Playwright E2E browser tests in Docker: 111 tests across Chromium, Firefox and WebKit covering login, navigation, event CRUD, form validation, CSRF, label accessibility, profile, and JS interactions; run via `make e2e`
- HTML test report with per-test screenshots (`make e2e-report` to view)
- Table Manager: client-side status and event type filter bars (same look as /events/ page); default hides Completed and Cancelled events
- Debriefing manage: date-range filter with quick-fill buttons (same as date-range report); record count shown next to buttons
- Date inputs on debriefing manage and date-range report now use Flatpickr with Czech date format (dd.mm.YYYY)

### Fixed
- Audit log no longer creates spurious entries when saving a user with no actual changes; version counter also skipped when data is identical (closes #249)
- Split event modal: date/time input now uses the standard Flatpickr datetime picker (same as event create/edit), replacing the broken `type="date"` + `type="text"` combination that showed dates in US format (closes #239)
- Reports: "Příští směna" column now shows the user's true next future assignment globally; previously it was empty when the report date range didn't include future events
- CSV exports now include UTF-8 BOM so Excel on Windows auto-detects Czech characters correctly

### Changed
- Consolidated email template CSS: standardized colour palette across `base.html` and `admin_digest.html` (`#c00` → `#c0392b`, `#222` → `#333333`, consistent grey tones); added canonical palette comment; no layout changes (closes #206)
- Increased email queue drain rate from 6 s to 3 s per message (~20 emails/min instead of ~10)
- Centralized CSRF token handling: new `csrfFetch()` wrapper in `csrf-fetch.js` replaces 16 manual `X-CSRFToken` header injections across 4 JS files (closes #207)
- Extracted large inline `<script>` blocks from 4 templates into external JS files for better CSP compliance and maintainability: `table-manager.js`, `events-detail-nav.js`, `events-detail-equipment.js`, `events-create-equipment.js`, `admin-notifications.js` (closes #203)
- Refactored 10 oversized route functions (>60 lines) across 8 files into thin route handlers + private helpers; no behaviour changes (closes #195) (#213)
- DRY up duplicated guard/render/enqueue pattern in `mail.py` via `_guarded_send()` helper; −38 lines of boilerplate (closes #196) (#214)
- Centralized timezone handling via `get_app_tz()` helper — all datetime conversions now read `AppSettings.timezone` from the database instead of hardcoding `Europe/Prague` (closes #197)
- Split 180-line `generate_work_report()` in `work_report_generator.py` into 5 focused helpers; main function is now a 28-line orchestrator (closes #199)
- Deduplicated `_make_user()` / `_login()` test helpers from 5 test files into `conftest.py`; −72 lines of copy-paste code (closes #200)
- Replaced 34 inline `style=` attributes in `table_manager.html` and `notifications.html` with semantic CSS classes; added mobile-responsive overrides (closes #204)
- Consolidated duplicated `.paid-toggle` / `.dark-toggle` CSS from 4 templates into `main.css` with CSS custom properties (closes #202)
- Added live on-blur field validation to all forms — mandatory fields show errors immediately when tabbing away, and errors clear as you type

### Fixed
- Reports CSV exports now convert UTC datetimes to the configured app timezone before formatting
- Fixed 28 `<label>` elements not associated with form controls in `events/detail.html` and `admin/digest/index.html` (accessibility) (#218)
- Replaced remaining inline event handlers (`onsubmit`, `onclick`) in `profile.html`, `detail.html`, and `table_manager.html` with `data-confirm` attributes and JS listeners (CSP compliance)
- Added missing `id="email"` on profile page disabled email input (label accessibility)

## [0.14.0] - 2026-05-14

### Added
- Equipment item availability status tracking: items can be marked as Unavailable with a reason and "since" timestamp; new permission `equipment_item.availability_modify` (Admin + Coordinator) (closes #67, #196) (#210)
- Equipment availability check button on event create and event detail pages: checks selected/assigned items for conflicts with other events and unavailability before saving (closes #196) (#210)
- Unavailable items shown highlighted in orange on event create and detail pages; cannot be assigned to events or people (closes #196) (#210)
- Event create page: equipment items can now be pre-assigned directly when creating the event (previously only on the detail page) (closes #196) (#210)
- Warning banners on event detail page show all conflicts for already-assigned items, with clickable links to the conflicting events (closes #196) (#210)
- Equipment list: new Dostupnost column showing unavailable items with a badge (closes #196) (#210)
- Reports date-range page: quick shortcut buttons for this month, last month, year-to-date, and full year (closes #186) (#186)
- Users: new "Manuálně vytvořit uživatele" button — create a user account without an invite (closes #187) (#187)
- Event detail: "✂ Rozdělit akci" button — splits an event into two consecutive parts; both inherit spots, assignments, and equipment (closes #140) (#188)
- Event detail: ‹ › navigation buttons to switch between events in the list (keyboard ← → also works) (closes #140) (#188)
- User profile: iCal calendar feed subscription link — subscribe in Google Calendar, Apple Calendar, or Outlook for automatic updates (closes #106) (#190)

## [0.13.2] - 2026-05-13

### Fixed
- Admin digest: replaced 24h elapsed guard with a calendar-date check — digest now fires exactly once per calendar day at the configured hour regardless of scheduler restart time (closes #185)
- Scheduled backup: hour gate now correctly converts to the configured local timezone before comparison; was incorrectly comparing against UTC (closes #185)
- Digest poll interval changed from every 30 minutes to every 1 hour for consistency (closes #185)
- VERSION file bumped to 0.13.1 was missed in PR #184; corrected to 0.13.2 here (closes #185)

## [0.13.1] - 2026-05-13

### Added
- All user-facing email notifications converted from plain text to HTML with a shared branded layout (closes #184)
- Email notifications now include a direct link to the relevant event using the configured app base URL (closes #184)
- Admin notifications page: new "Zkušební oznámení" tool — enter an email address, pick an event, and send a test notification to verify HTML rendering and links (closes #184)
- Test email address field on notifications page persists across page reloads via `localStorage` (closes #184)
- `assignments_opened` notification: lists open spots with required qualifications and description (closes #184)
- `unfilled_spots_reminder` notification: lists each unfilled spot with required qualifications and description (closes #184)
- Notification toggles split: "Nová akce zveřejněna" and "Otevřeny přihlášky" now have independent on/off controls (closes #184)

### Fixed
- Account activation email was still using the deleted plain-text template; now routed through the outbox (closes #184)
- Debriefing test notification crashed with `Assignment has no attribute event_id` (closes #184)

## [0.13.0] - 2026-05-12

### Added
- Users list (`/users/`): new sortable "Poslední přihlášení" column — records timestamp on every successful login
- Reports (`/reports/`): user selector in the "Přehled uživatele" card so coordinators/admins can navigate directly to any active user's report
- Admin digest: new "Aktivita uživatelů" block — shows the number of audit log entries per user for a configurable time window (default 24 h, top 10 users, sorted by activity desc)
- Admin digest — Servisní statistiky block: new "Velikosti tabulek" section listing individual PostgreSQL table sizes sorted from largest to smallest; configurable count (default 5, max 50)
- Session timeout: login sessions now expire after a configurable period (default 24 hours); configurable in `/admin/settings/` (closes #183)

### Fixed
- Admin digest: deleting a digest block returned 400 (CSRF token was silently dropped due to a missing `>` on the form tag) (closes #179)
- Admin digest — Servisní statistiky: "E-maily (maximum fronty)" always showed 0 because the metric was based on 15-minute snapshots while emails drain every 6 s; replaced with a direct count of emails enqueued in the configured window
- Import: restricted to Admin only — Coordinators could previously access the import feature (closes #182)
- Users list: sorting by "Poslední přihlášení" now always places users who have never logged in at the bottom (closes #182)
- Login: CSRF token no longer expires causing "CSRF token has expired" errors on the login page (closes #182)
- Login form: email field validates on blur (not prematurely while typing); password field has no frontend validation (closes #182)

## [0.12.0] - 2026-05-12

### Added
- Tabulkový manažer (Table Manager): new view for managing all events of a Master Event in a single table — inline spot-count editing, event name editing, row colour coding, clone, ±1 day and ±1 hour shifting of dates/times, spot assignment, and draft deletion (closes #147)
- Czech locale-aware sorting throughout the application: user pickers, master event lists, qualification lists, equipment lists, and JS table columns all use correct Czech alphabet order including diacritics and the `ch` digraph
- New permission `event.delete_draft`: Admins and Coordinators can delete events in Draft status; delete button on event detail page and in Table Manager
- Table Manager: row flash highlight after every update to help locate the changed row
- Table Manager: event row background colour picker stored in event description; colour-coded rows are dark-mode compatible
- Table Manager: Esc key closes all inline edit popups
- Table Manager: clicking the date or time text opens a full date/time picker in addition to the ±1 shift buttons
- Table Manager: ⏩ button next to the status badge advances the event to the next stage (Draft → Published → Přihlášky otevřeny) with an inline confirmation showing the target state name
- Users with `event.assign_other` permission can assign spots at any event stage except Completed and Archived

### Fixed
- Admin digest: preferred send hour is now interpreted in the configured timezone (e.g. `Europe/Prague`) instead of UTC (closes #173)
- Admin dashboard: "Čekají na aktivaci" count no longer includes archived users; "Archivovaní" stat added to Users card (closes #174)
- Clone event now copies the full event description including the colour tag
- Table Manager: pencil edit icons now visible in dark mode on colour-coded rows

## [0.11.2] - 2026-05-11

### Fixed
- CSP: replace generic `https:` scheme-only allowlist with specific host directives for cdnjs, fonts.googleapis.com, fonts.gstatic.com, and cdn.jsdelivr.net; add `connect-src` for FullCalendar API calls (closes #159)
- CSP: remove all remaining inline event handlers (`onclick`, `onchange`, `oninput`, etc.) from templates so `script-src` no longer needs `unsafe-inline`; add `font-src data:` for FullCalendar's embedded icon font (closes #160)
- Bundle FullCalendar 6.1.15 JS locally; eliminates Firefox `NS_ERROR_CORRUPTED_CONTENT` caused by jsDelivr returning a text/plain error page for a non-existent CSS file (closes #161)
- Soft-deleted qualifications no longer appear in user profile pages, event spot assignment views, or event template forms (closes #158)
- User report: removed treated-patient / participant count column; this per-event metric is not meaningful in per-user reports (closes #157)
- Form validation: fields no longer turn green prematurely before the whole form is validated; fields without any validation rule stay neutral; green is applied only when the entire form passes (closes #141)
- Fixed "Teď" button in datetime pickers being non-functional due to duplicate `class=` attributes left from a prior inline-handler refactor (closes #166)

## [0.11.1] - 2026-05-11

### Added
- User archiving: admins can archive departed users, hiding them from all lists and dropdowns while preserving their historical data (closes #123)
- Minimum test coverage enforced at 83%; CI and local test runs now fail if coverage drops below this threshold (closes #47)
- pytest now exits immediately with a clear error message when the test database is unreachable, instead of failing all tests one by one (closes #52)
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

[Unreleased]: https://github.com/spidermila/MedCover/compare/v0.14.0...HEAD
[0.14.0]: https://github.com/spidermila/MedCover/compare/v0.13.2...v0.14.0
[0.13.2]: https://github.com/spidermila/MedCover/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/spidermila/MedCover/compare/v0.13.0...v0.13.1
[0.12.0]: https://github.com/spidermila/MedCover/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/spidermila/MedCover/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/spidermila/MedCover/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/spidermila/MedCover/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/spidermila/MedCover/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/spidermila/MedCover/releases/tag/v0.9.0
