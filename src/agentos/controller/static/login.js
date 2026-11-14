const status = document.getElementById('status');
const code = new URLSearchParams(location.hash.slice(1)).get('code');
history.replaceState(null, '', '/login');
if (!code) {
  status.textContent = 'This link has no sign-in code. Run agentos controller login for a new one.';
} else {
  fetch('/api/session', {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  }).then(async (res) => {
    if (res.ok) {
      location.replace('/');
      return;
    }
    const data = await res.json().catch(() => ({}));
    status.textContent = (data.error && data.error.message) || 'Sign-in failed.';
  }).catch(() => { status.textContent = 'The controller could not be reached.'; });
}
