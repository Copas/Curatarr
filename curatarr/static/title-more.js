// <title-more>: Browse titles keeps going as you scroll. When the end of the
// list comes near, the next page's titles are fetched and added to the
// grid, and this is replaced by the next page's own (none after the last).
// Its Previous and Next links stay for when script doesn't run; with it,
// they give way to a quiet "Loading more…". It starts itself wherever it's
// put, so the list redrawn by the search box keeps scrolling on.
(function () {
  var near = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) entry.target.load();
    });
  }, { rootMargin: "0px 0px 1200px 0px" }) : null;

  class TitleMore extends HTMLElement {
    connectedCallback() {
      if (!near || !this.dataset.next) return;
      var links = this.querySelector("nav");
      if (links) links.hidden = true;
      near.observe(this);
    }

    disconnectedCallback() {
      if (near) near.unobserve(this);
    }

    async load() {
      if (this.busy) return;
      this.busy = true;
      var note = this.querySelector(".more-loading");
      if (note) note.hidden = false;
      try {
        var response = await fetch(this.dataset.next, { headers: { "X-Requested-With": "fetch" } });
        if (!response.ok) throw new Error(response.status);
        var page = window.curatarrSwap.page(await response.text());
        var grid = this.closest("#title-results") && this.closest("#title-results").querySelector(".poster-grid");
        var fresh = page.querySelector("#title-results .poster-grid");
        if (!grid || !fresh || !this.isConnected) return;
        Array.prototype.slice.call(fresh.children).forEach(function (tile) {
          grid.appendChild(document.adoptNode(tile));
        });
        var next = page.querySelector("#title-results title-more");
        if (next) this.replaceWith(document.adoptNode(next));
        else this.remove();
      } catch (_error) {
        // Offline for a moment: show the links again, and try once more
        // when this comes near the screen again.
        if (note) note.hidden = true;
        var links = this.querySelector("nav");
        if (links) links.hidden = false;
        this.busy = false;
        near.unobserve(this);
        near.observe(this);
      }
    }
  }

  if (!customElements.get("title-more")) customElements.define("title-more", TitleMore);
})();
