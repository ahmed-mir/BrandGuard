(() => {
  const button = document.querySelector("[data-load-demo-example]");
  const dataElement = document.getElementById("brandguard-demo-example");
  const form = button?.closest("form");
  if (!button || !dataElement || !form) return;

  let example;
  try {
    example = JSON.parse(dataElement.textContent);
  } catch (_error) {
    button.disabled = true;
    return;
  }
  button.hidden = false;

  button.addEventListener("click", () => {
    const editableNames = ["title", "audience", "campaign_purpose", "content_text"];
    const hasExistingText = editableNames.some((name) => {
      const field = form.elements.namedItem(name);
      return field && field.value.trim().length > 0;
    });
    if (hasExistingText && !window.confirm("Replace the values currently entered in this form with the synthetic LumaDesk example?")) return;

    Object.entries(example).forEach(([name, value]) => {
      const field = form.elements.namedItem(name);
      if (!field) return;
      field.value = value;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const status = document.querySelector("[data-demo-example-status]");
    if (status) status.hidden = false;
    form.elements.namedItem("content_text")?.focus();
  });
})();
