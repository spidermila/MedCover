/* Event template form — paid toggle + dynamic spot rows (edit mode aware). */
(function () {
  var cb  = document.getElementById('paid');
  var lbl = cb ? cb.closest('.form-check').querySelector('.paid-label') : null;
  function update() { if (lbl) lbl.classList.toggle('is-paid', cb.checked); }
  if (cb) { cb.addEventListener('change', update); update(); }
})();

(function () {
  var addBtn    = document.getElementById('addSpotBtn');
  var container = document.getElementById('spotRows');
  var totalInp  = document.getElementById('spotTotal');
  var tpl       = document.getElementById('spotRowTpl');
  if (!addBtn || !tpl) return;

  /* In edit mode there may already be rows; start index after them. */
  var idx = parseInt(totalInp.value, 10);

  function addRow() {
    var frag = tpl.content.cloneNode(true);
    var row  = frag.querySelector('.spot-row-item');
    row.innerHTML = row.innerHTML
      .replaceAll('__SPOT_DESC__',        'spot_desc_'     + idx)
      .replaceAll('__SPOT_OPTIONAL__',    'spot_optional_' + idx)
      .replaceAll('__SPOT_OPTIONAL_ID__', 'tpl_spot_opt_'  + idx)
      .replaceAll('__SPOT_CRED__',        'spot_cred_'     + idx)
      .replaceAll('__SPOT_CRED_ID_',      'tpl_spot_'      + idx + '_cred_')
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
      row.querySelectorAll('[name^="spot_optional_"]').forEach(function (el) { el.name = 'spot_optional_' + i; });
      row.querySelectorAll('[name^="spot_cred_"]').forEach(function (el) { el.name = 'spot_cred_' + i; });
    });
    idx = container.querySelectorAll('.spot-row-item').length;
    totalInp.value = idx;
  }

  /* Wire up remove buttons on pre-existing rows (edit mode). */
  container.querySelectorAll('.remove-spot-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      btn.closest('.spot-row-item').remove();
      reindex();
    });
  });

  addBtn.addEventListener('click', addRow);
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
