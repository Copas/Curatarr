// The one way pages put HTML from Curatarr's own server into themselves (a
// part redrawn in place, a list refreshed while typing, a page's content
// after a button press). Every script that updates a page in place goes
// through here instead of setting innerHTML itself, so:
//  - HTML is only ever turned into page content in one place, through one
//    Trusted Types policy ("curatarr-html"), which refuses HTML with inline
//    script in it (an on…= handler, a javascript: link; Curatarr's pages
//    have none). <script src> tags in a whole page are inert: a parsed
//    document never runs them, here or once its parts are moved into the
//    page. The security policy can then tell the browser to refuse HTML
//    from anywhere else;
//  - a part that's redrawn keeps what the person had open, ticked or typed
//    in it, the page stays where it was scrolled to, and times are shown in
//    the viewer's own time zone again.
(function () {
  // Kept in step with tests/test_page_security.py, which runs it over pages.
  var SCRIPTED = /<[^>]*\son[a-z]+\s*=|\b(?:href|src|action|formaction)\s*=\s*["']?\s*javascript:/i;

  function check(html) {
    if (SCRIPTED.test(html)) throw new Error("Refused HTML with inline script");
    return html;
  }

  var policy = window.trustedTypes
    ? window.trustedTypes.createPolicy("curatarr-html", { createHTML: check })
    : { createHTML: check };

  // A whole page (or any HTML) as a document of its own, not yet shown.
  function page(html) {
    return new DOMParser().parseFromString(policy.createHTML(String(html)), "text/html");
  }

  // The first element of an HTML snippet. A table row only survives being
  // read inside a table, so a snippet meant to replace one is read there.
  function element(html, like) {
    var text = String(html).trim();
    if (like && like.tagName === "TR") {
      return page("<table><tbody>" + text + "</tbody></table>").querySelector("tbody > tr");
    }
    return page(text).body.firstElementChild;
  }

  // What the person had open, ticked or typed in `from`, carried to `to`
  // (matched by id; hidden inputs keep the server's value).
  function carryOver(from, to) {
    from.querySelectorAll("details[id]").forEach(function (old) {
      var match = to.querySelector("#" + CSS.escape(old.id));
      if (match) match.open = old.open;
    });
    from.querySelectorAll("input[id], textarea[id], select[id]").forEach(function (old) {
      var match = to.querySelector("#" + CSS.escape(old.id));
      if (!match || match.type === "hidden" || old.type === "hidden") return;
      if (old.type === "checkbox" || old.type === "radio") match.checked = old.checked;
      else match.value = old.value;
    });
  }

  // After new content arrives: local times, and anything else a script
  // draws into the page (the recent-additions reel).
  function settle(root) {
    if (window.curatarrLocalTime) window.curatarrLocalTime(root);
    if (window.curatarrReel) window.curatarrReel(root);
  }

  // Put `fresh` (an element) where `old` is, keeping the scroll position and
  // (unless `keep` is false: redrawn after an action, it shows what the
  // server now has) what was open, ticked or typed.
  function replace(old, fresh, keep) {
    if (!old || !fresh) return null;
    if (keep !== false) carryOver(old, fresh);
    var x = window.scrollX, y = window.scrollY;
    old.replaceWith(fresh);
    window.scrollTo(x, y);
    settle(fresh);
    return fresh;
  }

  // Put the children of `source` (an element from another document) into
  // `target`, in place of what it held.
  function fill(target, source) {
    var x = window.scrollX, y = window.scrollY;
    target.replaceChildren.apply(target, Array.prototype.slice.call(source.childNodes));
    window.scrollTo(x, y);
    settle(target);
    return target;
  }

  // Fetch a snippet and redraw `el` with it (its own data-live-url unless
  // given). Resolves to the new element, or null when nothing usable came
  // back (left as it was: an action already done is never sent again
  // because its redraw failed).
  function redraw(el, url, keep) {
    if (!el || !(url || el.dataset.liveUrl)) return Promise.resolve(null);
    return fetch(url || el.dataset.liveUrl, { headers: { Accept: "text/html" } })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) { return html.trim() ? replace(el, element(html, el), keep) : null; });
  }

  // Resolves at once while the tab is in view; otherwise when it next is.
  // Pages that check for news every few seconds wait on this, so a tab left
  // in the background asks nothing, and catches up as soon as it's looked at.
  function visible() {
    if (!document.hidden) return Promise.resolve();
    return new Promise(function (resolve) {
      document.addEventListener("visibilitychange", function shown() {
        if (document.hidden) return;
        document.removeEventListener("visibilitychange", shown);
        resolve();
      });
    });
  }

  window.curatarrSwap = {
    visible: visible,
    page: page,
    element: element,
    carryOver: carryOver,
    replace: replace,
    fill: fill,
    redraw: redraw,
    settle: settle,
  };
})();
