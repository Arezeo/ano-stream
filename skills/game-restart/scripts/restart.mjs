#!/usr/bin/env node
/**
 * game-restart — replay an ordered sequence of page-level inputs against a live
 * agentnumberone /play session.
 *
 * Default sequence (Voxelcraft/Minecraft-style):
 *   1. Cmd+Esc  -> in-game pause menu
 *   2. click    -> "New World"      (title screen)
 *   3. click    -> "Play New World" (confirm + generate)
 *   4. Esc      -> back into the game
 *
 * Two transports, same sequence:
 *   --backend cdp  (default) DevTools protocol: DOM-accurate, screenshot per step.
 *                            Needs Chrome with --remote-debugging-port=9222.
 *   --backend os             OS-level: `screencapture` + CGEvent helper. No browser
 *                            flag, no DevTools. Clicks use step x/y in WINDOW coords.
 *                            Needs Accessibility + Screen Recording permission.
 *
 * Override the sequence with --steps file.json.
 * Node >= 22 (global WebSocket). No npm dependencies.
 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HELPER = path.join(HERE, "os-helper");

const arg = (n, d) => {
  const i = process.argv.indexOf(`--${n}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : d;
};
const flag = (n) => process.argv.includes(`--${n}`);

const BACKEND = arg("backend", "cdp");
const PORT = Number(arg("port", 9222));
const URL_MATCH = new RegExp(arg("url-match", "agentnumberone|library-games"), "i");
const WINDOW = arg("window", "Chrome");
const OUT = arg("out", "/tmp/restart");
const STEPS_FILE = arg("steps", null);
fs.mkdirSync(OUT, { recursive: true });

const DEFAULT_STEPS = [
  { type: "key", key: "Escape", operator: true, settle: 900, name: "1-pause-menu" },
  { type: "click", match: "new world", operator: true, settle: 1800, name: "2-new-world" },
  { type: "click", match: "play new world|play selected|play", operator: true, settle: 3500, name: "3-play-new-world" },
  { type: "key", key: "Escape", operator: false, settle: 800, name: "4-back-in-game" },
];
const steps = STEPS_FILE ? JSON.parse(fs.readFileSync(STEPS_FILE, "utf8")) : DEFAULT_STEPS;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const MOD = process.platform === "darwin" ? "Meta" : "Control";
const MOD_VK = process.platform === "darwin" ? 93 : 17;
const VK = { Escape: 27, Enter: 13, Tab: 9, Space: 32 };
const stepText = (s) =>
  s.type === "click" ? (s.match ? `click /${s.match}/` : `click ${s.x},${s.y}`)
  : s.type === "key" ? `${s.operator ? "Cmd+" : ""}${s.key}`
  : s.type === "clear-chat" ? "clear chat" : `wait ${s.ms}`;

/* ─────────────────────────── CDP backend ─────────────────────────── */

let id = 0;
const pending = new Map();
let send = null;

function wire(ws) {
  send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const i = ++id;
      pending.set(i, { resolve, reject });
      ws.send(JSON.stringify({ id: i, method, params }));
      setTimeout(() => pending.has(i) && (pending.delete(i), reject(new Error(`timeout ${method}`))), 30000);
    });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
    }
  };
}

async function findTab() {
  let r;
  try {
    r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  } catch {
    throw Object.assign(
      new Error(`no DevTools endpoint on 127.0.0.1:${PORT} — relaunch Chrome with --remote-debugging-port=${PORT}`),
      { code: 2 });
  }
  const all = await r.json();
  const tabs = all.filter((t) => t.type === "page" && URL_MATCH.test(t.url));
  if (!tabs.length) {
    const urls = all.filter((t) => t.type === "page").map((t) => t.url.slice(0, 70));
    throw Object.assign(new Error(`no tab matching ${URL_MATCH} — open: ${JSON.stringify(urls)}`), { code: 2 });
  }
  return tabs[0];
}

const ev = async (expression) => {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return r.result?.value;
};

const cdp = {
  async shot(name) {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    const p = path.join(OUT, `${name}.png`);
    fs.writeFileSync(p, Buffer.from(data, "base64"));
    return p;
  },
  async tapKey(key, operator) {
    const vk = VK[key] ?? 0;
    if (operator) await send("Input.dispatchKeyEvent", { type: "rawKeyDown", code: MOD, key: MOD, windowsVirtualKeyCode: MOD_VK, nativeVirtualKeyCode: MOD_VK });
    await send("Input.dispatchKeyEvent", { type: "rawKeyDown", code: key, key, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    await send("Input.dispatchKeyEvent", { type: "keyUp", code: key, key, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk });
    if (operator) await send("Input.dispatchKeyEvent", { type: "keyUp", code: MOD, key: MOD, windowsVirtualKeyCode: MOD_VK, nativeVirtualKeyCode: MOD_VK });
  },
  async clickAt(x, y, operator) {
    if (operator) await send("Input.dispatchKeyEvent", { type: "rawKeyDown", code: MOD, key: MOD, windowsVirtualKeyCode: MOD_VK, nativeVirtualKeyCode: MOD_VK });
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    if (operator) await send("Input.dispatchKeyEvent", { type: "keyUp", code: MOD, key: MOD, windowsVirtualKeyCode: MOD_VK, nativeVirtualKeyCode: MOD_VK });
  },
  async resolveClick(s) {
    if (s.x != null && s.y != null) return { x: Number(s.x), y: Number(s.y), text: "(coords)" };
    const src = new RegExp(s.match, s.flags || "i");
    return ev(`(() => {
      const RX = new RegExp(${JSON.stringify(src.source)}, ${JSON.stringify(src.flags)});
      const docs = [document];
      for (const f of document.querySelectorAll('iframe')) {
        try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}
      }
      let best = null;
      for (const doc of docs) for (const el of doc.querySelectorAll('button,[role=button],a,div,span,li')) {
        const txt = (el.innerText || el.textContent || '').trim();
        if (!txt || txt.length > 40 || !RX.test(txt)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        const area = r.width * r.height;
        if (!best || area < best.area) best = { x: r.x + r.width / 2, y: r.y + r.height / 2, area, text: txt.slice(0, 40) };
      }
      return best;
    })()`);
  },
  async clearChat() {
    return ev(`(() => {
      const composer = document.querySelector('textarea, input[type=text], [contenteditable=true], input[placeholder]');
      if (!composer) return 0;
      const scope = composer.closest('form,section,aside,div[class]') || document.body;
      let cleared = 0;
      for (const n of [...scope.querySelectorAll('li,div,p,article')]) {
        if (n === composer || n.contains(composer)) continue;
        if (!/message|chat|history|instruction/i.test(n.className || '')) continue;
        if (n.children.length > 6) continue;
        n.remove(); cleared++;
      }
      try {
        if ('value' in composer) composer.value = ''; else composer.textContent = '';
        composer.dispatchEvent(new Event('input', { bubbles: true }));
      } catch (e) {}
      return cleared;
    })()`);
  },
};

/* ──────────────────────────── OS backend ──────────────────────────── */

function osWindows() {
  try {
    return JSON.parse(execFileSync(HELPER, ["windows"], { encoding: "utf8" }));
  } catch (e) {
    throw Object.assign(new Error(`os-helper missing — build it: swiftc -O scripts/os-helper.swift -o scripts/os-helper`), { code: 2 });
  }
}

function osFindWindow() {
  const all = osWindows().filter((w) => w.w > 300 && w.h > 200);
  const hit = all.find((w) => `${w.owner} ${w.name}`.toLowerCase().includes(WINDOW.toLowerCase()));
  if (!hit) {
    const names = all.slice(0, 8).map((w) => `${w.owner}/${w.name}`);
    throw Object.assign(new Error(`no window matching "${WINDOW}" — saw: ${JSON.stringify(names)}`), { code: 2 });
  }
  return hit;
}

const os = {
  win: null,
  async shot(name) {
    const p = path.join(OUT, `${name}.png`);
    execFileSync("/usr/sbin/screencapture", ["-x", p]);
    return p;
  },
  async tapKey(key, operator) {
    const name = key === "Escape" ? "Escape" : key;
    if (operator) execFileSync(HELPER, ["keydown", "Cmd"]);
    execFileSync(HELPER, ["key", name]);
    if (operator) execFileSync(HELPER, ["keyup", "Cmd"]);
  },
  async clickAt(x, y, operator) {
    if (!this.win) this.win = osFindWindow();
    if (operator) execFileSync(HELPER, ["keydown", "Cmd"]);
    execFileSync(HELPER, ["click", String(Math.round(this.win.x + x)), String(Math.round(this.win.y + y))]);
    if (operator) execFileSync(HELPER, ["keyup", "Cmd"]);
  },
  async resolveClick(s) {
    if (s.x == null || s.y == null) {
      throw new Error(`os backend needs coordinates: give step x/y (window-relative). Screenshot ${OUT} shows the target.`);
    }
    return { x: Number(s.x), y: Number(s.y), text: "(coords)" };
  },
  async clearChat() {
    throw new Error("clear-chat is CDP-only; the os backend cannot read the DOM");
  },
};

/* ────────────────────────────── run ──────────────────────────────── */

(async () => {
  let backend;
  let tabUrl = `os:${WINDOW}`;

  if (BACKEND === "cdp") {
    const tab = await findTab();
    tabUrl = tab.url.slice(0, 120);
    const ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    wire(ws);
    await send("Page.enable");
    await send("Runtime.enable");
    backend = { ...cdp, close: () => ws.close() };
  } else if (BACKEND === "os") {
    os.win = osFindWindow();
    tabUrl = `${os.win.owner} / ${os.win.name} @ ${Math.round(os.win.x)},${Math.round(os.win.y)} ${Math.round(os.win.w)}x${Math.round(os.win.h)}`;
    backend = { ...os, close: () => {} };
  } else {
    throw new Error(`unknown backend: ${BACKEND} (cdp | os)`);
  }

  const results = [];
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    const label = s.name || `${i + 1}-${s.type}`;
    let ok = true;
    const extra = {};
    try {
      if (s.type === "key") await backend.tapKey(s.key, !!s.operator);
      else if (s.type === "click") {
        const c = await backend.resolveClick(s);
        await backend.clickAt(c.x, c.y, !!s.operator);
        extra.control = c.text;
      } else if (s.type === "wait") await sleep(s.ms || 500);
      else if (s.type === "clear-chat") {
        extra.clearedRows = await backend.clearChat();
        ok = extra.clearedRows > 0;
      } else { ok = false; extra.error = `unknown step type ${s.type}`; }
    } catch (e) {
      ok = false; extra.error = e.message;
    }
    await sleep(s.settle ?? 600);
    extra.shot = await backend.shot(label);
    results.push({ step: i + 1, action: stepText(s), ok, ...extra });
    if (!ok) break;
  }

  const good = results.length === steps.length && results.every((r) => r.ok);
  console.log(JSON.stringify({ backend: BACKEND, target: tabUrl, ok: good, results }, null, 1));
  backend.close();
  process.exit(good ? 0 : 3);
})().catch((e) => {
  console.error(JSON.stringify({ error: e.message }));
  process.exit(e.code || 3);
});
