/**
 * User conflict warning — shows a warning under a spot picker when the
 * selected user is already assigned to another event overlapping this one.
 *
 * Wire-up (server side):
 *   <select class="user-conflict-picker" data-spot-id="…">
 *     <option value="…" data-conflict="1"
 *             data-conflict-details='[{"name":"…","url":"…","start":"ISO","end":"ISO"}, …]'>
 *       ⚠️ Name
 *     </option>
 *   </select>
 *   <div class="user-conflict-warning d-none" data-spot-id="…"></div>
 *
 * When the user picks an option carrying data-conflict="1", the warning block
 * with the matching data-spot-id is populated with a Czech message linking to
 * each conflicting event. Picking an option without data-conflict (including
 * the empty placeholder) hides the warning again.
 */
(function () {
  "use strict";

  function findWarningFor(select) {
    var spotId = select.dataset.spotId;
    if (!spotId) return null;
    // Prefer a sibling in the same container; fall back to a document-wide match.
    var container = select.closest("td, div, form, tr") || document;
    var el = container.querySelector('.user-conflict-warning[data-spot-id="' + spotId + '"]');
    if (el) return el;
    return document.querySelector('.user-conflict-warning[data-spot-id="' + spotId + '"]');
  }

  function formatRange(startIso, endIso) {
    try {
      var opts = { dateStyle: "short", timeStyle: "short" };
      var s = startIso ? new Date(startIso).toLocaleString("cs-CZ", opts) : "?";
      var e = endIso ? new Date(endIso).toLocaleString("cs-CZ", opts) : "?";
      return s + "\u2013" + e;
    } catch (err) {
      return "";
    }
  }

  function renderWarning(warningEl, details) {
    if (!warningEl) return;
    if (!details || !details.length) {
      warningEl.classList.add("d-none");
      warningEl.textContent = "";
      return;
    }
    // Build message: "⚠️ Uživatel je již přihlášen na: <a>…</a> (10:00–14:00), …"
    while (warningEl.firstChild) warningEl.removeChild(warningEl.firstChild);
    var lead = document.createElement("span");
    lead.textContent = "\u26A0\uFE0F U\u017eivatel je ji\u017e p\u0159ihl\u00e1\u0161en na: ";
    warningEl.appendChild(lead);
    details.forEach(function (d, i) {
      if (i > 0) warningEl.appendChild(document.createTextNode(", "));
      var a = document.createElement("a");
      a.href = d.url || "#";
      a.textContent = "\u201E" + (d.name || "?") + "\u201C";
      a.target = "_blank";
      a.rel = "noopener";
      warningEl.appendChild(a);
      var rng = formatRange(d.start, d.end);
      if (rng) {
        warningEl.appendChild(document.createTextNode(" (" + rng + ")"));
      }
    });
    warningEl.appendChild(document.createTextNode("."));
    warningEl.classList.remove("d-none");
  }

  function onChange(ev) {
    var sel = ev.currentTarget;
    var warningEl = findWarningFor(sel);
    if (!warningEl) return;
    var opt = sel.options[sel.selectedIndex];
    if (!opt || opt.dataset.conflict !== "1") {
      renderWarning(warningEl, null);
      return;
    }
    var details = [];
    if (opt.dataset.conflictDetails) {
      try {
        details = JSON.parse(opt.dataset.conflictDetails);
      } catch (err) {
        details = [];
      }
    }
    renderWarning(warningEl, details);
  }

  // Delegate from document so pickers restored by table-manager.js after an
  // innerHTML replacement (assign-failure path) keep firing conflict warnings.
  document.addEventListener("change", function (ev) {
    var target = ev.target;
    if (target && target.matches && target.matches("select.user-conflict-picker")) {
      onChange({ currentTarget: target });
    }
  });
})();
