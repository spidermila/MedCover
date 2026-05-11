/* Paid checkbox label toggle — event create/edit forms. */
(function () {
  var cb  = document.getElementById('paid');
  var lbl = cb ? cb.closest('.form-check').querySelector('.paid-label') : null;
  function update() { if (lbl) lbl.classList.toggle('is-paid', cb.checked); }
  if (cb) { cb.addEventListener('change', update); update(); }
})();

/* Auto-fill end_datetime from start_datetime (create/edit forms).
 * If end is empty, or end is before the newly chosen start, copy start → end. */
document.addEventListener("DOMContentLoaded", function () {
  var startEl = document.getElementById("start_datetime");
  var endEl   = document.getElementById("end_datetime");
  if (!startEl || !endEl) return;
  var startFp = startEl._flatpickr;
  var endFp   = endEl._flatpickr;
  if (!startFp || !endFp) return;
  startFp.config.onChange.push(function (selectedDates) {
    if (!selectedDates.length) return;
    var startDate = selectedDates[0];
    var endDates  = endFp.selectedDates;
    if (!endDates.length || endDates[0] < startDate) {
      endFp.setDate(startDate, true);
    }
  });
});

/* Show/hide planned_participants_row based on event type.
 * Replaces the inline <script> that previously lived in create.html / edit.html. */
function toggleEventTypeFields(type) {
  var row = document.getElementById('planned_participants_row');
  if (row) row.style.display = (type === 'TRAINING') ? '' : 'none';
}
document.addEventListener("DOMContentLoaded", function () {
  var sel = document.getElementById('event_type');
  if (!sel) return;
  toggleEventTypeFields(sel.value);
  sel.addEventListener('change', function () { toggleEventTypeFields(sel.value); });
});

/* Dynamic spot rows — event create form only. */
(function () {
  var addBtn    = document.getElementById('addSpotBtn');
  var container = document.getElementById('spotRows');
  var totalInp  = document.getElementById('spotTotal');
  var tpl       = document.getElementById('spotRowTpl');
  if (!addBtn || !tpl) return;

  var idx = 0;

  function addRow() {
    var frag = tpl.content.cloneNode(true);
    var row  = frag.querySelector('.spot-row-item');
    row.innerHTML = row.innerHTML
      .replaceAll('__SPOT_DESC__',        'spot_desc_'     + idx)
      .replaceAll('__SPOT_OPTIONAL__',    'spot_optional_' + idx)
      .replaceAll('__SPOT_OPTIONAL_ID__', 'spot_optional_id_' + idx)
      .replaceAll('__SPOT_CRED__',        'spot_cred_'     + idx)
      .replaceAll('__SPOT_CRED_ID_',      'spot_cred_id_'  + idx + '_')
      .replaceAll('__', '');
    row.querySelector('.remove-spot-btn').addEventListener('click', function () {
      row.remove();
      reindex();
    });
    container.appendChild(frag);
    idx++;
    totalInp.value = container.querySelectorAll('.spot-row-item').length;
  }

  function reindex() {
    container.querySelectorAll('.spot-row-item').forEach(function (row, i) {
      row.querySelectorAll('[name^="spot_desc_"]').forEach(function (el) { el.name = 'spot_desc_' + i; });
      row.querySelectorAll('[name^="spot_cred_"]').forEach(function (el) { el.name = 'spot_cred_' + i; });
    });
    idx = container.querySelectorAll('.spot-row-item').length;
    totalInp.value = idx;
  }

  addBtn.addEventListener('click', addRow);
})();
