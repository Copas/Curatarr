async function pointAt(js, send, sleep, selector) {
  const box = await js(`(() => { const r = document.querySelector('${selector}').getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.x, y: box.y });
  await sleep(1500);
  return box;
}
export default {
  async "pointing at a link fetches it; clicking opens the early copy"({ go, js, send, sleep }) {
    await go("/");
    const box = await pointAt(js, send, sleep, '.app-nav-desktop a[href="/titles"]');
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 });
    await sleep(2000);
    const r = await js("({path: location.pathname, delivery: performance.getEntriesByType('navigation')[0].deliveryType})");
    return r.path === "/titles" && r.delivery === "navigational-prefetch" ? true : r;
  },
  async "a link not pointed at isn't fetched early"({ go, js, sleep }) {
    await go("/");
    await sleep(1500);
    await js("location.href = '/chapters'");
    await sleep(2000);
    const r = await js("({path: location.pathname, delivery: performance.getEntriesByType('navigation')[0].deliveryType})");
    return r.path === "/chapters" && r.delivery === "" ? true : r;
  },
  async "pages cross-fade between each other"({ go, js }) {
    await go("/");
    const r = await js("[...document.styleSheets].some(s => { try { return [...s.cssRules].some(rule => rule.cssText.includes('@view-transition') || (rule.cssRules && [...rule.cssRules].some(x => x.cssText.includes('@view-transition')))); } catch { return false; } })");
    return r === true ? true : "no @view-transition rule";
  },
};
