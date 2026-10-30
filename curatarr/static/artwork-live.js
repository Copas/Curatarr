// Posters → Jobs and a job's page, while pictures are being made: asks
// Curatarr every few seconds what changed and updates only that, without
// reloading the page. Progress bars move in place; a picture's panel or a
// job's row is redrawn on its own only when it changes (a new picture, a
// new state), keeping what was open, ticked or typed in it. Nothing else on
// the page is touched, so open sections stay open and the page stays put.
(function () {
  var INTERVAL_MS = 5000;
  var root = document.getElementById("artwork-live");
  if (!root || !root.dataset.active) return;  // nothing being made

  function parts() {
    return Array.prototype.slice.call(root.querySelectorAll("[data-live]"));
  }

  function carryOver(from, to) {
    from.querySelectorAll("details[id]").forEach(function (old) {
      var match = to.querySelector("#" + CSS.escape(old.id));
      if (match) match.open = old.open;
    });
    from.querySelectorAll("input[id], textarea[id], select[id]").forEach(function (old) {
      var match = to.querySelector("#" + CSS.escape(old.id));
      if (!match || match.type === "hidden") return;
      if (old.type === "checkbox" || old.type === "radio") match.checked = old.checked;
      else match.value = old.value;
    });
  }

  function busy(el) {
    // Someone's in the middle of something in it: redraw it later.
    var focused = document.activeElement;
    return focused && el.contains(focused) && /INPUT|TEXTAREA|SELECT/.test(focused.tagName);
  }

  function redraw(el) {
    return fetch(el.dataset.liveUrl)
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html) return;
        var holder = document.createElement(el.tagName === "TR" ? "tbody" : "div");
        holder.innerHTML = html.trim();
        var next = holder.firstElementChild;
        if (!next) return;
        carryOver(el, next);
        el.replaceWith(next);
        if (window.curatarrLocalTime) window.curatarrLocalTime(next);
      });
  }

  function check() {
    var images = [], jobs = [];
    parts().forEach(function (el) {
      (el.dataset.live === "image" ? images : jobs).push(el.dataset.liveId);
    });
    var url = root.dataset.statusUrl + "?images=" + images.join(",") + "&jobs=" + jobs.join(",");
    return fetch(url)
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (status) {
        if (!status) return true;
        Object.keys(status.progress || {}).forEach(function (id) {
          root.querySelectorAll('[data-progress-for="' + id + '"]').forEach(function (bar) {
            if (bar.tagName === "PROGRESS") bar.value = status.progress[id];
            else bar.textContent = status.progress[id] + "%";
          });
        });
        var keys = { image: status.images || {}, row: status.rows || {}, head: status.heads || {} };
        var stale = parts().filter(function (el) {
          var key = (keys[el.dataset.live] || {})[el.dataset.liveId];
          return key !== undefined && key !== el.dataset.liveKey;
        });
        var ready = stale.filter(function (el) { return !busy(el); });
        return Promise.all(ready.map(redraw)).then(function () {
          // Keep asking while anything's being made or waits to be redrawn.
          return status.active || stale.length > 0;
        });
      });
  }

  function loop() {
    check()
      .catch(function () { return true; })
      .then(function (again) {
        if (again !== false) setTimeout(loop, INTERVAL_MS);
      });
  }

  setTimeout(loop, INTERVAL_MS);
})();
