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
  if (row) row.classList.toggle('d-none', type !== 'TRAINING');
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

  // Start after any server-rendered rows (present when form is re-shown after a POST error).
  var idx = container ? container.querySelectorAll('.spot-row-item').length : 0;

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

  /* Wire remove buttons on server-rendered rows (re-shown after POST error). */
  container.querySelectorAll('.spot-row-item .remove-spot-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      btn.closest('.spot-row-item').remove();
      reindex();
    });
  });

  addBtn.addEventListener('click', addRow);
})();

/* Dynamic equipment plan rows — create and edit forms. */
(function () {
  var addEqBtn  = document.getElementById('addEqBtn');
  var eqContainer = document.getElementById('eqRows');
  var eqTotal   = document.getElementById('eqTotal');
  var eqTpl     = document.getElementById('eqRowTpl');
  if (!addEqBtn || !eqTpl) return;

  function reindexEq() {
    eqContainer.querySelectorAll('.eq-row').forEach(function (row, i) {
      row.querySelectorAll('[name^="eq_type_id_"]').forEach(function (el) { el.name = 'eq_type_id_' + i; });
      row.querySelectorAll('[name^="eq_qty_"]').forEach(function (el) { el.name = 'eq_qty_' + i; });
    });
    eqTotal.value = eqContainer.querySelectorAll('.eq-row').length;
  }

  function addEqRow() {
    var idx = eqContainer.querySelectorAll('.eq-row').length;
    var frag = eqTpl.content.cloneNode(true);
    var row = frag.querySelector('.eq-row');
    row.innerHTML = row.innerHTML
      .replaceAll('__EQ_TYPE__', 'eq_type_id_' + idx)
      .replaceAll('__EQ_QTY__',  'eq_qty_' + idx);
    row.querySelector('.remove-eq-btn').addEventListener('click', function () {
      row.remove();
      reindexEq();
    });
    eqContainer.appendChild(frag);
    eqTotal.value = eqContainer.querySelectorAll('.eq-row').length;
  }

  /* Wire remove buttons on pre-existing rows (edit mode / restored form). */
  eqContainer.querySelectorAll('.remove-eq-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      btn.closest('.eq-row').remove();
      reindexEq();
    });
  });

  /* Sync eq_total on page load (edit mode: rows pre-rendered from DB). */
  eqTotal.value = eqContainer.querySelectorAll('.eq-row').length;

  addEqBtn.addEventListener('click', addEqRow);
})();

/* Spot constraint pre-validation — blocks submit and shows inline error. */
(function () {
  var form = document.querySelector('form');
  var container = document.getElementById('spotRows');
  if (!form || !container) return;

  var rpQualIds = JSON.parse(container.dataset.rpQualIds || '[]').map(Number);

  var errEl = document.createElement('div');
  errEl.className = 'alert alert-danger mt-2 d-none';
  container.after(errEl);

  function validate() {
    var rows = container.querySelectorAll('.spot-row-item');
    if (!rows.length) return container.dataset.msgNoSpots;
    var mandatory = Array.from(rows).filter(function (r) {
      var cb = r.querySelector('[name^="spot_optional_"]');
      return !cb || !cb.checked;
    });
    if (!mandatory.length) return container.dataset.msgNoMandatory;
    var hasRp = mandatory.some(function (r) {
      return Array.from(r.querySelectorAll('[name^="spot_cred_"]:checked')).some(function (cb) {
        return rpQualIds.indexOf(+cb.value) !== -1;
      });
    });
    return hasRp ? null : container.dataset.msgNoRpQual;
  }

  form.addEventListener('submit', function (e) {
    var err = validate();
    if (err) {
      e.preventDefault();
      errEl.textContent = err;
      errEl.classList.remove('d-none');
      errEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } else {
      errEl.classList.add('d-none');
    }
  });
})();
