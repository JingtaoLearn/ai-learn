"use strict";

import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [baseUrl, sessionCookie, chromium] = process.argv.slice(2);
if (!baseUrl || !sessionCookie || !chromium) {
  throw new Error("usage: browser_acceptance.js BASE_URL SESSION_COOKIE CHROMIUM");
}

const profile = await mkdtemp(join(tmpdir(), "quant-browser-"));
const browser = spawn(
  chromium,
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "pipe"] },
);

let stderr = "";
const webSocketUrl = await new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error(`Chromium startup timed out: ${stderr}`)), 15000);
  browser.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
    const match = stderr.match(/DevTools listening on (ws:\/\/\S+)/);
    if (match) {
      clearTimeout(timeout);
      resolve(match[1]);
    }
  });
  browser.once("exit", (code) => reject(new Error(`Chromium exited early (${code}): ${stderr}`)));
});

const socket = new WebSocket(webSocketUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let nextId = 1;
const pending = new Map();
const listeners = new Map();
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const waiter = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
    else waiter.resolve(message.result);
    return;
  }
  const key = `${message.sessionId || ""}:${message.method}`;
  const waiter = listeners.get(key);
  if (waiter) {
    listeners.delete(key);
    waiter(message.params);
  }
});

function send(method, params = {}, sessionId = undefined) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

function once(method, sessionId) {
  return new Promise((resolve) => listeners.set(`${sessionId}:${method}`, resolve));
}

try {
  const { targetId } = await send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  await send("Page.enable", {}, sessionId);
  await send("Runtime.enable", {}, sessionId);
  await send("Network.enable", {}, sessionId);
  const cookie = await send(
    "Network.setCookie",
    { name: "quant_session", value: sessionCookie, url: baseUrl, httpOnly: true, sameSite: "Lax" },
    sessionId,
  );
  if (!cookie.success) throw new Error("Could not install authenticated browser cookie");

  const routes = {
    "/": "dashboard",
    "/operators": "operators",
    "/templates/single_stock_daily_causal/1": "template-detail",
    "/experiments/new": "experiment-new",
    "/history": "history",
  };
  for (const scriptsDisabled of [false, true]) {
    await send(
      "Emulation.setScriptExecutionDisabled",
      { value: scriptsDisabled },
      sessionId,
    );
    for (const width of [390, 1280]) {
      await send(
        "Emulation.setDeviceMetricsOverride",
        { width, height: 844, deviceScaleFactor: 1, mobile: width === 390 },
        sessionId,
      );
      for (const [route, expectedPage] of Object.entries(routes)) {
        const loaded = once("Page.loadEventFired", sessionId);
        await send("Page.navigate", { url: `${baseUrl}${route}` }, sessionId);
        await loaded;
        const { result } = await send(
          "Runtime.evaluate",
          {
            expression: `({
              page: document.body.dataset.page,
              hasMain: Boolean(document.querySelector("main")),
              hasPrimaryAction: ${route === "/experiments/new"
                ? 'Boolean(document.querySelector(\'form[data-testid="experiment-form"] button[type="submit"]\'))'
                : "true"},
              documentWidth: document.documentElement.scrollWidth,
              viewportWidth: window.innerWidth
            })`,
            returnByValue: true,
          },
          sessionId,
        );
        const value = result.value;
        if (value.page !== expectedPage || !value.hasMain || !value.hasPrimaryAction) {
          throw new Error(`Browser selector failure for ${route}: ${JSON.stringify(value)}`);
        }
        if (value.documentWidth > value.viewportWidth) {
          throw new Error(`Horizontal page overflow for ${route} at ${width}px`);
        }
      }
    }
  }
} finally {
  socket.close();
  browser.kill("SIGTERM");
  await rm(profile, { recursive: true, force: true });
}
