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
 * already accepted or if the user cancelled.
 *
 * Suppresses duplicate events from the same click burst (macOS trackpads,
 * some assistive tech) on both the accept and cancel paths: a short-lived
 * burst flag is set before the dialog is shown and cleared on the next
 * event-loop tick, so intentional later clicks re-prompt normally.
 * On acceptance, a permanent flag is also set so repeat events after the
 * async action starts do not re-prompt. */
function guardedConfirm(el, message) {
  if (el.dataset.confirmed === "1") return false;
  if (el.dataset.confirmBurst === "1") return false;
  el.dataset.confirmBurst = "1";
  var accepted = confirm(message);
  if (accepted) el.dataset.confirmed = "1";
  setTimeout(function () { delete el.dataset.confirmBurst; }, 0);
  return accepted;
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
