/**
 * Table Manager — inline editing, spot counts, assignments, clone, delete,
 * colour picker, status advance.
 *
 * Expects a config element:
 *   <div id="tm-config"
 *        data-me-id="…"
 *        data-can-assign="1|0"
 *        data-can-edit-event="1|0"
 *        data-can-create-event="1|0"
 *        data-can-archive-draft="1|0"
 *        data-can-publish="1|0"
 *        data-can-open-assignments="1|0">
 *   </div>
 *
 * CSRF token is injected automatically by csrfFetch() (csrf-fetch.js).
 */
(function () {
  "use strict";

  var cfg = document.getElementById("tm-config");
  if (!cfg) return;

  var ME_ID = parseInt(cfg.dataset.meId, 10);

  var canAssign          = cfg.dataset.canAssign === "1";
  var canEditEvent       = cfg.dataset.canEditEvent === "1";
  var canCreateEvent     = cfg.dataset.canCreateEvent === "1";
  var canArchiveDraft      = cfg.dataset.canArchiveDraft === "1";
  var canPublish         = cfg.dataset.canPublish === "1";
  var canOpenAssignments = cfg.dataset.canOpenAssignments === "1";

  // ── Client-side status + type filters ──────────────────────────────────────
  var DEFAULT_STATUSES = ["DRAFT", "PUBLISHED", "ASSIGNMENTS_OPEN", "ASSIGNMENTS_CLOSED"];
  var allRows = document.querySelectorAll("#tm-table tbody tr[data-event-id]");
  var spotHeaderCols = document.querySelectorAll(".tm-spot-header-col");
  var totalSpotCols = spotHeaderCols.length;

  function getActiveFilters(containerSelector, dataAttr) {
    var active = [];
    document.querySelectorAll(containerSelector + " .filter-btn.active").forEach(function (btn) {
      active.push(btn.dataset[dataAttr]);
    });
    return active;
  }

  function applyFilters() {
    var activeStatuses = getActiveFilters("#tm-status-filter", "filterStatus");
    var activeTypes = getActiveFilters("#tm-type-filter", "filterType");
    // Collect which event IDs pass both filters
    var visibleEvents = {};
    var anyVisible = false;
    var maxVisibleSpots = 0;
    allRows.forEach(function (row) {
      var eid = row.dataset.eventId;
      if (visibleEvents[eid] !== undefined) return;
      var statusOk = activeStatuses.indexOf(row.dataset.status) !== -1;
      var typeOk = activeTypes.indexOf(row.dataset.eventType) !== -1;
      visibleEvents[eid] = statusOk && typeOk;
      if (visibleEvents[eid]) anyVisible = true;
    });
    // Compute max spot count among visible rows
    allRows.forEach(function (row) {
      if (visibleEvents[row.dataset.eventId]) {
        var sc = parseInt(row.dataset.spotCount, 10) || 0;
        if (sc > maxVisibleSpots) maxVisibleSpots = sc;
      }
    });
    // Show/hide all rows for each event
    allRows.forEach(function (row) {
      row.style.display = visibleEvents[row.dataset.eventId] ? "" : "none";
    });
    // Hide excess spot columns (header + data cells)
    for (var i = 0; i < totalSpotCols; i++) {
      var show = i < maxVisibleSpots;
      spotHeaderCols[i].style.display = show ? "" : "none";
    }
    allRows.forEach(function (row) {
      var cells = row.querySelectorAll(".tm-spot-data-col");
      for (var i = 0; i < cells.length; i++) {
        cells[i].style.display = i < maxVisibleSpots ? "" : "none";
      }
    });
    // Toggle empty-filter message
    var emptyMsg = document.getElementById("tm-filter-empty-msg");
    if (emptyMsg) emptyMsg.classList.toggle("d-none", anyVisible);
  }

  function setupFilterBar(containerSelector, dataAttr) {
    document.querySelectorAll(containerSelector + " .filter-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        btn.classList.toggle("active");
        applyFilters();
      });
    });
  }

  setupFilterBar("#tm-status-filter", "filterStatus");
  setupFilterBar("#tm-type-filter", "filterType");

  var resetBtn = document.getElementById("tm-filter-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", function () {
      document.querySelectorAll("#tm-status-filter .filter-btn").forEach(function (btn) {
        var isDefault = DEFAULT_STATUSES.indexOf(btn.dataset.filterStatus) !== -1;
        btn.classList.toggle("active", isDefault);
      });
      document.querySelectorAll("#tm-type-filter .filter-btn").forEach(function (btn) {
        btn.classList.add("active");
      });
      applyFilters();
    });
  }

  // Apply default filters on page load
  applyFilters();

  // ── Error toast close button ────────────────────────────────────────────────
  var errClose = document.getElementById("tm-spots-error-close");
  if (errClose) {
    errClose.addEventListener("click", function () {
      this.closest(".alert").style.display = "none";
    });
  }

  // ── Flash helpers ───────────────────────────────────────────────────────────
  function flashRows(eventId) {
    document.querySelectorAll('tr[data-event-id="' + eventId + '"]').forEach(function (tr) {
      tr.classList.remove("tm-row-flash");
      void tr.offsetWidth;
      tr.classList.add("tm-row-flash");
    });
  }

  function reloadWithHighlight(eventId) {
    var url = new URL(location.href);
    url.searchParams.set("highlight", eventId);
    location.href = url.toString();
  }

  // On load: flash rows from ?highlight param and clean URL
  (function () {
    var params = new URLSearchParams(location.search);
    var eid = params.get("highlight");
    if (!eid) return;
    document.querySelectorAll('tr[data-event-id="' + eid + '"]').forEach(function (tr) {
      tr.classList.add("tm-row-flash");
    });
    var url = new URL(location.href);
    url.searchParams.delete("highlight");
    history.replaceState(null, "", url);
  })();

  // ── Spot assignment ─────────────────────────────────────────────────────────
  if (canAssign) {
    document.querySelectorAll(".tm-assign-select").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var userId = this.value;
        if (!userId) return;
        var spotId = this.dataset.spotId;
        var cell = this.closest("td");
        var originalHtml = cell.innerHTML;

        csrfFetch("/master-events/" + ME_ID + "/table/assign/" + spotId, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: "user_id=" + encodeURIComponent(userId),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            cell.innerHTML =
              '<span class="tm-assigned-name">' + data.user_name + '</span>' +
              '<button class="btn btn-sm p-0 ms-1 text-danger tm-unassign-btn"' +
              ' data-assignment-id="' + data.assignment_id + '"' +
              ' data-user-name="' + data.user_name + '"' +
              ' title="Odhlásit">\u2715</button>';
            attachUnassign(cell.querySelector(".tm-unassign-btn"));
            flashRows(cell.closest("tr").dataset.eventId);
          } else {
            alert(data.error || "Chyba p\u0159i p\u0159i\u0159azen\u00ed.");
            cell.innerHTML = originalHtml;
            attachAssign(cell.querySelector(".tm-assign-select"));
          }
        })
        .catch(function () {
          alert("Chyba s\u00edt\u011b.");
          cell.innerHTML = originalHtml;
          attachAssign(cell.querySelector(".tm-assign-select"));
        });
      });
    });

    function attachAssign(sel) {
      if (!sel) return;
      sel.addEventListener("change", function () {
        var userId = this.value;
        if (!userId) return;
        var spotId = this.dataset.spotId;
        var cell = this.closest("td");
        var originalHtml = cell.innerHTML;
        csrfFetch("/master-events/" + ME_ID + "/table/assign/" + spotId, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: "user_id=" + encodeURIComponent(userId),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            cell.innerHTML =
              '<span class="tm-assigned-name">' + data.user_name + '</span>' +
              '<button class="btn btn-sm p-0 ms-1 text-danger tm-unassign-btn"' +
              ' data-assignment-id="' + data.assignment_id + '"' +
              ' data-user-name="' + data.user_name + '"' +
              ' title="Odhlásit">\u2715</button>';
            attachUnassign(cell.querySelector(".tm-unassign-btn"));
          } else {
            alert(data.error || "Chyba p\u0159i p\u0159i\u0159azen\u00ed.");
            cell.innerHTML = originalHtml;
            attachAssign(cell.querySelector(".tm-assign-select"));
          }
        });
      });
    }

    function attachUnassign(btn) {
      if (!btn) return;
      btn.addEventListener("click", function () {
        var assignmentId = this.dataset.assignmentId;
        var cell = this.closest("td");

        csrfFetch("/master-events/" + ME_ID + "/table/unassign/" + assignmentId, {
          method: "POST",
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            reloadWithHighlight(cell.closest("tr").dataset.eventId);
          } else {
            alert(data.error || "Chyba p\u0159i odhl\u00e1\u0161en\u00ed.");
          }
        });
      });
    }

    document.querySelectorAll(".tm-unassign-btn").forEach(function (btn) { attachUnassign(btn); });
  }

  // ── Inline time editing ─────────────────────────────────────────────────────
  if (canEditEvent) {
    var _activeTimeCell = null;
    var popup = document.getElementById("tm-time-edit-popup");
    var timeDateInput = document.getElementById("tm-time-date-input");
    var timeTimeInput = document.getElementById("tm-time-time-input");
    var timeError = document.getElementById("tm-time-error");

    document.querySelectorAll(".tm-time-cell").forEach(function (cell) {
      cell.addEventListener("click", function (e) {
        if (e.target.classList.contains("tm-time-display")) {
          openTimeEdit(this);
        }
      });
    });

    function openTimeEdit(cell) {
      _activeTimeCell = cell;
      var val = cell.dataset.value;
      timeDateInput.value = val.slice(0, 10);
      timeTimeInput.value = val.slice(11, 16);
      timeError.style.display = "none";
      timeError.textContent = "";
      var rect = cell.getBoundingClientRect();
      document.body.appendChild(popup);
      popup.style.display = "block";
      popup.style.top = (window.scrollY + rect.bottom + 4) + "px";
      popup.style.left = (window.scrollX + rect.left) + "px";
      timeTimeInput.focus();
      timeTimeInput.select();
    }

    // Auto-insert colon after 2 digits
    timeTimeInput.addEventListener("input", function () {
      var v = this.value.replace(/[^0-9:]/g, "");
      if (v.length === 2 && !v.includes(":")) this.value = v + ":";
      else this.value = v;
    });

    function closeTimeEdit() {
      popup.style.display = "none";
      _activeTimeCell = null;
    }

    document.getElementById("tm-time-cancel").addEventListener("click", closeTimeEdit);

    document.getElementById("tm-time-save").addEventListener("click", function () {
      if (!_activeTimeCell) return;
      var cell = _activeTimeCell;
      var eventId = cell.dataset.eventId;
      var field = cell.dataset.field;
      var timeVal = timeTimeInput.value.trim();

      if (!/^\d{2}:\d{2}$/.test(timeVal)) {
        timeError.textContent = "Zadejte \u010das ve form\u00e1tu HH:MM (nap\u0159. 08:30).";
        timeError.style.display = "block";
        return;
      }
      var parts = timeVal.split(":").map(Number);
      if (parts[0] > 23 || parts[1] > 59) {
        timeError.textContent = "Neplatn\u00fd \u010das.";
        timeError.style.display = "block";
        return;
      }

      var value = timeDateInput.value + "T" + timeVal;

      csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/update", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "field=" + encodeURIComponent(field) + "&value=" + encodeURIComponent(value),
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          reloadWithHighlight(eventId);
        } else {
          timeError.textContent = data.error || "Chyba p\u0159i ulo\u017een\u00ed.";
          timeError.style.display = "block";
        }
      })
      .catch(function () {
        timeError.textContent = "Chyba s\u00edt\u011b.";
        timeError.style.display = "block";
      });
    });

    // Enter key saves in time popup
    [timeDateInput, timeTimeInput].forEach(function (el) {
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") document.getElementById("tm-time-save").click();
      });
    });

    // ── Inline name editing ───────────────────────────────────────────────────
    var _activeNameBtn = null;
    var namePopup = document.getElementById("tm-name-edit-popup");
    var nameInput = document.getElementById("tm-name-input");
    var nameError = document.getElementById("tm-name-error");

    document.querySelectorAll(".tm-name-edit-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        _activeNameBtn = this;
        nameInput.value = this.dataset.value;
        nameError.style.display = "none";
        nameError.textContent = "";
        var rect = this.getBoundingClientRect();
        document.body.appendChild(namePopup);
        namePopup.style.display = "block";
        namePopup.style.top = (window.scrollY + rect.bottom + 4) + "px";
        namePopup.style.left = (window.scrollX + rect.left) + "px";
        nameInput.focus();
        nameInput.select();
      });
    });

    function closeNameEdit() {
      namePopup.style.display = "none";
      _activeNameBtn = null;
    }

    document.getElementById("tm-name-cancel").addEventListener("click", closeNameEdit);

    document.getElementById("tm-name-save").addEventListener("click", function () {
      if (!_activeNameBtn) return;
      var eventId = _activeNameBtn.dataset.eventId;
      var value = nameInput.value.trim();
      if (!value) {
        nameError.textContent = "N\u00e1zev nesm\u00ed b\u00fdt pr\u00e1zdn\u00fd.";
        nameError.style.display = "block";
        return;
      }
      csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/update", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "field=name&value=" + encodeURIComponent(value),
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          reloadWithHighlight(eventId);
        } else {
          nameError.textContent = data.error || "Chyba p\u0159i ulo\u017een\u00ed.";
          nameError.style.display = "block";
        }
      })
      .catch(function () {
        nameError.textContent = "Chyba s\u00edt\u011b.";
        nameError.style.display = "block";
      });
    });

    nameInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") document.getElementById("tm-name-save").click();
    });

    // Esc closes whichever popup is open
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (popup.style.display !== "none") closeTimeEdit();
      if (namePopup.style.display !== "none") closeNameEdit();
      var cp = document.getElementById("tm-color-popup");
      if (cp && cp.style.display !== "none") cp.style.display = "none";
      var ap = document.getElementById("tm-advance-popup");
      if (ap && ap.style.display !== "none") ap.style.display = "none";
    });

    // Close popups on outside click
    document.addEventListener("click", function (e) {
      if (popup.style.display !== "none" && !popup.contains(e.target) && !e.target.closest(".tm-time-cell")) {
        closeTimeEdit();
      }
      if (namePopup.style.display !== "none" && !namePopup.contains(e.target) && !e.target.classList.contains("tm-name-edit-btn")) {
        closeNameEdit();
      }
    });

    // ── Day shift ±1 buttons ──────────────────────────────────────────────────
    document.querySelectorAll(".tm-day-shift-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var eventId = this.dataset.eventId;
        var delta = this.dataset.delta;
        csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/update", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: "field=shift_day&value=" + encodeURIComponent(delta),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            reloadWithHighlight(eventId);
          } else {
            alert(data.error || "Chyba p\u0159i posunu data.");
          }
        })
        .catch(function () { alert("Chyba s\u00edt\u011b."); });
      });
    });

    // ── Hour shift ±1 buttons ─────────────────────────────────────────────────
    document.querySelectorAll(".tm-hour-shift-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var eventId = this.dataset.eventId;
        var which = this.dataset.which;
        var delta = this.dataset.delta;
        csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/update", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: "field=shift_hour&value=" + encodeURIComponent(which + ":" + delta),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            reloadWithHighlight(eventId);
          } else {
            alert(data.error || "Chyba p\u0159i posunu \u010dasu.");
          }
        })
        .catch(function () { alert("Chyba s\u00edt\u011b."); });
      });
    });

    // ── Spot count ± buttons ──────────────────────────────────────────────────
    function spotsAdjust(btn, delta) {
      var eventId = btn.dataset.eventId;
      var qualIdsJson = btn.dataset.qualIdsJson;
      var newCount = parseInt(btn.dataset.count, 10) + delta;
      if (newCount < 0) return;

      csrfFetch("/master-events/" + ME_ID + "/table/spots/update", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "event_id=" + encodeURIComponent(eventId) +
              "&qual_ids_json=" + encodeURIComponent(qualIdsJson) +
              "&new_count=" + encodeURIComponent(newCount),
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          reloadWithHighlight(eventId);
        } else {
          var errEl = document.getElementById("tm-spots-error-msg");
          var toast = document.getElementById("tm-spots-error-toast");
          errEl.textContent = data.error || "Chyba p\u0159i ukl\u00e1d\u00e1n\u00ed.";
          toast.style.display = "block";
        }
      })
      .catch(function () {
        document.getElementById("tm-spots-error-msg").textContent = "Chyba s\u00edt\u011b.";
        document.getElementById("tm-spots-error-toast").style.display = "block";
      });
    }

    document.querySelectorAll(".tm-spots-minus").forEach(function (btn) {
      btn.addEventListener("click", function () { spotsAdjust(btn, -1); });
    });
    document.querySelectorAll(".tm-spots-plus").forEach(function (btn) {
      btn.addEventListener("click", function () { spotsAdjust(btn, +1); });
    });
  }

  // ── Clone event ─────────────────────────────────────────────────────────────
  if (canCreateEvent) {
    document.querySelectorAll(".tm-clone-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var eventId = this.dataset.eventId;
        csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/clone", {
          method: "POST",
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            reloadWithHighlight(data.new_event_id);
          } else {
            alert(data.error || "Chyba p\u0159i klon\u00e1n\u00ed akce.");
          }
        })
        .catch(function () { alert("Chyba s\u00edt\u011b."); });
      });
    });
  }

  // ── Delete draft event ──────────────────────────────────────────────────────
  if (canArchiveDraft) {
    document.querySelectorAll(".tm-delete-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var eventId = this.dataset.eventId;
        var eventName = this.dataset.eventName;
        if (!confirm('Archivovat akci \u201E' + eventName + '\u201C?')) return;
        csrfFetch("/events/" + eventId + "/delete", {
          method: "POST",
          headers: { "Accept": "application/json" },
        })
        .then(function (r) {
          return r.json().catch(function () { return null; }).then(function (data) { return { ok: r.ok, data: data }; });
        })
        .then(function (result) {
          if (result.ok) {
            location.reload();
          } else {
            alert((result.data && result.data.error) || "Chyba p\u0159i maz\u00e1n\u00ed akce.");
          }
        })
        .catch(function () { alert("Chyba s\u00edt\u011b."); });
      });
    });
  }

  // ── Advance status ──────────────────────────────────────────────────────────
  if (canPublish || canOpenAssignments) {
    var advPopup = document.getElementById("tm-advance-popup");
    var advEventName = document.getElementById("tm-advance-event-name");
    var advNextStatus = document.getElementById("tm-advance-next-status");
    var advConfirmBtn = document.getElementById("tm-advance-confirm");
    var _advEventId = null;

    document.querySelectorAll(".tm-advance-btn").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        _advEventId = this.dataset.eventId;
        advEventName.textContent = this.dataset.eventName;
        advNextStatus.textContent = this.dataset.nextStatus;
        advConfirmBtn.textContent = this.dataset.nextStatus;
        var rect = this.getBoundingClientRect();
        document.body.appendChild(advPopup);
        advPopup.style.display = "block";
        advPopup.style.top = (window.scrollY + rect.bottom + 4) + "px";
        advPopup.style.left = (window.scrollX + rect.left) + "px";
      });
    });

    document.getElementById("tm-advance-cancel").addEventListener("click", function () {
      advPopup.style.display = "none";
      _advEventId = null;
    });

    document.getElementById("tm-advance-confirm").addEventListener("click", function () {
      if (!_advEventId) return;
      var eventId = _advEventId;
      advPopup.style.display = "none";
      _advEventId = null;
      csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/update", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "field=advance_status",
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          reloadWithHighlight(eventId);
        } else {
          alert(data.error || "Chyba p\u0159i zm\u011bn\u011b stavu.");
        }
      })
      .catch(function () { alert("Chyba s\u00edt\u011b."); });
    });

    document.addEventListener("click", function (e) {
      if (advPopup.style.display !== "none" && !advPopup.contains(e.target) && !e.target.closest(".tm-advance-btn")) {
        advPopup.style.display = "none";
        _advEventId = null;
      }
    });
  }

  // ── Colour picker ───────────────────────────────────────────────────────────
  var colorPopup = document.getElementById("tm-color-popup");
  var _colorEventId = null;

  function applyEventColor(eventId, color) {
    document.querySelectorAll('tr[data-event-id="' + eventId + '"] td.tm-row-cell').forEach(function (td) {
      td.style.backgroundColor = color || "";
    });
    if (_colorEventId === eventId) {
      document.querySelectorAll(".tm-color-swatch").forEach(function (s) {
        s.classList.toggle("active", s.dataset.color.toUpperCase() === (color || "").toUpperCase());
      });
    }
  }

  function saveEventColor(eventId, color) {
    csrfFetch("/master-events/" + ME_ID + "/table/event/" + eventId + "/update", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "field=color&value=" + encodeURIComponent(color),
    }).catch(function () {});
  }

  document.querySelectorAll(".tm-color-btn").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      _colorEventId = this.dataset.eventId;
      var firstCell = document.querySelector('tr[data-event-id="' + _colorEventId + '"] td.tm-row-cell');
      var current = firstCell ? firstCell.style.backgroundColor : "";
      document.querySelectorAll(".tm-color-swatch").forEach(function (s) { s.classList.remove("active"); });
      if (current) {
        document.querySelectorAll(".tm-color-swatch").forEach(function (s) {
          if (s.dataset.color) {
            var tmp = document.createElement("div");
            tmp.style.backgroundColor = s.dataset.color;
            document.body.appendChild(tmp);
            var rgb = getComputedStyle(tmp).backgroundColor;
            document.body.removeChild(tmp);
            if (rgb === current) s.classList.add("active");
          }
        });
      }
      var rect = this.getBoundingClientRect();
      document.body.appendChild(colorPopup);
      colorPopup.style.display = "block";
      colorPopup.style.top = (window.scrollY + rect.bottom + 4) + "px";
      colorPopup.style.left = (window.scrollX + rect.left) + "px";
    });
  });

  document.querySelectorAll(".tm-color-swatch").forEach(function (swatch) {
    swatch.addEventListener("click", function () {
      if (!_colorEventId) return;
      var color = this.dataset.color;
      applyEventColor(_colorEventId, color);
      saveEventColor(_colorEventId, color);
      flashRows(_colorEventId);
      colorPopup.style.display = "none";
      _colorEventId = null;
    });
  });

  document.getElementById("tm-color-reset").addEventListener("click", function () {
    if (!_colorEventId) return;
    applyEventColor(_colorEventId, "");
    saveEventColor(_colorEventId, "");
    flashRows(_colorEventId);
    colorPopup.style.display = "none";
    _colorEventId = null;
  });

  document.addEventListener("click", function (e) {
    if (colorPopup.style.display !== "none" && !colorPopup.contains(e.target) && !e.target.classList.contains("tm-color-btn")) {
      colorPopup.style.display = "none";
      _colorEventId = null;
    }
  });
})();
