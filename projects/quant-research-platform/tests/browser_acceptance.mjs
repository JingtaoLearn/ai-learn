"use strict";

import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [baseUrl, sessionCookie, chromium, reportExperimentId, studyFormJson] =
  process.argv.slice(2);
if (!baseUrl || !sessionCookie || !chromium || !reportExperimentId || !studyFormJson) {
  throw new Error(
    "usage: browser_acceptance.mjs BASE_URL SESSION_COOKIE CHROMIUM REPORT_EXPERIMENT_ID STUDY_FORM_JSON",
  );
}
const studyFormValues = JSON.parse(studyFormJson);

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
  const timeout = setTimeout(() => reject(new Error(`Chromium startup timed out: ${stderr}`)), 60000);
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
    const evaluation = await send(
      "Runtime.evaluate",
      { expression, returnByValue: true },
      sessionId,
    );
    if (evaluation.exceptionDetails) {
      throw new Error(`Browser evaluation failed: ${JSON.stringify(evaluation.exceptionDetails)}`);
    }
    return evaluation.result.value;
  }

  async function navigate(path) {
    const loaded = once("Page.loadEventFired", sessionId);
    await send("Page.navigate", { url: `${baseUrl}${path}` }, sessionId);
    await loaded;
  }

  async function keyboardActivate(selector) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    const loaded = once("Page.loadEventFired", sessionId);
    await send(
      "Input.dispatchKeyEvent",
      { type: "rawKeyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
      sessionId,
    );
    await send(
      "Input.dispatchKeyEvent",
      {
        type: "char",
        key: "Enter",
        code: "Enter",
        text: "\r",
        unmodifiedText: "\r",
        windowsVirtualKeyCode: 13,
      },
      sessionId,
    );
    await send(
      "Input.dispatchKeyEvent",
      { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
      sessionId,
    );
    await loaded;
  }

  async function selectAndWaitForPreview(selectorQuery, value) {
    const evaluation = await send(
      "Runtime.evaluate",
      {
        expression: `new Promise((resolve, reject) => {
          const status = document.querySelector('[data-testid="live-duplicate-preview"]');
          const timeout = setTimeout(
            () => reject(new Error("preview-settled event timed out: " + status.textContent)),
            15000,
          );
          status.addEventListener("quant:preview-settled", (event) => {
            clearTimeout(timeout);
            resolve(event.detail);
          }, { once: true });
          const selector = document.querySelector(${JSON.stringify(selectorQuery)});
          selector.value = ${JSON.stringify(value)};
          selector.dispatchEvent(new Event("change", { bubbles: true }));
        })`,
        awaitPromise: true,
        returnByValue: true,
      },
      sessionId,
    );
    if (evaluation.exceptionDetails) {
      throw new Error(`Live preview failed: ${JSON.stringify(evaluation.exceptionDetails)}`);
    }
    return evaluation.result.value;
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

  async function assertLayout(label, width) {
    const layout = await evaluate(`(() => {
      const clipped = Array.from(document.querySelectorAll("main *")).filter((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" &&
          style.visibility !== "hidden" &&
          rect.width > 0 &&
          !element.closest(".table-wrap") &&
          (rect.left < -1 || rect.right > window.innerWidth + 1);
      }).slice(0, 5).map((element) => ({
        tag: element.tagName,
        className: element.className,
        text: element.textContent.trim().slice(0, 60),
        rect: element.getBoundingClientRect().toJSON(),
      }));
      const shortTargets = ${width === 390
        ? `Array.from(document.querySelectorAll(
            "main button, main .button, main input:not([type=hidden]), main select, main textarea"
          )).filter((element) => {
            const rect = element.getBoundingClientRect();
            return getComputedStyle(element).display !== "none" &&
              rect.width > 0 && rect.height > 0 && rect.height < 43;
          }).slice(0, 5).map((element) => ({
            tag: element.tagName,
            name: element.getAttribute("name"),
            height: element.getBoundingClientRect().height,
          }))`
        : "[]"};
      return {
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        clipped,
        shortTargets,
      };
    })()`);
    if (
      layout.documentWidth > layout.viewportWidth ||
      layout.clipped.length ||
      layout.shortTargets.length
    ) {
      throw new Error(`Layout failure for ${label} at ${width}px: ${JSON.stringify(layout)}`);
    }
  }

  async function assertDangerContrast(width) {
    for (const [theme, preference] of [
      ["dark", "dark"],
      ["light", "light"],
      ["system", "light"],
      ["system", "dark"],
    ]) {
      await send(
        "Emulation.setEmulatedMedia",
        { features: [{ name: "prefers-color-scheme", value: preference }] },
        sessionId,
      );
      await evaluate(
        `document.documentElement.dataset.theme = ${JSON.stringify(theme)}`,
      );
      const point = await evaluate(`(() => {
        const button = document.querySelector(".danger-button");
        const box = button.getBoundingClientRect();
        return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
      })()`);
      const ratio = async () => evaluate(`(() => {
        const parse = (value) => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
        const luminance = (color) => {
          const channels = parse(color).map((value) => {
            const normalized = value / 255;
            return normalized <= 0.04045
              ? normalized / 12.92
              : ((normalized + 0.055) / 1.055) ** 2.4;
          });
          return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
        };
        const style = getComputedStyle(document.querySelector(".danger-button"));
        const foreground = luminance(style.color);
        const background = luminance(style.backgroundColor);
        return (Math.max(foreground, background) + 0.05) /
          (Math.min(foreground, background) + 0.05);
      })()`);
      const normal = await ratio();
      await send(
        "Input.dispatchMouseEvent",
        { type: "mouseMoved", x: point.x, y: point.y },
        sessionId,
      );
      const hover = await ratio();
      await send(
        "Input.dispatchMouseEvent",
        { type: "mouseMoved", x: 0, y: 0 },
        sessionId,
      );
      if (normal < 4.5 || hover < 4.5) {
        throw new Error(
          `Cancel contrast failed for ${theme}/${preference} at ${width}px: ` +
          `${JSON.stringify({ normal, hover })}`,
        );
      }
    }
  }

  async function runStudyLifecycle(width, scriptsDisabled) {
    await send(
      "Emulation.setDeviceMetricsOverride",
      { width, height: 844, deviceScaleFactor: 1, mobile: width === 390 },
      sessionId,
    );
    await navigate("/studies/new");
    if (scriptsDisabled) {
      const fieldsetsAreExplicit = await evaluate(`(() => {
        const controls = Array.from(document.querySelectorAll(
          '[name^="operator_"][name*="_param__"], [name^="search__"]',
        ));
        return controls.length > 0 && controls.every((control) => {
          const fieldset = control.closest("fieldset.parameter-set");
          return fieldset && getComputedStyle(fieldset).display !== "none" &&
            fieldset.getClientRects().length > 0;
        });
      })()`);
      if (!fieldsetsAreExplicit) {
        throw new Error("No-JS Study controls escaped a visible operator-version fieldset");
      }
    }
    const invalidLoaded = once("Page.loadEventFired", sessionId);
    await evaluate(`(() => {
      const form = document.querySelector('[data-testid="study-form"]');
      Object.assign(form.elements, {});
      const values = ${JSON.stringify(studyFormValues)};
      for (const [name, value] of Object.entries(values)) {
        if (form.elements.namedItem(name)) form.elements.namedItem(name).value = value;
      }
      form.elements.namedItem(
        "search__fit__prior_log_ols__1.0.0__window_sessions",
      ).value = "[2,";
      form.submit();
    })()`);
    await invalidLoaded;
    await expectPage("study-new");
    const invalidField = await evaluate(`(() => {
      const name = "search__fit__prior_log_ols__1.0.0__window_sessions";
      const field = document.getElementById(name);
      const link = document.querySelector('.validation-summary a[href="#' + name + '"]');
      return {
        invalid: field?.getAttribute("aria-invalid"),
        describedBy: field?.getAttribute("aria-describedby"),
        linked: Boolean(link),
        focused: document.activeElement === field,
      };
    })()`);
    if (
      invalidField.invalid !== "true" ||
      invalidField.describedBy !==
        "search__fit__prior_log_ols__1.0.0__window_sessions-error" ||
      !invalidField.linked ||
      !invalidField.focused
    ) {
      throw new Error(
        `Finite-range error is not field-accessible at ${width}px: ${JSON.stringify(invalidField)}`,
      );
    }
    const loaded = once("Page.loadEventFired", sessionId);
    await evaluate(`(() => {
      const values = Object.fromEntries(new FormData(
        document.querySelector('[data-testid="study-form"]')
      ));
      Object.assign(values, ${JSON.stringify(studyFormValues)}, {
        seed: String(
          ${JSON.stringify(`${scriptsDisabled ? "nojs" : "js"}-${width}`)}
            .split("").reduce((total, character) => total + character.charCodeAt(0), 0)
        ),
        unique_trial_budget: "1", max_suggestions: "1",
        outer_folds: "1", inner_folds: "1", scoring_sessions: "1",
        minimum_training_sessions: "2", purge_sessions: "0",
        holdout_sessions: "1", evaluation_version: "1.0.0",
        parent_study_ids: "", prior_unique_candidate_count: "0",
        lineage_complete: "true",
        "search__fit__prior_log_ols__1.0.0__window_sessions": "[2,3]",
      });
      const submission = document.createElement("form");
      submission.method = "post";
      submission.action = "/studies/preview";
      for (const [name, value] of Object.entries(values)) {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        submission.append(input);
      }
      document.body.append(submission);
      submission.submit();
    })()`);
    await loaded;
    await expectPage("study-preview");
    const previewIsPlan = await evaluate(
      `(() => {
        const text = document.body.textContent;
        return text.includes("Planned outer OOS") &&
          text.includes("Minimum Experiment bindings") &&
          text.includes("Conditional maximum bindings") &&
          text.includes("Complete frozen Study plan") &&
          !text.includes("Assumed reuse") &&
          !text.includes("Observed outer OOS evidence");
      })()`,
    );
    if (!previewIsPlan) throw new Error("Study preview did not render the frozen protocol and canonical estimate");
    await assertLayout("study preview", width);
    await keyboardActivate('form[action="/studies/edit"] button[type="submit"]');
    await expectPage("study-new");
    const restored = await evaluate(`(() => {
      const form = document.querySelector('[data-testid="study-form"]');
      return {
        range: form.elements.namedItem(
          "search__fit__prior_log_ols__1.0.0__window_sessions",
        ).value,
        seed: form.elements.namedItem("seed").value,
        start: form.elements.namedItem("start_date").value,
        end: form.elements.namedItem("end_date").value,
        lineage: form.elements.namedItem("lineage_complete").value,
      };
    })()`);
    if (
      restored.range !== "[2,3]" ||
      restored.seed !== String(
        `${scriptsDisabled ? "nojs" : "js"}-${width}`
          .split("").reduce((total, character) => total + character.charCodeAt(0), 0)
      ) ||
      !restored.start ||
      !restored.end ||
      restored.lineage !== "true"
    ) {
      throw new Error(`Edit plan lost wizard values at ${width}px: ${JSON.stringify(restored)}`);
    }
    await keyboardActivate('[data-testid="preview-study"]');
    await expectPage("study-preview");
    await keyboardActivate('form[action="/studies"] button[type="submit"]');
    await expectPage("study-preview");
    if (!(await evaluate('Boolean(document.querySelector(\'[data-testid="stale-preview"]\'))'))) {
      throw new Error("Study stale-preview recovery was not rendered");
    }
    await keyboardActivate('form[action="/studies"] button[type="submit"]');
    await expectPage("study-detail");
    const studyPath = await evaluate("location.pathname");
    await assertLayout("study detail", width);
    await assertDangerContrast(width);
    await keyboardActivate('form[action$="/advance"] button[type="submit"]');
    await expectPage("study-detail");
    const advanceOutcome = await evaluate(
      'document.querySelector(\'[data-testid="study-outcome"]\')?.textContent',
    );
    if (!advanceOutcome) throw new Error("Study advance outcome is missing");
    await keyboardActivate('form[action$="/control"] input[value="PAUSE"] + input + button');
    await expectPage("study-detail");
    const outcome = await evaluate(
      'document.querySelector(\'[data-testid="study-outcome"]\')?.textContent',
    );
    if (!outcome?.includes("paused") || (await evaluate("location.pathname")) !== studyPath) {
      throw new Error(`Study control outcome is missing: ${outcome}`);
    }
    await keyboardActivate('a[href$="/report"]');
    await expectPage("study-report");
    const reportIsPlan = await evaluate(
      'document.body.textContent.includes("Planned outer OOS windows") && !document.body.textContent.includes("Verified report")',
    );
    if (!reportIsPlan) throw new Error("Study report evidence semantics are invalid");
    await assertLayout("study report", width);
  }

  const routes = {
    "/": "dashboard",
    "/operators": "operators",
    "/templates/single_stock_daily_causal/1": "template-detail",
    "/experiments/new": "experiment-new",
    "/studies": "studies",
    "/studies/new": "study-new",
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
                : route === "/studies/new"
                ? 'Boolean(document.querySelector(\'form[data-testid="study-form"] button[type="submit"]\'))'
                : "true"},
              hasThemeSelector: Boolean(document.querySelector("[data-theme-selector]")),
              documentWidth: document.documentElement.scrollWidth,
              viewportWidth: window.innerWidth
            })`);
        if (
          value.page !== expectedPage ||
          !value.hasMain ||
          !value.hasPrimaryAction ||
          !value.hasThemeSelector
        ) {
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
    operatorValues.parameter_schema =
      '{"type":"object","properties":{"choice":{"type":"string","enum":[null,"","strict"],"nullable":true},"count":{"type":"integer","enum":[1,2]},"enabled":{"type":"boolean","enum":[true,false]},"ratio":{"type":"number","enum":[1.5,2.5]}},"required":["choice","count","enabled","ratio"],"additionalProperties":false}';
    operatorValues.defaults = '{"choice":"","count":1,"enabled":true,"ratio":1.5}';
    operatorValues.tests =
      '[{"input":{"values":[1.0,2.0]},"parameters":{"choice":"","count":1,"enabled":true,"ratio":1.5},"expected":2.0}]';
    await evaluate(`(() => {
      const values = ${JSON.stringify(operatorValues)};
      for (const [name, value] of Object.entries(values)) {
        document.querySelector('[name="' + name + '"]').value = value;
      }
    })()`);
    await keyboardActivate('form[data-testid="operator-form"] button[type="submit"]');
    await expectPage("operator-detail");
    const operatorDetailComplete = await evaluate(`[
      "operator-version-context",
      "operator-parameter-schema",
      "operator-defaults",
      "operator-validation-evidence",
      "operator-version-history"
    ].every((id) => document.querySelector('[data-testid="' + id + '"]'))`);
    if (!operatorDetailComplete) throw new Error("Operator detail contract is incomplete");
    console.error("stage operator-published");

    await navigate("/experiments/new");
    console.error("stage experiment-new");
    if (!scriptsDisabled) {
      const novelPreview = await selectAndWaitForPreview(
        '[data-operator-selector="fit"]',
        "browser_fit@latest",
      );
      if (novelPreview.state !== "new") {
        throw new Error(`Novel live preview did not resolve: ${JSON.stringify(novelPreview)}`);
      }
    }
    const generated = await evaluate(
      "document.querySelectorAll('[data-testid^=\"generated-params-\"]').length >= 7",
    );
    if (!generated) throw new Error("Schema-generated parameter controls are missing");
    const adaptiveControls = await evaluate(`[
      "template_initial_state",
      "template_terminal_handling",
      "operator_fit_param__prior_log_ols__1.0.0__price_column",
      "operator_fit_param__browser_fit__1.0.0__choice",
      "operator_fit_param__browser_fit__1.0.0__count",
      "operator_fit_param__browser_fit__1.0.0__enabled",
      "operator_fit_param__browser_fit__1.0.0__ratio"
    ].every((name) => document.querySelector('[name="' + name + '"]')?.tagName === "SELECT")`);
    if (!adaptiveControls) throw new Error("Enum or boolean controls are not accessible selects");
    if (!scriptsDisabled) {
      const typedEnums = await evaluate(`(() => {
        const parameters = window.experimentTask().operators.fit.parameters;
        return parameters.choice === "" &&
          parameters.count === 1 &&
          parameters.enabled === true &&
          parameters.ratio === 1.5;
      })()`);
      if (!typedEnums) throw new Error("Live preview did not preserve typed enum values");
    }
    const datasetControls = await evaluate(`(() => {
      const dataset = document.querySelector('[name="dataset_id"]');
      const start = document.querySelector('[name="start_date"]');
      const end = document.querySelector('[name="end_date"]');
      return {
        dataset: dataset?.value,
        startType: start?.type,
        endType: end?.type,
        endValue: end?.value,
        endMax: end?.max,
        exposesSnapshot: Array.from(dataset?.options || []).some(
          (option) => /[0-9a-f]{64}/.test(option.value),
        ),
      };
    })()`);
    if (
      datasetControls.dataset !== "SYNTH.SS" ||
      datasetControls.startType !== "date" ||
      datasetControls.endType !== "date" ||
      !datasetControls.endValue ||
      datasetControls.endValue !== datasetControls.endMax ||
      datasetControls.exposesSnapshot
    ) {
      throw new Error(`Dataset/date controls are invalid: ${JSON.stringify(datasetControls)}`);
    }
    const summaryVisible = await evaluate(
      'Boolean(document.querySelector(\'[data-testid="resolved-summary"]\')) && Boolean(document.querySelector(\'[data-testid="live-duplicate-preview"]\'))',
    );
    if (!summaryVisible) throw new Error("Live resolution and duplicate states are missing");
    if (scriptsDisabled) {
      await evaluate(`(() => {
        for (const selector of document.querySelectorAll("[data-operator-selector]")) {
          const explicit = selector.dataset.operatorSelector === "fit"
            ? Array.from(selector.options).find((option) => option.value === "browser_fit@1.0.0")
            : Array.from(selector.options).find((option) => !option.value.endsWith("@latest"));
          selector.value = explicit.value;
        }
      })()`);
    }
    await keyboardActivate(
      'form[data-testid="experiment-form"] button[type="submit"]:not([formaction])',
    );
    await expectPage("experiment-detail");
    console.error("stage experiment-created");
    const experimentPath = await evaluate("location.pathname");
    const detailComplete = await evaluate(`[
      "experiment-dataset",
      "template-parameters",
      "operator-resolution",
      "canonical-metrics",
      "attempt-timeline"
    ].every((id) => document.querySelector('[data-testid="' + id + '"]'))`);
    if (!detailComplete) throw new Error("Experiment detail contract is incomplete");
    const hasPending = await evaluate(
      'document.querySelector(\'[data-testid="attempt-timeline"]\').textContent.includes("PENDING")',
    );
    if (!hasPending) throw new Error("Experiment progress state is missing");

    await navigate("/experiments/new");
    if (!scriptsDisabled) {
      const duplicatePreview = await selectAndWaitForPreview(
        '[data-operator-selector="fit"]',
        "browser_fit@latest",
      );
      if (duplicatePreview.state !== "duplicate") {
        throw new Error(
          `Live duplicate preview did not resolve: ${JSON.stringify(duplicatePreview)}`,
        );
      }
    }
    if (scriptsDisabled) {
      await evaluate(`(() => {
        for (const selector of document.querySelectorAll("[data-operator-selector]")) {
          const explicit = selector.dataset.operatorSelector === "fit"
            ? Array.from(selector.options).find((option) => option.value === "browser_fit@1.0.0")
            : Array.from(selector.options).find((option) => !option.value.endsWith("@latest"));
          selector.value = explicit.value;
        }
      })()`);
    }
    await keyboardActivate('[data-testid="preview-experiment"]');
    await expectPage("experiment-preview");
    console.error("stage duplicate-preview");
    const duplicate = await evaluate(
      'document.querySelector(\'[data-testid="duplicate-preview"] h1\').textContent.includes("Existing")',
    );
    if (!duplicate) throw new Error("Duplicate preview did not detect the existing identity");
    const previewComplete = await evaluate(
      'document.querySelectorAll("[data-preview-slot]").length === 7 && document.querySelector(\'[data-testid="preview-identity"]\').textContent.includes("Dataset snapshot")',
    );
    if (!previewComplete) throw new Error("Resolved preview audit is incomplete");

    await navigate(experimentPath);
    await keyboardActivate('[data-testid="rerun-form"] button');
    await expectPage("experiment-detail");
    console.error("stage experiment-rerun");
    const rerunVisible = await evaluate(
      'document.querySelector(\'[data-testid="attempt-timeline"]\').textContent.includes("#2")',
    );
    if (!rerunVisible) throw new Error("Rerun attempt is missing from the timeline");

    await navigate(
      `/history?status=PENDING&search=${experimentPath.slice(-64)}&drift=current`,
    );
    const historyHasExperiment = await evaluate(
      `document.body.textContent.includes(${JSON.stringify(experimentPath.slice(-64))})`,
    );
    if (!historyHasExperiment) throw new Error("Experiment history is missing the submitted identity");
    const filtersVisible = await evaluate(
      'Boolean(document.querySelector(\'[data-testid="history-filters"]\')) && document.querySelector("table").textContent.includes("Status")',
    );
    if (!filtersVisible) throw new Error("History filters or status column are missing");
    console.error("stage history");

    await navigate(`/experiments/${reportExperimentId}`);
    const canonicalMetrics = await evaluate(
      'document.querySelector(\'[data-testid="canonical-metrics"]\').textContent.includes("final_equity_cny")',
    );
    if (!canonicalMetrics) throw new Error("Canonical metrics are missing");
    const sandbox = await evaluate(
      'document.querySelector("iframe").getAttribute("sandbox")',
    );
    if (sandbox !== "allow-scripts") {
      throw new Error(`Report sandbox is invalid: ${sandbox}`);
    }
    console.error("stage report-sandbox");

    if (!scriptsDisabled) {
      await navigate("/");
      const keyboardTheme = await evaluate(`(() => {
        const selector = document.querySelector("[data-theme-selector]");
        selector.focus();
        return {
          focused: document.activeElement === selector,
          labelled: Boolean(selector.closest("label")),
        };
      })()`);
      if (!keyboardTheme.focused || !keyboardTheme.labelled) {
        throw new Error(`Theme selector is not keyboard accessible: ${JSON.stringify(keyboardTheme)}`);
      }
      const dark = await evaluate(`(() => {
        const selector = document.querySelector("[data-theme-selector]");
        selector.value = "dark";
        selector.dispatchEvent(new Event("change", { bubbles: true }));
        return {
          selected: document.documentElement.dataset.theme,
          stored: localStorage.getItem("quant-theme"),
          background: getComputedStyle(document.documentElement).backgroundColor,
        };
      })()`);
      if (dark.selected !== "dark" || dark.stored !== "dark") {
        throw new Error(`Dark theme did not persist: ${JSON.stringify(dark)}`);
      }
      await navigate("/history");
      const persisted = await evaluate(
        'document.documentElement.dataset.theme === "dark" && document.querySelector("[data-theme-selector]").value === "dark"',
      );
      if (!persisted) throw new Error("Theme did not persist across navigation");

      await evaluate(`(() => {
        const selector = document.querySelector("[data-theme-selector]");
        selector.value = "system";
        selector.dispatchEvent(new Event("change", { bubbles: true }));
      })()`);
      await send(
        "Emulation.setEmulatedMedia",
        { features: [{ name: "prefers-color-scheme", value: "light" }] },
        sessionId,
      );
      const systemLight = await evaluate(
        '({theme: document.documentElement.dataset.theme, stored: localStorage.getItem("quant-theme"), canvas: getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim()})',
      );
      await send(
        "Emulation.setEmulatedMedia",
        { features: [{ name: "prefers-color-scheme", value: "dark" }] },
        sessionId,
      );
      const systemDark = await evaluate(
        'getComputedStyle(document.documentElement).getPropertyValue("--canvas").trim()',
      );
      if (
        systemLight.theme !== "system" ||
        systemLight.stored !== "system" ||
        systemLight.canvas === systemDark
      ) {
        throw new Error(
          `System theme did not follow OS preference: ${JSON.stringify({ systemLight, systemDark })}`,
        );
      }
      console.error("stage theme-persistence");
    }
    for (const width of [390, 1280]) {
      await runStudyLifecycle(width, scriptsDisabled);
    }
  }
} finally {
  socket.close();
  if (browser.exitCode === null) {
    const exited = new Promise((resolve) => browser.once("exit", resolve));
    browser.kill("SIGTERM");
    await exited;
  }
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
