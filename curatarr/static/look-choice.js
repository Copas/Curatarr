// Generate and Batches: while the house look is chosen, its own settings
// stand in for the editable ones, which are hidden (and ignored if sent).
(function () {
  function sync(form) {
    var house = form.querySelector('input[name="look"][value="house"]');
    var own = form.querySelector("[data-own-settings]");
    if (!house || !own) return;
    own.hidden = house.checked;
  }
  document.querySelectorAll("form").forEach(function (form) {
    if (!form.querySelector("[data-look-choice]")) return;
    sync(form);
    form.addEventListener("change", function (event) {
      if (event.target.name === "look") sync(form);
    });
  });
})();
