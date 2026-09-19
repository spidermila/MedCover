/* Repeated native form fields keep the condition editor independent of row IDs. */
(function () {
  const rows = document.getElementById('conditionRows');
  const add = document.getElementById('addCondition');
  const template = document.getElementById('conditionRowTemplate');
  if (!rows || !add || !template) return;
  add.addEventListener('click', function () {
    const fragment = template.content.cloneNode(true);
    const select = fragment.querySelector('select');
    rows.appendChild(fragment);
    select.focus();
  });
  rows.addEventListener('click', function (event) {
    const button = event.target.closest('.remove-condition');
    if (button) button.closest('.condition-row').remove();
  });
})();
