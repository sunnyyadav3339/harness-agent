// Resolves once the DOM has been quiet for quietMs and no busy selector is
// visible, or after maxMs. Returns how long it waited.
(args) => new Promise((resolve) => {
  const { quietMs = 250, maxMs = 2500, busy = [] } = args || {};
  const start = performance.now();
  let last = performance.now();
  const obs = new MutationObserver(() => { last = performance.now(); });
  obs.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  const busyVisible = () => busy.some((sel) => {
    try {
      return Array.from(document.querySelectorAll(sel)).some((el) => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
      });
    } catch (e) { return false; }
  });
  const tick = () => {
    const now = performance.now();
    if (now - start >= maxMs || (now - last >= quietMs && !busyVisible())) {
      obs.disconnect();
      resolve(Math.round(now - start));
    } else {
      setTimeout(tick, 50);
    }
  };
  setTimeout(tick, 50);
})
