// Popups: the action menus (templates/_menu.html) and confirm-in-place boxes
// (templates/_confirm.html). The browser does the work where it can: a
// button with commandfor opens its popover, the popover sits in the top layer,
// Escape or a click elsewhere closes it and focus goes back to the button,
// anchor positioning keeps it beside the button, focusgroup gives a menu its
// arrow keys. This covers what the browser leaves out, and each gap on its
// own for a browser that lacks that one feature:
//   - aria-expanded on the button, and a keyboard open moving into a menu;
//   - invoker buttons (commandfor): the click opens and closes it here;
//   - anchor positioning: placed under the button (above if no room below);
//   - focusgroup: Up, Down, Home and End, and the menu roles;
//   - popovers at all: shown and hidden with a class, closed by Escape and
//     a click elsewhere.
// Everything listens on the document, so parts redrawn in place work too.
(function () {
  var hasPopover = "popover" in HTMLElement.prototype;
  var hasInvokers = "commandForElement" in HTMLButtonElement.prototype;
  var hasAnchors = !!(window.CSS && CSS.supports("position-area: block-end"));
  var hasFocusgroup = "focusGroup" in HTMLElement.prototype;
  var POPUPS = ".action-menu-items, .confirm-pop";
  var byKeyboard = null; // the button last pressed from the keyboard

  if (!hasPopover) document.documentElement.classList.add("no-popover");

  function triggerOf(pop) {
    return document.querySelector('[commandfor="' + pop.id + '"]:not([command="hide-popover"])');
  }
  function isOpen(pop) {
    return hasPopover ? pop.matches(":popover-open") : pop.classList.contains("is-open");
  }
  function open(pop, trigger) {
    if (isOpen(pop)) return;
    if (hasPopover) {
      try { pop.showPopover({ source: trigger }); } catch (e) { pop.showPopover(); }
    } else {
      document.querySelectorAll(POPUPS).forEach(function (other) { if (other !== pop) close(other); });
      pop.classList.add("is-open");
      opened(pop);
    }
  }
  function close(pop, restore) {
    if (!isOpen(pop)) return;
    if (hasPopover) pop.hidePopover();
    else {
      pop.classList.remove("is-open");
      closed(pop);
      if (restore && triggerOf(pop)) triggerOf(pop).focus();
    }
  }
  function items(pop) {
    return Array.prototype.filter.call(pop.querySelectorAll("button"), function (b) { return !b.disabled; });
  }

  // Under its button, or above it when there's no room below; kept on screen.
  function place(pop, trigger) {
    if (hasAnchors && hasPopover) return;
    var box = trigger.getBoundingClientRect(), gap = 4, edge = 8;
    pop.style.position = "fixed";
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var top = box.bottom + gap;
    if (top + h > window.innerHeight - edge && box.top - gap - h >= edge) top = box.top - gap - h;
    var left = Math.min(box.left, window.innerWidth - w - edge);
    pop.style.top = Math.max(edge, top) + "px";
    pop.style.left = Math.max(edge, left) + "px";
  }

  function opened(pop) {
    var trigger = triggerOf(pop);
    if (!trigger) return;
    trigger.setAttribute("aria-expanded", "true");
    place(pop, trigger);
    var menu = pop.querySelector('[focusgroup~="menu"]');
    if (menu && !hasFocusgroup) {
      menu.setAttribute("role", "menu");
      items(menu).forEach(function (b) { if (!b.hasAttribute("role")) b.setAttribute("role", "menuitem"); });
    }
    // Opened from the keyboard: into the menu, at its first item.
    if (menu && byKeyboard === trigger && items(menu)[0]) items(menu)[0].focus();
    byKeyboard = null;
  }
  function closed(pop) {
    var trigger = triggerOf(pop);
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }

  // The browser's popovers say when they open and close ("toggle" doesn't
  // bubble, so listen as it passes down).
  document.addEventListener("toggle", function (event) {
    var pop = event.target;
    if (!pop.matches || !pop.matches(POPUPS)) return;
    if (event.newState === "open") opened(pop);
    else closed(pop);
  }, true);

  document.addEventListener("pointerdown", function () { byKeyboard = null; }, true);
  document.addEventListener("keydown", function (event) {
    var trigger = event.target.closest && event.target.closest("[commandfor]");
    var pop = trigger && document.getElementById(trigger.getAttribute("commandfor"));
    if (pop && pop.matches(POPUPS) && trigger.getAttribute("command") !== "hide-popover") {
      if (event.key === "Enter" || event.key === " ") byKeyboard = trigger;
      // Down on a menu's button: open it if needed, and go to its first item.
      if (event.key === "ArrowDown" && pop.querySelector('[focusgroup~="menu"]')) {
        event.preventDefault();
        if (!isOpen(pop)) { byKeyboard = trigger; open(pop, trigger); }
        if (items(pop)[0]) items(pop)[0].focus();
      }
      return;
    }
    var openPop = !hasPopover && document.querySelector(".is-open:is(" + POPUPS + ")");
    if (openPop && event.key === "Escape") { close(openPop, true); return; }
    // Arrow keys between a menu's items, where focusgroup doesn't.
    var menu = !hasFocusgroup && event.target.closest && event.target.closest('[focusgroup~="menu"]');
    if (!menu || ["ArrowDown", "ArrowUp", "Home", "End"].indexOf(event.key) < 0) return;
    var list = items(menu), at = list.indexOf(document.activeElement);
    if (at < 0) return;
    var next = event.key === "Home" ? 0 : event.key === "End" ? list.length - 1
      : (at + (event.key === "ArrowUp" ? -1 : 1) + list.length) % list.length;
    event.preventDefault();
    list[next].focus({ preventScroll: true });
  }, true);

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest && event.target.closest("[commandfor]");
    var pop = trigger && document.getElementById(trigger.getAttribute("commandfor"));
    if (pop && pop.matches(POPUPS)) {
      if (hasInvokers && hasPopover) return; // the browser does it
      event.preventDefault();
      var command = trigger.getAttribute("command");
      if (command === "hide-popover" || (command === "toggle-popover" && isOpen(pop))) close(pop, true);
      else open(pop, trigger);
      return;
    }
    // Without popovers, a click elsewhere closes an open one.
    if (!hasPopover) {
      document.querySelectorAll(".is-open:is(" + POPUPS + ")").forEach(function (p) {
        if (!p.contains(event.target)) close(p);
      });
    }
  });

  // A menu item or a confirm button does its thing, and its popup closes.
  document.addEventListener("submit", function (event) {
    var pop = event.submitter && event.submitter.closest(POPUPS);
    if (pop) close(pop);
  });
})();
