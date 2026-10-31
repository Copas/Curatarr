// Buttons that work in place, without reloading the page: Review and a job's
// page (Approve, Turn down, Make it again, the job's own buttons), so going
// through many pictures is quick, Jobs (Cancel, Cancel all waiting) and
// Enhance → Subtitles. On Review, a picture that's dealt with folds to a
// one-line note and the next moves up; on a job's page, just that picture
// (and the top of the page) is redrawn. If the background request fails,
// the form is sent the usual way, so nothing's lost.
(function () {
  var notes = document.createElement("div");
  notes.className = "artwork-notes";
  notes.setAttribute("aria-live", "polite");
  document.body.appendChild(notes);

  function note(text, category) {
    var item = document.createElement("div");
    item.className = "artwork-note artwork-note-" + (category || "success");
    item.textContent = text;
    notes.appendChild(item);
    setTimeout(function () { item.remove(); }, 6000);
  }

  function redraw(el) {
    if (!el || !el.dataset.liveUrl) return Promise.resolve();
    return fetch(el.dataset.liveUrl)
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html) return;
        // A table row only survives being parsed inside a table body.
        var holder = document.createElement(el.tagName === "TR" ? "tbody" : "div");
        holder.innerHTML = html.trim();
        var next = holder.firstElementChild;
        if (!next) return;
        el.replaceWith(next);
        if (window.curatarrLocalTime) window.curatarrLocalTime(next);
      });
  }

  function reviewCount(count) {
    document.querySelectorAll('.posters-nav a[href$="/library/posters/review"] .badge')
      .forEach(function (badge) {
        if (count) badge.textContent = count; else badge.remove();
      });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
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
          return;
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
