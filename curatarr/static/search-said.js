// The search page shows its titles at once, then fetches "What was said"
// (the transcript search, the slow part) into #said.
const said = document.getElementById("said");
if (said) {
  fetch(said.dataset.said, { headers: { Accept: "text/html" } })
    .then((response) => {
      if (!response.ok) throw new Error(response.status);
      return response.text();
    })
    .then((html) => { window.curatarrSwap.fill(said, window.curatarrSwap.page(html).body); })
    .catch(() => {
      const failed = document.createElement("p");
      failed.className = "text-warning small";
      failed.textContent = "Couldn't search what was said. Try again in a moment.";
      said.replaceChildren(failed);
    });
}
