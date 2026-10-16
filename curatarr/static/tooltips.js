// Tooltips for anything with data-tip: what a button will do before you press it.
// Shown on mouse hover and keyboard focus; on a touch screen, press and hold.
(function () {
  var HOLD_MS = 450;
  var tip = null;
  var owner = null;
  var held = false;

  function hide() {
    if (tip) tip.remove();
    if (owner) owner.removeAttribute("aria-describedby");
    tip = owner = null;
  }

  function show(el) {
    if (owner === el) return;
    hide();
    owner = el;
    tip = document.createElement("div");
    tip.className = "app-tip";
    tip.id = "app-tip";
    tip.setAttribute("role", "tooltip");
    tip.textContent = el.dataset.tip;
    document.body.appendChild(tip);
    el.setAttribute("aria-describedby", tip.id);
    var box = el.getBoundingClientRect();
    var width = tip.offsetWidth;
    var height = tip.offsetHeight;
    var gap = 8;
    var left = box.left + box.width / 2 - width / 2;
    left = Math.max(gap, Math.min(left, window.innerWidth - width - gap));
    var top = box.top - height - gap;
    if (top < gap) top = box.bottom + gap; // no room above: show below
    tip.style.left = left + window.scrollX + "px";
    tip.style.top = top + window.scrollY + "px";
  }

  function target(event) {
    return event.target.closest ? event.target.closest("[data-tip]") : null;
  }

  document.addEventListener("mouseover", function (event) {
    var el = target(event);
    if (el) show(el);
    else if (owner) hide();
  });
  document.addEventListener("focusin", function (event) {
    var el = target(event);
    if (el && el.matches(":focus-visible")) show(el);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") hide();
  });
  window.addEventListener("scroll", hide, { passive: true });

  var timer = null;
  document.addEventListener("touchstart", function (event) {
    var el = target(event);
    held = false;
    hide();
    if (!el) return;
    timer = setTimeout(function () {
      held = true;
      show(el);
    }, HOLD_MS);
  }, { passive: true });
  function cancel() { clearTimeout(timer); }
  document.addEventListener("touchmove", cancel, { passive: true });
  document.addEventListener("touchend", cancel, { passive: true });
  // A press-and-hold reads the tip; it shouldn't also press the button.
  document.addEventListener("click", function (event) {
    if (held && target(event)) {
      event.preventDefault();
      event.stopPropagation();
      held = false;
    }
  }, true);
  document.addEventListener("contextmenu", function (event) {
    if (target(event)) event.preventDefault();
  });
})();
