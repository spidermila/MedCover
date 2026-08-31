/**
 * MedCover — client-side form validation
 *
 * UX enhancement only — all rules are also enforced server-side.
 * Uses Bootstrap 5 is-invalid classes directly.
 */
(function () {
  "use strict";

  // ── Helpers ──────────────────────────────────────────────────────────────

  function setInvalid(el, message) {
    el.classList.add("is-invalid");
    var fb = el.nextElementSibling;
    // Required-but-empty fields use the red border only, so adding validation
    // feedback does not move the controls below them after submitting.
    if (!message) {
      if (fb && fb.classList.contains("invalid-feedback")) {
        fb.textContent = "";
        fb.classList.add("d-none");
      }
      return;
    }
    if (fb && fb.classList.contains("invalid-feedback")) {
      fb.textContent = message;
      fb.classList.remove("d-none");
    } else {
      fb = document.createElement("div");
      fb.className = "invalid-feedback";
      fb.textContent = message;
      el.parentNode.insertBefore(fb, el.nextSibling);
    }
  }

  function clearValidity(el) {
    el.classList.remove("is-invalid");
  }

  /**
   * Returns true if the element has at least one validation rule we can check.
   * Fields with no rules stay neutral — they should never turn green or red.
   */
  function hasValidationRules(el) {
    return (
      el.hasAttribute("required") ||
      el.hasAttribute("minlength") ||
      el.hasAttribute("maxlength") ||
      el.hasAttribute("pattern") ||
      el.type === "email" ||
      (el.type === "number" && (el.hasAttribute("min") || el.hasAttribute("max")))
    );
  }

  // ── Rules ─────────────────────────────────────────────────────────────────

  function validateRequired(el) {
    if (el.hasAttribute("required") && !el.value.trim()) {
      setInvalid(el);
      return false;
    }
    return true;
  }

  function validateMinLength(el) {
    var min = parseInt(el.getAttribute("minlength"), 10);
    if (!isNaN(min) && el.value.length > 0 && el.value.length < min) {
      setInvalid(el, "Minimální délka je " + min + " znaků.");
      return false;
    }
    return true;
  }

  function validateMaxLength(el) {
    var max = parseInt(el.getAttribute("maxlength"), 10);
    if (!isNaN(max) && el.value.length > max) {
      setInvalid(el, "Maximální délka je " + max + " znaků.");
      return false;
    }
    return true;
  }

  function validateNumericRange(el) {
    if (el.type !== "number") return true;
    var val = parseFloat(el.value);
    if (isNaN(val)) return true;
    var min = el.getAttribute("min");
    var max = el.getAttribute("max");
    if (min !== null && val < parseFloat(min)) {
      setInvalid(el, "Minimální hodnota je " + min + ".");
      return false;
    }
    if (max !== null && val > parseFloat(max)) {
      setInvalid(el, "Maximální hodnota je " + max + ".");
      return false;
    }
    return true;
  }

  // ── Date range: end must be ≥ start ──────────────────────────────────────

  function validateDateRange(form) {
    var startEl = form.querySelector("[name='start_datetime']");
    var endEl = form.querySelector("[name='end_datetime']");
    if (!startEl || !endEl) return true;
    var startVal = startEl._flatpickr ? startEl._flatpickr.selectedDates[0] : new Date(startEl.value);
    var endVal = endEl._flatpickr ? endEl._flatpickr.selectedDates[0] : new Date(endEl.value);
    if (!startVal || !endVal) return true;
    if (endVal < startVal) {
      setInvalid(endEl, "Konec akce musí být po jejím začátku.");
      return false;
    }
    return true;
  }

  // ── Assignments open: must be before event start ─────────────────────────

  function validateAssignmentsOpenRange(form) {
    var startEl = form.querySelector("[name='start_datetime']");
    var openEl  = form.querySelector("[name='assignments_open_datetime']");
    if (!startEl || !openEl) return true;
    if (!openEl.value.trim()) return true;
    var startVal = startEl._flatpickr ? startEl._flatpickr.selectedDates[0] : new Date(startEl.value);
    var openVal  = openEl._flatpickr  ? openEl._flatpickr.selectedDates[0]  : new Date(openEl.value);
    if (!startVal || !openVal) return true;
    if (openVal >= startVal) {
      setInvalid(openEl, "Otevření přihlášek musí být před začátkem akce.");
      return false;
    }
    return true;
  }

  // ── Password confirmation ─────────────────────────────────────────────────

  function validatePasswordConfirm(form) {
    var pw = form.querySelector("[name='new_password']");
    var conf = form.querySelector("[name='confirm_password']");
    if (!pw || !conf) return true;
    if (pw.value && conf.value && pw.value !== conf.value) {
      setInvalid(conf, "Hesla se neshodují.");
      return false;
    }
    return true;
  }

  // ── Validate a single form ────────────────────────────────────────────────

  function validateForm(form) {
    var ok = true;
    form.querySelectorAll("input, textarea, select").forEach(function (el) {
      if (el.disabled || el.type === "hidden") return;
      clearValidity(el);
      if (!hasValidationRules(el)) return; // no rules → stays neutral, no color

      var fieldOk = true;
      fieldOk = validateRequired(el) && fieldOk;
      fieldOk = validateMinLength(el) && fieldOk;
      fieldOk = validateMaxLength(el) && fieldOk;
      fieldOk = validateNumericRange(el) && fieldOk;
      // Native HTML validity (type=email, pattern, etc.)
      if (fieldOk && el.value.trim() && !el.checkValidity()) {
        setInvalid(el, el.validationMessage || "Neplatná hodnota.");
        fieldOk = false;
      }
      if (!fieldOk) {
        ok = false;
      }
    });

    ok = validateDateRange(form) && ok;
    ok = validateAssignmentsOpenRange(form) && ok;
    ok = validatePasswordConfirm(form) && ok;

    return ok;
  }

  // ── Validate a single field (for live feedback) ───────────────────────────

  function validateField(el) {
    clearValidity(el);
    if (!hasValidationRules(el)) return true;

    var ok = true;
    ok = validateRequired(el) && ok;
    ok = validateMinLength(el) && ok;
    ok = validateMaxLength(el) && ok;
    ok = validateNumericRange(el) && ok;
    if (ok && el.value.trim() && !el.checkValidity()) {
      setInvalid(el, el.validationMessage || "Neplatná hodnota.");
      ok = false;
    }
    return ok;
  }

  // ── Wire up all forms ─────────────────────────────────────────────────────

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[novalidate]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (!validateForm(form)) {
          e.preventDefault();
          e.stopPropagation();
        }
      });

      // Live validation: validate on blur, and re-validate on input if
      // the field is already marked invalid (so errors clear as you type).
      form.querySelectorAll("input, textarea, select").forEach(function (el) {
        if (el.disabled || el.type === "hidden") return;
        if (!hasValidationRules(el)) return;

        el.addEventListener("blur", function () {
          validateField(el);
        });

        el.addEventListener("input", function () {
          if (el.classList.contains("is-invalid")) {
            validateField(el);
          }
        });

        // For <select> elements, validate on change (input doesn't fire reliably)
        if (el.tagName === "SELECT") {
          el.addEventListener("change", function () {
            validateField(el);
          });
        }

        // Flatpickr changes the input value after it has blurred into the
        // calendar. Revalidate after that change so a valid date clears any
        // transient "required" error raised by the blur handler.
        var flatpickrInstance = el._flatpickr ||
          (el.previousElementSibling && el.previousElementSibling._flatpickr);
        if (flatpickrInstance) {
          flatpickrInstance.config.onChange.push(function () {
            validateField(el);
          });
        }
      });

      // Live cross-field: date range (start/end datetime)
      var startEl = form.querySelector("[name='start_datetime']");
      var endEl   = form.querySelector("[name='end_datetime']");
      if (startEl && endEl) {
        function checkDateRange() {
          // Only validate cross-field if both have values
          if (startEl.value && endEl.value) {
            clearValidity(endEl);
            if (!validateDateRange(form) ) return;
            validateField(endEl);
          }
        }
        startEl.addEventListener("change", checkDateRange);
        endEl.addEventListener("change", checkDateRange);
      }

      // Live cross-field: assignments open datetime vs event start
      var openAssignEl = form.querySelector("[name='assignments_open_datetime']");
      if (startEl && openAssignEl) {
        function checkOpenRange() {
          if (startEl.value && openAssignEl.value) {
            clearValidity(openAssignEl);
            if (!validateAssignmentsOpenRange(form)) return;
            validateField(openAssignEl);
          }
        }
        startEl.addEventListener("change", checkOpenRange);
        openAssignEl.addEventListener("change", checkOpenRange);
      }

      // Live cross-field: password confirmation
      var confEl = form.querySelector("[name='confirm_password']");
      if (confEl) {
        confEl.addEventListener("blur", function () {
          clearValidity(confEl);
          if (!validatePasswordConfirm(form)) return;
          validateField(confEl);
        });
        confEl.addEventListener("input", function () {
          if (confEl.classList.contains("is-invalid")) {
            clearValidity(confEl);
            if (!validatePasswordConfirm(form)) return;
            validateField(confEl);
          }
        });
      }
    });
  });
})();
