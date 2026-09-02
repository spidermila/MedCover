/* Shared "Vydat" modal for the equipment items page. Wires per-row
 * .btn-open-issue buttons to a single #issueItemModal, rewriting the form
 * action from an actionBase template like "/equipment/items/__ID__/issue". */
document.addEventListener("DOMContentLoaded", function () {
  var cfgEl = document.getElementById("issueItemCfg");
  var modalEl = document.getElementById("issueItemModal");
  var formEl = document.getElementById("issueItemForm");
  var nameEl = document.getElementById("issueItemName");
  if (!cfgEl || !modalEl || !formEl || !nameEl) return;

  var actionBase;
  try {
    actionBase = JSON.parse(cfgEl.textContent).actionBase;
  } catch (e) {
    return;
  }

  var modal = new bootstrap.Modal(modalEl);

  document.querySelectorAll(".btn-open-issue").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.dataset.itemId;
      var name = btn.dataset.itemName || "";
      if (!id) return;
      formEl.action = actionBase.replace("__ID__", id);
      nameEl.textContent = name;
      var sel = formEl.querySelector('select[name="user_id"]');
      if (sel) sel.value = "";
      modal.show();
    });
  });
});
