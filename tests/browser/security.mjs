// Pages tell the browser to refuse HTML put into them any other way than
// through swap.js's check (Trusted Types).
const checks = {
  async "the browser refuses HTML put in any other way"({ go, js }) {
    await go("/");
    const r = await js("(() => { try { document.createElement('div').innerHTML = '<b>x</b>'; return 'allowed'; } catch (e) { return e.name; } })()");
    return r === "TypeError" ? true : r;
  },
};
// The browser logs the refusal it was asked to make.
checks["the browser refuses HTML put in any other way"].expect = /TrustedHTML/;
export default checks;
