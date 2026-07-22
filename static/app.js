// Shared utilities used across multiple templates.

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}

// POST JSON with the X-Requested-With header the server's CSRF check requires;
// resolves to the parsed JSON body.
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    },
    body: JSON.stringify(body || {}),
  });
  return res.json();
}

let _toastTimer;
function showToast(msg, type = 'success') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast toast-' + type + ' toast-visible';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = 'toast'; }, 3500);
}

async function handleAccept(btn) {
  const fromAddr = btn.dataset.from;
  const sug = btn.closest('.suggestion');
  const select = sug.querySelector('.label-select');
  const label = select ? select.value : btn.dataset.label;
  btn.disabled = true;
  btn.textContent = 'Moving…';
  if (select) select.disabled = true;
  try {
    const res = await fetch('/accept', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({ from_addr: fromAddr, label }),
    });
    const data = await res.json();
    if (data.ok) {
      const shortLabel = label.replace('MailMatrixCategories/', '');
      sug.innerHTML = '<span class="accepted-msg">✓ Moved to ' + esc(shortLabel) + ' · emailRules.json updated</span>';
    } else {
      btn.disabled = false;
      btn.textContent = 'Accept';
      if (select) select.disabled = false;
      alert('Error: ' + (data.error || 'Unknown error'));
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Accept';
    if (select) select.disabled = false;
    alert('Error: ' + err.message);
  }
}
