// On a phone: the browser's bar matches the header, and a table that
// scrolls sideways shows a shadow at the edge there's more beyond.
export default {
  async "the browser's bar follows the scheme"({ go, js, send }) {
    const out = {};
    for (const scheme of ["light", "dark"]) {
      await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
      await go("/");
      out[scheme] = await js(`(() => { const m = [...document.querySelectorAll('meta[name=theme-color]')].find(m => !m.media || matchMedia(m.media).matches);
        return [m && m.content, getComputedStyle(document.querySelector('.app-header')).backgroundColor]; })()`);
    }
    return out.light[0] === "#ffffff" && out.dark[0] === "#172431" ? true : out;
  },
  async "a table that scrolls sideways shows where there's more"({ go, js, send, sleep }) {
    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 900, deviceScaleFactor: 1, mobile: true });
    await go("/history");
    const edges = (x) => js(`(async () => { const t = [...document.querySelectorAll('.table-responsive')].find(t => t.scrollWidth > t.clientWidth);
      if (!t) return 'no table wider than the screen';
      t.scrollLeft = ${x}; await new Promise(r => setTimeout(r, 400));
      return [+getComputedStyle(t, '::before').opacity, +getComputedStyle(t, '::after').opacity]; })()`);
    const start = await edges(0), end = await edges(100000);
    await send("Emulation.clearDeviceMetricsOverride");
    return start[0] === 0 && start[1] === 1 && end[0] === 1 && end[1] === 0 ? true : { start, end };
  },
};
