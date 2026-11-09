// Playwright CLI formats every response with titles from every open tab.
// Page.title() has no timeout and can wait forever on a paused renderer. Keep
// that optional metadata from blocking commands for other, healthy pages.
exports.default = async function ({ page }) {
  const originalTitle = page.title.bind(page);
  let pending;
  page.title = async () => {
    if (!pending) {
      pending = originalTitle().then(
        value => { pending = undefined; return value; },
        () => { pending = undefined; return '[title unavailable]'; }
      );
    }
    let timer;
    try {
      return await Promise.race([
        pending,
        new Promise(resolve => { timer = setTimeout(() => resolve('[title unavailable: tab unresponsive]'), 750); })
      ]);
    } finally {
      clearTimeout(timer);
    }
  };
};
