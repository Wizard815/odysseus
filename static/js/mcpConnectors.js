// static/js/mcpConnectors.js
//
// Per-chat "Connectors" toggle: which MCP servers are hidden for the
// CURRENT chat only (routes/mcp_routes.py's GET /api/mcp/servers/lite,
// routes/session_routes.py's GET /session/{sid}/mcp-toggle and the
// mcp_disabled_server_ids field on PATCH /session/{sid}, enforced in
// src/agent_loop.py via _mcp_disabled_map). This is separate from the
// global per-server enable/disable in Settings -> Integrations, which
// affects every chat -- this only ever touches the one conversation
// that's open when you toggle it.

import uiModule from './ui.js';
import { getCurrentSessionId, hasPendingChat } from './sessions.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = window.location.origin;
let _modal = null;
let _servers = [];         // [{id, name, status, tool_count}]
let _disabled = new Set(); // server ids hidden for the current session
let _loadedForSid = null;  // which session the above was fetched for
// Chosen BEFORE a real session exists yet, while a "New Chat" is pending
// (no session_id until the first message is sent — see sessions.js
// createDirectChat/materializePendingSession). Applied to the session at
// creation time instead of requiring a PATCH afterward.
let _pendingDisabled = new Set();

/** Read by sessions.js materializePendingSession() when it finally POSTs
 * /api/session, so a chat started with connectors already turned off never
 * sends those servers' tool schemas in its very first prompt. */
export function getPendingMcpDisabledServerIds() {
  return Array.from(_pendingDisabled);
}

/** Called by sessions.js whenever a fresh "New Chat" is started, so a
 * previous pending chat's choices don't leak into the next one. */
export function clearPendingMcpDisabledServerIds() {
  _pendingDisabled = new Set();
}

// Cross-module access without a circular import (sessions.js needs the two
// functions above; mcpConnectors.js already imports FROM sessions.js).
// Mirrors the existing window.chatModule/window.documentModule pattern used
// elsewhere in this codebase for the same reason.
window.mcpConnectorsModule = { getPendingMcpDisabledServerIds, clearPendingMcpDisabledServerIds };

function _escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

function _injectStyles() {
  if (document.getElementById('mcp-connectors-style')) return;
  const style = document.createElement('style');
  style.id = 'mcp-connectors-style';
  style.textContent = `
    .mcp-connector-row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:9px 4px; border-bottom:1px solid var(--border-color, rgba(255,255,255,0.08)); }
    .mcp-connector-row:last-child { border-bottom:none; }
    .mcp-connector-info { display:flex; align-items:center; gap:8px; min-width:0; }
    .mcp-connector-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; }
    .mcp-connector-dot.connected { background:#22c55e; }
    .mcp-connector-dot.disconnected { background:#6b7280; }
    .mcp-connector-text { min-width:0; }
    .mcp-connector-name { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .mcp-connector-meta { font-size:11px; opacity:0.6; }
    .mcp-connector-switch { position:relative; display:inline-block; width:36px; height:20px; flex:0 0 auto; }
    .mcp-connector-switch input { opacity:0; width:0; height:0; }
    .mcp-connector-slider { position:absolute; cursor:pointer; inset:0; background:rgba(255,255,255,0.18); border-radius:20px; transition:background 0.15s; }
    .mcp-connector-slider::before { content:""; position:absolute; height:14px; width:14px; left:3px; bottom:3px; background:#fff; border-radius:50%; transition:transform 0.15s; }
    .mcp-connector-switch input:checked + .mcp-connector-slider { background:var(--accent-primary, #4a9eff); }
    .mcp-connector-switch input:checked + .mcp-connector-slider::before { transform:translateX(16px); }
    .mcp-connector-switch input:disabled + .mcp-connector-slider { opacity:0.4; cursor:not-allowed; }
    .mcp-connector-empty { padding:20px 4px; text-align:center; opacity:0.6; font-size:13px; }
  `;
  document.head.appendChild(style);
}

function _row(server) {
  const isOn = !_disabled.has(server.id); // switch shows "enabled for this chat"
  const row = document.createElement('div');
  row.className = 'mcp-connector-row';
  row.innerHTML = `
    <div class="mcp-connector-info">
      <span class="mcp-connector-dot ${server.status === 'connected' ? 'connected' : 'disconnected'}"></span>
      <div class="mcp-connector-text">
        <div class="mcp-connector-name">${_escapeHtml(server.name)}</div>
        <div class="mcp-connector-meta">${server.tool_count} tool${server.tool_count === 1 ? '' : 's'}</div>
      </div>
    </div>
    <label class="mcp-connector-switch">
      <input type="checkbox" ${isOn ? 'checked' : ''} data-server-id="${server.id}">
      <span class="mcp-connector-slider"></span>
    </label>`;
  const input = row.querySelector('input');
  input.addEventListener('change', () => _onToggle(server.id, input));
  return row;
}

async function _onToggle(serverId, inputEl) {
  const sid = getCurrentSessionId();
  if (!sid) {
    // No real session row yet. If a "New Chat" is pending (the normal case
    // right after clicking New Chat, before the first message is sent),
    // remember the choice locally -- materializePendingSession() applies it
    // when the session is actually created, so it's already in effect
    // before the first prompt is built. Only block entirely when there's no
    // chat context at all to attach the preference to.
    if (!hasPendingChat()) {
      inputEl.checked = !inputEl.checked; // revert, nothing to persist against
      if (uiModule && uiModule.showToast) uiModule.showToast('Start a new chat first to set connectors');
      return;
    }
    if (inputEl.checked) _pendingDisabled.delete(serverId);
    else _pendingDisabled.add(serverId);
    _disabled = new Set(_pendingDisabled);
    _syncIndicator();
    return;
  }
  const wasDisabled = _disabled.has(serverId);
  // Optimistic update.
  if (inputEl.checked) _disabled.delete(serverId);
  else _disabled.add(serverId);
  inputEl.disabled = true;
  try {
    const body = new URLSearchParams();
    body.set('mcp_disabled_server_ids', JSON.stringify(Array.from(_disabled)));
    const r = await fetch(`${API}/api/session/${encodeURIComponent(sid)}`, {
      method: 'PATCH', credentials: 'same-origin', body,
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _syncIndicator();
  } catch (e) {
    // Revert on failure.
    if (wasDisabled) _disabled.add(serverId); else _disabled.delete(serverId);
    inputEl.checked = !inputEl.checked;
    if (uiModule && uiModule.showError) uiModule.showError('Could not update connectors: ' + (e.message || e));
  } finally {
    inputEl.disabled = false;
  }
}

function _renderList() {
  const body = _modal.querySelector('#mcp-connectors-body');
  body.innerHTML = '';
  if (!_servers.length) {
    body.innerHTML = '<div class="mcp-connector-empty">No MCP servers connected. Add one in Settings → Integrations.</div>';
    return;
  }
  for (const server of _servers) body.appendChild(_row(server));
}

function _syncIndicator() {
  const overflow = document.getElementById('overflow-connectors-btn');
  if (overflow) overflow.classList.toggle('active', _disabled.size > 0);
  try { document.dispatchEvent(new CustomEvent('overflow-state-change')); } catch (_) {}
}

function _getModal() {
  if (_modal) return _modal;
  _injectStyles();
  _modal = document.createElement('div');
  _modal.id = 'mcp-connectors-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content">
      <div class="modal-header">
        <h4><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>Connectors for this chat</h4>
        <button class="close-btn" id="mcp-connectors-close" aria-label="Close">✖</button>
      </div>
      <p class="muted" style="margin:0 0 8px;font-size:12px;">Turn a server off for just this conversation. Works before you send the first message too, so unneeded tools never enter the prompt. Doesn't affect other chats, and doesn't change the global enable/disable in Settings → Integrations.</p>
      <div class="modal-body" id="mcp-connectors-body"></div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.querySelector('#mcp-connectors-close').addEventListener('click', closeConnectorsPicker);
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

export async function openConnectorsPicker() {
  const modal = _getModal();
  modal.style.display = 'flex';
  const sid = getCurrentSessionId();
  try {
    const fetches = [fetch(`${API}/api/mcp/servers/lite`, { credentials: 'same-origin' })];
    if (sid) fetches.push(fetch(`${API}/api/session/${encodeURIComponent(sid)}/mcp-toggle`, { credentials: 'same-origin' }));
    const results = await Promise.all(fetches);
    for (const r of results) if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _servers = await results[0].json();
    if (sid) {
      const toggleState = await results[1].json();
      _disabled = new Set(toggleState.mcp_disabled_server_ids || []);
    } else if (hasPendingChat()) {
      _disabled = new Set(_pendingDisabled);
    } else {
      _disabled = new Set();
    }
    _loadedForSid = sid;
    _renderList();
  } catch (e) {
    if (uiModule && uiModule.showError) uiModule.showError('Could not load connectors');
  }
}

export function closeConnectorsPicker() {
  if (_modal) _modal.style.display = 'none';
}

export function initMcpConnectors() {
  const overflow = document.getElementById('overflow-connectors-btn');
  if (overflow) overflow.addEventListener('click', openConnectorsPicker);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initMcpConnectors);
} else {
  initMcpConnectors();
}
