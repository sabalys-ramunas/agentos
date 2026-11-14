// badge.js with token values inlined. Generated, do not edit.
customElements.define("x-badge", class extends HTMLElement {
  constructor() {
    super();
    const root = this.attachShadow({mode: "open"});
    const planted = this.hasAttribute("planted");
    root.innerHTML = `
      <style>
        :host { display: block; margin-top: 16px; }
        .in { color: #ef4444; padding: 4px; }
        .bad { background-color: #fde68a; }
        .act { font: inherit; padding: 4px 8px; border: 0; border-radius: 4px;
               background-color: #3b82f6; color: #ffffff; }
      </style>
      <span class="in">Shadow text</span>
      <button class="act" data-ds="button">Inside</button>
      ${planted ? '<span id="p-shadow-bad" class="bad">Hard-coded</span> <span id="p-shadow-hand" role="link" tabindex="0">Fake link</span>' : ''}`;
  }
});
