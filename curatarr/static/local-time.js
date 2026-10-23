// Show times in the viewer's own time zone, the way people say them: "today
// at 5:55 PM", "tomorrow at 4 PM", "Wednesday at 3:21 AM", "Oct 31". The
// server renders UTC as a fallback inside <time datetime="..."> elements (see
// the "when" filter); hovering shows the full date and time.
(function () {
  var DAY = 86400000;
  var WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  function midnight(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }

  // 4:52 PM, or 4 PM on the hour (in the viewer's own 12- or 24-hour style).
  function clock(d) {
    var text = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return d.getMinutes() === 0 ? text.replace(/[:.]00(?=\D|$)/, "") : text;
  }

  // today, tomorrow, yesterday, a weekday within the week either side, else
  // the date (with the year only when it isn't this year's).
  function day(d, now) {
    var days = Math.round((midnight(d) - midnight(now)) / DAY);
    if (days === 0) return { text: "today", relative: true };
    if (days === 1) return { text: "tomorrow", relative: true };
    if (days === -1) return { text: "yesterday", relative: true };
    if (days > 1 && days < 7) return { text: WEEKDAYS[d.getDay()], relative: false };
    if (days < -1 && days > -7) return { text: "last " + WEEKDAYS[d.getDay()], relative: true };
    var options = { month: "short", day: "numeric" };
    if (d.getFullYear() !== now.getFullYear()) options.year = "numeric";
    return { text: d.toLocaleDateString([], options), relative: false };
  }

  function phrase(d, style, now) {
    var on = day(d, now);
    if (style === "date") return on;
    // A time on its own ("time", "seconds") says which day only when it isn't today.
    if ((style === "time" || style === "seconds") && on.text === "today") {
      return { text: clock(d), relative: false };
    }
    return { text: on.text + " at " + clock(d), relative: on.relative };
  }

  // "the first on today" reads wrong: drop a preceding "on" before today,
  // tomorrow, yesterday and "last Monday".
  function dropOn(el) {
    var before = el.previousSibling;
    if (before && before.nodeType === 3 && /\bon\s+$/.test(before.nodeValue)) {
      before.nodeValue = before.nodeValue.replace(/\bon\s+$/, "");
    }
  }

  // "Today at 5 PM" when it starts a cell or line; "today" mid-sentence.
  function startsLine(el) {
    for (var n = el.previousSibling; n; n = n.previousSibling) {
      if (n.nodeType === 3 ? n.nodeValue.trim() : n.nodeType === 1) return false;
    }
    return true;
  }

  function apply(root) {
    var now = new Date();
    (root || document).querySelectorAll("time[data-style]").forEach(function (el) {
      var d = new Date(el.getAttribute("datetime"));
      if (isNaN(d)) return;
      var said = phrase(d, el.dataset.style, now);
      if (said.relative) dropOn(el);
      el.textContent = startsLine(el)
        ? said.text.charAt(0).toUpperCase() + said.text.slice(1)
        : said.text;
      el.title = d.toLocaleString([], {
        weekday: "long", year: "numeric", month: "long", day: "numeric",
        hour: "numeric", minute: "2-digit", timeZoneName: "short",
      });
    });
  }
  window.curatarrLocalTime = apply;
  apply();
})();
