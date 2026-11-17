// On/off settings are switches: the native switch attribute (Safari draws and
// announces one), with role="switch" and Bootstrap's look where it doesn't.
export default {
  async "settings' on/off controls are switches that still toggle"({ go, js }) {
    let seen = 0;
    for (const path of ["/settings", "/chapters/settings", "/rules/household", "/library/posters"]) {
      await go(path);
      const r = await js(`(() => {
        const all = [...document.querySelectorAll('input[type=checkbox][switch]')];
        const loose = [...document.querySelectorAll('input[role=switch]:not([switch])')].length;
        const first = all.find(i => !i.disabled);
        if (!first) return { count: all.length, loose };
        const before = first.checked; first.click();
        const toggled = first.checked !== before; first.click();
        return { count: all.length, loose, toggled, role: first.getAttribute('role'),
                 drawn: getComputedStyle(first).appearance };
      })()`);
      if (r.loose) return { path, ...r }; // a switch without the attribute
      if (!r.count) continue; // none shown here in the demo
      seen++;
      if (!(r.toggled && r.role === "switch" && r.drawn === "none")) return { path, ...r };
    }
    return seen >= 2 ? true : { seen };
  },
};
