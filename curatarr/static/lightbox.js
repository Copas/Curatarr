// Pictures with data-full open full screen when clicked; a click anywhere or
// Esc closes them again.
const lightbox = document.createElement("dialog");
lightbox.className = "lightbox";
lightbox.setAttribute("aria-label", "Picture, full screen");
const big = document.createElement("img");
big.alt = "";
lightbox.append(big);
document.body.append(lightbox);
lightbox.addEventListener("click", () => lightbox.close());
lightbox.addEventListener("close", () => { big.removeAttribute("src"); });

document.addEventListener("click", (event) => {
  const picture = event.target.closest("[data-full]");
  if (!picture) return;
  event.preventDefault();
  big.src = picture.dataset.full;
  big.alt = picture.dataset.alt || "";
  lightbox.showModal();
});
