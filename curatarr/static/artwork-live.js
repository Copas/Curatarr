// Posters → Jobs and a job's page: refresh while pictures are being made.
(function () {
  var INTERVAL_MS = 5000;

  function poll() {
    var el = document.getElementById("artwork-live");
    if (!el || !el.dataset.live) return;  // nothing running: stop
    var focused = document.activeElement;
    if (focused && el.contains(focused) && /INPUT|TEXTAREA|SELECT/.test(focused.tagName)) {
      setTimeout(poll, INTERVAL_MS);  // someone's typing feedback: wait
      return;
    }
    fetch(el.dataset.refreshUrl)
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
