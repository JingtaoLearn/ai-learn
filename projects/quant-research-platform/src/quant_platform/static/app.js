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
  selector.addEventListener("change", updateParameters);
  updateParameters();
}
