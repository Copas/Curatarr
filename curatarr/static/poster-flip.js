// <poster-flip>: a title with other posters (each account's own, or the
// original a custom one replaced) shows them in turn with Jellyfin's, in
// the order given, turning over like a card at a random moment every 3 to 6
// seconds, so a page of them doesn't change in step. It only runs while
// it's on screen, holds still while pointed at or focused, and with reduced
// motion it shows Jellyfin's poster and stays put. The others are fetched
// once the poster is first on screen; one that can't be had is skipped.
(function () {
  var MIN = 3000, MAX = 6000; // ms between changes
  var HALF = 250; // ms for each half of the turn
  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  var seen = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var flip = entry.target.closest("poster-flip");
      if (flip) flip.onScreen(entry.isIntersecting);
    });
  }) : null;

  function turn(img, from, to, easing) {
    var frames = [
      { transform: "perspective(800px) rotateY(" + from + "deg)" },
      { transform: "perspective(800px) rotateY(" + to + "deg)" },
    ];
    if (!img.animate) return Promise.resolve();
    return img.animate(frames, { duration: HALF, easing: easing, fill: "forwards" }).finished.catch(function () {});
  }

  class PosterFlip extends HTMLElement {
    connectedCallback() {
      this.img = this.querySelector("img");
      if (!this.img || !this.dataset.others || still.matches) return;
      if (seen) seen.observe(this.img);
      else this.onScreen(true);
    }

    disconnectedCallback() {
      if (seen && this.img) seen.unobserve(this.img);
      this.onScreen(false);
    }

    onScreen(visible) {
      clearTimeout(this.timer);
      this.visible = visible;
      if (!visible) return;
      if (!this.sources) {
        // In the order given; each is usable once it has loaded.
        var urls = [this.img.src].concat(this.dataset.others.split(" "));
        this.sources = urls.map(function (url, i) {
          var source = { url: url, ready: i === 0 };
          if (i) {
            var probe = new Image();
            probe.addEventListener("load", function () { source.ready = true; });
            probe.src = url;
            source.url = probe.src;
          }
          return source;
        });
        this.showing = 0;
      }
      this.wait();
    }

    wait() {
      var self = this;
      this.timer = setTimeout(function () { self.change(); }, MIN + Math.random() * (MAX - MIN));
    }

    next() {
      // The next ready picture after the one showing, never the same one.
      for (var step = 1; step < this.sources.length; step++) {
        var at = (this.showing + step) % this.sources.length;
        if (this.sources[at].ready && this.sources[at].url !== this.sources[this.showing].url) return at;
      }
      return -1;
    }

    change() {
      if (!this.visible) return;
      var at = this.next();
      if (at < 0 || document.hidden || this.img.matches(":hover") || this.contains(document.activeElement)) {
        this.wait();
        return;
      }
      var img = this.img, self = this;
      this.showing = at;
      turn(img, 0, 90, "ease-in").then(function () {
        img.src = self.sources[at].url;
        return img.decode().catch(function () {});
      }).then(function () {
        return turn(img, -90, 0, "ease-out");
      }).then(function () {
        img.getAnimations().forEach(function (a) { a.cancel(); });
        if (self.visible) self.wait();
      });
    }
  }

  if (!customElements.get("poster-flip")) customElements.define("poster-flip", PosterFlip);
})();
