// Show times in the viewer's own time zone. The server renders UTC as a
// fallback inside <time datetime="..."> elements (see the "when" filter).
(function () {
  function pad(n) { return String(n).padStart(2, "0"); }
  function date(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function clock(d, seconds) {
    var options = { hour: "numeric", minute: "2-digit" };
    if (seconds) options.second = "2-digit";
    return d.toLocaleTimeString([], options);
  }
  function apply(root) {
    (root || document).querySelectorAll("time[data-style]").forEach(function (el) {
      var d = new Date(el.getAttribute("datetime"));
      if (isNaN(d)) return;
      var style = el.dataset.style;
      el.textContent =
        style === "date" ? date(d)
        : style === "time" ? clock(d, false)
        : style === "seconds" ? clock(d, true)
        : date(d) + " " + clock(d, false);
      el.title = d.toLocaleString([], { timeZoneName: "short" });
    });
  }
  window.curatarrLocalTime = apply;
  apply();
})();
