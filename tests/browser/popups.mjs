// Menus and confirm-in-place boxes (templates/_menu.html, _confirm.html,
// static/popups.js): beside their button, closed by Escape or a click
// elsewhere, keyboard-usable; then again with the fallbacks a browser without
// invoker buttons or anchor positioning gets.
const near = `(pop, trigger) => {
  const p = pop.getBoundingClientRect(), t = trigger.getBoundingClientRect();
  const beside = Math.abs(p.top - t.bottom) < 12 || Math.abs(t.top - p.bottom) < 12;
  const onScreen = p.left >= 0 && p.right <= innerWidth && p.top >= 0 && p.bottom <= innerHeight;
  return beside && onScreen && Math.abs(p.left - t.left) < 260;
}`;

async function key(send, k, code) {
  const text = k === "Enter" ? "\r" : k === " " ? " " : undefined;
  const vk = { Enter: 13, " ": 32, Escape: 27, ArrowDown: 40, ArrowUp: 38, End: 35, Home: 36 }[k];
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code: code || k, windowsVirtualKeyCode: vk, text });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: code || k, windowsVirtualKeyCode: vk });
}
async function clickAt(send, js, selector) {
  const box = await js(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  }
}

async function menu({ go, js, send, sleep }) {
  await send("Emulation.setFocusEmulationEnabled", { enabled: true });
  await go("/review");
  const trigger = '.action-menu [command="toggle-popover"]';
  if (!(await js(`!!document.querySelector('${trigger}')`))) return "no menu on Review";
  // pointer: opens beside the button, focus stays on the button
  await clickAt(send, js, trigger);
  await sleep(300);
  const opened = await js(`(() => { const t = document.querySelector('${trigger}'), m = document.getElementById(t.getAttribute('commandfor'));
    return { open: m.matches(':popover-open'), expanded: t.getAttribute('aria-expanded'), near: (${near})(m, t), focus: document.activeElement === t }; })()`);
  if (!(opened.open && opened.expanded === "true" && opened.near && opened.focus)) return { opened };
  // a click elsewhere closes it
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: 5, y: 300, button: "left", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: 5, y: 300, button: "left", clickCount: 1 });
  await sleep(300);
  const outside = await js(`(() => { const t = document.querySelector('${trigger}'); return { open: document.getElementById(t.getAttribute('commandfor')).matches(':popover-open'), expanded: t.getAttribute('aria-expanded') }; })()`);
  if (outside.open || outside.expanded !== "false") return { outside };
  // keyboard: Enter opens and goes to the first item, arrows move, Escape returns
  await js(`document.querySelector('${trigger}').focus()`);
  await key(send, "Enter");
  await sleep(300);
  const first = await js("document.activeElement.textContent.trim()");
  await key(send, "ArrowDown");
  const second = await js("document.activeElement.textContent.trim()");
  await key(send, "End");
  const last = await js("document.activeElement.textContent.trim()");
  await key(send, "Escape");
  await sleep(300);
  const back = await js(`(() => { const t = document.querySelector('${trigger}'); return { focus: document.activeElement === t, open: document.getElementById(t.getAttribute('commandfor')).matches(':popover-open') }; })()`);
  return first === "Snooze 30 days" && second === "Snooze 90 days" && last === "Never purge" && back.focus && !back.open
    ? true : { first, second, last, back };
}

async function confirmBox({ go, js, send, sleep }) {
  await go("/free-space");
  const trigger = '.confirm-inline [command="toggle-popover"]';
  if (!(await js(`!!document.querySelector('${trigger}')`))) return "no confirm button on Free space";
  await clickAt(send, js, trigger);
  await sleep(300);
  const r = await js(`(() => { const t = document.querySelector('${trigger}'), p = document.getElementById(t.getAttribute('commandfor'));
    const submit = p.querySelector('button:not([type=button])');
    return { open: p.matches(':popover-open'), near: (${near})(p, t), submits: !!submit && submit.form === t.closest('form') }; })()`);
  if (!(r.open && r.near && r.submits)) return r;
  await clickAt(send, js, '.confirm-pop:popover-open [command="hide-popover"]');
  await sleep(300);
  const cancelled = await js(`!document.querySelector('.confirm-pop:popover-open')`);
  return cancelled ? true : "Cancel didn't close it";
}

async function tipOverMenu({ go, js, send, sleep }) {
  await go("/review");
  await clickAt(send, js, '.action-menu [command="toggle-popover"]');
  await sleep(300);
  await js(`document.querySelector('.action-menu-items:popover-open [data-tip]').dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))`);
  await sleep(200);
  const r = await js(`(() => { const tip = document.getElementById('app-tip'); return tip ? tip.matches(':popover-open') : 'no tip'; })()`);
  await key(send, "Escape");
  return r === true ? true : r;
}

const checks = {
  "a menu opens beside its button, closes on a click elsewhere, works from the keyboard": menu,
  "a confirm box opens beside its button and Cancel closes it": confirmBox,
  "a tooltip shows over an open menu": tipOverMenu,
  async "without invoker buttons or anchor positioning, the same"(ctx) {
    await ctx.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      delete HTMLButtonElement.prototype.commandForElement;
      const supports = CSS.supports.bind(CSS);
      CSS.supports = (...a) => String(a.join(':')).includes('position-area') ? false : supports(...a);
      Object.defineProperty(HTMLButtonElement.prototype, 'command', { get() { return ''; }, set() {}, configurable: true });
      Object.defineProperty(HTMLButtonElement.prototype, 'commandForElement', { get: undefined, configurable: true });` });
    // The browser itself still has invokers: stop it acting on commandfor so
    // only popups.js's fallback can open anything.
    await ctx.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      document.addEventListener('command', e => e.preventDefault(), true);
      delete HTMLButtonElement.prototype.commandForElement;
      // and the stylesheet's anchor positioning, as a browser without it would skip it
      // (a constructed sheet: the page's security policy refuses a <style>)
      document.addEventListener('DOMContentLoaded', () => {
        const s = new CSSStyleSheet();
        s.replaceSync('.action-menu-items, .confirm-pop { position-area: none !important; position-try-fallbacks: none !important; }');
        document.adoptedStyleSheets = [...document.adoptedStyleSheets, s];
      });` });
    const a = await menu(ctx);
    if (a !== true) return { menu: a };
    const b = await confirmBox(ctx);
    return b === true ? true : { confirm: b };
  },
};
export default checks;
