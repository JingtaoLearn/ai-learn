"use strict";

import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [baseUrl, sessionCookie, chromium, reportExperimentId] = process.argv.slice(2);
if (!baseUrl || !sessionCookie || !chromium || !reportExperimentId) {
  throw new Error(
    "usage: browser_acceptance.mjs BASE_URL SESSION_COOKIE CHROMIUM REPORT_EXPERIMENT_ID",
  );
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
  await send(
    "Network.setExtraHTTPHeaders",
    { headers: { Origin: baseUrl } },
    sessionId,
  );

  async function evaluate(expression) {
    const { result } = await send(
      "Runtime.evaluate",
      { expression, returnByValue: true },
      sessionId,
    );
    if (result.exceptionDetails) {
      throw new Error(`Browser evaluation failed: ${JSON.stringify(result.exceptionDetails)}`);
    }
    return result.value;
  }

  async function navigate(path) {
    const loaded = once("Page.loadEventFired", sessionId);
    await send("Page.navigate", { url: `${baseUrl}${path}` }, sessionId);
    await loaded;
  }

  async function submit(expression) {
    const loaded = once("Page.loadEventFired", sessionId);
    await evaluate(expression);
    await loaded;
  }

  async function expectPage(page) {
    const actual = await evaluate("document.body.dataset.page");
    if (actual !== page) {
      const diagnostic = await evaluate(
        "({location: location.href, body: document.body.textContent.slice(0, 500)})",
      );
      throw new Error(
        `Expected page ${page}, received ${actual}: ${JSON.stringify(diagnostic)}`,
      );
    }
  }

  const routes = {
    "/": "dashboard",
    "/operators": "operators",
    "/templates/single_stock_daily_causal/1": "template-detail",
    "/experiments/new": "experiment-new",
    "/history": "history",
  };
  for (const scriptsDisabled of [false, true]) {
    console.error(`browser-mode scriptsDisabled=${scriptsDisabled}`);
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
        await navigate(route);
        const value = await evaluate(`({
              page: document.body.dataset.page,
              hasMain: Boolean(document.querySelector("main")),
              hasPrimaryAction: ${route === "/experiments/new"
                ? 'Boolean(document.querySelector(\'form[data-testid="experiment-form"] button[type="submit"]\'))'
                : "true"},
              documentWidth: document.documentElement.scrollWidth,
              viewportWidth: window.innerWidth
            })`);
        if (value.page !== expectedPage || !value.hasMain || !value.hasPrimaryAction) {
          throw new Error(`Browser selector failure for ${route}: ${JSON.stringify(value)}`);
        }
        if (value.documentWidth > value.viewportWidth) {
          throw new Error(`Horizontal page overflow for ${route} at ${width}px`);
        }
      }
    }

    await navigate("/operators/submit");
    console.error("stage operator-submit");
    await send(
      "Input.dispatchKeyEvent",
      { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
      sessionId,
    );
    await send(
      "Input.dispatchKeyEvent",
      { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
      sessionId,
    );
    const focused = await evaluate(
      "document.activeElement !== document.body && document.activeElement.matches('a,button,input,select,textarea')",
    );
    if (!focused) throw new Error("Keyboard focus did not reach an interactive control");

    const operatorValues = {
      operator_id: "browser_fit",
      version: "1.0.0",
      slot: "fit",
      title_zh: "浏览器拟合",
      summary_zh: "真实浏览器隔离提交流程。",
      source:
        "OPERATOR_API_VERSION = 1\nSLOT = 'fit'\n\ndef apply(payload, parameters):\n    return payload['values'][-1]\n",
      parameter_schema:
        '{"type":"object","properties":{},"required":[],"additionalProperties":false}',
      defaults: "{}",
      tests:
        '[{"input":{"values":[1.0,2.0]},"parameters":{},"expected":2.0}]',
      documentation: "# Browser fit\n\nDeterministic browser acceptance fixture.",
    };
    await submit(`(() => {
      const values = ${JSON.stringify(operatorValues)};
      for (const [name, value] of Object.entries(values)) {
        document.querySelector('[name="' + name + '"]').value = value;
      }
      document.querySelector('form[data-testid="operator-form"]').submit();
    })()`);
    await expectPage("operator-detail");
    console.error("stage operator-published");

    await navigate("/experiments/new");
    console.error("stage experiment-new");
    const generated = await evaluate(
      "document.querySelectorAll('[data-testid^=\"generated-params-\"]').length >= 7",
    );
    if (!generated) throw new Error("Schema-generated parameter controls are missing");
    if (scriptsDisabled) {
      await evaluate(`(() => {
        for (const selector of document.querySelectorAll("[data-operator-selector]")) {
          const explicit = Array.from(selector.options).find((option) => !option.value.endsWith("@latest"));
          selector.value = explicit.value;
        }
      })()`);
    }
    await submit(
      'document.querySelector(\'form[data-testid="experiment-form"]\').submit()',
    );
    await expectPage("experiment-detail");
    console.error("stage experiment-created");
    const experimentPath = await evaluate("location.pathname");
    const hasPending = await evaluate(
      'document.querySelector(\'[data-testid="attempt-timeline"]\').textContent.includes("PENDING")',
    );
    if (!hasPending) throw new Error("Experiment progress state is missing");

    await navigate("/experiments/new");
    if (scriptsDisabled) {
      await evaluate(`(() => {
        for (const selector of document.querySelectorAll("[data-operator-selector]")) {
          const explicit = Array.from(selector.options).find((option) => !option.value.endsWith("@latest"));
          selector.value = explicit.value;
        }
      })()`);
    }
    await submit(`(() => {
      const form = document.querySelector('form[data-testid="experiment-form"]');
      form.action = "/experiments/preview";
      form.submit();
    })()`);
    await expectPage("experiment-preview");
    console.error("stage duplicate-preview");
    const duplicate = await evaluate(
      'document.querySelector(\'[data-testid="duplicate-preview"] h1\').textContent.includes("Existing")',
    );
    if (!duplicate) throw new Error("Duplicate preview did not detect the existing identity");

    await navigate(experimentPath);
    await submit('document.querySelector(\'[data-testid="rerun-form"]\').submit()');
    await expectPage("experiment-detail");
    console.error("stage experiment-rerun");
    const rerunVisible = await evaluate(
      'document.querySelector(\'[data-testid="attempt-timeline"]\').textContent.includes("#2")',
    );
    if (!rerunVisible) throw new Error("Rerun attempt is missing from the timeline");

    await navigate("/history");
    const historyHasExperiment = await evaluate(
      `document.body.textContent.includes(${JSON.stringify(experimentPath.slice(-64))})`,
    );
    if (!historyHasExperiment) throw new Error("Experiment history is missing the submitted identity");
    console.error("stage history");

    await navigate(`/experiments/${reportExperimentId}`);
    const sandbox = await evaluate(
      'document.querySelector("iframe").getAttribute("sandbox")',
    );
    if (sandbox !== "allow-scripts") {
      throw new Error(`Report sandbox is invalid: ${sandbox}`);
    }
    console.error("stage report-sandbox");
  }
} finally {
  socket.close();
  browser.kill("SIGTERM");
  await rm(profile, { recursive: true, force: true });
}
