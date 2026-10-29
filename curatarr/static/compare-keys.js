// Posters → Compare: ← and → choose A or B, ↓ says they're about the same.
document.addEventListener("keydown", (event) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
  if (typing || event.altKey || event.ctrlKey || event.metaKey) return;
  if (document.querySelector("dialog[open]")) return;  // a picture's full screen
  const button = document.querySelector(`#compare-form [data-key="${event.key}"]`);
  if (!button) return;
  event.preventDefault();
  button.click();
});
