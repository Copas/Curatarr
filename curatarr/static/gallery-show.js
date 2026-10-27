// The Gallery's full-screen viewer: every picture on the page in order, with
// the arrow keys, swiping or the buttons to move, and a slideshow. Esc, or a
// click on the picture's surroundings, closes it.
(() => {
  const pictures = [...document.querySelectorAll("[data-full]")];
  if (!pictures.length) return;
  const viewer = document.createElement("dialog");
  viewer.className = "lightbox gallery-viewer";
  viewer.setAttribute("aria-label", "Pictures, full screen");
  viewer.innerHTML = `
    <img alt="">
    <p class="gallery-viewer-caption"></p>
    <button type="button" class="gallery-viewer-nav gallery-viewer-prev" aria-label="Previous picture">‹</button>
    <button type="button" class="gallery-viewer-nav gallery-viewer-next" aria-label="Next picture">›</button>
    <div class="gallery-viewer-bar">
      <button type="button" class="btn btn-sm btn-outline-light" data-play>Slideshow</button>
      <button type="button" class="btn btn-sm btn-outline-light" data-close>Close</button>
    </div>`;
  document.body.append(viewer);
  const big = viewer.querySelector("img");
  const caption = viewer.querySelector(".gallery-viewer-caption");
  const playButton = viewer.querySelector("[data-play]");
  let index = 0;
  let timer = null;
  const SLIDE_MS = 6000;

  function show(at) {
    index = (at + pictures.length) % pictures.length;
    const picture = pictures[index];
    big.src = picture.dataset.full;
    big.alt = picture.dataset.alt || "";
    caption.textContent = `${picture.dataset.alt || ""} · ${index + 1} of ${pictures.length}`;
    // Load the next one ahead, so the slideshow doesn't wait on it.
    new Image().src = pictures[(index + 1) % pictures.length].dataset.full;
  }
  function stop() {
    clearInterval(timer);
    timer = null;
    playButton.textContent = "Slideshow";
  }
  function play() {
    stop();
    timer = setInterval(() => show(index + 1), SLIDE_MS);
    playButton.textContent = "Pause";
  }
  function open(at, slideshow) {
    show(at);
    if (!viewer.open) viewer.showModal();
    if (slideshow) play();
  }

  document.addEventListener("click", (event) => {
    const picture = event.target.closest("[data-full]");
    if (picture && !viewer.contains(picture)) {
      event.preventDefault();
      open(pictures.indexOf(picture), false);
    }
    if (event.target.closest("[data-slideshow]")) open(0, true);
  });
  viewer.addEventListener("click", (event) => {
    if (event.target.closest(".gallery-viewer-prev")) { stop(); show(index - 1); }
    else if (event.target.closest(".gallery-viewer-next")) { stop(); show(index + 1); }
    else if (event.target.closest("[data-play]")) { timer ? stop() : play(); }
    else if (event.target.closest("[data-close]") || event.target === viewer) viewer.close();
  });
  viewer.addEventListener("close", () => { stop(); big.removeAttribute("src"); });
  document.addEventListener("keydown", (event) => {
    if (!viewer.open) return;
    if (event.key === "ArrowRight") { stop(); show(index + 1); }
    if (event.key === "ArrowLeft") { stop(); show(index - 1); }
    if (event.key === " ") { event.preventDefault(); timer ? stop() : play(); }
  });
  let touchX = null;
  viewer.addEventListener("touchstart", (event) => { touchX = event.touches[0].clientX; }, { passive: true });
  viewer.addEventListener("touchend", (event) => {
    if (touchX === null) return;
    const moved = event.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(moved) > 40) { stop(); show(index + (moved < 0 ? 1 : -1)); }
  });
})();
