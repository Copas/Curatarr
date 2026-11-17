// The header's light/dark switch: says which scheme the device itself uses,
// so picking that one clears the saved choice and the page follows the
// device again (see routes.theme_preference). Without this script the
// switch still works; the choice is then simply kept.
(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches("form[data-theme-switch]")) return;
    var system = form.querySelector('input[name="system"]');
    if (system) system.value = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }, true);
})();
