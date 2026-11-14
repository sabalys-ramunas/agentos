// An open shadow root with its own styles: tokens reach in through inheritance.
customElements.define("x-badge", class extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({mode: "open"});
    const planted = this.hasAttribute("planted");
    root.innerHTML = `
      <style>
        :host { display: block; margin-top: var(--space-4); }
        .in { color: var(--color-danger); padding: var(--space-1); }
        .bad { background-color: #fde68a; }
        .act { font: inherit; padding: var(--space-1) var(--space-2); border: 0; border-radius: var(--radius-sm);
               background-color: var(--color-action-primary); color: var(--color-action-on-primary); }
      </style>
      <span class="in">Shadow text</span>
      <button class="act" data-ds="button">Inside</button>
      ${planted ? '<span id="p-shadow-bad" class="bad">Hard-coded</span> <span id="p-shadow-hand" role="link" tabindex="0">Fake link</span>' : ''}`;
  }
});
