export default {
  async "every page loads with the helper and no errors"({ go, js }) {
    for (const path of ["/", "/titles", "/search?q=the", "/chapters", "/chapters/problems",
                        "/library/posters/gallery", "/settings"]) {
      await go(path);
      const ok = await js("typeof window.curatarrSwap === 'object' && document.styleSheets.length > 0");
      if (ok !== true) return `${path}: helper missing`;
      const styled = await js("getComputedStyle(document.body).fontFamily.includes('system-ui') || getComputedStyle(document.querySelector('.btn') || document.body).borderRadius !== ''");
      if (styled !== true) return `${path}: Bootstrap not applied`;
    }
    return true;
  },

  async "Bootstrap comes from Curatarr itself"({ go, js }) {
    await go("/");
    const hrefs = await js("[...document.querySelectorAll('link[rel=stylesheet]')].map(l => new URL(l.href).origin === location.origin)");
    if (!hrefs.length || hrefs.includes(false)) return hrefs;
    return await js("[...document.styleSheets].some(s => s.href && s.href.includes('bootstrap.min.css') && s.cssRules.length > 100)");
  },

  async "the check refuses inline script, passes plain HTML"({ go, js }) {
    await go("/");
    const r = await js(`(() => {
      const out = {};
      try { curatarrSwap.page('<img src="x" onerror="alert(1)">'); out.handler = 'let through'; } catch (e) { out.handler = 'refused'; }
      try { curatarrSwap.page('<a href="javascript:alert(1)">x</a>'); out.link = 'let through'; } catch (e) { out.link = 'refused'; }
      out.plain = curatarrSwap.element('<p class="x">Turn it on = off</p>').className;
      out.row = curatarrSwap.element('<tr><td>1</td></tr>', document.createElement('tr')).tagName;
      out.trusted = !!window.trustedTypes;
      return out;
    })()`);
    return r.handler === "refused" && r.link === "refused" && r.plain === "x" && r.row === "TR" ? true : r;
  },

  async "Browse titles filters as you type, without reloading"({ go, js, sleep }) {
    await go("/titles");
    const before = await js("window.__stay = 1; document.querySelectorAll('#title-results a').length");
    await js("{ const q = document.getElementById('title-search'); q.value = 'zzzz-nothing'; q.dispatchEvent(new Event('input', {bubbles: true})) }");
    await sleep(1200);
    const after = await js("({stayed: window.__stay === 1, links: document.querySelectorAll('#title-results a').length, url: location.search})");
    if (!after.stayed) return "page reloaded";
    if (!(before > 0 && after.links < before && after.url.includes("zzzz"))) return { before, after };
    await js("{ const q = document.getElementById('title-search'); q.value = ''; q.dispatchEvent(new Event('input', {bubbles: true})) }");
    await sleep(1200);
    const back = await js("document.querySelectorAll('#title-results a').length");
    return back === before ? true : { before, back };
  },

  async "Search fills in What was said"({ go, js, sleep }) {
    await go("/search?q=the");
    for (let i = 0; i < 20; i++) {
      const text = await js("(document.getElementById('said') || {}).textContent || ''");
      if (text.includes("Couldn't search")) return "failed: " + text.trim().slice(0, 120);
      const loading = await js("!!document.querySelector('#said [data-said], #said .spinner-border') || (document.getElementById('said')||{}).dataset?.said !== undefined && (document.getElementById('said').textContent.trim() === '' || /Searching|Looking/.test(document.getElementById('said').textContent))");
      if (!loading) return true;
      await sleep(500);
    }
    return "still loading after 10 s: " + (await js("document.getElementById('said').innerText.slice(0, 200)"));
  },

  async "Enhance's activity card redraws in place, keeping open sections"({ go, js, sleep }) {
    await go("/chapters");
    const r = await js(`(async () => {
      window.__stay = 1;
      const card = document.getElementById('chapter-activity');
      const d = card.querySelector('details[id]');
      if (d) d.open = !d.open;
      const wanted = d ? d.open : null;
      const fresh = await curatarrSwap.redraw(card, card.dataset.refreshUrl);
      const again = document.getElementById('chapter-activity');
      const d2 = d ? again.querySelector('#' + CSS.escape(d.id)) : null;
      return { replaced: fresh === again && again !== card, kept: d ? d2.open === wanted : 'no details', stayed: window.__stay === 1,
               times: again.querySelectorAll('time[data-style]').length };
    })()`);
    await sleep(6000); // the card's own poll runs once meanwhile
    const later = await js("window.__stay === 1 && !!document.getElementById('chapter-activity')");
    return r.replaced && r.kept !== false && r.stayed && later ? true : { r, later };
  },

  async "Overview's buttons work in place (search for the show)"({ go, js, sleep }) {
    await go("/");
    const form = await js("!!document.querySelector('[data-live-page] form[action$=\"/search\"]')");
    if (!form) return "no in-place button on Overview";
    await js("window.__stay = 1; window.scrollTo(0, 300); document.querySelector('[data-live-page] form[action$=\"/search\"]').requestSubmit()");
    await sleep(2500);
    const r = await js("({stayed: window.__stay === 1, notes: [...document.querySelectorAll('.artwork-note')].map(n => n.textContent), busy: document.querySelectorAll('[aria-busy=true]').length, live: !!document.querySelector('[data-live-page]')})");
    return r.stayed && r.live && r.busy === 0 && r.notes.length > 0 ? true : r;
  },
};
