// Overview's Recently added: each reel of posters drifts slowly from right to
// left, round and round, and has an ordinary scroll bar. The drift is the
// reel scrolling itself, so the bar, a swipe or the mouse wheel all work and
// the drift carries on from wherever they leave it. The list is repeated
// once so the loop has no seam; the copy is hidden from screen readers and
// the keyboard. Pointing at it, touching it or focusing it pauses it, and
// with reduced motion it doesn't move.
(function () {
  var SPEED = 22; // pixels a second: slow enough to read titles as they pass
  var RESUME = 2500; // ms after the last hand-scroll before drifting again
  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  var reels = [];

  function missing(img) {
    img.closest(".reel-art").classList.add("no-art");
  }

  function watch(img) {
    if (img.complete && img.naturalWidth === 0) missing(img);
    else img.addEventListener("error", function () { missing(img); });
  }

  function setUp(reel) {
    var track = reel.querySelector(".reel-track");
    if (!track || track.dataset.ready) return;
    track.dataset.ready = "1";
    track.querySelectorAll("img").forEach(watch);
    // Too few to fill the width: they sit still rather than loop with a gap.
    if (track.scrollWidth <= reel.clientWidth || still.matches) return;
    Array.prototype.slice.call(track.children).forEach(function (item) {
      var copy = item.cloneNode(true);
      copy.setAttribute("aria-hidden", "true");
      copy.querySelectorAll("a").forEach(function (a) { a.tabIndex = -1; });
      copy.querySelectorAll("img").forEach(watch);
      track.appendChild(copy);
    });
    var state = { reel: reel, track: track, at: 0, set: 0, held: 0, over: false };
    function hand() { state.held = Date.now(); }
    reel.addEventListener("pointerenter", function (e) { if (e.pointerType === "mouse") state.over = true; });
    reel.addEventListener("pointerleave", function () { state.over = false; });
    reel.addEventListener("pointerdown", hand);
    reel.addEventListener("wheel", hand, { passive: true });
    reel.addEventListener("touchstart", hand, { passive: true });
    reel.addEventListener("keydown", hand);
    reel.addEventListener("scroll", function () {
      // A scroll we didn't make (the bar, a swipe, the keyboard): follow it.
      if (Math.abs(reel.scrollLeft - state.set) > 1) {
        state.at = reel.scrollLeft;
        hand();
      }
    }, { passive: true });
    reels.push(state);
  }

  var last = null;
  function frame(now) {
    var seconds = last === null ? 0 : Math.min((now - last) / 1000, 0.1);
    last = now;
    reels = reels.filter(function (s) { return s.reel.isConnected; });
    reels.forEach(function (s) {
      var half = s.track.scrollWidth / 2;
      var paused = s.over || s.reel.contains(document.activeElement) ||
        Date.now() - s.held < RESUME;
      if (!paused) s.at += SPEED * seconds;
      // Past the first set, the copy looks the same: step back a set.
      if (s.at >= half) s.at -= half;
      if (!paused || Math.abs(s.reel.scrollLeft - s.at) > 1) {
        s.reel.scrollLeft = s.at;
        s.set = s.reel.scrollLeft;
      }
    });
    window.requestAnimationFrame(frame);
  }

  function start(root) {
    (root || document).querySelectorAll("[data-reel]").forEach(setUp);
  }

  window.curatarrReel = start;
  start();
  window.requestAnimationFrame(frame);
})();
