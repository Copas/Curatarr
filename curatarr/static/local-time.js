// <local-time>: a time in the viewer's own time zone, the way people say it:
// "today at 5:55 PM", "tomorrow at 4 PM", "Wednesday at 3:21 AM", "Oct 31".
// The server renders <local-time><time datetime="..." data-style="...">UTC
// fallback</time></local-time> (the "when" filter); the element rewrites the
// text whenever it's put on a page, so parts redrawn in place are covered
// with nothing to call. Hovering shows the full date and time.
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

  // "just now", "25m ago", "14h ago", "3d ago": for lists of what just happened.
  function ago(d, now) {
    var minutes = Math.max(0, Math.floor((now - d) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + "m ago";
    if (minutes < 1440) return Math.floor(minutes / 60) + "h ago";
    return Math.floor(minutes / 1440) + "d ago";
  }

  // "in 2 h 14 min", "in 5 min": a countdown, kept up to date below.
  function until(d, now) {
    var minutes = Math.ceil((d - now) / 60000);
    if (minutes < 1) return "any moment now";
    if (minutes < 60) return "in " + minutes + " min";
    var hours = Math.floor(minutes / 60), rest = minutes % 60;
    return "in " + hours + " h" + (rest ? " " + rest + " min" : "");
  }

  function phrase(d, style, now) {
    if (style === "ago") return { text: ago(d, now), relative: false };
    if (style === "in") return { text: until(d, now), relative: false };
    var on = day(d, now);
    if (style === "date") return on;
    // A time on its own ("time", "seconds") says which day only when it isn't today.
    if ((style === "time" || style === "seconds") && on.text === "today") {
      return { text: clock(d), relative: false };
    }
    return { text: on.text + " at " + clock(d), relative: on.relative };
  }

  // "the first on today" reads wrong: drop a preceding "on" before today,
  // tomorrow, yesterday and "last Monday". (el is the <local-time>: what's
  // before it in the sentence.)
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

  var countdowns = new Set();
  var timer = null;

  class LocalTime extends HTMLElement {
    connectedCallback() {
      this.render();
      // Countdowns ("in 5 min") move on by themselves, every 30 seconds,
      // and only while one is on the page.
      if (this.querySelector('time[data-style="in"]')) {
        countdowns.add(this);
        if (!timer) {
          timer = setInterval(function () { countdowns.forEach(function (el) { el.render(); }); }, 30000);
        }
      }
    }

    disconnectedCallback() {
      countdowns.delete(this);
      if (!countdowns.size && timer) { clearInterval(timer); timer = null; }
    }

    render() {
      var time = this.querySelector("time[data-style]");
      if (!time) return;
      var d = new Date(time.getAttribute("datetime"));
      if (isNaN(d)) return;
      var said = phrase(d, time.dataset.style, new Date());
      if (said.relative) dropOn(this);
      time.textContent = startsLine(this)
        ? said.text.charAt(0).toUpperCase() + said.text.slice(1)
        : said.text;
      time.title = d.toLocaleString([], {
        weekday: "long", year: "numeric", month: "long", day: "numeric",
        hour: "numeric", minute: "2-digit", timeZoneName: "short",
      });
    }
  }

  if (!customElements.get("local-time")) customElements.define("local-time", LocalTime);
})();
