// Field errors (static/forms.js, ui.css): only once a field is left, in words
// under it and for screen readers, gone as soon as it's put right; text boxes
// grow as you type.
async function type(send, text) {
  for (const ch of text) await send("Input.insertText", { text: ch });
}
const state = (sel) => `(() => { const f = document.querySelector(${JSON.stringify(sel)});
  const err = f.dataset.error && document.getElementById(f.dataset.error);
  return { invalid: f.getAttribute('aria-invalid'), border: getComputedStyle(f).borderTopColor,
           error: err && !err.hidden ? err.textContent : null,
           described: (f.getAttribute('aria-describedby') || '').includes(f.dataset.error || '-') }; })()`;

export default {
  async "a wrong number says so once left, not while typing, and clears when fixed"({ go, js, send, sleep }) {
    await send("Emulation.setFocusEmulationEnabled", { enabled: true });
    await go("/shows/reset-unwatched");
    const sel = 'input[name="skip_days"]';
    const danger = await js("getComputedStyle(document.documentElement).getPropertyValue('--app-danger')");
    await js(`(() => { const f = document.querySelector('${sel}'); f.focus(); f.select(); })()`);
    await type(send, "99999");
    const typing = await js(state(sel));
    if (typing.error || typing.invalid === "true") return { typing };
    await js("document.activeElement.blur()");
    await sleep(400); // the border's colour transition
    const left = await js(state(sel));
    const red = await js(`(() => { const c = document.createElement('span'); c.style.color = 'var(--app-danger)'; document.body.append(c); const v = getComputedStyle(c).color; c.remove(); return v; })()`);
    if (!(left.invalid === "true" && left.error && /3650/.test(left.error) && left.described && left.border === red)) return { left, red, danger };
    await js(`(() => { const f = document.querySelector('${sel}'); f.focus(); f.select(); })()`);
    await type(send, "30");
    const fixed = await js(state(sel));
    return fixed.invalid === "false" && !fixed.error && !fixed.described ? true : { fixed };
  },
  async "sending with a wrong field shows its error at once"({ go, js, sleep }) {
    await go("/shows/reset-unwatched");
    const sel = 'input[name="skip_days"]';
    await js(`(() => { const f = document.querySelector('${sel}'); f.value = '-5'; f.form.requestSubmit(); })()`);
    await sleep(200);
    const r = await js(state(sel));
    const stayed = await js("location.pathname");
    return r.invalid === "true" && r.error && stayed === "/shows/reset-unwatched" ? true : { r, stayed };
  },
  async "a text box grows as you type"({ go, js, send }) {
    await go("/chapters/settings");
    const sel = 'textarea[name="path_mappings"]';
    if (!(await js(`!!document.querySelector('${sel}')`))) return "no text box";
    // (it's in a setting card: open it first, or it can't take focus)
    const before = await js(`(() => { const t = document.querySelector('${sel}'); const d = t.closest('details'); if (d) d.open = true;
      t.value = ''; t.focus(); return t.offsetHeight; })()`);
    await type(send, "/a => /b\n/c => /d\n/e => /f\n/g => /h\n/i => /j");
    const after = await js(`document.querySelector('${sel}').offsetHeight`);
    return after > before + 40 ? true : { before, after };
  },
};
