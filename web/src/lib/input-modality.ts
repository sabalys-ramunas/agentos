/**
 * Marks whether the last input was a pointer or the keyboard, for one rule in app.css.
 * Radix moves focus by script, and browsers treat that as keyboard focus, so without this
 * a mouse click on a tab or menu leaves a focus ring behind.
 */
if (typeof window !== 'undefined') {
  const root = document.documentElement;
  window.addEventListener('pointerdown', () => (root.dataset.input = 'pointer'), { capture: true });
  window.addEventListener('keydown', () => (root.dataset.input = 'keyboard'), { capture: true });
}
