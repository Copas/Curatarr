// A link to one card (Settings#never-compress, from another page) opens it.
(function () {
  function openLinked() {
    var id = decodeURIComponent(location.hash.slice(1));
    var card = id && document.getElementById(id);
    if (card && card.matches("details.setting-card")) {
      card.open = true;
      card.scrollIntoView({ block: "start" });
    }
  }
  openLinked();
  window.addEventListener("hashchange", openLinked);
})();

// Settings cards (Enhance, Household choices): a card's On/Off follows its
// switches as they're changed, and the save bar says when something isn't
// saved yet.
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
  // Accounts hidden from Jellyfin's sign-in screen show (and count) only
  // while their switch is on.
  function hiddenAccounts() {
    var box = form.querySelector('input[name="picks_hidden"]');
    if (!box) return;
    form.querySelectorAll("[data-hidden-account]").forEach(function (label) {
      label.hidden = !box.checked;
      label.querySelector("input").disabled = !box.checked;
    });
  }
  form.addEventListener("change", function () {
    sync();
    hiddenAccounts();
    if (unsaved) unsaved.hidden = false;
  });
  form.addEventListener("input", function () { if (unsaved) unsaved.hidden = false; });
})();
