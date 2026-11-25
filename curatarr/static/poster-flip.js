// <poster-flip>: a film whose poster in Jellyfin is a made one, with its
// original kept, alternates the two, fading from one to the other at a
// random moment every 3 to 6 seconds, so a page of them doesn't change in
// step. It only runs while it's on screen, holds still while pointed at or
// focused, and with reduced motion it shows the made poster and stays put.
// The original is fetched only once the poster is first on screen.
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
      if (!this.img || !this.dataset.other || still.matches) return;
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
        var made = this.img.src;
        var other = new Image();
        other.src = this.dataset.other;
        this.sources = [made, other.src];
        this.showing = 0;
        var self = this;
        // A film whose original can't be had stays as it is.
        other.addEventListener("error", function () { self.broken = true; });
      }
      this.wait();
    }

    wait() {
      var self = this;
      this.timer = setTimeout(function () { self.change(); }, MIN + Math.random() * (MAX - MIN));
    }

    change() {
      if (this.broken || !this.visible) return;
      if (document.hidden || this.img.matches(":hover") || this.contains(document.activeElement)) {
        this.wait();
        return;
      }
      var img = this.img, self = this;
      this.showing = 1 - this.showing;
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
