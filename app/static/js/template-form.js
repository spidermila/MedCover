/* Event template form — paid toggle + dynamic spot rows (edit mode aware). */
(function () {
  var cb  = document.getElementById('paid');
  var lbl = cb ? cb.closest('.form-check').querySelector('.paid-label') : null;
  function update() { if (lbl) lbl.classList.toggle('is-paid', cb.checked); }
  if (cb) { cb.addEventListener('change', update); update(); }
})();
