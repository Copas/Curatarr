// Buttons on a page marked data-live-page (Problems, Overview) work in place: the click is sent in
// the background, only that button shows it's working, and the page's content
// is then redrawn where it is (scroll kept, details left open stay open). So
// several things can be clicked one after another without waiting for a reload.
// Messages come up as notes in the corner. If the background request fails,
// the form is sent the usual way, so nothing's lost.
(function () {
  var pending = {}; // form key → true while its request is out
  function note(text, category) {
    window.curatarrNote(text, category, 8000);
  }

  function key(form, button) {
    return form.action + "|" + (button && button.name ? button.name + "=" + button.value : "") +
      "|" + Array.prototype.map.call(form.querySelectorAll("input[type=hidden]:not([name=csrf_token])"),
        function (i) { return i.name + "=" + i.value; }).join("&");
  }

  function busy(form, on) {
    form.querySelectorAll("button, summary").forEach(function (b) {
      b.classList.toggle("is-busy", on);
      if (on) b.setAttribute("aria-busy", "true"); else b.removeAttribute("aria-busy");
      if (b.tagName === "BUTTON") b.disabled = on;
    });
  }

  function remark(root) {
    // After a redraw, buttons whose request is still out stay marked.
    root.querySelectorAll("form").forEach(function (form) {
      var k = key(form, form.querySelector("button"));
      if (pending[k]) busy(form, true);
    });
  }

  function parse(html) {
    return window.curatarrSwap.page(html);
  }

  function notesFrom(doc) {
    doc.querySelectorAll("main > .alert:not([id])").forEach(function (alert) {
      var category = (alert.className.match(/alert-(\w+)/) || [])[1];
      note(alert.textContent.trim(), category === "danger" ? "danger" : category);
    });
  }

  function redraw(doc) {
    var fresh = doc.querySelector("[data-live-page]");
    var here = document.querySelector("[data-live-page]");
    if (!fresh || !here) return false;
    var open = {};
    here.querySelectorAll("details[open]").forEach(function (d, n) { open[n] = true; });
    var x = window.scrollX, y = window.scrollY;
    here.replaceWith(fresh);
    fresh.querySelectorAll("details").forEach(function (d, n) { if (open[n]) d.open = true; });
    ["nav.section-nav", ".app-nav"].forEach(function (selector) {
      var a = document.querySelector(selector), b = doc.querySelector(selector);
      if (a && b) a.replaceWith(b);
    });
    window.scrollTo(x, y);
    remark(fresh);
    return true;
  }

  // The answer wasn't this page (signed out since, say, so it's the sign-in
  // page): go there, rather than leave the button working for ever.
  function show(doc, url) {
    if (!redraw(doc)) location.assign(url);
  }

  function refresh(html, url) {
    // The answer is usually this page already; otherwise fetch it afresh
    // (its messages were shown on the page the action went to).
    var doc = parse(html);
    notesFrom(doc);
    if (url && new URL(url).pathname === location.pathname) {
      show(doc, url);
      return Promise.resolve();
    }
    return fetch(location.href)
      .then(function (r) { return r.text().then(function (page) { show(parse(page), r.url); }); });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || !form.closest("[data-live-page]")) return;
    if (form.hasAttribute("data-reload")) return; // starts something the page follows after loading
    if ((form.method || "").toLowerCase() !== "post" || form.target === "_blank") return;
    var button = event.submitter;
    var k = key(form, button);
    event.preventDefault();
    if (pending[k]) return; // already sent
    var action = (button && button.getAttribute("formaction")) || form.action;
    var data = new FormData(form, button || undefined);
    pending[k] = true;
    busy(form, true);
    fetch(action, { method: "POST", body: data, credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.text().then(function (html) { return refresh(html, response.url); });
      })
      .then(function () { delete pending[k]; })
      .catch(function () {
        delete pending[k];
        note("Couldn't do that in place; sending it the usual way.", "warning");
        // Sent as the button would have sent it: to its own address, with
        // its name and value (a plain submit leaves both out).
        if (button && button.getAttribute("formaction")) form.action = action;
        if (button && button.name) {
          var extra = document.createElement("input");
          extra.type = "hidden";
          extra.name = button.name;
          extra.value = button.value;
          form.appendChild(extra);
        }
        if (!form.isConnected) {
          // Redrawn away meanwhile: a form outside the page can't be sent.
          form.hidden = true;
          document.body.appendChild(form);
        }
        HTMLFormElement.prototype.submit.call(form);
      });
  }, true);
})();
