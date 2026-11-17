// <poster-reel>: Overview's Recently added. Each reel of posters drifts
// slowly from right to left, round and round, and has an ordinary scroll bar.
// The drift is the reel scrolling itself, so the bar, a swipe or the mouse
// wheel all work and the drift carries on from wherever they leave it. The
// list is repeated once so the loop has no seam; the copy is hidden from
// screen readers and the keyboard. Pointing at it, touching it or focusing it
// pauses it, and with reduced motion it doesn't move. A reel sets itself up
// when it's put on the page (a redraw included), and the one animation loop
// runs only while a reel is drifting.
(function () {
  var SPEED = 22; // pixels a second: slow enough to read titles as they pass
  var RESUME = 2500; // ms after the last hand-scroll before drifting again
  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  var drifting = new Set();
  var running = false, last = null;

  function missing(img) {
    img.closest(".reel-art").classList.add("no-art");
  }

  function watch(img) {
    if (img.complete && img.naturalWidth === 0) missing(img);
    else img.addEventListener("error", function () { missing(img); });
  }

  function frame(now) {
    if (!drifting.size) { running = false; last = null; return; }
    var seconds = last === null ? 0 : Math.min((now - last) / 1000, 0.1);
    last = now;
    drifting.forEach(function (reel) { reel.step(seconds); });
    window.requestAnimationFrame(frame);
  }

  class PosterReel extends HTMLElement {
    connectedCallback() {
      if (!this.ready) this.setUp();
      if (this.looping) {
        drifting.add(this);
        if (!running) { running = true; window.requestAnimationFrame(frame); }
      }
    }

    disconnectedCallback() {
      drifting.delete(this);
    }

    setUp() {
      var track = this.querySelector(".reel-track");
      if (!track) return;
      this.ready = true;
      this.track = track;
      track.querySelectorAll("img").forEach(watch);
      // Too few to fill the width: they sit still rather than loop with a gap.
      if (track.scrollWidth <= this.clientWidth || still.matches) return;
      Array.prototype.slice.call(track.children).forEach(function (item) {
        var copy = item.cloneNode(true);
        copy.setAttribute("aria-hidden", "true");
        copy.querySelectorAll("a").forEach(function (a) { a.tabIndex = -1; });
        copy.querySelectorAll("img").forEach(watch);
        track.appendChild(copy);
      });
      this.looping = true;
      this.at = this.set = this.held = 0;
      this.over = false;
      var reel = this;
      function hand() { reel.held = Date.now(); }
      this.addEventListener("pointerenter", function (e) { if (e.pointerType === "mouse") reel.over = true; });
      this.addEventListener("pointerleave", function () { reel.over = false; });
      this.addEventListener("pointerdown", hand);
      this.addEventListener("wheel", hand, { passive: true });
      this.addEventListener("touchstart", hand, { passive: true });
      this.addEventListener("keydown", hand);
      this.addEventListener("scroll", function () {
        // A scroll we didn't make (the bar, a swipe, the keyboard): follow it.
        if (Math.abs(reel.scrollLeft - reel.set) > 1) {
          reel.at = reel.scrollLeft;
          hand();
        }
      }, { passive: true });
    }

    step(seconds) {
      var half = this.track.scrollWidth / 2;
      var paused = this.over || this.contains(document.activeElement) ||
        Date.now() - this.held < RESUME;
      if (!paused) this.at += SPEED * seconds;
      // Past the first set, the copy looks the same: step back a set.
      if (this.at >= half) this.at -= half;
      if (!paused || Math.abs(this.scrollLeft - this.at) > 1) {
        this.scrollLeft = this.at;
        this.set = this.scrollLeft;
      }
    }
  }

  if (!customElements.get("poster-reel")) customElements.define("poster-reel", PosterReel);
})();
