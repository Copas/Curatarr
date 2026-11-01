// Generate and Batches: while a saved look (or the house look) is chosen, its
// own settings stand in for the editable ones, which are hidden (and ignored
// if sent).
(function () {
  function sync(form) {
    var chosen = form.querySelector('input[name="look"]:checked');
    var own = form.querySelector("[data-own-settings]");
    if (!chosen || !own) return;
    own.hidden = chosen.value !== "custom";
  }
  document.querySelectorAll("form").forEach(function (form) {
    if (!form.querySelector("[data-look-choice]")) return;
    sync(form);
    form.addEventListener("change", function (event) {
      if (event.target.name === "look") sync(form);
    });
  });
})();
