// Parts of a page that start themselves (custom elements): wherever they
// appear, at load or in a part redrawn in place, with nothing to call.
export default {
  async "times show in the viewer's zone, at load and after a redraw"({ go, js }) {
    await go("/history");
    const atLoad = await js(`[...document.querySelectorAll('local-time time')].map(t => t.textContent)`);
    if (!atLoad.length) return "no times on History";
    if (atLoad.some((t) => t.endsWith("UTC"))) return { atLoad };
    // a part redrawn in place, the way swap.js puts new HTML in the page
    const after = await js(`(() => {
      const table = document.querySelector('main table');
      const fresh = curatarrSwap.element(table.outerHTML, table);
      table.replaceWith(fresh);
      return [...fresh.querySelectorAll('local-time time')].map(t => [t.textContent, !!t.title]);
    })()`);
    if (!after.length || after.some(([text, titled]) => text.endsWith("UTC") || !titled)) return { after };
    // one put on the page later, a countdown
    const later = await js(`(() => {
      const soon = new Date(Date.now() + 5 * 60000).toISOString();
      const p = document.createElement('p');
      p.append('Resets ', curatarrSwap.element('<local-time><time datetime="' + soon + '" data-style="in">x UTC</time></local-time>'));
      document.querySelector('main').append(p);
      return p.querySelector('time').textContent;
    })()`);
    return /^in [45] min$/.test(later) ? true : { later };
  },
  async "a made poster and its original take turns, put on the page by a redraw"({ go, js }) {
    await go("/");
    const r = await js(`(async () => {
      const flip = curatarrSwap.element('<poster-flip data-others="/static/wordmark.svg"><img class="poster" alt="" src="/static/logo.svg"></poster-flip>');
      document.querySelector('main').prepend(flip);
      const img = flip.querySelector('img'), seen = new Set([img.src]);
      for (let i = 0; i < 16 && seen.size < 2; i++) {
        await new Promise(r => setTimeout(r, 500));
        seen.add(img.src);
      }
      await new Promise(r => setTimeout(r, 800));
      return { defined: flip.matches(':defined'), seen: [...seen], faded: img.classList.contains('flip-out'),
               shown: getComputedStyle(img).display };
    })()`);
    return r.defined && r.seen.length === 2 && !r.faded && r.shown !== "none" ? true : r;
  },
  async "a reel of posters sets itself up once, even when moved"({ go, js, sleep }) {
    await go("/");
    const r = await js(`(async () => {
      const items = Array.from({ length: 30 }, (_, i) => '<li class="reel-item"><a href="/titles"><span class="reel-art"><img alt="" src="/static/logo.svg"></span>Title ' + i + '</a></li>').join('');
      const reel = curatarrSwap.element('<poster-reel class="reel card mb-3" tabindex="0" aria-label="Test"><ul class="reel-track list-unstyled mb-0">' + items + '</ul></poster-reel>');
      document.querySelector('main').prepend(reel);
      const doubled = reel.querySelectorAll('.reel-track > li').length;
      reel.remove(); document.querySelector('main').prepend(reel);
      const start = reel.scrollLeft;
      await new Promise(r => setTimeout(r, 1500));
      return { defined: reel.matches(':defined'), doubled, again: reel.querySelectorAll('.reel-track > li').length,
               copiesHidden: reel.querySelectorAll('[aria-hidden=true]').length, moved: reel.scrollLeft - start };
    })()`);
    return r.defined && r.doubled === 60 && r.again === 60 && r.copiesHidden === 30 && r.moved > 5 ? true : r;
  },
  async "a running sync line put on the page by a redraw updates itself"({ go, js, sleep }) {
    await go("/");
    // What Overview's redraw after Sync now does: a running line replaces the old one.
    await js(`(() => {
      window.__asks = 0;
      const real = window.fetch;
      window.fetch = (...a) => { if (String(a[0]).includes('/overview/reconcile-status')) window.__asks++; return real(...a); };
      const old = document.querySelector('reconcile-status');
      const running = old.outerHTML.replace('<reconcile-status ', '<reconcile-status data-active="1" ').replace(/status-[a-z]+"/, 'status-running"');
      old.replaceWith(curatarrSwap.element(running));
    })()`);
    await sleep(11500);
    const r = await js(`(() => { const l = document.querySelector('reconcile-status');
      return { asks: window.__asks, active: l.dataset.active, cls: l.className, link: (l.querySelector('a') || {}).textContent }; })()`);
    // The demo has no sync running: one check, then it settles and offers the new numbers.
    return r.asks === 1 && !r.active && !/status-running/.test(r.cls) && r.link === "Show the new numbers" ? true : r;
  },
};

