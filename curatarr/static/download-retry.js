// Problems: "Blocklist and try another" works in place. The card says what
// happened instead of the page reloading, so nothing opened closes.
(function () {
  document.querySelectorAll("form[data-download-retry]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var card = form.closest("[data-download-card]");
      var button = form.querySelector("button");
      button.disabled = true;
      button.textContent = "Working…";
      function done(ok, message) {
        var line = document.createElement("p");
        line.className = "small mb-0 mt-2 " + (ok ? "text-success" : "text-warning");
        line.textContent = message;
        card.appendChild(line);
        if (ok) form.remove();
        else {
          button.disabled = false;
          button.textContent = "Blocklist and search";
        }
      }
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
      })
        .then(function (response) {
          if (!response.ok) throw new Error();
          return response.json();
        })
        .then(function (answer) { done(answer.ok, answer.message); })
        .catch(function () { done(false, "Couldn't reach Curatarr. Check History before trying again."); });
    });
  });
})();
