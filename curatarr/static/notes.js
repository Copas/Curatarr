// Notes in the corner after a button that works in place ("Saved", "Couldn't
// do that"): one region, "Notifications", in the browser's top layer (a
// manual popover: above everything, open dialogs included, and not closed by
// a click elsewhere). Each note has a Dismiss button and goes by itself after
// a few seconds, but never while the pointer is on it or focus is in it.
// Screen readers are told the text as it comes (ariaNotify, else a polite
// live region). What a note says is also on the page itself once it's
// redrawn, so missing one loses nothing.
(function () {
  var region = null;
  var returnTo = null;  // the last thing focused outside the notes
  document.addEventListener("focusin", function (event) {
    if (!region || !region.contains(event.target)) returnTo = event.target;
  });

  function container() {
    if (region && region.isConnected) return region;
    region = document.createElement("div");
    region.className = "artwork-notes";
    region.setAttribute("role", "region");
    region.setAttribute("aria-label", "Notifications");
    if (!("ariaNotify" in Element.prototype)) region.setAttribute("aria-live", "polite");
    if ("popover" in HTMLElement.prototype) region.popover = "manual";
    document.body.appendChild(region);
    return region;
  }

  function open(c) {
    if (c.popover && !c.matches(":popover-open")) c.showPopover();
  }

  function closeIfEmpty(c) {
    if (c.children.length) return;
    if (c.popover && c.matches(":popover-open")) c.hidePopover();
  }

  function note(text, category, ms) {
    var c = container();
    var item = document.createElement("div");
    item.className = "artwork-note artwork-note-" + (category || "success");
    var words = document.createElement("span");
    words.textContent = text;
    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "btn-close artwork-note-close";
    dismiss.setAttribute("aria-label", "Dismiss");
    item.append(words, dismiss);
    c.appendChild(item);
    open(c);
    if (c.ariaNotify) c.ariaNotify(text);

    function remove() {
      if (!item.isConnected) return;
      var hadFocus = item.contains(document.activeElement);
      item.remove();
      if (hadFocus) {
        // Focus goes to the next note, or back where it was.
        var next = c.querySelector(".artwork-note-close");
        if (next) next.focus();
        else if (returnTo && returnTo.isConnected) returnTo.focus();
      }
      closeIfEmpty(c);
    }

    function later() {
      if (item.matches(":hover, :focus-within")) setTimeout(later, 1000);
      else remove();
    }

    dismiss.addEventListener("click", remove);
    setTimeout(later, ms || 8000);
  }

  window.curatarrNote = note;
})();
