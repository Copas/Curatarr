// Keep the chapter maintenance Activity card live without a manual refresh:
// it's swapped only when something in it changed, open sections stay open,
// and the page doesn't jump.
(function () {
  var INTERVAL_MS = 5000;
  var last = null;  // the last card fetched (the page's own has local times)

  // Low priority: a check in the background, so a page someone clicks to
  // loads first on a slow connection.
  function poll() {
    var el = document.getElementById("chapter-activity");
    if (!el) return;
    var url = el.dataset.refreshUrl;
    if (!url) return;
    window.curatarrSwap.visible()
      .then(function () { return fetch(url, { priority: "low" }); })
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html || html === last) return;  // nothing new
        last = html;
        window.curatarrSwap.replace(el, window.curatarrSwap.element(html));
      })
      .catch(function () {})
      .finally(function () { setTimeout(poll, INTERVAL_MS); });
  }

  setTimeout(poll, INTERVAL_MS);
})();
