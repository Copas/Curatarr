// Generate and Batches: choosing a look fills in its settings, and Restore
// the default puts the default look's back. The fields stay editable.
(function () {
  function fill(form, values) {
    if (!values) return;
    Object.keys(values).forEach(function (name) {
      var field = form.querySelector('[name="' + name + '"]');
      if (!field) return;
      if (field.type === "checkbox") field.checked = Boolean(values[name]);
      else field.value = values[name];
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  document.querySelectorAll("[data-look-choice]").forEach(function (choice) {
    var form = choice.closest("form");
    var data = choice.querySelector("[data-look-values]");
    var looks = data ? JSON.parse(data.textContent) : {};
    var select = choice.querySelector('select[name="look"]');
    select.addEventListener("change", function () { fill(form, looks[select.value]); });
    choice.querySelector("[data-restore-default]").addEventListener("click", function () {
      select.value = "default";
      fill(form, looks["default"]);
    });
  });
})();
