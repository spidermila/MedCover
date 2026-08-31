/* Phone, email, name and password field validation — used on user detail, profile and create pages. */
(function () {
  var PHONE_RE = /^\d{9}$|^(\+|00)\d{10,15}$/;
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function validatePhone(input) {
    var val = input.value.trim();
    if (val === '') {
      input.classList.remove('is-invalid');
      return true;
    }
    var ok = PHONE_RE.test(val.replace(/\s+/g, ''));
    input.classList.toggle('is-invalid', !ok);
    return ok;
  }

  function validateEmail(input) {
    var val = input.value.trim();
    if (val === '') {
      input.classList.remove('is-invalid');
      return true;
    }
    var ok = EMAIL_RE.test(val);
    input.classList.toggle('is-invalid', !ok);
    return ok;
  }

  function validateName(input) {
    var ok = input.value.trim() !== '';
    input.classList.toggle('is-invalid', !ok);
    return ok;
  }

  function validatePassword(input) {
    var val = input.value;
    if (val === '') {
      input.classList.remove('is-invalid');
      return true;
    }
    var ok = val.length >= 8;
    input.classList.toggle('is-invalid', !ok);
    return ok;
  }

  function attach(id, fn) {
    var el = document.getElementById(id);
    if (!el) return null;
    el.addEventListener('input',  function () { fn(el); });
    el.addEventListener('blur',   function () { fn(el); });
    return el;
  }

  var phoneInput = attach('edit_phone', validatePhone) ||
                   attach('create_phone', validatePhone) ||
                   attach('phone', validatePhone);

  var emailInput = attach('edit_email', validateEmail) ||
                   attach('create_email', validateEmail);

  var nameInput  = attach('edit_name', validateName) ||
                   attach('create_name', validateName);

  var pwInput    = attach('new_password', validatePassword);

  // Attach submit guard to detail (/save), create (/create) and profile forms
  var form = document.querySelector('form[action*="/save"]') ||
             document.querySelector('form[action*="/create"]') ||
             (phoneInput && phoneInput.closest('form'));
  if (form) {
    form.addEventListener('submit', function (e) {
      var ok = true;
      if (nameInput  && !validateName(nameInput))    ok = false;
      if (emailInput && !validateEmail(emailInput))  ok = false;
      if (phoneInput && !validatePhone(phoneInput))  ok = false;
      if (pwInput    && !validatePassword(pwInput))  ok = false;
      if (!ok) e.preventDefault();
    });
  }
})();
