"use strict";

(() => {
  const allowedThemes = new Set(["light", "dark", "system"]);
  let theme = allowedThemes.has(document.documentElement.dataset.theme)
    ? document.documentElement.dataset.theme
    : "system";
  try {
    const stored = localStorage.getItem("quant-theme");
    if (allowedThemes.has(stored)) theme = stored;
  } catch (error) {
    console.warn("Theme preference storage is unavailable.", error.name);
  }
  document.documentElement.dataset.theme = theme;
  document.documentElement.classList.add("js");
})();
