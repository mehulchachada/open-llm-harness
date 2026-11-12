// Take one screenshot of a local page with a private headless browser, over the DevTools protocol.
// usage: node shot.mjs <spec.json>
// spec: { browser, profile, url, out, width, height, settle_ms, local_storage: {k: v}, click: [css, ...] }
// Prints one JSON line: { ok, out, title, console_errors } or { ok: false, error }.
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { createServer } from "node:net";

const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () =>
  new Promise((ok, bad) => {
    const s = createServer();
    s.once("error", bad);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => ok(port));
    });
  });

let browser;
try {
  const port = await freePort();
  browser = spawn(
    spec.browser,
    [
      "--headless=new",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${spec.profile}`,
      `--window-size=${spec.width},${spec.height}`,
      "--hide-scrollbars",
      "--no-first-run",
      "--disable-extensions",
      "about:blank",
    ],
    { stdio: "ignore" }
  );
  let wsUrl = null;
  for (let i = 0; i < 60 && !wsUrl; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      wsUrl = list.find((t) => t.type === "page")?.webSocketDebuggerUrl ?? null;
    } catch {}
    if (!wsUrl) await sleep(200);
  }
  if (!wsUrl) throw new Error("the browser did not start");

  const ws = new WebSocket(wsUrl);
  await new Promise((ok, bad) => {
    ws.onopen = ok;
    ws.onerror = () => bad(new Error("could not attach to the browser"));
  });
  let seq = 0;
  const pending = new Map();
  const consoleErrors = [];
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const { ok, bad } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? bad(new Error(m.error.message)) : ok(m.result);
    } else if (m.method === "Runtime.exceptionThrown") {
      consoleErrors.push(String(m.params?.exceptionDetails?.exception?.description ?? "exception").slice(0, 300));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params?.type === "error") {
      consoleErrors.push(m.params.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 300));
    }
  };
  const send = (method, params = {}) =>
    new Promise((ok, bad) => {
      const id = ++seq;
      pending.set(id, { ok, bad });
      ws.send(JSON.stringify({ id, method, params }));
    });
  const evaluate = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    return r.result?.value;
  };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: spec.width,
    height: spec.height,
    deviceScaleFactor: 1,
    mobile: false,
  });
  const storage = spec.local_storage ?? {};
  if (Object.keys(storage).length) {
    // Runs before any page script, so the app starts in the wanted state (theme, first-run done).
    await send("Page.addScriptToEvaluateOnNewDocument", {
      source: `try { const s = ${JSON.stringify(storage)}; for (const k in s) localStorage.setItem(k, s[k]); } catch (e) {}`,
    });
  }
  const nav = await send("Page.navigate", { url: spec.url });
  if (nav.errorText) throw new Error(`could not open ${spec.url}: ${nav.errorText}`);
  await sleep(spec.settle_ms);
  const missing = [];
  for (const selector of spec.click ?? []) {
    const hit = await evaluate(
      `(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; el.click(); return true; })()`
    );
    if (!hit) missing.push(selector);
    await sleep(700);
  }
  if (spec.scroll_to) {
    const hit = await evaluate(
      `(() => { const el = document.querySelector(${JSON.stringify(spec.scroll_to)}); if (!el) return false; el.scrollIntoView({ block: "start" }); return true; })()`
    );
    if (!hit) missing.push(spec.scroll_to);
    await sleep(500);
  }
  const shot = await send("Page.captureScreenshot", { format: "png" });
  mkdirSync(dirname(spec.out), { recursive: true });
  writeFileSync(spec.out, Buffer.from(shot.data, "base64"));
  const title = await evaluate("document.title");
  // Close the browser properly: a killed browser leaves helper processes holding the profile, and
  // the next screenshot could not start.
  const gone = new Promise((r) => browser.once("exit", r));
  send("Browser.close").catch(() => {});
  await Promise.race([gone, sleep(4000)]);
  ws.close();
  console.log(JSON.stringify({ ok: true, out: spec.out, title, console_errors: consoleErrors.slice(0, 8), missing }));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: String(e?.message ?? e) }));
} finally {
  browser?.kill();
}
