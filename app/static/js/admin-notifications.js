/**
 * Admin notifications — persist test email in localStorage and
 * populate hidden fields on test-notification form submit.
 */
(function () {
  "use strict";
  var LS_KEY = "medcover_test_email";
  var emailEl = document.getElementById("test_email");
  var eventEl = document.getElementById("test_event_id");
  var immediateEl = document.getElementById("test_send_immediately");

  if (emailEl) {
    var saved = localStorage.getItem(LS_KEY);
    if (saved) emailEl.value = saved;
    emailEl.addEventListener("input", function () {
      localStorage.setItem(LS_KEY, emailEl.value);
    });
  }

  document.querySelectorAll("[id^='test_form_']").forEach(function (form) {
    var code = form.id.replace("test_form_", "");
    form.addEventListener("submit", function () {
      var emailHidden = document.getElementById("test_email_" + code);
      var eventHidden = document.getElementById("test_event_id_" + code);
      var immediateHidden = document.getElementById("test_send_immediately_" + code);
      if (emailHidden) emailHidden.value = emailEl ? emailEl.value : "";
      if (eventHidden) eventHidden.value = eventEl ? eventEl.value : "";
      if (immediateHidden) immediateHidden.value = (immediateEl && immediateEl.checked) ? "1" : "0";
    });
  });
})();
