// Looks: saving, restoring and the saved looks' buttons work in place, then
// the page's own parts are drawn afresh, without reloading the page.
(function () {
  var page = document.getElementById("looks-page");
  var messages = document.getElementById("looks-messages");
  if (!page) return;

  function say(text, category) {
    var note = document.createElement("div");
    note.className = "alert alert-" + (category || "info") + " py-2 mb-2";
    note.setAttribute("role", "status");
    note.textContent = text;
    messages.prepend(note);
    setTimeout(function () { note.remove(); }, 8000);
  }

  function redraw() {
    return fetch(window.location.pathname, { headers: { Accept: "text/html" } })
      .then(function (response) { return response.text(); })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html").getElementById("looks-page");
        if (fresh) page.innerHTML = fresh.innerHTML;
      });
  }

  page.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches("form[data-looks]")) return;
    event.preventDefault();
    var buttons = form.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    fetch(form.action, { method: "POST", body: new FormData(form), headers: { "X-Curatarr-Async": "1" } })
      .then(function (response) {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then(function (answer) {
        (answer.messages || []).forEach(function (m) { say(m.text, m.category); });
        if (answer.ok) return redraw();
      })
      .catch(function () { HTMLFormElement.prototype.submit.call(form); })
      .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  });
})();
