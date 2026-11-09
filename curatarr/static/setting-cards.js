// Settings cards (Enhance, Household choices): a card's On/Off follows its switches as they're changed,
// and the save bar says when something isn't saved yet.
(function () {
  var form = document.querySelector(".sticky-actions") && document.querySelector(".sticky-actions").closest("form");
  if (!form) return;
  var unsaved = form.querySelector("[data-unsaved]");
  function sync() {
    form.querySelectorAll("[data-toggles]").forEach(function (chip) {
      var on = chip.dataset.toggles.split(",").some(function (name) {
        var box = form.querySelector('input[type="checkbox"][name="' + name + '"]');
        return box && box.checked;
      });
      chip.textContent = on ? "On" : "Off";
      chip.classList.toggle("chip-on", on);
    });
  }
  form.addEventListener("change", function () {
    sync();
    if (unsaved) unsaved.hidden = false;
  });
  form.addEventListener("input", function () { if (unsaved) unsaved.hidden = false; });
})();
