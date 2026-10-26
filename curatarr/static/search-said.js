// The search page shows its titles at once, then fetches "What was said"
// (the transcript search, the slow part) into #said.
const said = document.getElementById("said");
if (said) {
  fetch(said.dataset.said, { headers: { Accept: "text/html" } })
    .then((response) => {
      if (!response.ok) throw new Error(response.status);
      return response.text();
    })
    .then((html) => { said.innerHTML = html; })
    .catch(() => {
      said.innerHTML = '<p class="text-warning small">Couldn\'t search what was said. Try again in a moment.</p>';
    });
}
