/* Dashboard — open events "show all" toggle.
 * Reads total count from data-open-count attribute on the toggle button. */
var showingAll = false;
function toggleOpenAll() {
  showingAll = !showingAll;
  var openEligible = document.getElementById("open-eligible");
  var openAll      = document.getElementById("open-all");
  var openLabel    = document.getElementById("open-label");
  var btn          = document.getElementById("btn-show-all");
  var count        = btn ? (btn.dataset.openCount || "?") : "?";

  if (openEligible) openEligible.classList.toggle('d-none', showingAll);
  if (openAll)      openAll.classList.toggle('d-none', !showingAll);
  if (openLabel)    openLabel.textContent       = showingAll ? "(všechny)" : "(jen vaše kvalifikace)";
  if (btn) btn.textContent = showingAll ? "Jen moje kvalifikace" : "Zobrazit vše (" + count + ")";
}

document.addEventListener("DOMContentLoaded", function () {
  var btn = document.getElementById("btn-show-all");
  if (btn) btn.addEventListener("click", toggleOpenAll);
});
