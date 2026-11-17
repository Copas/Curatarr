// Checks that poll in the background ask the browser for low priority, so a
// page someone clicks to loads first.
export default {
  async "Enhance's activity check goes out at low priority"({ go, send, on, sleep }) {
    await send("Network.enable");
    const sent = [];
    const off = on((m) => {
      if (m.method === "Network.requestWillBeSent" && m.params.type === "Fetch") {
        sent.push([new URL(m.params.request.url).pathname, m.params.request.initialPriority]);
      }
    });
    await go("/chapters");
    await sleep(9000); // it asks every few seconds
    off();
    const polls = sent.filter(([path]) => path.includes("/chapters/activity"));
    return polls.length && polls.every(([, p]) => p === "Low") ? true : sent;
  },
};
