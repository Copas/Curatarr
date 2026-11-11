// Browse titles: the list follows the search box and type as you type (no
// Find button, no page reload). Without script the form still submits.
(function () {
  const form = document.getElementById("title-filter");
  const results = document.getElementById("title-results");
  if (!form || !results) return;
  form.querySelectorAll(".js-hide").forEach((el) => { el.hidden = true; });
  let timer = null;
  let asked = 0;
  async function refresh() {
    const params = new URLSearchParams(new FormData(form));
    const url = `${form.action || location.pathname}?${params}`;
    const mine = ++asked;
    try {
      const response = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
      if (!response.ok || mine !== asked) return;
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = page.getElementById("title-results");
      const count = page.querySelector(".page-heading .status-pill");
      if (!fresh || mine !== asked) return;
      results.innerHTML = fresh.innerHTML;
      const shown = document.querySelector(".page-heading .status-pill");
      if (count && shown) shown.textContent = count.textContent;
      history.replaceState(null, "", url);
    } catch (_error) {
      // offline for a moment: the next keystroke tries again
    }
  }
  form.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 250); });
  form.addEventListener("change", () => { clearTimeout(timer); refresh(); });
  form.addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(timer); refresh(); });
})();
