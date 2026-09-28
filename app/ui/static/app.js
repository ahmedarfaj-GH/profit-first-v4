document.addEventListener("DOMContentLoaded", function () {
  var typeSelect = document.getElementById("type_select");
  var typeOther = document.getElementById("type_other");
  if (typeSelect && typeOther) {
    typeSelect.addEventListener("change", function () {
      typeOther.classList.toggle("is-hidden", typeSelect.value !== "__other__");
    });
  }
  // Inline handlers are blocked by the CSP, so auto-submitting selects are wired here.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });
});
