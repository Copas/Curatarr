// Keep the chapter maintenance Activity card live without a manual refresh:
// it's swapped only when something in it changed, open sections stay open,
// and the page doesn't jump.
(function () {
  var INTERVAL_MS = 5000;
  var last = null;  // the last card fetched (the page's own has local times)

  function poll() {
    var el = document.getElementById("chapter-activity");
    if (!el) return;
    var url = el.dataset.refreshUrl;
    if (!url) return;
    fetch(url)
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html || html === last) return;  // nothing new
        last = html;
        var wrapper = document.createElement("div");
        wrapper.innerHTML = html.trim();
        var next = wrapper.firstElementChild;
        if (!next) return;
        el.querySelectorAll("details[id]").forEach(function (old) {
          var match = next.querySelector("#" + CSS.escape(old.id));
          if (match) match.open = old.open;
        });
        var x = window.scrollX, y = window.scrollY;
        el.replaceWith(next);
        window.scrollTo(x, y);
        if (window.curatarrLocalTime) window.curatarrLocalTime(next);
      })
      .catch(function () {})
      .finally(function () { setTimeout(poll, INTERVAL_MS); });
  }

  setTimeout(poll, INTERVAL_MS);
})();
