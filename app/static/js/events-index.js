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
  var CLAIM_BASE      = cfg.claimBase || "";
  var ACTIVE_ME_NAME  = cfg.activeMeName || "";

  var STORAGE_VIEW  = "medcover_events_view";
  var STORAGE_ELIG  = "medcover_events_elig";
  var STORAGE_DATE  = "medcover_events_cal_date";

  var calendarInitialized = false;
  var calendar = null;
  var allCalendarEvents = null;
  var eligFilter = false;
  var currentCalDate = null;

  // ── Per-page JS filter (elig only — status + ME are server-side) ──

  function loadEligFilter() {
    try { return localStorage.getItem(STORAGE_ELIG) === "1"; } catch(e) { return false; }
  }
  function saveEligFilter(v) { localStorage.setItem(STORAGE_ELIG, v ? "1" : "0"); }

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

  // ── Table row visibility (elig only — status + ME are server-side) ──

  function applyLocalFilters() {
    var tbody = document.querySelector("#events-table tbody");
    if (!tbody) return;
    var visibleCount = 0;
    tbody.querySelectorAll("tr").forEach(function (row) {
      var eligOk = !eligFilter || row.dataset.eligible === "1";
      row.style.display = eligOk ? "" : "none";
      if (eligOk) visibleCount++;
    });
    var emptyMsg = document.getElementById("table-empty-msg");
    if (emptyMsg) emptyMsg.classList.toggle('d-none', visibleCount > 0);
    if (calendarInitialized && calendar) calendar.refetchEvents();
  }

  function toggleEligFilter() {
    eligFilter = !eligFilter;
    saveEligFilter(eligFilter);
    var btn = document.getElementById("btn-elig-filter");
    if (btn) {
      btn.classList.toggle("active", eligFilter);
      btn.blur();
    }
    applyLocalFilters();
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
            var eligOk = !eligFilter || e.extendedProps.eligible;
            var meOk = !ACTIVE_ME_NAME || (e.extendedProps.me_name || "") === ACTIVE_ME_NAME;
            return statusOk && eligOk && meOk;
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
        var rpLine = p.rp ? "\nZodpovědný zdravotník: " + p.rp : "";
        info.el.setAttribute("title",
          title + "\n" + p.start_local + " – " + p.end_local + spotsLine + rpLine + "\nStav: " + p.status);
        if (cancelled) {
          info.el.style.opacity = "0.55";
          var titleEl = info.el.querySelector(".fc-list-event-title a") || info.el.querySelector(".fc-event-title");
          if (titleEl) { titleEl.style.textDecoration = "line-through"; titleEl.style.color = "#6c757d"; }
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
        var spotId = s[0], desc = s[1] || 'Pozice #' + spotId;
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = CLAIM_BASE + spotId;
        form.className = 'mb-1';
        form.innerHTML = '<input type="hidden" name="csrf_token" value="' + csrf + '">' +
          '<button class="btn btn-success btn-sm w-100 text-start">' + desc + '</button>';
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
    var ids = Array.from(document.querySelectorAll(".row-event-check:checked")).map(function (cb) { return cb.value; });
    if (ids.length === 0) return;
    var actionLabels = { publish: "Zveřejnit", open_assignments: "Otevřít přihlášky", cancel: "Zrušit" };
    var label = actionLabels[action] || action;
    if (!confirm("Akce: " + label + "\nPočet vybraných akcí: " + ids.length + "\n\nPokračovat?")) return;
    var form = document.getElementById("bulk-form");
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
    eligFilter = loadEligFilter();
    var eligBtn = document.getElementById("btn-elig-filter");
    if (eligBtn) {
      eligBtn.classList.toggle("active", eligFilter);
      var eligTouchStartY = 0;
      var eligTouchFired = false;
      eligBtn.addEventListener("touchstart", function (e) {
        eligTouchStartY = e.touches[0].clientY;
      }, { passive: true });
      eligBtn.addEventListener("touchend", function (e) {
        var dy = Math.abs(e.changedTouches[0].clientY - eligTouchStartY);
        if (dy > 10) return;
        eligTouchFired = true;
        e.preventDefault();
        toggleEligFilter();
        setTimeout(function () { eligTouchFired = false; }, 500);
      }, { passive: false });
      eligBtn.addEventListener("click", function () {
        if (eligTouchFired) return;
        toggleEligFilter();
      });
    }

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

  // No longer needed as window globals — kept for backwards compat during any cached page loads
  window.setView = setView;
  window.navigateToMe = navigateToMe;
  window.clearSelection = clearSelection;
  window.submitBulk = submitBulk;
  window.toggleEligFilter = toggleEligFilter;
})();
