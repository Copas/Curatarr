// Overview: while a reconciliation runs, update its status line in place
// (the rest of the page is left alone, so nothing opened closes). When it
// finishes, offer the new numbers rather than reloading the page.
(function () {
  var INTERVAL_MS = 10000;
  var line = document.getElementById("reconcile-status");
  var last = null;  // the last line fetched (the page's own has local times)
  if (!line || !line.dataset.active) return;

  function check() {
    fetch(line.dataset.statusUrl)
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html) return true;
        var holder = document.createElement("div");
        holder.innerHTML = html.trim();
        var next = holder.firstElementChild;
        if (!next) return true;
        var running = Boolean(next.dataset.active);
        if (html !== last) {
          last = html;
          line.className = next.className;
          line.innerHTML = next.innerHTML;
          if (window.curatarrLocalTime) window.curatarrLocalTime(line);
        }
        if (!running) {
          var link = document.createElement("a");
          link.href = window.location.href;
          link.textContent = "Show the new numbers";
          line.appendChild(document.createTextNode(" "));
          line.appendChild(link);
        }
        return running;
      })
      .catch(function () { return true; })
      .then(function (again) { if (again) setTimeout(check, INTERVAL_MS); });
  }

  setTimeout(check, INTERVAL_MS);
})();
