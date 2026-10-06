/**
 * Event detail — prev/next navigation from sessionStorage event list.
 *
 * Expects a config element:
 *   <div id="event-nav-config" data-event-id="…" data-user-id="…"></div>
 */
(function () {
  "use strict";
  var cfg = document.getElementById("event-nav-config");
  if (!cfg) return;

  var CURRENT_ID = parseInt(cfg.dataset.eventId, 10);
  try {
    var raw = sessionStorage.getItem("medcover_event_nav");
    if (!raw) return;
    var nav = JSON.parse(raw);
    // The list survives logout in this tab: use only one saved by the same
    // person, whose list shows only the events they may see.
    if (nav.user !== cfg.dataset.userId) return;
    var ids = nav.ids;
    var base = nav.base;
    var idx = ids.indexOf(CURRENT_ID);
    if (idx === -1) return;

    var prevId = idx > 0 ? ids[idx - 1] : null;
    var nextId = idx < ids.length - 1 ? ids[idx + 1] : null;
    var pos = (idx + 1) + " / " + ids.length;

    var bar = document.querySelector(".d-flex.gap-2.flex-wrap");
    if (!bar) return;

    var group = document.createElement("div");
    group.className = "btn-group btn-group-sm";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "P\u0159edchoz\u00ed / n\u00e1sleduj\u00edc\u00ed akce");

    var prevBtn = document.createElement("a");
    prevBtn.className = "btn btn-outline-secondary px-3" + (prevId ? "" : " disabled");
    prevBtn.setAttribute("aria-label", "P\u0159edchoz\u00ed akce");
    prevBtn.href = prevId ? (base + prevId) : "#";
    prevBtn.textContent = "\u2039";

    var posSpan = document.createElement("span");
    posSpan.className = "btn btn-outline-secondary disabled px-2";
    posSpan.style.pointerEvents = "none";
    posSpan.textContent = pos;

    var nextBtn = document.createElement("a");
    nextBtn.className = "btn btn-outline-secondary px-3" + (nextId ? "" : " disabled");
    nextBtn.setAttribute("aria-label", "N\u00e1sleduj\u00edc\u00ed akce");
    nextBtn.href = nextId ? (base + nextId) : "#";
    nextBtn.textContent = "\u203a";

    group.appendChild(prevBtn);
    group.appendChild(posSpan);
    group.appendChild(nextBtn);
    bar.insertBefore(group, bar.firstChild);

    document.addEventListener("keydown", function (e) {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.key === "ArrowLeft"  && prevId) { e.preventDefault(); window.location.href = base + prevId; }
      if (e.key === "ArrowRight" && nextId) { e.preventDefault(); window.location.href = base + nextId; }
    });
  } catch (ignore) {}
})();
