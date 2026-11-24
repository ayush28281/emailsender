"use strict";

const workSamples = (() => {
  const library = document.getElementById("sample-library");
  const list = document.getElementById("sample-list");
  const template = document.getElementById("sample-template");
  const add = document.getElementById("sample-add");
  const limit = document.getElementById("sample-limit");
  const maxSamples = 4;
  let loaded;

  function updateLimit() {
    const full = list.children.length >= maxSamples;
    add.hidden = full;
    limit.hidden = !full;
  }

  function renderSample(sample) {
    const fieldset = template.content.firstElementChild.cloneNode(true);
    for (const field of fieldset.querySelectorAll("[data-field]")) {
      field.value = sample[field.dataset.field] || "";
    }
    const remove = fieldset.querySelector(".remove-sample");
    remove.addEventListener("click", () => {
      const next = fieldset.nextElementSibling || fieldset.previousElementSibling;
      fieldset.remove();
      list.dispatchEvent(new Event("input", {bubbles: true}));
      updateLimit();
      (next?.querySelector("[data-field]") || add).focus();
    });
    list.append(fieldset);
    return fieldset;
  }

  add.addEventListener("click", () => {
    if (list.children.length >= maxSamples) return;
    const fieldset = renderSample({});
    list.dispatchEvent(new Event("input", {bubbles: true}));
    updateLimit();
    fieldset.querySelector("[data-field]").focus();
  });

  library.addEventListener("invalid", () => { library.open = true; }, true);

  return {
    load(items) {
      const samples = Array.isArray(items) ? items : [];
      const snapshot = JSON.stringify(samples);
      if (snapshot === loaded) return;
      loaded = snapshot;
      list.replaceChildren();
      samples.forEach(renderSample);
      updateLimit();
    },
    values() {
      return Array.from(list.children, fieldset => Object.fromEntries(
        Array.from(fieldset.querySelectorAll("[data-field]"), field => [field.dataset.field, field.value.trim()])
      ));
    }
  };
})();
