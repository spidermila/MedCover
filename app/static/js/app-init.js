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

  // data-confirm on forms (submit) and buttons (click)
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!confirm(form.dataset.confirm)) e.preventDefault();
    });
  });
  document.querySelectorAll("button[data-confirm]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      if (!confirm(btn.dataset.confirm)) e.preventDefault();
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

/* Set a flatpickr field to the current date/time.
 * Called by the "Teď" button placed inside the same .input-group wrapper. */
function fpNow(btn) {
  var input = btn.closest(".input-group").querySelector(".flatpickr-dt");
  if (input && input._flatpickr) {
    input._flatpickr.setDate(new Date(), true);
  }
}
