// Runs one file of page checks in headless Chrome against a running Curatarr
// (BASE, e.g. http://127.0.0.1:5099), over the DevTools protocol. Each check
// is an async function given { go, js, sleep, send, on } and returns true, or
// what it found instead. A page error, or a console error or warning, fails
// the check too, unless the check expects it (its `expect`, a pattern).
//
//   node tests/browser/driver.mjs tests/browser/swap.mjs
//
// tests/test_browser.py starts a demo Curatarr and runs every file.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const BASE = process.env.BASE || "http://127.0.0.1:5099";
const CHROME = process.env.CHROME || "google-chrome";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const profile = mkdtempSync(join(tmpdir(), "curatarr-chrome-"));
const chrome = spawn(CHROME, [
  "--headless=new", "--remote-debugging-port=0", `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank",
], { stdio: "ignore" });

// Chrome writes the port it picked here (a first, cold start on a CI
// machine can take well over ten seconds).
let port;
for (let i = 0; i < 600 && !port; i++) {
  const file = join(profile, "DevToolsActivePort");
  if (existsSync(file)) port = readFileSync(file, "utf8").split("\n")[0];
  else await sleep(100);
}
if (!port) { chrome.kill(); throw new Error(`${CHROME} didn't start`); }
let targets;
for (let i = 0; i < 50 && !targets; i++) {
  try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); } catch { await sleep(100); }
}
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));

let id = 0;
const waiting = new Map();
const problems = [];
const listeners = new Set();
ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); return; }
  if (msg.method === "Runtime.exceptionThrown") {
    const d = msg.params.exceptionDetails;
    problems.push(`exception: ${d.exception?.description || d.text} @ ${d.url || ""}:${d.lineNumber}`);
  }
  if (msg.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(msg.params.type)) {
    problems.push(`console.${msg.params.type}: ${msg.params.args.map((a) => a.value ?? a.description).join(" ")}`);
  }
  if (msg.method === "Log.entryAdded" && ["error", "warning"].includes(msg.params.entry.level)) {
    problems.push(`log.${msg.params.entry.level}: ${msg.params.entry.text} ${msg.params.entry.url || ""}`);
  }
  listeners.forEach((fn) => fn(msg));
});
const send = (method, params = {}) => new Promise((done) => {
  const n = ++id; waiting.set(n, done); ws.send(JSON.stringify({ id: n, method, params }));
});
// Calls fn with every DevTools event until the returned function is called.
const on = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
await send("Runtime.enable"); await send("Log.enable"); await send("Page.enable");

async function go(path) {
  const loaded = new Promise((r) => {
    const off = on((m) => { if (m.method === "Page.loadEventFired") { off(); r(); } });
  });
  await send("Page.navigate", { url: BASE + path });
  await loaded;
  await sleep(300);
}
async function js(expression) {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) return { error: r.result.exceptionDetails.exception?.description };
  return r.result?.result?.value;
}

const checks = (await import(pathToFileURL(resolve(process.argv[2])).href)).default;
let failed = 0;
for (const [name, fn] of Object.entries(checks)) {
  problems.length = 0;
  let outcome;
  try { outcome = await fn({ go, js, sleep, send, on }); } catch (e) { outcome = `threw ${e}`; }
  const unexpected = problems.filter((p) => !(fn.expect && fn.expect.test(p)));
  const ok = outcome === true && unexpected.length === 0;
  if (!ok) failed++;
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${outcome === true ? "" : ` -> ${JSON.stringify(outcome)}`}`);
  unexpected.forEach((p) => console.log(`     ${p}`));
}
ws.close();
chrome.kill();
await sleep(300);
rmSync(profile, { recursive: true, force: true });
process.exit(failed ? 1 : 0);
