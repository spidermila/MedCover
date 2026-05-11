/**
 * MedCover — client-side form validation
 *
 * UX enhancement only — all rules are also enforced server-side.
 * Uses Bootstrap 5 is-invalid / is-valid classes directly.
 * NOTE: we do NOT add the Bootstrap "was-validated" class because that triggers
 * the CSS :valid selector which turns ALL filled fields green unconditionally.
 */
(function () {
  "use strict";

  // ── Helpers ──────────────────────────────────────────────────────────────

  function setInvalid(el, message) {
    el.classList.add("is-invalid");
    el.classList.remove("is-valid");
    var fb = el.nextElementSibling;
    if (fb && fb.classList.contains("invalid-feedback")) {
      fb.textContent = message;
    } else {
      fb = document.createElement("div");
      fb.className = "invalid-feedback";
      fb.textContent = message;
      el.parentNode.insertBefore(fb, el.nextSibling);
    }
  }

  function setValid(el) {
    el.classList.remove("is-invalid");
    el.classList.add("is-valid");
  }

  function clearValidity(el) {
    el.classList.remove("is-invalid", "is-valid");
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
      setInvalid(el, "Toto pole je povinné.");
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
    // Note: setValid is NOT called here — handled centrally in validateForm
    // after all cross-field checks pass.
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
    // Note: setValid is NOT called here — handled centrally in validateForm.
  }

  // ── Validate a single form ────────────────────────────────────────────────

  function validateForm(form) {
    var ok = true;
    // Fields that have rules and passed — candidates for green if overall ok.
    var passedFields = [];
    // Track cross-field fields so they can also get green when overall ok.
    var startEl = form.querySelector("[name='start_datetime']");
    var endEl   = form.querySelector("[name='end_datetime']");
    var pwEl    = form.querySelector("[name='new_password']");
    var confEl  = form.querySelector("[name='confirm_password']");

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
      } else if (el.value.trim()) {
        passedFields.push(el);
      }
    });

    ok = validateDateRange(form) && ok;
    ok = validatePasswordConfirm(form) && ok;

    // Only mark fields green when the ENTIRE form passes all checks.
    // This prevents the confusing state where some fields are green while
    // others are red (e.g. start_datetime green while end_datetime is red).
    if (ok) {
      passedFields.forEach(function (el) { setValid(el); });
      // Cross-field fields: green only if they have values and overall ok.
      if (startEl && startEl.value.trim()) setValid(startEl);
      if (endEl   && endEl.value.trim())   setValid(endEl);
      if (pwEl    && pwEl.value)           setValid(pwEl);
      if (confEl  && confEl.value)         setValid(confEl);
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
        // NOTE: do NOT add "was-validated" class here.
        // Bootstrap's was-validated triggers .was-validated :valid CSS which
        // turns ALL fields with any value green unconditionally via the native
        // :valid pseudo-class, regardless of our custom validation outcome.
      });
    });
  });
})();
