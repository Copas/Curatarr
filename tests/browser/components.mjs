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
};
