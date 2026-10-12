// Keep the chapter maintenance Activity card live without a manual refresh.
(function () {
  var INTERVAL_MS = 5000;

  function poll() {
    var el = document.getElementById("chapter-activity");
    if (!el) return;
    var url = el.dataset.refreshUrl;
    if (!url) return;
    fetch(url)
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html) return;
        var wrapper = document.createElement("div");
        wrapper.innerHTML = html.trim();
        var next = wrapper.firstElementChild;
        if (!next) return;
        el.replaceWith(next);
        if (window.curatarrLocalTime) window.curatarrLocalTime(next);
      })
      .catch(function () {})
      .finally(function () { setTimeout(poll, INTERVAL_MS); });
  }

  setTimeout(poll, INTERVAL_MS);
})();
