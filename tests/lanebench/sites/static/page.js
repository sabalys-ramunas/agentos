// Lanebench fixture site behaviour. Everything keys off data-action and data-lb, never class names,
// and works through open shadow roots (the shadow_wrap mutation).
(() => {
  const up = (n) => n.parentNode || n.host || null;
  const closest = (el, sel) => { for (let n = el; n; n = up(n)) if (n.nodeType === 1 && n.matches(sel)) return n; return null; };
  const deepAll = (root, sel, out = []) => {
    root.querySelectorAll(sel).forEach((n) => out.push(n));
    root.querySelectorAll('*').forEach((n) => { if (n.shadowRoot) deepAll(n.shadowRoot, sel, out); });
    return out;
  };
  const byLb = (scope, key) => deepAll(scope, `[data-lb="${key}"]`)[0] || null;
  const api = async (url, body) => {
    const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw data;
    return data;
  };
  const say = (key, text) => { const el = byLb(document, key); if (el) el.textContent = text; };

  document.addEventListener('click', async (e) => {
    const start = e.composedPath()[0];
    const el = start && start.nodeType === 1 ? closest(start, '[data-action]') : null;
    if (!el || el.tagName === 'FORM') return;
    const a = el.dataset.action;
    if (a === 'like') {
      const on = el.getAttribute('aria-pressed') !== 'true';
      const r = await api('/api/like', {post: el.dataset.post, on});
      el.setAttribute('aria-pressed', String(r.on));
      const count = el.querySelector('[data-count]');
      if (count) count.textContent = ` ${r.likes}`;
    } else if (a === 'follow') {
      const on = el.getAttribute('aria-pressed') !== 'true';
      const r = await api('/api/follow', {user: el.dataset.user, on});
      for (const b of deepAll(document, `[data-action="follow"][data-user="${el.dataset.user}"]`)) {
        b.setAttribute('aria-pressed', String(r.on));
        const label = r.label;
        if (b.hasAttribute('aria-label')) { b.setAttribute('aria-label', label); b.setAttribute('title', label); }
        else b.textContent = label;
      }
    } else if (a === 'comment-open') {
      const form = byLb(document, `cform:${el.dataset.post}`);
      if (form) { form.hidden = false; const ta = form.querySelector('textarea'); if (ta) ta.focus({preventScroll: true}); }
    } else if (a === 'cookies') {
      await api('/api/cookies', {choice: el.dataset.value});
      const wall = byLb(document, 'cookie-wall'); if (wall) wall.remove();
    } else if (a === 'save-login') {
      const r = await api('/api/save-login', {save: el.dataset.value === 'yes'});
      location.assign(r.next);
    } else if (a === 'device-approve') {
      const r = await api('/api/device-approve', {});
      location.assign(r.next);
    } else if (a === 'notif') {
      await api('/api/notifications', {on: el.dataset.value === 'on'});
      const modal = byLb(document, 'modal'); if (modal) modal.hidden = true;
    } else if (a === 'publish-banner') {
      const r = await api('/api/publish', {text: el.dataset.text, source: 'banner'});
      say('compose-status', r.message);
    } else if (a === 'pick-photo') {
      const input = byLb(document, 'photo-input');
      if (input) { window.__lbOwnClick = true; try { input.click(); } finally { window.__lbOwnClick = false; } }
    } else if (a === 'save-draft') {
      const form = byLb(document, 'composer');
      const r = await api('/api/draft', {text: form.querySelector('textarea').value, upload: form.dataset.upload || null});
      say('compose-status', r.message);
    }
  });

  document.addEventListener('change', async (e) => {
    const el = e.composedPath()[0];
    if (!el || el.getAttribute('data-lb') !== 'photo-input' || !el.files.length) return;
    const body = new FormData(); body.append('photo', el.files[0]);
    const r = await fetch('/api/upload', {method: 'POST', body});
    const data = await r.json();
    const form = byLb(document, 'composer');
    form.dataset.upload = data.id;
    say('preview', data.message);
  });

  document.addEventListener('submit', async (e) => {
    const form = e.composedPath()[0];
    if (!form || !form.dataset || !form.dataset.action) return;
    e.preventDefault();
    const a = form.dataset.action;
    const fd = new FormData(form);
    if (a === 'login') {
      try {
        const r = await api('/api/login', {username: fd.get('username'), password: fd.get('password')});
        location.assign(r.next);
      } catch (err) { const el = byLb(document, 'login-error'); if (el) el.hidden = false; }
    } else if (a === 'consent') {
      try {
        const r = await api('/api/consent', {terms: fd.get('terms') === 'on', partners: fd.get('partners') === 'on'});
        location.assign(r.next);
      } catch (err) { const el = byLb(document, 'consent-error'); if (el) el.hidden = false; }
    } else if (a === 'comment') {
      const text = (fd.get('text') || '').toString();
      const r = await api('/api/comment', {post: form.dataset.post, text});
      const list = byLb(document, `comments:${form.dataset.post}`);
      if (list) { const li = document.createElement('li'); li.className = list.firstElementChild ? list.firstElementChild.className : ''; li.textContent = `@${r.handle} ${text}`; list.append(li); }
      form.querySelector('textarea').value = '';
      form.hidden = true;
    } else if (a === 'publish') {
      const r = await api('/api/publish', {text: (fd.get('text') || '').toString(), upload: form.dataset.upload || null, source: 'composer'});
      say('compose-status', r.message);
    } else if (a === 'settings') {
      const r = await api('/api/settings', {display_name: (fd.get('display_name') || '').toString(), notifications: fd.get('notifications') === 'on'});
      say('settings-status', r.message);
    }
  }, true);

  // A modal that shows a moment after the page loads (mutations.add_modal).
  for (const modal of document.querySelectorAll('[data-modal-delay]')) {
    setTimeout(() => { modal.hidden = false; }, Number(modal.dataset.modalDelay) || 800);
  }

  // A lazy list: more posts come in as the reader reaches the end (mutations.lazy_list).
  const rest = document.querySelector('[data-lb="lazy-rest"]');
  const sentinel = document.querySelector('[data-lb="lazy-sentinel"]');
  if (rest && sentinel) {
    const batch = Number(rest.dataset.batch) || 4;
    const more = () => {
      const items = Array.from(rest.content.children).slice(0, batch);
      for (const item of items) rest.before(item);
      if (!rest.content.children.length) { sentinel.remove(); observer.disconnect(); }
    };
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) setTimeout(more, 300);
    });
    observer.observe(sentinel);
  }
})();
