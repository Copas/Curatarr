// Field errors: a field the browser finds wrong (out of its range, not a web
// address, required and empty) says so under itself once someone has left
// it or tried to send the form, never while they're still typing, and stops
// as soon as it's put right. The red border is CSS (:user-invalid); this adds
// the words, the browser's own message ("Value must be less than or equal
// to 3650"), and keeps screen readers in step: aria-invalid follows
// :user-invalid, the message joins the field's description only while it
// shows, and an error that appears as someone leaves a field is announced
// once, politely. Listens on the document, so redrawn parts work too.
(function () {
  if (!(window.CSS && CSS.supports("selector(:user-invalid)"))) return; // the browser's bubble on sending stays
  var FIELDS = "input, select, textarea";
  var announcer = document.createElement("div");
  announcer.className = "visually-hidden";
  announcer.setAttribute("aria-live", "polite");
  var announcedField = null, timer = null, made = 0;
  var flagged = new WeakSet(), announced = new WeakSet();

  function spoken(el) { return (el.textContent || "").replace(/\s+/g, " ").trim(); }

  // The message goes after the field (after its input group, if it's in one).
  function errorFor(el, create) {
    var id = el.dataset.error, error = id && document.getElementById(id);
    if (error || !create) return error;
    error = document.createElement("span");
    error.className = "field-error";
    error.id = "field-error-" + (++made);
    error.hidden = true;
    var after = el.closest(".input-group") || el;
    after.insertAdjacentElement("afterend", error);
    el.dataset.error = error.id;
    return error;
  }

  function describe(el, error, on) {
    var ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(function (x) { return x && x !== error.id; });
    if (on) ids.push(error.id);
    if (ids.length) el.setAttribute("aria-describedby", ids.join(" "));
    else el.removeAttribute("aria-describedby");
  }

  function announce(el, error) {
    if (!announcer.isConnected) document.body.appendChild(announcer);
    var label = el.labels && el.labels[0] ? spoken(el.labels[0]) + ": " : "";
    clearTimeout(timer);
    announcer.textContent = "";
    announcedField = el;
    // after the screen reader has read the field that gets focus next
    timer = setTimeout(function () { announcer.textContent = label + spoken(error); }, 500);
  }

  function sync(el, speak) {
    if (!el.matches || !el.matches(FIELDS) || el.type === "hidden" || el.disabled) return;
    if (el.matches(":user-invalid")) {
      var error = errorFor(el, true);
      error.textContent = el.validationMessage;
      error.hidden = false;
      el.setAttribute("aria-invalid", "true");
      describe(el, error, true);
      if (speak && !announced.has(el)) { announce(el, error); announced.add(el); }
      flagged.add(el);
    } else if (flagged.has(el)) {
      var shown = errorFor(el, false);
      el.setAttribute("aria-invalid", "false");
      if (shown) { shown.hidden = true; describe(el, shown, false); }
      flagged.delete(el);
      announced.delete(el);
      if (announcedField === el) { clearTimeout(timer); announcer.textContent = ""; }
    }
  }

  // An empty required field reads as invalid from the start in some browsers:
  // say it isn't, until someone has had a go at it.
  function start() {
    document.querySelectorAll("[required]:not([aria-invalid])").forEach(function (el) { el.setAttribute("aria-invalid", "false"); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();

  document.addEventListener("blur", function (e) { sync(e.target, true); }, true);
  document.addEventListener("change", function (e) { sync(e.target, true); }, true);
  document.addEventListener("input", function (e) { if (flagged.has(e.target)) sync(e.target); });
  // A failed send flags every wrong field; focus goes to the first, which
  // reads its error, so these aren't announced as well.
  document.addEventListener("invalid", function (e) { announced.add(e.target); sync(e.target); }, true);
  document.addEventListener("reset", function (e) {
    setTimeout(function () { Array.prototype.forEach.call(e.target.elements, function (el) { sync(el); }); });
  }, true);
})();
