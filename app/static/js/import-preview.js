/* Import preview — check-all and row selection. */
document.addEventListener("DOMContentLoaded", function () {
  var checkAll  = document.getElementById("checkAll");
  var rowChecks = document.querySelectorAll(".row-include");
  var btnImport = document.getElementById("btnImport");
  var countSpan = document.getElementById("selectedCount");

  function userCount() {
    // Read the server-rendered user_count hidden field (always present).
    var el = document.querySelector('input[name="user_count"]');
    return el ? (parseInt(el.value, 10) || 0) : 0;
  }

  function updateCount() {
    var selectedEvents = Array.from(rowChecks).filter(function (c) { return c.checked; }).length;
    var totalUsers = userCount();
    if (countSpan) {
      var parts = [];
      if (rowChecks.length > 0) parts.push(selectedEvents + " z " + rowChecks.length + " akcí");
      if (totalUsers > 0) parts.push(totalUsers + " uživatelů");
      countSpan.textContent = parts.length > 0 ? "Vybráno: " + parts.join(", ") : "";
    }
    // Disable only when there is literally nothing to import.
    if (btnImport) btnImport.disabled = selectedEvents === 0 && totalUsers === 0;
    rowChecks.forEach(function (c) {
      c.closest("tr").classList.toggle("row-excluded", !c.checked);
    });
  }

  if (checkAll) {
    checkAll.addEventListener("change", function () {
      rowChecks.forEach(function (c) { c.checked = checkAll.checked; });
      updateCount();
    });
  }

  rowChecks.forEach(function (c) {
    c.addEventListener("change", function () {
      if (checkAll) checkAll.checked = Array.from(rowChecks).every(function (c) { return c.checked; });
      updateCount();
    });
  });

  var confirmForm = document.getElementById("confirmForm");
  if (confirmForm) {
    confirmForm.addEventListener("submit", function (e) {
      var selectedEvents = Array.from(rowChecks).filter(function (c) { return c.checked; }).length;
      var totalUsers = userCount();
      var parts = [];
      if (selectedEvents > 0) parts.push(selectedEvents + " akcí");
      if (totalUsers > 0) parts.push(totalUsers + " uživatelů");
      var detail = parts.length > 0 ? "\n(" + parts.join(", ") + ")" : "";
      if (!guardedConfirm(confirmForm, "Opravdu chcete spustit import?" + detail + "\n\nTato operace vytvoří nebo aktualizuje záznamy v databázi.")) {
        e.preventDefault();
        return;
      }
      if (btnImport) btnImport.disabled = true;
    });
  }

  updateCount();
});
