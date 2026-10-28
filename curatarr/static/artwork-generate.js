// Posters → Generate: find titles as you type and add them to the job.
(function () {
  var search = document.getElementById("title-search");
  var results = document.getElementById("title-results");
  var chosen = document.getElementById("chosen-titles");
  if (!search || !results || !chosen) return;
  var timer = null;

  function picked(id) {
    return chosen.querySelector('input[value="' + id + '"]') !== null;
  }

  function add(title) {
    if (picked(title.id)) return;
    var chip = document.createElement("span");
    chip.className = "badge text-bg-secondary artwork-chip";
    chip.textContent = title.title;
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "media";
    input.value = title.id;
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn-close btn-close-white ms-1";
    remove.setAttribute("aria-label", "Remove " + title.title);
    remove.setAttribute("data-remove-chip", "");
    chip.appendChild(input);
    chip.appendChild(remove);
    chosen.appendChild(chip);
  }

  function show(titles) {
    results.textContent = "";
    titles.forEach(function (title) {
      var item = document.createElement("li");
      item.className = "list-group-item list-group-item-action";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-link p-0 text-start";
      button.textContent = title.title + " (" + title.kind + ")";
      button.addEventListener("click", function () {
        add(title);
        results.textContent = "";
        search.value = "";
        search.focus();
      });
      item.appendChild(button);
      results.appendChild(item);
    });
  }

  search.addEventListener("input", function () {
    clearTimeout(timer);
    var text = search.value.trim();
    if (text.length < 2) {
      results.textContent = "";
      return;
    }
    timer = setTimeout(function () {
      fetch(search.dataset.findUrl + "?q=" + encodeURIComponent(text))
        .then(function (response) { return response.ok ? response.json() : { titles: [] }; })
        .then(function (data) { show(data.titles || []); })
        .catch(function () {});
    }, 250);
  });

  search.addEventListener("keydown", function (event) {
    if (event.key === "Enter") event.preventDefault();  // don't send the form
  });

  chosen.addEventListener("click", function (event) {
    var button = event.target.closest("[data-remove-chip]");
    if (button) button.closest(".artwork-chip").remove();
  });
})();
