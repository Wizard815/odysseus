// Argos connection suite: the model engine's own settings panel.
//
// Everything here talks to routes/argos_routes.py, never to Argos directly --
// Argos is a separate origin and sends no CORS headers, so the browser cannot
// call it. The backend proxies and holds the base URL in the settings file.
//
// Self-initializing on DOMContentLoaded, like workspace.js and
// mcpConnectors.js: there is no central init hook in this codebase for a
// standalone module, and the panel's markup is always present in index.html.

import uiModule from './ui.js?v=20260916largetoolscroll1';

const API = '/api/argos';

const el = (id) => document.getElementById(id);

function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

function msg(text, isError) {
  const m = el('argos-msg');
  if (!m) return;
  m.textContent = text || '';
  m.className = isError ? 'admin-error' : '';
  m.style.color = isError ? '' : 'var(--accent, var(--red))';
}

async function api(method, path, body) {
  const init = { method, credentials: 'same-origin', headers: {} };
  if (body) {
    // FastAPI reads these as form fields, matching the rest of the codebase.
    init.body = new URLSearchParams(body);
    init.headers['Content-Type'] = 'application/x-www-form-urlencoded';
  }
  const res = await fetch(API + path, init);
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.detail || `request failed (${res.status})`);
  return data;
}

function setBadge(text, tone) {
  const b = el('argos-badge');
  if (!b) return;
  b.textContent = text;
  b.style.color = tone === 'bad' ? '#e55' : (tone === 'good' ? 'var(--accent, var(--red))' : '');
}

function renderStatus(st) {
  const overview = el('argos-overview');
  const profiles = el('argos-profiles');
  const models = el('argos-models');

  if (!st.configured) {
    setBadge('not configured');
    el('argos-overview-card').style.display = 'none';
    el('argos-profiles-card').style.display = 'none';
    el('argos-models-card').style.display = 'none';
    return;
  }

  if (!st.reachable) {
    setBadge('unreachable', 'bad');
    el('argos-overview-card').style.display = '';
    el('argos-profiles-card').style.display = 'none';
    el('argos-models-card').style.display = 'none';
    overview.innerHTML = `<div style="opacity:0.8;">No answer from <code>${esc(st.url)}</code>.
      Check that the engine is running and that the URL is reachable from this container.</div>`;
    return;
  }

  setBadge(st.mcp_registered ? 'connected' : 'reachable', 'good');

  el('argos-overview-card').style.display = '';
  el('argos-overview').innerHTML = `
    <div><strong>URL</strong> <code>${esc(st.url)}</code></div>
    <div><strong>Version</strong> ${esc(st.version ?? 'unknown')}${st.commit ? ` <span style="opacity:0.5;">(${esc(st.commit)})</span>` : ''}</div>
    <div><strong>Models</strong> ${st.model_count ?? '?'} configured, ${st.loaded_count ?? '?'} loaded</div>
    <div><strong>Active profile</strong> ${st.active_profile ? esc(st.active_profile) : '<span style="opacity:0.6;">none</span>'}</div>
    <div><strong>Tools</strong> ${st.mcp_registered ? 'connected to Odysseus' : '<span style="opacity:0.6;">not connected</span>'}</div>
    ${st.running && st.running.length ? `<div><strong>Running</strong> ${st.running.map(r => esc(r.model)).join(', ')}</div>` : ''}`;

  // Profiles: click a row to activate it. Argos applies a profile to launches
  // that start afterwards, which the note above the list spells out.
  const list = st.profiles || [];
  if (list.length) {
    el('argos-profiles-card').style.display = '';
    profiles.innerHTML = list.map(p => {
      const active = p.id === st.active_profile;
      return `<div class="settings-row" style="align-items:center;gap:8px;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;font-weight:${active ? '600' : '400'};">${esc(p.id)}${active ? ' <span class="admin-badge">active</span>' : ''}</div>
          ${p.description ? `<div style="font-size:11px;opacity:0.6;">${esc(p.description)}</div>` : ''}
        </div>
        <button type="button" class="admin-btn-sm" data-argos-profile="${esc(p.id)}">${active ? 'Clear' : 'Activate'}</button>
      </div>`;
    }).join('');
    profiles.querySelectorAll('[data-argos-profile]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.argosProfile;
        const clearing = btn.textContent.trim() === 'Clear';
        try {
          await api('PUT', '/profile', { name: clearing ? '' : id });
          msg(clearing ? 'Cleared the active profile' : `Activated ${id}`);
          await refresh();
        } catch (e) { msg(String(e.message || e), true); }
      });
    });
  } else {
    el('argos-profiles-card').style.display = 'none';
  }

  // The model list comes from the status call's counts; a full per-model list
  // is a follow-up, so this card states what is known without implying more.
  el('argos-models-card').style.display = '';
  models.innerHTML = `<div style="opacity:0.75;">${st.model_count ?? 0} model(s) configured in Argos.
    Manage them there — this panel reports state, it does not edit the model list.</div>`;

  const conn = el('argos-connect');
  const disc = el('argos-disconnect');
  if (conn && disc) {
    conn.style.display = st.mcp_registered ? 'none' : '';
    disc.style.display = st.mcp_registered ? '' : 'none';
  }
}

async function refresh() {
  try {
    const cfg = await api('GET', '/config');
    const input = el('argos-url');
    if (input && document.activeElement !== input) input.value = cfg.url || '';
    const st = await api('GET', '/status');
    renderStatus(st);
  } catch (e) {
    msg(String(e.message || e), true);
  }
}

function init() {
  const save = el('argos-save');
  if (!save || save.dataset.bound) return;
  save.dataset.bound = '1';

  save.addEventListener('click', async () => {
    try {
      const r = await api('PUT', '/config', { url: el('argos-url').value || '' });
      el('argos-url').value = r.url || '';
      msg(r.url ? 'Saved' : 'Cleared the URL');
      await refresh();
    } catch (e) { msg(String(e.message || e), true); }
  });

  el('argos-refresh')?.addEventListener('click', () => { msg(''); refresh(); });

  el('argos-connect')?.addEventListener('click', async () => {
    const btn = el('argos-connect');
    btn.disabled = true;
    msg('Connecting…');
    try {
      const r = await api('POST', '/connect', { url: el('argos-url').value || '' });
      if (r.connected) {
        msg('Connected — Argos tools are now available to the agent');
      } else {
        msg(r.error ? `Registered, but connecting failed: ${r.error}` : 'Registered, but not connected yet', true);
      }
      await refresh();
    } catch (e) { msg(String(e.message || e), true); }
    finally { btn.disabled = false; }
  });

  el('argos-disconnect')?.addEventListener('click', async () => {
    if (!await uiModule.styledConfirm('Stop offering Argos tools to the agent? The URL and profiles stay configured.', { confirmText: 'Disconnect', danger: true })) return;
    try {
      await api('DELETE', '/connect');
      msg('Disconnected Argos tools');
      await refresh();
    } catch (e) { msg(String(e.message || e), true); }
  });

  refresh();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

export default { refresh };
