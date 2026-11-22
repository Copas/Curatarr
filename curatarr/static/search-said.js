// The search page shows its titles at once, then fetches "What was said"
// (the transcript search, the slow part) into #said.
const said = document.getElementById("said");
const SHOW_FAILURE_AFTER = 2000; // ms
if (said) {
  fetch(said.dataset.said, { headers: { Accept: "text/html" } })
    .then((response) => {
      if (!response.ok) throw new Error(response.status);
      return response.text();
    })
    .then((html) => { window.curatarrSwap.fill(said, window.curatarrSwap.page(html).body); })
    .catch(() => {
      // Following a link cancels the search, which fails it here while the
      // next page loads: say so only if this page is still showing a moment
      // later (leaving it stops the timer).
      setTimeout(() => {
        const failed = document.createElement("p");
        failed.className = "text-warning small";
        failed.textContent = "Couldn't search what was said. Try again in a moment.";
        said.replaceChildren(failed);
      }, SHOW_FAILURE_AFTER);
    });
}
