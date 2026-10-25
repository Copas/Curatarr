// Favourite ribbons: "everyone sees Sam's favourites" ticks Sam's column in
// the account-by-account grid, and the grid sets it back (partly ticked when
// only some accounts see them).
(() => {
  const column = (who) => document.querySelectorAll(`[data-ribbon-see="${who}"]`);
  const sync = (who) => {
    const all = document.querySelector(`[data-ribbon-all="${who}"]`);
    const boxes = [...column(who)];
    if (!all || !boxes.length) return;
    const ticked = boxes.filter((b) => b.checked).length;
    all.checked = ticked === boxes.length;
    all.indeterminate = ticked > 0 && ticked < boxes.length;
  };
  document.addEventListener("change", (event) => {
    const all = event.target.closest("[data-ribbon-all]");
    if (all) {
      column(all.dataset.ribbonAll).forEach((b) => { b.checked = all.checked; });
      all.indeterminate = false;
      return;
    }
    const one = event.target.closest("[data-ribbon-see]");
    if (one) sync(one.dataset.ribbonSee);
  });
  document.querySelectorAll("[data-ribbon-all]").forEach((all) => sync(all.dataset.ribbonAll));
})();
