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

// Who's in them: a tick for each person in the reference photos folder, with
// their first photo. The ticks write the hidden "cast" field (names, comma
// separated), which is what's sent and what choosing a look fills in; up to
// three can be ticked. Changing the folder lists its people instead.
(function () {
  var cast = document.getElementById("g-cast");
  var picker = document.getElementById("g-cast-picker");
  var folder = document.getElementById("g-photos");
  var help = document.querySelector("[data-cast-help]");
  if (!cast || !picker || !folder) return;
  var most = parseInt(picker.dataset.most, 10) || 3;
  var timer = null;

  function boxes() {
    return Array.prototype.slice.call(picker.querySelectorAll("input[type=checkbox]"));
  }

  function limit() {
    var full = boxes().filter(function (b) { return b.checked; }).length >= most;
    boxes().forEach(function (b) {
      b.disabled = full && !b.checked;
      b.closest(".cast-person").classList.toggle("is-chosen", b.checked);
    });
  }

  function fromField() {
    var wanted = cast.value.split(",").map(function (n) { return n.trim().toLowerCase(); });
    boxes().forEach(function (b) { b.checked = wanted.indexOf(b.value.toLowerCase()) !== -1; });
    limit();
  }

  function toField() {
    cast.value = boxes().filter(function (b) { return b.checked; })
      .map(function (b) { return b.value; }).join(", ");
    limit();
  }

  function person(p) {
    var label = document.createElement("label");
    label.className = "cast-person";
    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = "form-check-input";
    box.value = p.name;
    var img = document.createElement("img");
    img.src = p.thumb;
    img.alt = "";
    img.loading = "lazy";
    var name = document.createElement("span");
    name.className = "cast-name";
    name.textContent = p.name;
    var count = document.createElement("span");
    count.className = "cast-count";
    count.textContent = p.photos + (p.photos === 1 ? " photo" : " photos");
    label.append(box, img, name, count);
    return label;
  }

  function load() {
    var path = folder.value.trim();
    fetch(picker.dataset.peopleUrl + "?folder=" + encodeURIComponent(path))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.folder !== folder.value.trim()) return; // typed on since
        picker.textContent = "";
        data.people.forEach(function (p) { picker.appendChild(person(p)); });
        fromField();
        if (data.people.length) toField(); // drop names not in this folder
        if (help) {
          help.textContent = data.people.length
            ? "Tick up to " + most + ". None ticked: the first " + most + " in the folder."
            : path && !data.found ? "Curatarr can't find that folder."
            : path ? "No photos found in that folder."
            : "Choose a reference photos folder to see who's in it.";
        }
      })
      .catch(function () {});
  }

  picker.addEventListener("change", toField);
  cast.addEventListener("change", fromField); // a look was chosen
  folder.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(load, 400);
  });
  folder.addEventListener("change", function () { clearTimeout(timer); load(); });
  fromField();
})();
