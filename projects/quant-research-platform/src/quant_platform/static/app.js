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
