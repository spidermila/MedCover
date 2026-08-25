/* Global Bootstrap and Flatpickr initialisation — loaded by base.html */
document.addEventListener("DOMContentLoaded", function () {
  // Bootstrap popovers (used by help_icon macro)
  document.querySelectorAll('[data-bs-toggle="popover"]').forEach(function (el) {
    new bootstrap.Popover(el, { html: false });
  });
  // Bootstrap tooltips
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
    new bootstrap.Tooltip(el);
  });
  // Flatpickr datetime inputs
  flatpickr(".flatpickr-dt", {
    enableTime: true,
    time_24hr: true,
    dateFormat: "Y-m-dTH:i",
    altInput: true,
    altFormat: "d.m.Y H:i",
    locale: "cs",
    allowInput: true,
  });
  // Flatpickr date-only inputs
  flatpickr(".flatpickr-date", {
    dateFormat: "Y-m-d",
    altInput: true,
    altFormat: "d.m.Y",
    locale: "cs",
    allowInput: true,
  });

  // data-confirm on forms (submit) and buttons (click). Guard prevents a
  // single physical click that dispatches multiple events (macOS trackpads,
  // some assistive tech) from popping the confirm dialog more than once.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!guardedConfirm(form, form.dataset.confirm)) {
        e.preventDefault();
        return;
      }
      form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (b) {
        b.disabled = true;
      });
    });
  });
  document.querySelectorAll("button[data-confirm]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      if (!guardedConfirm(btn, btn.dataset.confirm)) e.preventDefault();
    });
  });

  // Clickable table rows: tr[data-href]
  document.querySelectorAll("tr[data-href]").forEach(function (tr) {
    tr.style.cursor = "pointer";
    tr.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input, select, textarea, form")) return;
      window.location.href = tr.dataset.href;
    });
  });

  // Flatpickr "Teď" buttons
  document.querySelectorAll(".btn-fpnow").forEach(function (btn) {
    btn.addEventListener("click", function () { fpNow(btn); });
  });

  // History back buttons
  document.querySelectorAll(".btn-history-back").forEach(function (btn) {
    btn.addEventListener("click", function () { history.back(); });
  });
});

/* Confirm once per element: returns true if the user just accepted, false if
 * already accepted or if the user cancelled. Sets el.dataset.confirmed="1"
 * on acceptance so repeat events on the same element short-circuit. */
function guardedConfirm(el, message) {
  if (el.dataset.confirmed === "1") return false;
  if (!confirm(message)) return false;
  el.dataset.confirmed = "1";
  return true;
}

/* Clear a guard set by guardedConfirm, e.g. after an async failure. */
function clearGuardedConfirm(el) {
  delete el.dataset.confirmed;
}

/* Set a flatpickr field to the current date/time.
 * Called by the "Teď" button placed inside the same .input-group wrapper. */
function fpNow(btn) {
  var input = btn.closest(".input-group").querySelector(".flatpickr-dt");
  if (input && input._flatpickr) {
    input._flatpickr.setDate(new Date(), true);
  }
}
