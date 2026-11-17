const state = `(() => {
  const html = document.documentElement;
  const shown = [...document.querySelectorAll('.theme-toggle')].filter(b => getComputedStyle(b).display !== 'none').map(b => b.value);
  return {
    attr: html.getAttribute('data-bs-theme'),
    meta: document.querySelector('meta[name=color-scheme]').content,
    scheme: getComputedStyle(html).colorScheme,
    bg: getComputedStyle(document.body).backgroundColor,
    bsBg: getComputedStyle(html).getPropertyValue('--bs-body-bg').trim(),
    card: getComputedStyle(document.querySelector('.card') || document.body).backgroundColor,
    switchTo: shown,
  };
})()`;
const LIGHT_BG = "rgb(244, 247, 250)";
const DARK_BG = "rgb(16, 25, 35)";

async function device(send, scheme) {
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
}
async function press(js, sleep, value) {
  await js(`document.querySelector('.theme-toggle[value=${value}]').click()`);
  await sleep(1500);
}

export default {
  async "no choice saved: follows a light device"({ go, js, send }) {
    await device(send, "light");
    await go("/");
    const s = await js(state);
    return s.attr === null && s.meta === "light dark" && s.bg === LIGHT_BG && s.bsBg === "#fff" &&
      s.switchTo.join() === "dark" ? true : s;
  },
  async "no choice saved: follows a dark device, Bootstrap included"({ go, js, send }) {
    await device(send, "dark");
    await go("/");
    const s = await js(state);
    return s.attr === null && s.scheme === "dark" && s.bg === DARK_BG && s.bsBg === "#212529" &&
      s.switchTo.join() === "light" ? true : s;
  },
  async "on a dark device, switching to light is kept; switching back follows the device again"({ go, js, send, sleep }) {
    await device(send, "dark");
    await go("/");
    await press(js, sleep, "light");
    const picked = await js(state);
    if (!(picked.attr === "light" && picked.bg === LIGHT_BG && picked.switchTo.join() === "dark")) return { picked };
    // the device turns light later: still light, and still a choice
    await device(send, "light");
    const kept = await js(state);
    if (kept.attr !== "light") return { kept };
    await device(send, "dark");
    await press(js, sleep, "dark");
    const back = await js(state);
    if (!(back.attr === null && back.bg === DARK_BG)) return { back };
    // following the device: it turning light turns the page light
    await device(send, "light");
    const follows = await js(state);
    return follows.bg === LIGHT_BG && follows.switchTo.join() === "dark" ? true : { follows };
  },
  async "pages look right in both schemes (no leftover hard-coded colours)"({ go, js, send }) {
    const out = {};
    for (const scheme of ["light", "dark"]) {
      await device(send, scheme);
      for (const path of ["/", "/titles", "/chapters", "/settings"]) {
        await go(path);
        out[scheme + path] = await js(state.replace("return {", "return { path: location.pathname,"));
      }
    }
    const bad = Object.entries(out).filter(([k, s]) => (k.startsWith("light") ? s.bg !== LIGHT_BG : s.bg !== DARK_BG));
    return bad.length ? Object.fromEntries(bad) : true;
  },
};
