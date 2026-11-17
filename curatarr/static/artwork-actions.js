// Buttons that work in place, without reloading the page: Review (each
// picture's buttons, and Approve all on this page) and a job's page
// (Approve, Turn down, Make it again, the job's own buttons), so going
// through many pictures is quick, Jobs (Cancel, Cancel all waiting) and
// Enhance → Subtitles. On Review, a picture that's dealt with folds to a
// one-line note and the next moves up, and when none are left the next
// pictures load in; on a job's page, just that picture
// (and the top of the page) is redrawn. If the background request fails,
// the form is sent the usual way, so nothing's lost.
(function () {
  function note(text, category) {
    window.curatarrNote(text, category, 6000);
  }

  function redraw(el) {
    // Redrawn after an action: it shows what the server now has.
    return window.curatarrSwap.redraw(el, null, false);
  }

  function reviewCount(count) {
    document.querySelectorAll('.posters-nav a[href$="/library/posters/review"] .badge')
      .forEach(function (badge) {
        if (count) badge.textContent = count; else badge.remove();
      });
  }

  // Review: once everything shown is dealt with, the next pictures load in
  // its place (those dealt with have left the list, so the same page now
  // holds the next ones), without a click on Next.
  function nextPictures() {
    var list = document.querySelector("[data-review-list]");
    if (!list || list.querySelector("[data-review-item]")) return;
    var wait = document.createElement("p");
    wait.className = "small text-secondary mt-3";
    wait.textContent = "Loading the next pictures…";
    list.appendChild(wait);
    return redraw(list).then(function () {
      var fresh = document.querySelector("[data-review-list]");
      if (fresh && fresh.querySelector("[data-review-item]")) {
        fresh.scrollIntoView({ block: "start" });
      }
    });
  }

  // Review → Approve all on this page: each picture goes on for the
  // accounts ticked under it, then the next pictures load in their place.
  function approveAll(event, form) {
    var data = new FormData(form);
    form.querySelectorAll('input[name="image"]').forEach(function (input) {
      var panel = document.getElementById("image-" + input.value);
      var decide = panel && panel.querySelector("form.artwork-decide");
      if (!decide) return;
      var id = input.value;
      data.append("audience-" + id, "1");
      var everyone = decide.querySelector('input[name="everyone"]');
      if (everyone && everyone.checked) data.append("everyone-" + id, "on");
      decide.querySelectorAll('input[name="who"]:checked').forEach(function (box) {
        data.append("who-" + id, box.value);
      });
    });
    event.preventDefault();
    var buttons = form.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    fetch(form.action, { method: "POST", body: data, headers: { "X-Curatarr-Async": "1" } })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then(function (answer) {
        (answer.messages || []).forEach(function (m) { note(m.text, m.category); });
        reviewCount(answer.review);
        var list = document.querySelector("[data-review-list]");
        return redraw(list).then(function () {
          var fresh = document.querySelector("[data-review-list]");
          if (fresh) fresh.scrollIntoView({ block: "start" });
        });
      })
      .catch(function () {
        // The background request failed: send it the usual way (each
        // picture then goes to whom it would be ticked for).
        HTMLFormElement.prototype.submit.call(form);
      });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.matches("form[data-approve-all]")) return approveAll(event, form);
    if (!form.matches("form[data-async]")) return;
    var button = event.submitter;
    var action = (button && button.getAttribute("formaction")) || form.action;
    var data = new FormData(form, button || undefined);
    event.preventDefault();
    var buttons = form.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    fetch(action, { method: "POST", body: data, headers: { "X-Curatarr-Async": "1" } })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then(function (answer) {
        (answer.messages || []).forEach(function (m) { note(m.text, m.category); });
        reviewCount(answer.review);
        var item = form.closest("[data-review-item]");
        if (item && answer.ok) {
          // Review: dealt with, so it folds away and the next moves up.
          var done = document.createElement("p");
          done.className = "artwork-reviewed small text-secondary";
          var title = item.querySelector("h2");
          done.textContent = "✓ " + ((answer.messages[0] || {}).text || (title ? title.textContent : "Done"));
          item.replaceWith(done);
          return nextPictures();
        }
        var panel = form.closest("[data-live]");
        var top = document.querySelector('[data-live="head"]');
        var counts = document.querySelector('[data-live="status"]');
        var parts = [panel, top, counts];
        if (form.dataset.redraw) {
          // A page-wide button (Jobs → Cancel all waiting): every part of
          // that kind, and the button itself is done.
          parts = parts.concat(Array.prototype.slice.call(
            document.querySelectorAll('[data-live="' + form.dataset.redraw + '"]')));
          if (answer.ok) form.remove();
        }
        if (panel && panel.dataset.live === "head") {
          // A job-wide button (the whole set, cancel, retry): every picture.
          parts = parts.concat(Array.prototype.slice.call(
            document.querySelectorAll('[data-live="image"]')));
        }
        var seen = [];
        return Promise.all(parts.filter(function (el) {
          if (!el || seen.indexOf(el) >= 0) return false;
          seen.push(el);
          return true;
        }).map(redraw));
      })
      .catch(function () {
        // The background request failed: send it the usual way instead.
        if (button && button.getAttribute("formaction")) form.action = button.getAttribute("formaction");
        HTMLFormElement.prototype.submit.call(form);
      })
      .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  });
})();
