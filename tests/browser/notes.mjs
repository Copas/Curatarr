export default {
  async "a note sits in the top layer, can be dismissed, and waits while pointed at"({ go, js, sleep }) {
    await go("/titles");
    const r = await js(`(async () => {
      curatarrNote('First', 'success', 1500);
      curatarrNote('Second', 'warning', 1500);
      const region = document.querySelector('.artwork-notes');
      const out = { open: region.matches(':popover-open'), role: region.getAttribute('role'), label: region.getAttribute('aria-label'),
                    count: region.children.length, corner: getComputedStyle(region).right };
      region.querySelector('.artwork-note-close').click();
      out.afterDismiss = region.children.length;
      return out;
    })()`);
    if (!(r.open && r.role === "region" && r.label === "Notifications" && r.count === 2 && r.afterDismiss === 1 && r.corner === "16px")) return r;
    await sleep(2500);
    const gone = await js("({left: document.querySelector('.artwork-notes').children.length, open: document.querySelector('.artwork-notes').matches(':popover-open')})");
    return gone.left === 0 && gone.open === false ? true : gone;
  },
  async "a focused note stays until focus leaves"({ go, js, sleep, send }) {
    await send("Emulation.setFocusEmulationEnabled", { enabled: true });
    await go("/titles");
    await js("document.getElementById('title-search').focus(); curatarrNote('Stay', 'success', 800); document.querySelector('.artwork-note-close').focus()");
    await sleep(2000);
    const stays = await js("document.querySelectorAll('.artwork-note').length");
    await js("document.querySelector('.artwork-note-close').click()");
    const back = await js("({left: document.querySelectorAll('.artwork-note').length, focus: document.activeElement.id})");
    return stays === 1 && back.left === 0 && back.focus === "title-search" ? true : { stays, back };
  },
};
