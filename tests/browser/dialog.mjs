async function wheel(send, x, y) {
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  for (let i = 0; i < 5; i++) await send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY: 400 });
}
async function trial({ go, js, send, sleep }, withRule) {
  await send("Emulation.setDeviceMetricsOverride", { width: 1000, height: 500, deviceScaleFactor: 1, mobile: false });
  await go("/settings");
  if (!(await js("document.documentElement.scrollHeight > innerHeight + 300"))) return "page too short";
  await js(`{ const d = document.createElement('dialog'); d.className = 'lightbox';
    d.append(document.createElement('img')); document.body.append(d);
    ${withRule ? "" : "d.style.overscrollBehavior = 'auto'; const s = new CSSStyleSheet(); s.replaceSync('dialog::backdrop{overflow:visible!important;overscroll-behavior:auto!important}'); document.adoptedStyleSheets = [s];"}
    d.showModal(); }`);
  await sleep(300);
  await wheel(send, 500, 250);
  await sleep(800);
  return js("scrollY");
}
export default {
  async "without the rule, the page scrolls under the viewer (test is real)"(ctx) {
    const y = await trial(ctx, false); return y > 0 ? true : { y };
  },
  async "with it, the page stays still"(ctx) {
    const y = await trial(ctx, true); return y === 0 ? true : { y };
  },
};
