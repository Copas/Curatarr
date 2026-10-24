// Copy buttons: <button data-copy="#element-id"> copies that element's text.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const source = document.querySelector(button.dataset.copy);
  if (!source) return;
  const text = source.value ?? source.textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    source.select?.();
    document.execCommand("copy");
  }
  const label = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = label; }, 1500);
});
