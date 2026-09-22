document.addEventListener("DOMContentLoaded", function () {
  var typeSelect = document.getElementById("type_select");
  var typeOther = document.getElementById("type_other");
  if (typeSelect && typeOther) {
    typeSelect.addEventListener("change", function () {
      typeOther.classList.toggle("is-hidden", typeSelect.value !== "__other__");
    });
  }
});
