/* Events list + calendar page.
 * Page config is read from <script id="events-page-cfg" type="application/json"> */
(function () {
  var cfg = {};
  try {
    var cfgEl = document.getElementById("events-page-cfg");
    if (cfgEl) cfg = JSON.parse(cfgEl.textContent);
  } catch (e) {}

  var FEED_URL_BASE   = cfg.feedUrl  || "";
  var HAS_DRAFT_PERM  = cfg.hasDraftPerm || false;
  var ACTIVE_STATUSES = cfg.activeStatuses || [];
  var ACTIVE_TYPES    = cfg.activeTypes || [];
  var ALL_EVENT_TYPES = cfg.allEventTypes || [];
  var CLAIM_BASE      = cfg.claimBase || "";
  var ACTIVE_ME_NAME  = cfg.activeMeName || "";
  var FOR_ME          = cfg.forMe || false;

  var STORAGE_VIEW  = "medcover_events_view";
  var STORAGE_DATE  = "medcover_events_cal_date";

  var calendarInitialized = false;
  var calendar = null;
  var allCalendarEvents = null;
  var currentCalDate = null;

  function loadCalendarDate() {
    try { return localStorage.getItem(STORAGE_DATE) || null; } catch(e) { return null; }
  }
  function saveCalendarDate(dateStr) {
    try { localStorage.setItem(STORAGE_DATE, dateStr); } catch(e) {}
  }
  function _localYMD(d) {
    var m = d.getMonth() + 1;
    var day = d.getDate();
    return d.getFullYear() + '-' + (m < 10 ? '0' : '') + m + '-' + (day < 10 ? '0' : '') + day;
  }

  // ── Table row visibility (status + ME + elig are all server-side) ──

  function applyLocalFilters() {
    if (calendarInitialized && calendar) calendar.refetchEvents();
    _saveEventNav();
  }

  function _saveEventNav() {
    try {
      var tbody = document.querySelector("#events-table tbody");
      if (!tbody) return;
      var ids = [];
      tbody.querySelectorAll("tr").forEach(function (row) {
        if (row.style.display !== "none") {
          var id = row.dataset.eventId;
          if (id) ids.push(parseInt(id, 10));
        }
      });
      // Strip path down to "/events/" as the base for building detail URLs
      var base = window.location.pathname.replace(/\/events\/.*$/, "/events/");
      sessionStorage.setItem("medcover_event_nav", JSON.stringify({ids: ids, base: base}));
    } catch(e) {}
  }

  // ── ME filter navigation (server-side; select triggers URL change) ──────

  function navigateToMe(meId) {
    var params = new URLSearchParams(window.location.search);
    if (meId) {
      params.set("me_id", meId);
    } else {
      params.delete("me_id");
    }
    params.delete("page");
    window.location.href = window.location.pathname + "?" + params.toString();
  }

  // ── View toggle ───────────────────────────────────────────────────────────

  function setView(view) {
    localStorage.setItem(STORAGE_VIEW, view);
    document.getElementById("view-table").classList.toggle('d-none', view !== "table");
    document.getElementById("view-calendar").classList.toggle('d-none', view !== "calendar");
    document.getElementById("btn-table-view").classList.toggle("active", view === "table");
    document.getElementById("btn-calendar-view").classList.toggle("active", view === "calendar");
    if (view === "calendar" && !calendarInitialized) initCalendar();
  }

  // ── Calendar ──────────────────────────────────────────────────────────────

  function initCalendar() {
    calendarInitialized = true;
    var el = document.getElementById("fullcalendar");
    calendar = new FullCalendar.Calendar(el, {
      initialView: "dayGridMonth",
      initialDate: loadCalendarDate() || undefined,
      locale: "cs",
      firstDay: 1,
      headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,listMonth" },
      buttonText: { today: "Dnes", month: "Měsíc", week: "Týden", list: "Seznam" },
      datesSet: function (info) {
        // info.start is the first visible grid day (e.g. Mon 27 Apr for May view).
        // Using the midpoint of the visible range always lands in the correct
        // displayed month, regardless of which weekday the grid starts on.
        var mid = new Date((info.start.getTime() + info.end.getTime()) / 2);
        currentCalDate = _localYMD(new Date(mid.getFullYear(), mid.getMonth(), 1));
      },
      events: async function (fetchInfo, successCallback, failureCallback) {
        try {
          if (!allCalendarEvents) {
            var r = await fetch(FEED_URL_BASE);
            allCalendarEvents = await r.json();
          }
          successCallback(allCalendarEvents.filter(function (e) {
            var statusOk = ACTIVE_STATUSES.includes(e.extendedProps.status_key);
            var typeOk = ACTIVE_TYPES.length === ALL_EVENT_TYPES.length
              ? true
              : ACTIVE_TYPES.includes(e.extendedProps.event_type);
            var eligOk = !FOR_ME || e.extendedProps.eligible;
            var meOk = !ACTIVE_ME_NAME || (e.extendedProps.me_name || "") === ACTIVE_ME_NAME;
            return statusOk && typeOk && eligOk && meOk;
          }));
        } catch (err) { failureCallback(err); }
      },
      eventClick: function (info) {
        info.jsEvent.preventDefault();
        window.location.href = info.event.url;
      },
      eventDidMount: function (info) {
        var p = info.event.extendedProps;
        var cancelled = p.status === "Zrušena";
        var spotsLine = cancelled ? "" : "\nObsazení: " + p.filled + "/" + p.total;
        var title = p.me_name ? info.event.title + " (" + p.me_name + ")" : info.event.title;
        var rpLine = p.rp ? "\nZodpovědná osoba: " + p.rp : "";
        info.el.setAttribute("title",
          title + "\n" + p.start_local + " – " + p.end_local + spotsLine + rpLine + "\nStav: " + p.status);
        if (cancelled) {
          info.el.classList.add("fc-event-cancelled");
        }
      },
      height: "auto"
    });
    calendar.render();
    window.addEventListener("pagehide", function () {
      if (currentCalDate) saveCalendarDate(currentCalDate);
    });
  }

  // ── Spot pick modal ───────────────────────────────────────────────────────

  function initSpotPickModal() {
    var modal = document.getElementById('spotPickModal');
    if (!modal || !CLAIM_BASE) return;
    modal.addEventListener('show.bs.modal', function (e) {
      var btn = e.relatedTarget;
      var eventName = btn.dataset.eventName;
      var spots = JSON.parse(btn.dataset.spots);
      var csrf = btn.dataset.csrf;
      document.getElementById('spotPickModalLabel').textContent = eventName;
      var body = document.getElementById('spotPickBody');
      body.innerHTML = '';
      spots.forEach(function (s) {
        var spotId = s[0], desc = s[1], quals = s[2] || [], optional = s[3];
        var label = desc || '';
        if (quals.length > 0) {
          label += (label ? ' — ' : '') + quals.join(', ');
        }
        if (!label) { label = 'Nespecifikovaná pozice'; }
        if (optional) { label += ' (volitelná)'; }
        var csrfInput = document.createElement('input');
        csrfInput.type = 'hidden';
        csrfInput.name = 'csrf_token';
        csrfInput.value = csrf;
        var btn = document.createElement('button');
        btn.className = 'btn btn-success btn-sm w-100 text-start';
        btn.textContent = label;
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = CLAIM_BASE + spotId;
        form.className = 'mb-1';
        form.appendChild(csrfInput);
        form.appendChild(btn);
        body.appendChild(form);
      });
    });
  }

  // ── Bulk selection ────────────────────────────────────────────────────────

  function clearSelection() {
    document.querySelectorAll(".row-event-check").forEach(function (cb) { cb.checked = false; });
    var ca = document.getElementById("check-all-events");
    if (ca) { ca.checked = false; ca.indeterminate = false; }
    var tb = document.getElementById("bulk-toolbar");
    if (tb) tb.classList.add("d-none");
  }

  function submitBulk(action) {
    var form = document.getElementById("bulk-form");
    if (!form) return;
    var ids = Array.from(document.querySelectorAll(".row-event-check:checked")).map(function (cb) { return cb.value; });
    if (ids.length === 0) return;
    var actionLabels = { publish: "Zveřejnit", open_assignments: "Otevřít přihlášky", cancel: "Zrušit" };
    var label = actionLabels[action] || action;
    if (!guardedConfirm(form, "Akce: " + label + "\nPočet vybraných akcí: " + ids.length + "\n\nPokračovat?")) return;
    document.querySelectorAll("[data-bulk-action]").forEach(function (b) { b.disabled = true; });
    document.getElementById("bulk-action-input").value = action;
    var container = document.getElementById("bulk-ids-container");
    container.innerHTML = "";
    ids.forEach(function (id) {
      var inp = document.createElement("input");
      inp.type = "hidden"; inp.name = "event_ids"; inp.value = id;
      container.appendChild(inp);
    });
    form.submit();
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    applyLocalFilters();

    var saved = localStorage.getItem(STORAGE_VIEW) || "table";
    setView(saved);

    // Bulk selection
    var checkAll   = document.getElementById("check-all-events");
    var toolbar    = document.getElementById("bulk-toolbar");
    var countLabel = document.getElementById("bulk-count-label");

    function visibleChecks() {
      return Array.from(document.querySelectorAll(".row-event-check")).filter(function (cb) {
        var tr = cb.closest("tr");
        return tr && tr.style.display !== "none";
      });
    }

    function updateBulkToolbar() {
      if (!toolbar) return;
      var checked = visibleChecks().filter(function (cb) { return cb.checked; });
      var total   = visibleChecks().length;
      toolbar.classList.toggle("d-none", checked.length === 0);
      if (countLabel) countLabel.textContent = checked.length + " vybráno";
      if (checkAll) {
        checkAll.indeterminate = checked.length > 0 && checked.length < total;
        checkAll.checked = total > 0 && checked.length === total;
      }
    }

    if (checkAll) {
      checkAll.addEventListener("change", function () {
        visibleChecks().forEach(function (cb) { cb.checked = checkAll.checked; });
        updateBulkToolbar();
      });
    }
    document.querySelectorAll(".row-event-check").forEach(function (cb) {
      cb.addEventListener("change", updateBulkToolbar);
    });

    initSpotPickModal();

    // View toggle buttons (replaces inline onclick in template)
    var btnTable = document.getElementById("btn-table-view");
    var btnCal   = document.getElementById("btn-calendar-view");
    if (btnTable) btnTable.addEventListener("click", function () { setView("table"); });
    if (btnCal)   btnCal.addEventListener("click",   function () { setView("calendar"); });

    // ME filter select (replaces inline onchange in template)
    var meSelect = document.getElementById("me-filter-select");
    if (meSelect) meSelect.addEventListener("change", function () { navigateToMe(meSelect.value); });

    // Bulk action buttons (replaces inline onclick in template)
    document.querySelectorAll("[data-bulk-action]").forEach(function (btn) {
      btn.addEventListener("click", function () { submitBulk(btn.dataset.bulkAction); });
    });

    // Clear selection button
    var clearBtn = document.getElementById("btn-clear-selection");
    if (clearBtn) clearBtn.addEventListener("click", clearSelection);
  });

  function submitPrintout() {
    var ids = Array.from(document.querySelectorAll(".row-event-check:checked")).map(function (cb) { return cb.value; });
    if (ids.length === 0) return;
    var form = document.getElementById("printout-form");
    if (!form) return;
    var container = document.getElementById("printout-ids-container");
    container.innerHTML = "";
    ids.forEach(function (id) {
      var inp = document.createElement("input");
      inp.type = "hidden"; inp.name = "event_ids"; inp.value = id;
      container.appendChild(inp);
    });
    form.submit();
  }

  var printoutBtn = document.getElementById("btn-printout");
  if (printoutBtn) printoutBtn.addEventListener("click", submitPrintout);

  // No longer needed as window globals — kept for backwards compat during any cached page loads
  window.setView = setView;
  window.navigateToMe = navigateToMe;
  window.clearSelection = clearSelection;
  window.submitBulk = submitBulk;
})();
