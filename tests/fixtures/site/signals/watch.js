// What the page itself can see of us. Writes to the DOM only when it finds something, so a clean
// signal read leaves the page untouched: new window globals, expando properties on elements, any DOM
// mutation after load, or a cookie change. Index keys (a form's controls, window[0] for a frame) are
// the page's own and do not count.
(() => {
  const own = (names) => names.filter((n) => !/^\d+$/.test(n));
  let baseline = null;
  let cookies = null;
  const report = (what) => {
    if (!document.body.dataset.leak) document.body.dataset.leak = what;
  };
  window.addEventListener('load', () => {
    baseline = new Set(own(Object.getOwnPropertyNames(window)));
    cookies = document.cookie;
    const observer = new MutationObserver((records) => {
      observer.disconnect();
      const r = records[0];
      report('mutation:' + r.type + ':' + (r.attributeName || (r.target && r.target.nodeName) || ''));
    });
    observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    setInterval(() => {
      if (document.body.dataset.leak) return;
      const found = own(Object.getOwnPropertyNames(window)).filter((n) => !baseline.has(n)).map((n) => 'window.' + n);
      for (const el of document.querySelectorAll('*')) {
        const keys = own(Object.keys(el));
        if (keys.length) found.push(el.tagName.toLowerCase() + '.' + keys.join(','));
      }
      if (document.cookie !== cookies) found.push('cookie');
      if (found.length) {
        observer.disconnect();
        report(found.slice(0, 10).join(' '));
      }
    }, 100);
  });
})();
