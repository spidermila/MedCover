# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Event lists on /events, /dashboard, and the /reports pages now visually distinguish event types by row background: „Školení“ rows use a subtle blue tint, „Prezentační akce“ rows a subtle amber tint; „Zdravotní dozor“ keeps the default background. Colors adapt to light and dark themes. (#502)

### Fixed
- Generated work-report xlsx (Výkaz práce) now opens with A4 portrait orientation and a fixed 91 % print scale matching the legacy Google-Sheets template, so users can print without adjusting Excel's page-setup dialog every month. (#503)

## [1.1.0] - 2026-09-01

### Added
- Events can now be cloned from their detail page. The creation form is pre-filled from the source event while responsible person and assignments are not copied.
- Cross-event assignment conflicts are shown in assignment pickers and on the dashboard. Assignments with an overlap remain possible and show a warning with a link to the conflicting event.
- User list now shows qualifications, and the user detail page shows both roles and qualifications.

### Changed
- Form fields no longer show a green validity checkmark. Empty required fields keep only the red border, preventing validation messages from shifting page content.
- Work report generation now defaults to the last completed calendar month.

### Fixed
- Events list calendar view now honours the event-type filter (Zdravotní dozor / Školení / Prezentační akce). Previously the type buttons only filtered the table; the calendar showed every event regardless of which type filters were active. (#487)
- The date/time picker no longer leaves an entered event date marked as invalid after a selection.
- Cloning context is retained when the pre-filled event form has validation errors.
- Date and time displays throughout the application now consistently use the configured application timezone.

## [1.0.0] - 2026-08-31

První stabilní vydání MedCoveru. Aplikace nahrazuje původní tabulku v Google Sheets ve všech oblastech evidence zdravotních dozorů, školení i prezentačních akcí Českého červeného kříže.

### Added
- Nadřazené akce a akce v seznamu i v tabulce hlavního výpisu nově zobrazují ikony typů vybavení plánovaných na dané akci; typ vybavení má vlastní editovatelnou ikonu (výchozí 📦) spravovanou na stránce vybavení. (#477)
- Spot assignment pickers (event detail and Table Manager) now flag users who are already assigned to another non-cancelled, non-completed, non-archived event overlapping this one: the user’s name is prefixed with a ⚠️ in the dropdown, and picking them shows an inline warning naming each conflicting event with a link and its time range. Uses the same overlap rules as equipment conflict detection (back-to-back events don’t conflict; drafts do). Cross-event, cross-master-event. (#341)
- New Czech 403 page („Nemáte oprávnění k zobrazení …“) rendered for authorization failures such as opening an event detail via a conflict-warning link when the current user is not allowed to view that event.

### Changed
- Debriefing formulář se zaměřuje na poznámky, které chce účastník sdílet, ne na subjektivní číselné hodnocení akce. Původní pětistupňová stupnice „Celkové hodnocení akce“ byla nahrazena třemi možnostmi: „Ne, všechno bylo jako obvykle.“, „Ano, něco bylo neobvykle dobré nebo špatné. Popíšu to níže.“ a „Ano, něco bylo neobvykle dobré nebo špatné, ale nemám čas to sepisovat — probereme to jindy.“ Textová pole s poznámkami se ve formuláři zobrazí jen tehdy, kdy jsou relevantní. Sloupec „Hodnocení“ v přehledech debriefingů se přejmenoval na „Poznámka“ a zobrazuje odpovídající štítek. Migrace přejmenuje sloupec `grade` → `event_note_status` a data z historického importu označí hodnotou 0.
- Členové (role Member) již nemohou měnit své jméno v profilu — pole je jen ke čtení a označeno nápovědou „Nelze změnit — kontaktujte administrátora.“ Úpravy jména jsou vyhrazeny administrátorům a koordinátorům (nové oprávnění `user.edit_name`). Telefon zůstává upravitelný všemi. (#457)
- Výchozí čas upomínky na neobsazené pozice se posunul z 24 h na 72 h před začátkem akce, aby měli koordinátoři víc času na reakci. Existující akce, které stále nesly původní výchozí hodnotu „24“, migrace nastaví na „72“. (#474)
- Validace formulářů: povinná prázdná pole už neukazují text „Toto pole je povinné.“ pod polem — zobrazí se jen červený rámeček, aby se obsah pod polem po odeslání neposunul. Textové validační chyby s konkrétní zprávou (např. neplatný formát e-mailu) se zobrazují dál beze změny. (#478)

### Fixed
- Šablona akce: editace šablony bez popisu už nepředvyplňuje pole „Popis“ textem „None“, který by se jinak uložil do databáze. Pole je teď při chybějícím popisu prázdné, tak jak má být. (#472)

### Removed
- Šablona akce: pole „Plán připomenutí“ bylo odstraněno z formuláře i seznamu šablon. Hodnota se nikdy nepřenášela na akci vytvořenou ze šablony, takže se jednalo o mrtvé pole. Plán připomenutí zůstává na úrovni akce (výchozí 72 h před začátkem). (#474)

### Security
- Content Security Policy `style-src` no longer allows `'unsafe-inline'`. Inline styles now require the same per-request nonce already used for `script-src`, closing the last vector for CSS-based UI-redressing attacks and inline-style injection. FullCalendar and Flatpickr continue to work unchanged — the former reads the nonce from the new `<meta name="csp-nonce">` tag in `base.html`, the latter is covered by a small `document.createElement` shim that nonces any `<style>` element created at runtime. (#234)

## [0.19.1] - 2026-08-26

### Fixed
- Optional (`volitelné`) spots on events with all mandatory spots filled are now claimable again: assignments no longer auto-close until every spot — mandatory and optional — is filled. The events list, event detail page, and the dashboard „Vyžaduje pozornost“ section now render two distinct obsazení badges (mandatory: red → green, optional: yellow → green), so it is immediately clear whether optional spots are still free. Applies to both the interactive claim flow and the Google-Sheets import path. (#441)
- Google-Sheets import: duplicate-event detection compares event dates in the configured app timezone instead of UTC, so events near midnight are no longer misclassified as duplicates (or missed as duplicates). The preview page also correctly surfaces duplicate matches again.
- Rapid double-clicks on „Uvolnit“ / „Zrušit“ / other destructive actions no longer show two stacked confirm dialogs — the guarded-confirm wrapper now suppresses the second dialog on both the accept and cancel paths.
- Czech UI strings: the opening low quote „ is now always paired with the correct closing “ (never a straight `"` or English `”`), including in dynamically-composed messages. Regression test added.
- Event create form: opening the form with `?master_event_id=…` (e.g. from a MasterEvent detail page) now pre-selects that master event in the dropdown.
- Dashboard: fixed Czech typo „Horizon“ → „Horizont“.

### Changed
- Assorted dev/runtime dependency bumps: `gunicorn` 26.0.0 → 26.1.0 (#446), `pytest-playwright` 0.8.0 → 0.9.0 (#444), `tox` 4.58.0 → 4.60.0 (#448), `mypy` 2.3.0 → 2.3.1 (#447), `pre-commit` 4.6.1 → 4.6.2 (#443).

## [0.19.0] - 2026-08-06

### Added
- Notification batching: event-related emails (event changes, published, assignments opened, cancelled, unfilled reminders, debriefing invites, assignment confirmed/released, archived/unarchived) are now deferred and grouped per recipient. A single email covers all pending event notifications for a user, one section per event; subject line indicates single-event vs multi-event batch. (#417, #268)
- Admin-configurable delay tiers at `/admin/notifications/` for events <24 h / 1–7 days / 1–4 weeks / >1 month away. Defaults: 5 / 60 / 360 / 1440 minutes. (#417, #268)
- Structured change payload for `event_changed`: consecutive edits within the delay window merge to the newest value per field; fields reverted to their original value are dropped from the aggregated email. (#417, #268)
- Two new notification types `event_archived` and `event_unarchived` with matching `AppSettings.notify_event_archived` / `notify_event_unarchived` toggles (default ON). (#417, #268)
- Test-notification form at `/admin/notifications/` gained an "Odeslat okamžitě" (send immediately) checkbox (default OFF) — unchecked routes the test through the normal deferred pipeline for end-to-end verification; checked bypasses the delay for template preview. (#417, #268)
- Equipment planning rework: events now declare **quantity per type** instead of pinning specific items. Equipment planning is integrated into the event create/edit form using the same dynamic builder as spots, with conflict-check flash messages linking to competing events. (#424, #400)
- Equipment item availability rework: maintenance windows (`unavailability_since` / `unavailability_until`) replace the status enum. New inline service form on the items list lets admins take an item out of service without leaving the page; "Vrátit do provozu" (return to service) clears the window in one click. Item delete is blocked, and item edit warns, when the change would create a future shortage. (#424, #400)
- Dashboard equipment shortage panel: upcoming events with planned quantity exceeding available stock, with links to each event. Visible to users with `event.equipment.plan` or `event.view`. (#424, #400)
- Profile signature upload: users with `work_report.generate` can upload a handwritten-signature image (PNG / JPEG / HEIC) from their profile. The image is auto-oriented, cropped, resized to 200 px height, re-encoded as PNG, and embedded into every generated work-report xlsx — removing the last manual step of the payroll workflow. Preview endpoint at `/users/profile/signature`. (#434, #428)
- "Pro mě" (For me) filter on the events list: shows open events with claimable, unoccupied spots the current user is eligible for. Persists across status, type, sorting, pagination, and navigation; runs server-side so pagination is now correct. (#384, #326)

### Fixed
- Sorting the user list by „Poslední přihlášení“ and sorting the events list by „Nadřazená akce“ or „Zodpovědná osoba“ no longer 500s. SQLAlchemy's mssql dialect emits `.nulls_last()` verbatim, which T-SQL does not accept; replaced with a portable `CASE WHEN col IS NULL THEN 1 ELSE 0 END` prefix key that keeps NULLs at the bottom in both directions.
- Pessimistic locking on MSSQL: replaced 6 bare `.with_for_update()` calls with explicit T-SQL `WITH (UPDLOCK, ROWLOCK)` / `WITH (UPDLOCK, HOLDLOCK, ROWLOCK)` table hints. SQLAlchemy's mssql dialect silently drops `.with_for_update()`, so pessimistic locks on spot assignment (`app/routes/assignments.py`), digest block edit/toggle (`app/routes/admin_digest.py`), and event equipment plan add / create / edit (`app/routes/events/equipment.py`, `app/routes/events/crud.py`) had degraded to plain SELECTs since the v0.17.0 PG→MSSQL switch. Spot claims still hit the UNIQUE-constraint backstop under contention, but the second concurrent user now gets a clean „už obsazena“ message instead of an `IntegrityError`.
- Scheduler main-loop poll interval reduced from 5 s to 1 s so it no longer silently caps `MAIL_QUEUE_INTERVAL_SECONDS` (default 3 s). Heartbeat writes are throttled independently at 5 s to avoid multiplying DB writes. (#431)
- Scheduler heartbeat file-touch failures are now logged instead of silently swallowed.
- Debriefing `manage` page N+1: `Event → Spot → Assignment → Debriefing` chain now eager-loaded in one query. On the dev dataset (150 completed events, 413 assignments, 318 debriefings) page render dropped from ~680 ms to ~145 ms. (#433)
- Signature upload pipeline hardening: 8 MB request cap, 25 M-pixel decompression-bomb guard (logs suspected bombs), exact stored-blob cap (50 KB) enforced via header, palette-transparency PNGs composited onto white, aspect ratio capped before resize, `signature_mimetype` used for all blob-presence checks so the blob is never loaded just to check existence. (#434)
- Cancelling an event now sends `event_cancelled`, not `event_archived`. (#417)
- Batched drain keyed on `(user_id, to_email)` — prevents cross-recipient leaks when a user's email changes mid-window. (#417)
- Outbox rows whose event was hard-deleted are removed from the queue. (#417)
- `event_published` / `assignments_opened` emails now always list every spot (grouped by required qualification) with a preamble explaining what the user can sign up for; `unfilled_reminder` lists individual spots and its count matches the live spot list. (#417)
- Business transaction is committed **before** SMTP send (previously after), so a mail failure can no longer roll back the underlying status change. (#417)
- Notification delay-tier settings reject non-monotonic values (a longer horizon must not have a shorter delay than a nearer one). (#417)
- Assignment audit: `spot_description` snapshotted before delete so the audit entry does not lose context.
- Migration re-parenting: signature-columns and notification-batching migrations re-parented onto the current head to avoid multi-head history.

### Changed
- `event_equipment_assignment` table dropped; `equipment_item.status` and `equipment_type.category` columns dropped. Availability is derived from maintenance windows alone. 3 migrations. (#424, #400)
- Event create/edit templates merged into a single `form.html` driven by a `mode` context variable. (#424, #400)
- `OutboxEmail` schema gained `user_id`, `event_id`, `change_type`, `change_value`, and `send_after` columns plus a composite index for the drain query. Legacy path (rows with `user_id IS NULL` — invites, password reset, admin digest) continues to drain one-per-tick unchanged. (#417, #268)
- Event-related `send_*` helpers now enqueue via `enqueue_deferred()` (upserts under `WITH (UPDLOCK, HOLDLOCK, ROWLOCK)` on `(user_id, event_id, notification_type)`) instead of immediately enqueueing a rendered email. Per-notification HTML templates were dropped: the batched drain composes every event-linked email from `email/event_batched.html`. (#417, #268)
- Archiving an event (via `/events/<id>/archive`, and via the MasterEvent archive cascade) now bulk-resets `send_after=NULL` on every pending outbox row for that event and enqueues an `event_archived` notice for the union of currently-assigned users **and** users who had pending notifications, so recipients get the archive notice together with any previously deferred edits in a single email. Cancelling an event does the same but enqueues the distinct `event_cancelled` notice. (#417, #268)
- Unarchiving an event enqueues a deferred `event_unarchived` notice to currently-assigned users, using the normal proximity delay so it can merge with subsequent edits. MasterEvent unarchive stays silent (no cascade). (#417, #268)
- Archiving **or** deactivating a user account now deletes all their pending outbox rows in the same transaction; the number of removed rows is included in the audit-log summary. (#417, #268)
- Backup/restore now hex-decodes any `LargeBinary` column on restore (round-trips the new signature blob correctly). (#434)
- Terminology: internal / template wording changed from "dobrovolník" (volunteer) to "uživatel" (user) — some users are not volunteers, but they are always users; "výjezdová zpráva / výjezd" changed to "Debriefing".
- `cryptography` bumped to `>=50.0.0` (CVE-2026-69247).
- Assorted dependency bumps: `actions/setup-python` 6→7 (#418), plus dev-only `mypy`, `faker`, `tox` bumps.

## [0.18.0] - 2026-07-13

### Added
- Printout generator: new "Tisk" action on the events list and on the Reports page generates a printable view of selected events for a configurable date range and optional Master Event; date filters use the configured app timezone (#406)
- Event spot constraints: an event must contain at least one mandatory spot and at least one spot whose required qualification is RP-capable; enforced server-side on event and template create/edit, with frontend validation (#383)

### Fixed
- Archivation logic: archived events are now excluded from per-user reports and Master Event statistics; ME archiving is idempotent; unarchiving a ME shows a hint that child events remain archived; event archive route renamed from `/delete` to `/archive` (#404)
- Import: events correctly transition to ASSIGNMENTS_CLOSED state during import (#385)
- HTML: unclosed attribute tag in master events index template (#404)

### Changed
- Excel and printout exports: cells beginning with `=`, `+`, `-`, or `@` are escaped to prevent formula injection in spreadsheet readers (#406)
- Permissions no longer stored in the database; permission definitions are now managed entirely in code — no change to effective permissions (#399)
- `event_template.view` permission removed from Member and Viewer roles (#399)
- Production deployment docs (`.env.prod.example`): corrected for user-assigned MSI and in-app AAD token injection on Azure Container Apps (#408)

## [0.17.1] - 2026-07-12

### Fixed
- Azure SQL managed-identity authentication: the web/scheduler containers hung on connect until `Login timeout expired` on Azure Container Apps. The `msodbcsql18` driver's `Authentication=ActiveDirectoryMsi` targets the VM IMDS endpoint, which Container Apps does not expose (identity is served via `IDENTITY_ENDPOINT`). The app now fetches the Azure AD access token itself from the Container Apps identity endpoint and injects it into pyodbc via `SQL_COPT_SS_ACCESS_TOKEN` (`app/db_auth.py`). Scoped to managed-identity URLs only; SQL-auth dev/test connections are unchanged.

## [0.17.0] - 2026-07-12

### Added
- Migration baseline guard (`scripts/check_migrations.py`, wired into CI and pre-commit): fails the build if the squashed Alembic baseline is re-squashed/rewritten (changed root revision id), or if history has multiple roots or heads. Prevents stranding existing databases on deploy. A sanctioned re-baseline procedure is documented in DEVOPS.md.

### Fixed
- Backup restore no longer corrupts IDENTITY columns of empty tables. `DBCC CHECKIDENT(..., RESEED, 0)` on an empty MSSQL table makes the *next* insert use the reseed value directly (0), handing out an invalid `id=0` primary key. Restore now only reseeds tables that actually have restored rows. This also fixes intermittent CI failures in the equipment-assignment tests (a restored empty `equipment_item` left identity seeded so a later insert got `id=0`, which routes treat as "no item selected").

### Changed
- Database engine switched from PostgreSQL to Microsoft SQL Server (MSSQL 2022 / Azure SQL). PostgreSQL is no longer supported.
- `docker-compose.yml` now uses the MSSQL 2022 Express container instead of PostgreSQL.
- `docker-compose.e2e.yml` updated to use MSSQL.
- `docker-compose.prod.yml` updated to use MSSQL.
- CI pipeline updated to use an MSSQL service container.
- `psycopg2-binary` removed from dependencies; `pyodbc` is the sole DB driver.
- Backup/restore engine updated for MSSQL (IDENTITY_INSERT, DBCC CHECKIDENT, FK constraint handling).
- Test suite migrated to MSSQL; uses a temporary MSSQL container via testcontainers when `TEST_DATABASE_URL` is not pre-set.

## [0.16.0] - 2026-06-09

### Added
- Spot assignment modal: shows spot description, required qualifications, and optional flag for each available spot (#354)
- Spots in assignment modal sorted by optional-last then by description (#354)
- All-events iCal feed: new `/ical/all/<token>` endpoint for subscribing to all published events; link shown on profile page (#339)
- Personal iCal feed now excludes archived events (#339)
- iCal tokens generated automatically when a user is created (#359)
- Admin menu restructured: Uživatelé moved to top navbar; admin-only links consolidated under single dropdown (#329)
- Import akcí link gated behind `admin.manage_settings` permission (#329)
- Event status badges improved with clearer colours and icons (#366)
- Bulk actions to mark events as paid or unpaid (#344)
- Date-range filtering on user-based reports with quick-range buttons and CSV filename includes range (#356)
- Linting overhaul: isort + black formatting, E501 enforcement, pylint C0415 on tests (#351)
- Pre-commit hook: detect inline JS event handlers in templates (#360)
- Podman support for e2e tests on macOS (#325)
- Separate Docker entrypoints for web and scheduler containers (#348)
- `safe_next` helper for return_url validation with fallback (#328)

### Fixed
- False qualification warnings on spot edit caused by broken JSON in data attributes (#370)
- "Přihlášky otevřeny" / "Přihlášky se otevřou" label logic on event detail page (#355)
- Event filters lost on pagination, archived toggle, and bulk actions (#328)
- Reject scheme-relative URLs in bulk action return_url (security hardening) (#328)
- Archived users can no longer access iCal exports (#358)
- Filter already-assigned users from spot assignment picker and Table Manager picker (#332)
- Do not allow submitting the same RP on event detail page (#349)
- Don't copy responsible_person_id when cloning an event in Table Manager (#333)
- Include general ME in the ME filter dropdown on events page (#343)
- Exclude cancelled/archived events from equipment conflict checks (#331)
- Malformed `<form>` tags causing CSRF errors in some browsers (#353)
- Only RP-eligible users shown in RP picker (#357)
- Cancel button on feedback page now returns to previous page (#342)
- Fix enum filter: pass members not `.value` to status comparisons
- Fix e2e entrypoint: use psycopg2 instead of psycopg

### Changed
- Removed dead unused code and de-duplicated test helpers (#367)
- Assignment audit summaries now include actor and event names (#319)
- Extracted shared `do_assign_user` / `do_unassign_user` service functions (#319)
- Split combined qualification badges in table manager for better wrapping (#352)
- Removed Koordinátor badge from events created by current user (#346)
- Added missing `back_populates` on UserAccount relationships (#330)
- Bump holidays from 0.97 to 0.98 (#374)
- Bump faker from 40.19.1 to 40.21.0 (#373)

## [0.15.0] - 2026-05-29

### Added
- Elevated RP permissions: RP-eligible users assigned to an event can now assign/unassign other users on that event, unless the master event has a coordinator (centrally managed from SPOT) (closes #255 Phase 2)
- `event.view_draft` permission added to Member role — members can now see draft events
- Auto-reassign RP: when the current responsible person leaves an event, the role is automatically transferred to the next RP-eligible attendee (previously it was simply cleared)
- iCal profile page: step-by-step guide for adding the MedCover calendar feed to Google Calendar, linked from the profile iCal card (#269)
- Playwright E2E browser tests in Docker: 111 tests across Chromium, Firefox and WebKit covering login, navigation, event CRUD, form validation, CSRF, label accessibility, profile, and JS interactions; run via `make e2e` (#221)
- HTML test report with per-test screenshots (`make e2e-report` to view) (#221)
- Table Manager: client-side status and event type filter bars (same look as /events/ page); default hides Completed and Cancelled events (#225)
- Debriefing manage: date-range filter with quick-fill buttons (same as date-range report); record count shown next to buttons (#230)
- Date inputs on debriefing manage and date-range report now use Flatpickr with Czech date format (dd.mm.YYYY) (#230)
- CI: pip-audit job and Dependabot config for automated dependency security scanning (#211)
- CI: Azure Container Apps build & deploy workflow

### Fixed
- Audit log no longer creates spurious entries when saving a user with no actual changes; version counter also skipped when data is identical (closes #249) (#287)
- Table Manager: color picker button now only visible to users with `event.edit` permission (previously visible to all) (#226)
- Coordinated master events: self-claim and self-release are now blocked when an ME has a coordinator assigned (UI buttons hidden + server-side guard)
- Split event modal: date/time input now uses the standard Flatpickr datetime picker (same as event create/edit), replacing the broken `type="date"` + `type="text"` combination that showed dates in US format (closes #239) (#286)
- Reports: "Příští směna" column now shows the user's true next future assignment globally; previously it was empty when the report date range didn't include future events (#227)
- CSV exports now include UTF-8 BOM so Excel on Windows auto-detects Czech characters correctly (#229)
- Reports CSV exports now convert UTC datetimes to the configured app timezone before formatting
- Fixed 28 `<label>` elements not associated with form controls in `events/detail.html` and `admin/digest/index.html` (accessibility) (#218)
- Replaced remaining inline event handlers (`onsubmit`, `onclick`) in `profile.html`, `detail.html`, and `table_manager.html` with `data-confirm` attributes and JS listeners (CSP compliance)
- Added missing `id="email"` on profile page disabled email input (label accessibility)
- Auto-close assignments now triggers correctly when the last mandatory spot is filled (closes #288)
- Dashboard crash when spots have NULL description (#323)
- Flatpickr calendar popup dark mode styling
- Form checkbox label alignment (closes #292)
- Replaced direct `AuditLogEntry` usage with `audit()` helper across all routes (closes #238) (#323)
- Removed duplicate dead-code route for admin user activation (closes #250)
- Password change now correctly bumps user version counter (#294)

### Changed
- Consolidated email template CSS: standardized colour palette across `base.html` and `admin_digest.html` (`#c00` → `#c0392b`, `#222` → `#333333`, consistent grey tones); added canonical palette comment; no layout changes (closes #206) (#223)
- Increased email queue drain rate from 6 s to 3 s per message (~20 emails/min instead of ~10)
- Unified all Czech RP labels to "Zodpovědná osoba": previously called "Zodpovědný zdravotník" (medical events), "Lektor" (training), "Vedoucí" (qualifications/dashboard) — now consistent across the entire UI regardless of event type (refs #255) (#293)
- Centralized CSRF token handling: new `csrfFetch()` wrapper in `csrf-fetch.js` replaces 16 manual `X-CSRFToken` header injections across 4 JS files (closes #207) (#224)
- Extracted large inline `<script>` blocks from 4 templates into external JS files for better CSP compliance and maintainability: `table-manager.js`, `events-detail-nav.js`, `events-detail-equipment.js`, `events-create-equipment.js`, `admin-notifications.js` (closes #203) (#212)
- Refactored 10 oversized route functions (>60 lines) across 8 files into thin route handlers + private helpers; no behaviour changes (closes #195) (#213)
- DRY up duplicated guard/render/enqueue pattern in `mail.py` via `_guarded_send()` helper; −38 lines of boilerplate (closes #196) (#214)
- Centralized timezone handling via `get_app_tz()` helper — all datetime conversions now read `AppSettings.timezone` from the database instead of hardcoding `Europe/Prague` (closes #197) (#215)
- Split 180-line `generate_work_report()` in `work_report_generator.py` into 5 focused helpers; main function is now a 28-line orchestrator (closes #199) (#216)
- Deduplicated `_make_user()` / `_login()` test helpers from 5 test files into `conftest.py`; −72 lines of copy-paste code (closes #200) (#217)
- Replaced 34 inline `style=` attributes in `table_manager.html` and `notifications.html` with semantic CSS classes; added mobile-responsive overrides (closes #204) (#219)
- Consolidated duplicated `.paid-toggle` / `.dark-toggle` CSS from 4 templates into `main.css` with CSS custom properties (closes #202) (#220)
- Replaced hardcoded colour values in web UI with CSS custom properties (closes #205)
- Added live on-blur field validation to all forms — mandatory fields show errors immediately when tabbing away, and errors clear as you type

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
- Import: new users in the import preview can be marked as archived at creation time (for departed users in historical data)
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

[Unreleased]: https://github.com/spidermila/MedCover/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/spidermila/MedCover/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/spidermila/MedCover/compare/v0.19.1...v1.0.0
[0.19.1]: https://github.com/spidermila/MedCover/compare/v0.19.0...v0.19.1
[0.19.0]: https://github.com/spidermila/MedCover/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/spidermila/MedCover/compare/v0.17.1...v0.18.0
[0.17.1]: https://github.com/spidermila/MedCover/compare/v0.17.0...v0.17.1
[0.17.0]: https://github.com/spidermila/MedCover/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/spidermila/MedCover/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/spidermila/MedCover/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/spidermila/MedCover/compare/v0.13.2...v0.14.0
[0.13.2]: https://github.com/spidermila/MedCover/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/spidermila/MedCover/compare/v0.13.0...v0.13.1
[0.12.0]: https://github.com/spidermila/MedCover/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/spidermila/MedCover/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/spidermila/MedCover/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/spidermila/MedCover/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/spidermila/MedCover/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/spidermila/MedCover/releases/tag/v0.9.0
