// The Gallery: a full-screen viewer for every picture on the page, which
// sets each beside the title's original (side by side, as a wipe, or held
// O for a peek), and the keyboard: arrows between pictures and titles,
// "/" to find a title, "?" for the list of keys.
(() => {
  const SLIDE_MS = 6000;
  const MODES = ["single", "side", "wipe"];
  const MODE_KEY = "curatarr.gallery.compare";
  const missing = new Set(); // originals that couldn't be found

  // An original that can't be found says so in its place.
  document.addEventListener("error", (event) => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement)) return;
    const holder = img.closest(".compare-original");
    if (!holder) return;
    missing.add(holder.dataset.full);
    holder.classList.add("compare-missing");
    holder.removeAttribute("data-full");
    holder.disabled = true;
    img.remove();
  }, true);

  const typing = (target) =>
    target instanceof Element && target.matches(":read-write, select, input[type='range'], [role='slider']");
  const shortcuts = () => document.querySelector("[data-gallery-keys]");

  // --- Find a title ---------------------------------------------------------
  const find = document.querySelector("[data-gallery-find]");
  const titles = () => [...document.querySelectorAll(".gallery-item")];
  function filter() {
    const items = titles();
    const none = document.querySelector(".gallery-none");
    const found = document.querySelector(".gallery-found");
    const words = find.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const item of items) {
      const title = item.dataset.title || "";
      const hit = words.every((word) => title.includes(word));
      item.hidden = !hit;
      shown += hit;
    }
    if (none) none.hidden = shown > 0;
    if (found) found.textContent = words.length ? `${shown} of ${items.length}` : "";
  }
  if (find) {
    find.addEventListener("input", filter);
    find.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && find.value) {
        event.preventDefault();
        find.value = "";
        filter();
      } else if (event.key === "ArrowDown" || event.key === "Enter") {
        const first = titles().find((item) => !item.hidden)?.querySelector("[data-nav]");
        if (first) { event.preventDefault(); first.focus(); }
      }
    });
  }

  // --- Arrow keys between pictures (by where they are on screen) -------------
  const DIRECTIONS = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
  function nearest(from, direction, candidates) {
    const at = from.getBoundingClientRect();
    const slack = 2;
    let aligned = null;
    let fallback = null;
    for (const el of candidates) {
      if (el === from) continue;
      const box = el.getBoundingClientRect();
      const horizontal = direction === "left" || direction === "right";
      const gap = {
        right: box.left - at.right, left: at.left - box.right,
        down: box.top - at.bottom, up: at.top - box.bottom,
      }[direction];
      if (gap < -slack) continue;
      const overlap = horizontal
        ? box.top < at.bottom && box.bottom > at.top
        : box.left < at.right && box.right > at.left;
      const offset = horizontal
        ? Math.abs((box.top + box.bottom) / 2 - (at.top + at.bottom) / 2)
        : Math.abs((box.left + box.right) / 2 - (at.left + at.right) / 2);
      if (overlap) {
        if (!aligned || gap < aligned.gap || (gap === aligned.gap && offset < aligned.offset)) {
          aligned = { el, gap, offset };
        }
      } else {
        const score = gap + 3 * offset;
        if (!fallback || score < fallback.score) fallback = { el, score };
      }
    }
    return (aligned || fallback)?.el || null;
  }
  document.addEventListener("keydown", (event) => {
    const direction = DIRECTIONS[event.key];
    if (!direction || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const from = event.composedPath()[0];
    if (!(from instanceof Element) || !from.matches("[data-nav]")) return;
    const area = from.closest("[data-spatial]");
    if (!area) return;
    const candidates = [...area.querySelectorAll("[data-nav]")].filter((el) => el.checkVisibility());
    const to = nearest(from, direction, candidates);
    if (!to) return; // nothing that way: let the page scroll
    event.preventDefault();
    to.focus();
    to.scrollIntoView({ block: "nearest", inline: "nearest" });
  });

  // --- The viewer --------------------------------------------------------------
  const viewer = document.createElement("dialog");
  viewer.className = "lightbox gallery-viewer";
  viewer.setAttribute("aria-label", "Pictures, full screen");
  viewer.append(...window.curatarrSwap.page(`
    <div class="gallery-stage" data-mode="single">
      <figure class="gallery-pane gallery-pane-original"><img alt=""><figcaption>Original</figcaption></figure>
      <figure class="gallery-pane gallery-pane-made"><img alt=""><figcaption></figcaption></figure>
      <span class="gallery-wipe-line" aria-hidden="true"></span>
    </div>
    <label class="gallery-wipe"><span class="visually-hidden">How much of the new picture to show</span><input type="range" min="0" max="100" value="50"></label>
    <p class="gallery-viewer-caption" aria-live="polite"></p>
    <button type="button" class="gallery-viewer-nav gallery-viewer-prev" aria-label="Previous picture">‹</button>
    <button type="button" class="gallery-viewer-nav gallery-viewer-next" aria-label="Next picture">›</button>
    <div class="gallery-viewer-bar">
      <div class="btn-group btn-group-sm gallery-modes" role="group" aria-label="Compare with the original">
        <button type="button" class="btn gallery-viewer-btn" data-mode="single">Just this</button>
        <button type="button" class="btn gallery-viewer-btn" data-mode="side">Side by side</button>
        <button type="button" class="btn gallery-viewer-btn" data-mode="wipe">Wipe</button>
      </div>
      <button type="button" class="btn btn-sm gallery-viewer-btn" data-peek>Hold for original</button>
      <button type="button" class="btn btn-sm gallery-viewer-btn" data-play>Slideshow</button>
      <button type="button" class="btn btn-sm gallery-viewer-btn" data-help aria-expanded="false">Keys</button>
      <button type="button" class="btn btn-sm btn-light" data-close>Close</button>
    </div>
    <div class="gallery-viewer-keys" hidden></div>`).body.childNodes);
  document.body.append(viewer);
  const stage = viewer.querySelector(".gallery-stage");
  const [originalImg, madeImg] = stage.querySelectorAll("img");
  const who = viewer.querySelector(".gallery-pane-made figcaption");
  const caption = viewer.querySelector(".gallery-viewer-caption");
  const wipe = viewer.querySelector(".gallery-wipe input");
  const playButton = viewer.querySelector("[data-play]");
  const peekButton = viewer.querySelector("[data-peek]");
  const helpButton = viewer.querySelector("[data-help]");
  const help = viewer.querySelector(".gallery-viewer-keys");

  let pictures = [];
  let index = 0;
  let timer = null;
  let mode = "single";
  try { mode = MODES.includes(localStorage.getItem(MODE_KEY)) ? localStorage.getItem(MODE_KEY) : "single"; } catch { /* no storage */ }

  const current = () => pictures[index];
  const originalOf = (picture) => {
    const url = picture?.dataset.original;
    return url && !missing.has(url) ? url : null;
  };

  function render() {
    const picture = current();
    const original = originalOf(picture);
    const shown = original ? mode : "single";
    stage.dataset.mode = shown;
    stage.classList.remove("peeking");
    for (const button of viewer.querySelectorAll(".gallery-modes button")) {
      button.disabled = !original;
      button.setAttribute("aria-pressed", String(button.dataset.mode === shown));
    }
    peekButton.disabled = !original || shown !== "single";
    viewer.querySelector(".gallery-wipe").hidden = shown !== "wipe";
    who.textContent = picture.dataset.who || "New";
    const notes = [picture.dataset.alt || ""];
    if (picture.dataset.original && !original) notes.push("no original found to compare");
    notes.push(`${index + 1} of ${pictures.length}`);
    caption.textContent = notes.filter(Boolean).join(" · ");
  }
  function show(at) {
    index = (at + pictures.length) % pictures.length;
    const picture = current();
    madeImg.src = picture.dataset.full;
    madeImg.alt = picture.dataset.alt || "";
    const original = originalOf(picture);
    if (original) {
      originalImg.src = original;
      originalImg.alt = (picture.dataset.alt || "").replace(/, its .*/, ", its original");
    } else {
      originalImg.removeAttribute("src");
    }
    render();
    // Load the next one ahead, so the slideshow doesn't wait on it.
    const next = pictures[(index + 1) % pictures.length];
    new Image().src = next.dataset.full;
    if (originalOf(next) && mode !== "single") new Image().src = originalOf(next);
  }
  madeImg.addEventListener("load", () => {
    if (madeImg.naturalHeight) stage.style.setProperty("--ratio", madeImg.naturalWidth / madeImg.naturalHeight);
  });
  originalImg.addEventListener("error", () => {
    const url = current()?.dataset.original;
    if (url && originalImg.getAttribute("src")) { missing.add(url); render(); }
  });
  function setMode(next) {
    mode = next;
    try { localStorage.setItem(MODE_KEY, mode); } catch { /* no storage */ }
    render();
  }
  function setWipe(percent) {
    const value = Math.max(0, Math.min(100, percent));
    wipe.value = String(Math.round(value));
    stage.style.setProperty("--wipe", `${value}%`);
  }
  setWipe(50);
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
  function open(picture, slideshow) {
    pictures = [...document.querySelectorAll("[data-full]")].filter(
      (el) => !viewer.contains(el) && !el.closest(".gallery-item[hidden]"),
    );
    if (!pictures.length) return;
    helpButton.hidden = !shortcuts();
    show(Math.max(0, pictures.indexOf(picture)));
    if (!viewer.open) viewer.showModal();
    if (slideshow) play();
  }
  function peek(on) {
    if (stage.dataset.mode === "single" && originalOf(current())) stage.classList.toggle("peeking", on);
  }
  function toggleHelp(force) {
    // The page's own list of keys, copied in when first wanted.
    if (!help.childElementCount && shortcuts()) {
      help.append(shortcuts().querySelector(".gallery-keys-groups").cloneNode(true));
    }
    const on = (force ?? help.hidden) && help.childElementCount > 0;
    help.hidden = !on;
    helpButton.setAttribute("aria-expanded", String(on));
  }

  document.addEventListener("click", (event) => {
    const picture = event.target.closest("[data-full]");
    if (picture && !viewer.contains(picture)) {
      event.preventDefault();
      open(picture, false);
    }
    if (event.target.closest("[data-slideshow]")) open(null, true);
  });
  viewer.addEventListener("click", (event) => {
    const target = event.target;
    if (target.closest(".gallery-viewer-prev")) { stop(); show(index - 1); }
    else if (target.closest(".gallery-viewer-next")) { stop(); show(index + 1); }
    else if (target.closest(".gallery-modes button")) setMode(target.closest("button").dataset.mode);
    else if (target.closest("[data-play]")) { timer ? stop() : play(); }
    else if (target.closest("[data-help]")) toggleHelp();
    else if (target.closest("[data-close]") || target === viewer || target === stage) viewer.close();
  });
  viewer.addEventListener("close", () => {
    stop();
    toggleHelp(false);
    madeImg.removeAttribute("src");
    originalImg.removeAttribute("src");
  });
  peekButton.addEventListener("pointerdown", () => peek(true));
  for (const end of ["pointerup", "pointerleave", "pointercancel"]) peekButton.addEventListener(end, () => peek(false));
  wipe.addEventListener("input", () => setWipe(Number(wipe.value)));
  // In the wipe, the line follows the mouse, or a finger dragging it.
  function follow(event) {
    const box = stage.getBoundingClientRect();
    if (box.width) setWipe(((event.clientX - box.left) / box.width) * 100);
  }
  stage.addEventListener("pointermove", (event) => {
    if (stage.dataset.mode !== "wipe") return;
    if (event.pointerType === "mouse" || stage.hasPointerCapture(event.pointerId)) follow(event);
  });
  stage.addEventListener("pointerdown", (event) => {
    if (stage.dataset.mode !== "wipe") return;
    stage.setPointerCapture(event.pointerId);
    follow(event);
  });

  // --- Keys ------------------------------------------------------------------
  function stepType(by) {
    const links = [...document.querySelectorAll("[data-kind-link]")];
    const at = links.findIndex((link) => link.getAttribute("aria-current") === "page");
    const to = links[at + by];
    if (to) window.location.href = to.href;
    return Boolean(to);
  }
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return; // Alt+Left is Back
    const target = event.composedPath()[0];
    if (typing(target)) return;
    if (event.key === "?") {
      event.preventDefault();
      if (viewer.open) toggleHelp();
      else if (shortcuts()) {
        const keys = shortcuts();
        keys.open = !keys.open;
        if (keys.open) keys.scrollIntoView({ block: "nearest" });
      }
      return;
    }
    if (event.shiftKey) return;
    if (viewer.open) {
      if (event.key === "ArrowRight") { event.preventDefault(); stop(); show(index + 1); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); stop(); show(index - 1); }
      else if (event.key === " ") { event.preventDefault(); timer ? stop() : play(); }
      else if (event.key === "c" || event.key === "C") {
        if (originalOf(current())) setMode(MODES[(MODES.indexOf(mode) + 1) % MODES.length]);
      } else if ((event.key === "o" || event.key === "O") && !event.repeat) peek(true);
      else if (stage.dataset.mode === "wipe" && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        event.preventDefault();
        setWipe(Number(wipe.value) + (event.key === "ArrowUp" ? 5 : -5));
      }
      return;
    }
    if (event.key === "/" && find) { event.preventDefault(); find.focus(); find.select(); return; }
    const page = document.querySelector(".gallery-title");
    if (!page) return;
    const strip = [...page.querySelectorAll(".gallery-strip [data-full]")];
    // A title's own page: the titles before and after it, its other types
    // of picture, and its pictures by number.
    if (event.key === "ArrowRight" && page.dataset.after) window.location.href = page.dataset.after;
    else if (event.key === "ArrowLeft" && page.dataset.before) window.location.href = page.dataset.before;
    else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (stepType(event.key === "ArrowDown" ? 1 : -1)) event.preventDefault();
    } else if (/^[1-9]$/.test(event.key) && strip[Number(event.key) - 1]?.dataset.full) {
      open(strip[Number(event.key) - 1], false);
    } else if (event.key === "Escape" && !shortcuts()?.open) window.location.href = page.dataset.back;
  });
  document.addEventListener("keyup", (event) => {
    if (event.key === "o" || event.key === "O") peek(false);
  });
  // Swiping steps through the pictures, except in the wipe (that drags it).
  let touchX = null;
  viewer.addEventListener("touchstart", (event) => { touchX = event.touches[0].clientX; }, { passive: true });
  viewer.addEventListener("touchend", (event) => {
    if (touchX === null || stage.dataset.mode === "wipe") { touchX = null; return; }
    const moved = event.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(moved) > 40) { stop(); show(index + (moved < 0 ? 1 : -1)); }
  });
})();
