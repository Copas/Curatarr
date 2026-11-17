// <reconcile-status>: Overview's sync line (templates/_reconcile_status.html).
// While a sync runs it updates itself in place every few seconds (the rest
// of the page is left alone, so nothing opened closes), and when it
// finishes it offers the new numbers rather than reloading the page. It
// starts whenever a running line is put on the page: at load, or when
// Overview redraws itself after Sync now.
(function () {
  var INTERVAL_MS = 10000;

  class ReconcileStatus extends HTMLElement {
    connectedCallback() {
      if (this.dataset.active && !this.timer) {
        var line = this;
        this.timer = setTimeout(function () { line.check(); }, INTERVAL_MS);
      }
    }

    disconnectedCallback() {
      clearTimeout(this.timer);
      this.timer = null;
    }

    // Low priority: a check in the background, so a page someone clicks to
    // loads first on a slow connection.
    check() {
      var line = this;
      window.curatarrSwap.visible()
        .then(function () { return fetch(line.dataset.statusUrl, { priority: "low" }); })
        .then(function (response) { return response.ok ? response.text() : null; })
        .then(function (html) {
          if (!html) return true;
          var next = window.curatarrSwap.element(html);
          if (!next) return true;
          var running = Boolean(next.dataset.active);
          if (html !== line.last) {
            line.last = html;  // the page's own copy has local times, so compare fetches
            line.className = next.className;
            window.curatarrSwap.fill(line, next);
          }
          if (!running) {
            delete line.dataset.active;
            var link = document.createElement("a");
            link.href = window.location.href;
            link.textContent = "Show the new numbers";
            line.appendChild(document.createTextNode(" "));
            line.appendChild(link);
          }
          return running;
        })
        .catch(function () { return true; })
        .then(function (again) {
          line.timer = again && line.isConnected ? setTimeout(function () { line.check(); }, INTERVAL_MS) : null;
        });
    }
  }

  if (!customElements.get("reconcile-status")) customElements.define("reconcile-status", ReconcileStatus);
})();
