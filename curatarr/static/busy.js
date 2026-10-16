// Show that a click registered while the next page loads, and stop a second
// click from sending the same request twice.
(function () {
  var bar = null;

  function loading() {
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "page-loading";
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-label", "Loading");
      document.body.appendChild(bar);
    }
    // Restart the animation each time.
    bar.classList.remove("is-on");
    void bar.offsetWidth;
    bar.classList.add("is-on");
  }

  function mark(el) {
    if (!el) return;
    el.classList.add("is-busy");
    el.setAttribute("aria-busy", "true");
  }

  function reset() {
    document.querySelectorAll(".is-busy").forEach(function (el) {
      el.classList.remove("is-busy");
      el.removeAttribute("aria-busy");
    });
    document.querySelectorAll("form[data-busy]").forEach(function (form) {
      delete form.dataset.busy;
    });
    if (bar) bar.classList.remove("is-on");
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.target === "_blank") return;
    if (form.dataset.busy) {
      event.preventDefault(); // already sent; wait for the answer
      return;
    }
    form.dataset.busy = "1";
    // Not disabled: a disabled button's name and value aren't sent with the form.
    mark(event.submitter || form.querySelector("button:not([type=button])"));
    loading();
  });

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target.closest ? event.target.closest("a[href]") : null;
    if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
    var href = link.getAttribute("href");
    if (!href || href.charAt(0) === "#" || link.origin !== location.origin) return;
    if (link.pathname === location.pathname && link.search === location.search && link.hash) return;
    if (link.matches(".btn, .section-nav a, .app-nav-link, .app-tabbar a, .more-row")) mark(link);
    loading();
  });

  // Coming back with the browser's Back button shows the page as it was left.
  window.addEventListener("pageshow", reset);
})();
