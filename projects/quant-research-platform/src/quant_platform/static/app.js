"use strict";

for (const field of document.querySelectorAll('input[name="action_id"]')) {
  if (!field.value) {
    field.value = self.crypto.randomUUID();
  }
}

for (const wrapper of document.querySelectorAll(".table-wrap")) {
  const update = () => {
    wrapper.dataset.scrollable = String(wrapper.scrollWidth > wrapper.clientWidth);
  };
  update();
  window.addEventListener("resize", update, { passive: true });
}

const experimentForm = document.querySelector(
  'form[data-testid="experiment-form"]',
);

function typedValue(field) {
  if (field.value === "" && field.dataset.nullable === "true") return null;
  if (field.dataset.parameterType === "integer") return Number.parseInt(field.value, 10);
  if (field.dataset.parameterType === "number") return Number.parseFloat(field.value);
  if (field.dataset.parameterType === "boolean") return field.value.toLowerCase() === "true";
  return field.value;
}

function selectedOperator(selector) {
  const option = selector.selectedOptions[0];
  const separator = selector.value.lastIndexOf("@");
  const operatorId = selector.value.slice(0, separator);
  const requestedVersion = selector.value.slice(separator + 1);
  return {
    slot: selector.dataset.operatorSelector,
    operatorId,
    requestedVersion,
    resolvedVersion: option.dataset.resolvedVersion,
    resolvedDigest: option.dataset.resolvedDigest,
  };
}

function selectedParameters(selection) {
  const parameterSet = experimentForm.querySelector(
    `[data-parameter-set="${selection.slot}"][data-selector="${selection.operatorId}@${selection.resolvedVersion}"]`,
  );
  const parameters = {};
  for (const field of parameterSet.querySelectorAll("[data-parameter-name]")) {
    parameters[field.dataset.parameterName] = typedValue(field);
  }
  return parameters;
}

function experimentTask() {
  const dataset = experimentForm.querySelector("[data-dataset-selector]").value;
  if (!dataset.includes("|")) return null;
  const [instrument, snapshotId] = dataset.split("|", 2);
  const templateParameters = {};
  for (const field of experimentForm.querySelectorAll("[data-template-parameter]")) {
    templateParameters[field.dataset.templateParameter] = typedValue(field);
  }
  const operators = {};
  for (const selector of experimentForm.querySelectorAll("[data-operator-selector]")) {
    const selection = selectedOperator(selector);
    operators[selection.slot] = {
      operator_id: selection.operatorId,
      version: selection.requestedVersion,
      parameters: selectedParameters(selection),
    };
  }
  return {
    schema_version: 1,
    dataset: { instrument, snapshot_id: snapshotId },
    template: {
      name: experimentForm.querySelector('[name="template_name"]').value,
      version: experimentForm.querySelector('[name="template_version"]').value,
      parameters: templateParameters,
    },
    operators,
  };
}

function updateResolvedSummary() {
  if (!experimentForm) return;
  const dataset = experimentForm.querySelector("[data-dataset-selector]");
  experimentForm.querySelector("[data-summary-dataset]").textContent =
    dataset.selectedOptions[0]?.textContent || "Dataset required";
  const container = experimentForm.querySelector("[data-summary-operators]");
  const rows = [];
  for (const selector of experimentForm.querySelectorAll("[data-operator-selector]")) {
    const selection = selectedOperator(selector);
    const row = document.createElement("p");
    const label = document.createElement("span");
    label.className = "mono";
    label.textContent = selection.slot;
    row.append(label);
    row.append(
      ` · ${selection.operatorId} · ${selection.requestedVersion} resolves to ` +
        `${selection.resolvedVersion} · ${selection.resolvedDigest.slice(0, 12)}`,
    );
    rows.push(row);
  }
  container.replaceChildren(...rows);
}

let previewTimer;
let previewController;
let previewSequence = 0;

function dispatchPreviewSettled(status, detail) {
  status.dispatchEvent(
    new CustomEvent("quant:preview-settled", {
      bubbles: true,
      detail,
    }),
  );
}

function settlePreviewText(status, state, text, detail = {}) {
  status.dataset.state = state;
  status.textContent = text;
  dispatchPreviewSettled(status, { state, message: text, ...detail });
}

async function updateDuplicatePreview() {
  if (!experimentForm) return;
  const status = experimentForm.querySelector(
    '[data-testid="live-duplicate-preview"]',
  );
  const task = experimentTask();
  if (!task) {
    settlePreviewText(
      status,
      "idle",
      "Duplicate preview requires an immutable dataset.",
    );
    return;
  }
  const sequence = ++previewSequence;
  const controller = new AbortController();
  previewController = controller;
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 10000);
  status.dataset.state = "loading";
  status.textContent = "Resolving immutable versions and checking for duplicates...";
  try {
    const response = await fetch("/api/experiments/preview", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": experimentForm.querySelector('[name="csrf_token"]').value,
      },
      body: JSON.stringify({ task }),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "Preview failed");
    if (sequence !== previewSequence) return;
    const preview = payload.preview;
    status.dataset.state = preview.duplicate ? "duplicate" : "new";
    const message = document.createElement("span");
    message.textContent = preview.duplicate
      ? "Exact duplicate found. Submission will return the existing experiment."
      : "New canonical experiment identity.";
    const identity = document.createElement("span");
    identity.className = "mono";
    identity.textContent = ` ${preview.experiment_id}`;
    status.replaceChildren(message, identity);
    if (preview.duplicate) {
      const link = document.createElement("a");
      link.href = `/experiments/${preview.experiment_id}`;
      link.textContent = " Open existing experiment";
      status.append(link);
    }
    dispatchPreviewSettled(status, {
      state: status.dataset.state,
      duplicate: preview.duplicate,
      experiment_id: preview.experiment_id,
      message: status.textContent,
    });
  } catch (error) {
    if (sequence !== previewSequence) return;
    if (error.name === "AbortError" && !timedOut) return;
    const reason = timedOut
      ? "request timed out after 10 seconds"
      : error.message || String(error);
    settlePreviewText(
      status,
      "error",
      `Duplicate preview unavailable: ${reason}`,
    );
  } finally {
    window.clearTimeout(timeout);
    if (sequence === previewSequence) previewController = undefined;
  }
}

function schedulePreview() {
  updateResolvedSummary();
  window.clearTimeout(previewTimer);
  if (previewController) {
    previewController.abort();
    previewController = undefined;
  }
  previewTimer = window.setTimeout(updateDuplicatePreview, 200);
}

for (const selector of document.querySelectorAll("[data-operator-selector]")) {
  const updateParameters = () => {
    const slot = selector.dataset.operatorSelector;
    const selected = selector.selectedOptions[0];
    const explicit = `${selector.value.split("@")[0]}@${selected.dataset.resolvedVersion}`;
    for (const parameters of document.querySelectorAll(
      `[data-parameter-set="${slot}"]`,
    )) {
      parameters.hidden = parameters.dataset.selector !== explicit;
    }
  };
  selector.addEventListener("change", () => {
    updateParameters();
    schedulePreview();
  });
  updateParameters();
}

if (experimentForm) {
  experimentForm.addEventListener("input", schedulePreview);
  experimentForm.addEventListener("change", schedulePreview);
  schedulePreview();
}
