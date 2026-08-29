"use strict";

import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [
  baseUrl,
  sessionCookie,
  chromium,
  reportExperimentId,
  reportAttemptId,
  studyFormJson,
  completedStudyId,
  screenshotRoot,
  scope = "full",
] =
  process.argv.slice(2);
if (
  !baseUrl ||
  !sessionCookie ||
  !chromium ||
  !screenshotRoot ||
  !new Set(["foundation", "report", "full"]).has(scope) ||
  (scope !== "foundation" &&
    (!reportExperimentId || !reportAttemptId || !studyFormJson || !completedStudyId))
) {
  throw new Error(
    "usage: browser_acceptance.mjs BASE_URL SESSION_COOKIE CHROMIUM REPORT_EXPERIMENT_ID REPORT_ATTEMPT_ID STUDY_FORM_JSON COMPLETED_STUDY_ID SCREENSHOT_ROOT [foundation|report|full]",
  );
}
const studyFormValues = studyFormJson ? JSON.parse(studyFormJson) : {};

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

  async function capture(name) {
    const image = await send(
      "Page.captureScreenshot",
      { format: "png", fromSurface: true, captureBeyondViewport: false },
      sessionId,
    );
    await writeFile(join(screenshotRoot, name), Buffer.from(image.data, "base64"));
  }

  async function pressEnter() {
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
  }

  async function pressTab() {
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
  }

  async function keyboardActivate(selector) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    const loaded = once("Page.loadEventFired", sessionId);
    await pressEnter();
    await loaded;
  }

  async function keyboardToggle(selector) {
    await evaluate(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    await pressEnter();
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
      const shortTargets = ${width < 768
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

  async function assertShellContract(label, width, expectedTask, expectedMobile) {
      const contract = await evaluate(`(() => {
        const visible = (element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" &&
            rect.width > 0 && rect.height > 0;
        };
        const rect = (element) => element?.getBoundingClientRect();
        const rail = document.querySelector(".task-rail");
        const mobile = document.querySelector(".mobile-nav");
        const utility = document.querySelector(".utility-menu");
        utility.open = true;
        const panel = document.querySelector(".utility-panel");
        const masthead = document.querySelector(".masthead");
        const shell = document.querySelector("main.shell");
        const mobileLinks = Array.from(mobile.querySelectorAll("a"));
        const targets = Array.from(document.querySelectorAll(
          "a, button, summary, input:not([type=hidden]), select, textarea"
        )).filter(visible);
        const undersized = targets.filter((element) => {
          const effectiveTarget = element.matches('input[type="checkbox"]')
            ? element.closest("label")
            : element;
          const box = rect(effectiveTarget);
          return box.width < 43 || box.height < 43;
        }).slice(0, 8).map((element) => ({
          tag: element.tagName,
          text: element.textContent.trim().slice(0, 40),
          width: rect(
            element.matches('input[type="checkbox"]') ? element.closest("label") : element
          ).width,
          height: rect(
            element.matches('input[type="checkbox"]') ? element.closest("label") : element
          ).height,
        }));
        const hitTest = (element) => {
          const box = rect(element);
          const hit = document.elementFromPoint(
            Math.max(0, Math.min(innerWidth - 1, box.left + box.width / 2)),
            Math.max(0, Math.min(innerHeight - 1, box.top + box.height / 2)),
          );
          return hit === element || element.contains(hit);
        };
        const panelBox = rect(panel);
        const mobileBox = rect(mobile);
        const mastheadBox = rect(masthead);
        const utilityBox = rect(utility.querySelector("summary"));
        return {
          railVisible: visible(rail),
          railWidth: rect(rail)?.width,
          mobileVisible: visible(mobile),
          mobileCount: mobileLinks.length,
          mobileTargets: mobileLinks.map((link) => ({
            width: rect(link).width,
            height: rect(link).height,
            hit: hitTest(link),
          })),
          taskCurrent: rail.querySelector('[aria-current="page"]')?.textContent.trim(),
          mobileCurrent: mobile.querySelector('[aria-current="page"]')?.textContent.trim(),
          utilityVisible: visible(panel),
          utilityHit: hitTest(utility.querySelector("summary")),
          utilityHeight: utilityBox.height,
          utilityBelowMasthead: panelBox.top >= mastheadBox.bottom - 1,
          utilityClearsMobile: !visible(mobile) || panelBox.bottom <= mobileBox.top + 1,
          shellBottomPadding: Number.parseFloat(getComputedStyle(shell).paddingBottom),
          mobileHeight: visible(mobile) ? mobileBox.height : 0,
          undersized,
        };
      })()`);
      const mobileExpected = width < 768;
      if (
        contract.mobileVisible !== mobileExpected ||
        contract.railVisible === mobileExpected ||
        contract.mobileCount !== 5 ||
        contract.taskCurrent !== expectedTask ||
        contract.mobileCurrent !== expectedMobile ||
        !contract.utilityVisible ||
        !contract.utilityHit ||
        contract.utilityHeight < 44 ||
        !contract.utilityBelowMasthead ||
        !contract.utilityClearsMobile ||
        contract.undersized.length ||
        (mobileExpected && contract.mobileTargets.some(
          (target) => target.width < 44 || target.height < 44 || !target.hit,
        )) ||
        (mobileExpected &&
          contract.shellBottomPadding < contract.mobileHeight + 16) ||
        (width >= 1024 && Math.abs(contract.railWidth - 240) > 1)
      ) {
        throw new Error(
          `Shell contract failed for ${label} at ${width}px: ${JSON.stringify(contract)}`,
        );
      }
      await evaluate('document.querySelector(".utility-menu").open = false');
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

  async function installSessionCookie() {
    const installed = await send(
      "Network.setCookie",
      {
        name: "quant_session",
        value: sessionCookie,
        url: baseUrl,
        httpOnly: true,
        sameSite: "Lax",
      },
      sessionId,
    );
    if (!installed.success) throw new Error("Could not restore authenticated browser cookie");
  }

  async function runFoundationScenarios() {
    for (const scriptsDisabled of [false, true]) {
      await send(
        "Emulation.setScriptExecutionDisabled",
        { value: scriptsDisabled },
        sessionId,
      );
      await send(
        "Emulation.setDeviceMetricsOverride",
        { width: 390, height: 844, deviceScaleFactor: 1, mobile: true },
        sessionId,
      );

      await send(
        "Network.deleteCookies",
        { name: "quant_session", url: baseUrl },
        sessionId,
      );
      await navigate("/login");
      await expectPage("login");
      if (!(await evaluate('Boolean(document.querySelector("[data-testid=login-panel]"))'))) {
        throw new Error("focused-login: login panel is missing");
      }

      await installSessionCookie();
      await navigate("/");
      await expectPage("dashboard");
      if (!(await evaluate('Boolean(document.querySelector("[data-testid=dashboard-empty]"))'))) {
        throw new Error("focused-empty-dashboard: empty state is missing");
      }

      await evaluate("document.activeElement?.blur()");
      await pressTab();
      const skipFocused = await evaluate(
        'document.activeElement?.classList.contains("skip-link")',
      );
      if (!skipFocused) throw new Error("focused-skip-link: skip link was not first");
      await pressEnter();
      const mainFocused = await evaluate(
        'document.activeElement?.matches("[data-testid=main-content]")',
      );
      if (!mainFocused) {
        throw new Error("focused-skip-link: traversal did not focus main content");
      }

      await evaluate('document.querySelector(".utility-menu").open = true');
      await keyboardActivate('form[action="/logout"] button[type="submit"]');
      await expectPage("login");
      const loggedOutCookies = await send(
        "Network.getCookies",
        { urls: [baseUrl] },
        sessionId,
      );
      if (loggedOutCookies.cookies.some((item) => item.name === "quant_session")) {
        throw new Error("focused-post-logout: session cookie remains");
      }
      await installSessionCookie();

      await navigate("/");
      await send(
        "Emulation.setEmulatedMedia",
        { features: [{ name: "forced-colors", value: "active" }] },
        sessionId,
      );
      const forcedColors = await evaluate(`({
        active: matchMedia("(forced-colors: active)").matches,
        currentBorder: getComputedStyle(
          document.querySelector('.mobile-nav [aria-current="page"]')
        ).borderTopWidth,
      })`);
      if (!forcedColors.active || forcedColors.currentBorder !== "2px") {
        throw new Error(`forced-colors scenario failed: ${JSON.stringify(forcedColors)}`);
      }

      await send(
        "Emulation.setEmulatedMedia",
        { features: [{ name: "prefers-reduced-motion", value: "reduce" }] },
        sessionId,
      );
      const reducedMotion = await evaluate(`({
        active: matchMedia("(prefers-reduced-motion: reduce)").matches,
        transition: getComputedStyle(document.querySelector("button")).transitionDuration,
        animation: getComputedStyle(document.querySelector("button")).animationDuration,
      })`);
      if (
        !reducedMotion.active ||
        reducedMotion.transition !== "0s" ||
        reducedMotion.animation !== "0s"
      ) {
        throw new Error(
          `prefers-reduced-motion scenario failed: ${JSON.stringify(reducedMotion)}`,
        );
      }
      await send("Emulation.setEmulatedMedia", { features: [] }, sessionId);
    }
  }

  async function runReportScenario(scriptsDisabled) {
    await send(
      "Emulation.setScriptExecutionDisabled",
      { value: scriptsDisabled },
      sessionId,
    );
    await send(
      "Emulation.setDeviceMetricsOverride",
      { width: 1280, height: 844, deviceScaleFactor: 1, mobile: false },
      sessionId,
    );
    await navigate(`/reports/${reportAttemptId}`);
    await expectPage("report-wrapper");
    const report = await evaluate(`({
      canonical: document.querySelector(".report-toolbar strong")?.textContent,
      fullscreen: Boolean(document.querySelector("[data-fullscreen-report]")),
      sandbox: document.querySelector("[data-testid=report-frame]")?.getAttribute("sandbox"),
    })`);
    if (
      report.canonical !== "Verified canonical report" ||
      !report.fullscreen ||
      report.sandbox !== "allow-scripts"
    ) {
      throw new Error(`focused-report-wrapper: ${JSON.stringify(report)}`);
    }
    if (!scriptsDisabled) {
      await keyboardToggle("[data-fullscreen-report]");
      const fullscreen = await evaluate(
        'document.fullscreenElement?.matches("[data-testid=report-frame]")',
      );
      if (!fullscreen) {
        throw new Error("focused-report-wrapper: full-screen action did not activate");
      }
      await evaluate("document.exitFullscreen()");
    }
  }

  async function runViewportProxies(scriptsDisabled) {
    await send(
      "Emulation.setScriptExecutionDisabled",
      { value: scriptsDisabled },
      sessionId,
    );
    await send(
      "Emulation.setDeviceMetricsOverride",
      { width: 320, height: 640, deviceScaleFactor: 2, mobile: true },
      sessionId,
    );
    await navigate("/");
    await assertLayout("320px DPR2 reflow", 320);
    await assertShellContract("320px DPR2 reflow", 320, "Overview", "Overview");
    const density = await evaluate("({width: innerWidth, devicePixelRatio})");
    if (density.width !== 320 || density.devicePixelRatio < 1.9) {
      throw new Error(`320px DPR2 reflow failed: ${JSON.stringify(density)}`);
    }
    if (!scriptsDisabled) await capture("overview-320-dpr2.png");

    await send(
      "Emulation.setDeviceMetricsOverride",
      { width: 320, height: 640, deviceScaleFactor: 1, mobile: true },
      sessionId,
    );
    await navigate("/");
    await evaluate('document.documentElement.style.fontSize = "200%"');
    await assertLayout("200% text-resize proxy", 320);
    await assertShellContract("200% text-resize proxy", 320, "Overview", "Overview");
    await evaluate('document.querySelector(".utility-menu").open = true');
    const utilitiesAvailable = await evaluate(`[
      '[data-theme-selector]',
      'form[action="/logout"] button',
      'a[href="/templates/single_stock_daily_causal/1"]'
    ].every((selector) => {
      const element = document.querySelector(selector);
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    })`);
    if (!utilitiesAvailable) {
      throw new Error("200% text-resize proxy did not preserve utility functions");
    }
    if (!scriptsDisabled) await capture("overview-320-text-resize-200.png");
    await evaluate('document.documentElement.style.removeProperty("font-size")');
  }

  async function runStudyLifecycle(width, scriptsDisabled) {
    await send(
      "Emulation.setDeviceMetricsOverride",
      { width, height: 844, deviceScaleFactor: 1, mobile: width === 390 },
      sessionId,
    );
    await navigate("/studies/new");
    if (!scriptsDisabled) {
      const toggleState = await evaluate(`(() => {
        const name = "study__fit__prior_log_ols__1.0.0__window_sessions";
        const checkbox = document.querySelector('[name="' + name + '"]');
        const editor = document.querySelector('[data-domain-editor="' + name + '"]');
        const field = document.querySelector(
          '[name="search__fit__prior_log_ols__1.0.0__window_sessions"]',
        );
        const initial = {
          hidden: editor.hidden,
          disabled: field.disabled,
          expanded: checkbox.getAttribute("aria-expanded"),
        };
        checkbox.checked = true;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        const selected = {
          hidden: editor.hidden,
          disabled: field.disabled,
          expanded: checkbox.getAttribute("aria-expanded"),
        };
        field.value = "[2,3]";
        checkbox.checked = false;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
        const cleared = {
          hidden: editor.hidden,
          disabled: field.disabled,
          expanded: checkbox.getAttribute("aria-expanded"),
        };
        return { initial, selected, cleared };
      })()`);
      if (
        !toggleState.initial.hidden || !toggleState.initial.disabled ||
        toggleState.initial.expanded !== "false" ||
        toggleState.selected.hidden || toggleState.selected.disabled ||
        toggleState.selected.expanded !== "true" ||
        !toggleState.cleared.hidden || !toggleState.cleared.disabled ||
        toggleState.cleared.expanded !== "false"
      ) {
        throw new Error(`Study parameter toggle is unsafe: ${JSON.stringify(toggleState)}`);
      }
    }
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
        "study__fit__prior_log_ols__1.0.0__window_sessions",
      ).checked = true;
      form.elements.namedItem(
        "study__fit__prior_log_ols__1.0.0__window_sessions",
      ).dispatchEvent(new Event("change", { bubbles: true }));
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
        "study__fit__prior_log_ols__1.0.0__window_sessions": "int",
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

  async function inspectCompletedStudy(width) {
    await send(
      "Emulation.setDeviceMetricsOverride",
      { width, height: 844, deviceScaleFactor: 1, mobile: width === 390 },
      sessionId,
    );
    const detailPath = `/studies/${completedStudyId}`;
    await navigate(detailPath);
    await expectPage("study-detail");
    const detail = await evaluate(`(() => {
      const firstRanking = document.querySelector('[data-testid="ranking-1"]');
      const text = document.body.textContent;
      return {
        terminal: Boolean(document.querySelector('[data-testid="terminal-study-state"]')),
        activeControls: Boolean(document.querySelector(
          'form[action$="/advance"], form[action$="/control"]'
        )),
        rankingCount: document.querySelectorAll('[data-testid^="ranking-"]').length,
        bindingCount: document.querySelectorAll('.study-ranking-table .binding').length,
        studiedValuesLead:
          firstRanking?.cells[0]?.textContent.includes("/operators/fit/window_sessions"),
        evidence:
          text.includes("TIE_BROKEN_BY_FROZEN_RULE") &&
          text.includes("DIVERGENT") &&
          text.includes("NOT_ESTABLISHED"),
      };
    })()`);
    if (
      !detail.terminal ||
      detail.activeControls ||
      detail.rankingCount !== 6 ||
      detail.bindingCount !== 39 ||
      !detail.studiedValuesLead ||
      !detail.evidence
    ) {
      throw new Error(
        `Completed Study evidence is incomplete at ${width}px: ${JSON.stringify(detail)}`,
      );
    }
    await assertLayout("completed study detail", width);
    await keyboardActivate(`a[href="${detailPath}/report"]`);
    await expectPage("study-report");
    const report = await evaluate(`(() => {
      const text = document.body.textContent;
      return {
        evidence:
          text.includes("/operators/fit/window_sessions") &&
          text.includes("TIE_BROKEN_BY_FROZEN_RULE") &&
          text.includes("DIVERGENT") &&
          text.includes("NOT_ESTABLISHED"),
        activeControls: Boolean(document.querySelector(
          'form[action$="/advance"], form[action$="/control"]'
        )),
      };
    })()`);
    if (!report.evidence || report.activeControls) {
      throw new Error(
        `Completed Study report is incomplete at ${width}px: ${JSON.stringify(report)}`,
      );
    }
    await assertLayout("completed study report", width);
    await keyboardActivate(`a[href="${detailPath}"]`);
    await expectPage("study-detail");
  }

  if (scope === "foundation") {
    await runFoundationScenarios();
  }

  if (scope === "report") {
    for (const scriptsDisabled of [false, true]) {
      await runReportScenario(scriptsDisabled);
      await runViewportProxies(scriptsDisabled);
    }
  }

  if (scope === "full") {
  const routes = {
    "/": { page: "dashboard", task: "Overview", mobile: "Overview" },
    "/operators": { page: "operators", task: "Operators", mobile: "Operators" },
    "/templates/single_stock_daily_causal/1": {
      page: "template-detail", task: "Template", mobile: "Operators",
    },
    "/experiments/new": {
      page: "experiment-new", task: "New experiment", mobile: "New experiment",
    },
    "/studies": { page: "studies", task: "Studies", mobile: "Studies" },
    "/studies/new": { page: "study-new", task: "New Study", mobile: "Studies" },
    "/history": { page: "history", task: "History", mobile: "History" },
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
      for (const [route, expected] of Object.entries(routes)) {
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
          value.page !== expected.page ||
          !value.hasMain ||
          !value.hasPrimaryAction ||
          !value.hasThemeSelector
        ) {
          throw new Error(`Browser selector failure for ${route}: ${JSON.stringify(value)}`);
        }
        if (value.documentWidth > value.viewportWidth) {
          throw new Error(`Horizontal page overflow for ${route} at ${width}px`);
        }
        await assertShellContract(route, width, expected.task, expected.mobile);
      }

    }

    for (const width of [767, 768, 1023, 1024]) {
      await send(
        "Emulation.setDeviceMetricsOverride",
        { width, height: 844, deviceScaleFactor: 1, mobile: width < 768 },
        sessionId,
      );
      await navigate("/");
      await assertShellContract("responsive boundary", width, "Overview", "Overview");
    }

    await navigate("/history?search=definitely-not-found");
    if (!(await evaluate('Boolean(document.querySelector("[data-testid=history-empty]"))'))) {
      throw new Error("Representative browser empty state is missing");
    }
    await navigate("/studies/not-a-study");
    await expectPage("error");
    if (!(await evaluate('Boolean(document.querySelector("[data-testid=error-state]"))'))) {
      throw new Error("Representative browser error state is missing");
    }

    for (const theme of ["light", "dark", "system"]) {
      await navigate(`/?theme=${theme}`);
      const selected = await evaluate("document.documentElement.dataset.theme");
      if (selected !== theme) {
        throw new Error(`No-JS theme persistence failed for ${theme}: ${selected}`);
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
      await keyboardToggle(".utility-menu > summary");
      if (!(await evaluate('document.querySelector(".utility-menu").open'))) {
        throw new Error("Utilities disclosure did not open from the keyboard");
      }
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
      await inspectCompletedStudy(width);
    }
  }

  for (const scriptsDisabled of [false, true]) {
    await runReportScenario(scriptsDisabled);
    await runViewportProxies(scriptsDisabled);
  }

  await send("Emulation.setScriptExecutionDisabled", { value: false }, sessionId);
  for (const theme of ["light", "dark"]) {
    for (const width of [390, 1440]) {
      await send(
        "Emulation.setDeviceMetricsOverride",
        { width, height: 900, deviceScaleFactor: 1, mobile: width === 390 },
        sessionId,
      );
      await navigate(`/?theme=${theme}`);
      const appliedTheme = await evaluate("document.documentElement.dataset.theme");
      if (appliedTheme !== theme) {
        throw new Error(
          `Explicit screenshot theme ${theme} was overridden by ${appliedTheme}`,
        );
      }
      await capture(`overview-${theme}-${width}.png`);
      await navigate(`/studies/${completedStudyId}?theme=${theme}`);
      const appliedStudyTheme = await evaluate("document.documentElement.dataset.theme");
      if (appliedStudyTheme !== theme) {
        throw new Error(
          `Explicit Study screenshot theme ${theme} was overridden by ${appliedStudyTheme}`,
        );
      }
      await capture(`completed-study-${theme}-${width}.png`);
    }
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
