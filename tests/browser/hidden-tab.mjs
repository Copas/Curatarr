export default {
  async "Enhance's activity card stops asking while the tab is hidden, catches up when shown"({ go, js, sleep }) {
    await go("/chapters");
    await js(`(() => {
      window.__asks = 0;
      const real = window.fetch;
      window.fetch = (...a) => { if (String(a[0]).includes('/chapters/activity')) window.__asks++; return real(...a); };
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.__hidden === true });
      window.__hidden = true; document.dispatchEvent(new Event('visibilitychange'));
    })()`);
    await sleep(12000);
    const whileHidden = await js("window.__asks");
    await js("window.__hidden = false; document.dispatchEvent(new Event('visibilitychange'))");
    await sleep(800);
    const shown = await js("window.__asks");
    await sleep(6000);
    const later = await js("window.__asks");
    return whileHidden === 0 && shown === 1 && later >= 2 ? true : { whileHidden, shown, later };
  },
};
