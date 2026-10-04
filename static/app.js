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
      // Let the page react (the live summary drops the sender's card).
      sug.dispatchEvent(new CustomEvent('mm:accepted', { bubbles: true, detail: { fromAddr, label } }));
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

// ── Shared analysis cards (AI Inbox + live summary) ─────────────────────────
// Everything interpolated here is untrusted mail content — always esc()/escAttr().

function renderActionCards(items) {
  return items.map(item => `
    <div class="inbox-card action-card">
      <div class="inbox-subject">${esc(item.subject || '(no subject)')}</div>
      <div class="inbox-meta">From: ${esc(item.from || '')}</div>
      <div class="action-reason">${esc(item.reason || '')}</div>
    </div>`).join('');
}

// One unfiled sender: subject/meta/preview plus, when Claude suggested a
// label, a label picker and an Accept button (handled by handleAccept).
function renderSenderCard(em, idx, sug, labels) {
  const count = (em.count || 1) > 1 ? `<span class="count-badge">${esc(em.count)}×</span>` : '';
  const preview = em.body_snippet
    ? `<div class="inbox-preview">${esc(em.body_snippet.slice(0, 200))}</div>`
    : '';

  let sugHtml = '';
  if (sug && sug.suggested_label) {
    const allOpts = [...labels];
    if (!allOpts.includes(sug.suggested_label)) allOpts.unshift(sug.suggested_label);
    const opts = allOpts.map(lbl =>
      `<option value="${escAttr(lbl)}"${lbl === sug.suggested_label ? ' selected' : ''}>${esc(lbl.replace('MailMatrixCategories/', ''))}</option>`
    ).join('');
    const newBadge = sug.is_new_label ? ' <span class="new-badge">new</span>' : '';
    const reason = sug.reason ? `<span class="suggestion-reason">${esc(sug.reason)}</span>` : '';
    sugHtml = `<div class="suggestion">
      <select class="label-select">${opts}</select>${newBadge}
      ${reason}
      <button class="accept-btn" data-from="${escAttr(em.from_addr)}" onclick="handleAccept(this)">Accept</button>
    </div>`;
  }

  return `<div class="inbox-card" id="inbox-card-${idx}" data-from="${escAttr(em.from_addr)}">
    <div class="inbox-subject">${esc(em.subject || '(no subject)')} ${count}</div>
    <div class="inbox-meta">From: ${esc(em.from_display || em.from_addr)}</div>
    <div class="inbox-meta">Date: ${esc(em.date || '')}</div>
    ${preview}
    ${sugHtml}
  </div>`;
}
