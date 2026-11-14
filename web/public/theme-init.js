// Applies a stored manual theme before first paint. "system" needs no attribute because
// the tokens use light-dark() and follow color-scheme on their own.
try {
  var t = localStorage.getItem('agentos.theme');
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
} catch (e) {}
