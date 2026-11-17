// Every visible piece of text on the main pages, in both schemes: its colour
// against the background behind it, worst first. WCAG 4.5:1 for normal text,
// 3:1 for large (24px, or 18.66px bold).
const PAGES = ["/", "/titles", "/search?q=the", "/chapters", "/chapters/problems", "/history",
  "/library/posters", "/library/posters/gallery", "/settings", "/free-space", "/shows/reset-unwatched"];
const measure = `(() => {
  const ctx = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
  const rgba = (c) => { ctx.clearRect(0,0,1,1); ctx.fillStyle = '#000'; ctx.fillStyle = c; ctx.fillRect(0,0,1,1); const d = ctx.getImageData(0,0,1,1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const lum = ([r,g,b]) => { const f = (v) => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; return .2126*f(r) + .7152*f(g) + .0722*f(b); };
  const over = (top, under) => [0,1,2].map(i => top[i]*top[3] + under[i]*(1-top[3])).concat(1);
  function bg(el) {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.backgroundImage !== 'none') return null; // over a picture: can't tell
      const c = rgba(s.backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let out = rgba(getComputedStyle(document.documentElement).colorScheme.includes('dark') ? '#000' : '#fff');
    for (const c of layers.reverse()) out = over(c, out);
    return out;
  }
  const found = {};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    if (el.closest('.visually-hidden, [aria-hidden=true], .title-show-hero')) continue;
    const s = getComputedStyle(el);
    const back = bg(el); if (!back) continue;
    const fg = over(rgba(s.color), back);
    const [a, b] = [lum(fg), lum(back)].sort((x, y) => y - x);
    const ratio = (a + .05) / (b + .05);
    const size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight) >= 700;
    const need = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio >= need) continue;
    const key = s.color + ' on ' + 'rgb(' + back.slice(0,3).map(Math.round).join(',') + ')';
    const who = el.tagName.toLowerCase() + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
    const f = found[key] ||= { ratio: +ratio.toFixed(2), count: 0, examples: [] };
    f.count++;
    if (f.examples.length < 3) f.examples.push(who + ' "' + n.textContent.trim().slice(0, 30) + '"');
  }
  return found;
})()`;

export default {
  async "no text below the contrast minimum, light and dark"({ go, js, send }) {
    const all = {};
    for (const scheme of ["light", "dark"]) {
      await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
      for (const path of PAGES) {
        await go(path);
        const found = await js(measure);
        if (found?.error) return { path, error: found.error };
        for (const [k, v] of Object.entries(found)) {
          const e = all[scheme + ": " + k] ||= { ratio: v.ratio, count: 0, examples: [] };
          e.count += v.count;
          for (const x of v.examples) if (e.examples.length < 4) e.examples.push(path + " " + x);
        }
      }
    }
    return Object.keys(all).length ? all : true;
  },
};
