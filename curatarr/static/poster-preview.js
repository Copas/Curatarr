// A poster picked for upload shows in the Poster panel before it's sent.
document.addEventListener("change", (event) => {
  const input = event.target.closest("input[data-preview]");
  if (!input || !input.files || !input.files[0]) return;
  const target = document.querySelector(input.dataset.preview);
  if (!target) return;
  const reader = new FileReader();
  reader.onload = () => {
    target.src = reader.result;
    const caption = document.querySelector(input.dataset.previewCaption);
    if (caption) caption.textContent = "Preview: not saved until you press Use this picture";
  };
  reader.readAsDataURL(input.files[0]);
});
