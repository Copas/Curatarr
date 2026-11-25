// <poster-flip>: a title with other posters (each account's own, or the
// original a custom one replaced) shows them in turn with Jellyfin's,
// fading from one to the next at a random moment every 3 to 6 seconds, so a
// page of them doesn't change in step. It only runs while it's on screen,
// holds still while pointed at or focused, and with reduced motion it shows
// Jellyfin's poster and stays put. The others are fetched only once the
// poster is first on screen, and one that can't be had is left out.
(function () {
  var MIN = 3000, MAX = 6000; // ms between changes
  var FADE = 400; // ms: matches the opacity transition in style.css
  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  var seen = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var flip = entry.target.closest("poster-flip");
      if (flip) flip.onScreen(entry.isIntersecting);
    });
  }) : null;

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
        var self = this;
        this.sources = [this.img.src];
        this.showing = 0;
        this.dataset.others.split(" ").forEach(function (url) {
          var other = new Image();
          other.addEventListener("load", function () { self.sources.push(other.src); });
          other.src = url;
        });
      }
      this.wait();
    }

    wait() {
      var self = this;
      this.timer = setTimeout(function () { self.change(); }, MIN + Math.random() * (MAX - MIN));
    }

    change() {
      if (!this.visible) return;
      if (this.sources.length < 2 || document.hidden || this.img.matches(":hover") || this.contains(document.activeElement)) {
        this.wait();
        return;
      }
      var img = this.img, self = this;
      this.showing = (this.showing + 1) % this.sources.length;
      var next = this.sources[this.showing];
      img.classList.add("flip-out");
      setTimeout(function () {
        img.src = next;
        img.decode().catch(function () {}).then(function () {
          img.classList.remove("flip-out");
          if (self.visible) self.wait();
        });
      }, FADE);
    }
  }

  if (!customElements.get("poster-flip")) customElements.define("poster-flip", PosterFlip);
})();
