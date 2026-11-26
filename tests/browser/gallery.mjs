// Posters → Gallery (static/gallery-show.js): arrow keys between pictures by
// where they are on screen, and the full-screen viewer comparing each with
// its original: side by side, as a wipe, and saying when none is found.
async function key(send, k) {
  const vk = { ArrowRight: 39, ArrowLeft: 37, Escape: 27, c: 67, "?": 191 }[k];
  const text = k.length === 1 ? k : undefined;
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code: k, windowsVirtualKeyCode: vk, text });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: k, windowsVirtualKeyCode: vk });
}
async function clickAt(send, js, selector) {
  const box = await js(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  }
}
const state = `(() => {
  const d = document.querySelector('.gallery-viewer'), stage = d.querySelector('.gallery-stage');
  const original = d.querySelector('.gallery-pane-original'), made = d.querySelector('.gallery-pane-made');
  return { open: d.open, mode: stage.dataset.mode, original: original.checkVisibility(), made: made.checkVisibility(),
    clip: getComputedStyle(made).clipPath, wipe: !d.querySelector('.gallery-wipe').hidden,
    compare: !d.querySelector('.gallery-modes button').disabled, caption: d.querySelector('.gallery-viewer-caption').textContent,
    help: !d.querySelector('.gallery-viewer-keys').hidden };
})()`;

const check = {
  async "the gallery compares each picture with its original, by keyboard"({ go, js, send, sleep }) {
    await send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 800, deviceScaleFactor: 1, mobile: false });
    await go("/library/posters/gallery");
    await js(`(() => {
      try { localStorage.removeItem('curatarr.gallery.compare'); } catch {}
      const strip = curatarrSwap.element(\`<div class="gallery-strip gallery-strip-primary" data-spatial>
        <button type="button" class="picture-wall-open" id="g1" data-nav data-full="/static/logo.svg" data-original="/static/wordmark.svg" data-alt="Heat, its poster" data-who="Made for Sam">one</button>
        <button type="button" class="picture-wall-open" id="g2" data-nav data-full="/static/wordmark.svg" data-alt="Heat, its original poster">two</button>
        <button type="button" class="picture-wall-open" id="g3" data-nav data-full="/static/logo.svg?x" data-original="/static/no-such-picture.svg" data-alt="Ronin, its poster">three</button>
      </div>\`);
      document.querySelector('main').prepend(strip);
      document.querySelector('main').append(curatarrSwap.element('<details data-gallery-keys><div class="gallery-keys-groups"><p>Keys</p></div></details>'));
      document.getElementById('g1').focus();
    })()`);
    await key(send, "ArrowRight");
    const moved = await js("document.activeElement.id");
    if (moved !== "g2") return { moved };
    await clickAt(send, js, "#g1");
    await sleep(300);
    const single = await js(state);
    if (!single.open || single.mode !== "single" || single.original || !single.made || !single.compare) return { single };
    await key(send, "c");
    const side = await js(state);
    if (side.mode !== "side" || !side.original || !side.made || side.wipe) return { side };
    await key(send, "c");
    const wipe = await js(state);
    if (wipe.mode !== "wipe" || !wipe.original || !wipe.wipe || !/inset/.test(wipe.clip)) return { wipe };
    await key(send, "ArrowRight"); // the original itself: nothing to compare it with
    const plain = await js(state);
    if (plain.mode !== "single" || plain.compare || plain.original) return { plain };
    await key(send, "ArrowRight"); // an original that can't be found
    await sleep(600);
    const lost = await js(state);
    if (lost.mode !== "single" || lost.compare || !/no original found/.test(lost.caption)) return { lost };
    await key(send, "?");
    const help = await js(state);
    if (!help.help) return { help };
    await key(send, "Escape");
    await sleep(300);
    const closed = await js(state);
    const kept = await js("(() => { try { return localStorage.getItem('curatarr.gallery.compare'); } catch { return 'wipe'; } })()");
    await js("(() => { try { localStorage.removeItem('curatarr.gallery.compare'); } catch {} })()");
    return !closed.open && kept === "wipe" ? true : { closed, kept };
  },
};
// A title's page: the next title's page is fetched ahead and put in place
// when you move on (here, made-up pages answer fetch).
check["flipping to the next title puts its page in place, loaded ahead"] = async ({ go, js, send, sleep }) => {
  await go("/library/posters/gallery");
  await js(`(() => {
    const article = (n) => '<article class="gallery-title" data-before="/library/posters/gallery/t' + (n - 1) +
      '" data-after="/library/posters/gallery/t' + (n + 1) + '" data-back="/library/posters/gallery"><h1 tabindex="-1">Title ' + n +
      '</h1><div class="gallery-strip gallery-strip-primary"><button type="button" data-full="/static/logo.svg">p</button></div></article>';
    window.fetched = [];
    window.fetch = async (url) => {
      window.fetched.push(String(url));
      const n = Number(String(url).match(/t(\\d+)$/)[1]);
      return new Response('<!doctype html><title>Title ' + n + '</title><main>' + article(n) + '</main>', { status: 200 });
    };
    document.querySelector('main').prepend(curatarrSwap.element(article(1)));
  })()`);
  await key(send, "ArrowRight");
  await sleep(600);
  const r = await js(`({ heading: document.querySelector('.gallery-title h1').textContent, title: document.title,
    path: location.pathname, pages: document.querySelectorAll('.gallery-title').length, fetched: window.fetched,
    focused: document.activeElement.tagName })`);
  const ahead = r.fetched.includes("/library/posters/gallery/t3");  // the one after, fetched ahead
  return r.heading === "Title 2" && r.title === "Title 2" && r.path === "/library/posters/gallery/t2" &&
    r.pages === 1 && r.focused === "H1" && ahead ? true : r;
};
// The third picture's original is missing on purpose.
check["the gallery compares each picture with its original, by keyboard"].expect = /no-such-picture/;
export default check;
