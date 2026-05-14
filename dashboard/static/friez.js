/**
 * friez.js — shared utilities for the Friez dashboard
 *   Chezburger's Community — https://discord.gg/9bm7uXVc2w
 */

/**
 * api(url, method, body) → parsed JSON response
 * Handles GET and POST with CSRF-safe headers.
 * Sends your order to the kitchen and brings back the tray.
 */
async function api(url, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
  };
  if (body && method !== 'GET') opts.body = JSON.stringify(body);
  try {
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) {
    console.error('[friez.js] API error:', e);
    return { error: 'Order failed — check the kitchen console.' };
  }
}

/** Show a result box with a message. type: 'ok' | 'err' */
function showResult(boxId, message, type = 'ok') {
  const box = document.getElementById(boxId);
  if (!box) return;
  box.classList.remove('hidden');
  box.innerHTML = `<span class="${type}">${message}</span>`;
}

/** Reload page after a short delay */
function reloadAfter(ms = 900) {
  setTimeout(() => location.reload(), ms);
}

// ── Menu cache + ID resolver ──────────────────────────────────────────────────
// The "menu" cache holds members, channels, roles, and categories for the guild.
// Think of it as the laminated sheet behind the counter at Chezburger's.
let _menuCache = null;
let _menuFetched = false;

async function getMenuCache() {
  if (_menuFetched) return _menuCache;
  _menuFetched = true;
  try {
    const r = await fetch('/friez/api/guild_cache');
    if (r.ok) _menuCache = await r.json();
  } catch (_) {}
  return _menuCache;
}

function resolveUser(id, cache) {
  if (!cache || !id) return null;
  const m = cache.members?.[String(id)];
  if (!m) return null;
  const uname   = m.username || String(id);
  const display = m.display_name || uname;
  if (display === uname) return `${display} (${id})`;
  return `${display} (${uname}, ${id})`;
}

function resolveChannel(id, cache) {
  if (!cache || !id) return null;
  const name = cache.channels?.[String(id)];
  return name ? `#${name}` : null;
}

function resolveRole(id, cache) {
  if (!cache || !id) return null;
  const name = cache.roles?.[String(id)];
  return name ? `@${name}` : null;
}

function resolveCategory(id, cache) {
  if (!cache || !id) return null;
  return cache.categories?.[String(id)] || null;
}

const SNOWFLAKE_RE = /^\d{17,20}$/;

function _colHeader(td) {
  try {
    const tr    = td.closest('tr');
    const table = td.closest('table');
    const idx   = Array.from(tr.children).indexOf(td);
    const th    = table.querySelector(`thead th:nth-child(${idx + 1})`);
    return th ? th.textContent.trim().toLowerCase() : '';
  } catch (_) { return ''; }
}

async function resolveAll() {
  // Only run on dashboard pages
  if (!document.querySelector('.dash-body, .dash-card')) return;

  const cache = await getMenuCache();
  if (!cache) return;

  // 1. Explicit data attributes
  document.querySelectorAll('[data-uid]').forEach(el => {
    const r = resolveUser(el.dataset.uid, cache);
    if (r) { el.title = el.textContent.trim(); el.textContent = r; }
  });
  document.querySelectorAll('[data-cid]').forEach(el => {
    const r = resolveChannel(el.dataset.cid, cache);
    if (r) { el.title = el.textContent.trim(); el.textContent = r; }
  });
  document.querySelectorAll('[data-rid]').forEach(el => {
    const r = resolveRole(el.dataset.rid, cache);
    if (r) { el.title = el.textContent.trim(); el.textContent = r; }
  });
  document.querySelectorAll('[data-catid]').forEach(el => {
    const r = resolveCategory(el.dataset.catid, cache);
    if (r) { el.title = el.textContent.trim(); el.textContent = r; }
  });

  // 2. Auto-sniff snowflakes in table cells and stat-rows
  document.querySelectorAll('td code, td strong, .stat-row strong').forEach(el => {
    const text = el.textContent.trim();
    if (!SNOWFLAKE_RE.test(text)) return;

    const td    = el.closest('td') || el.closest('.stat-row');
    const label = (td ? _colHeader(td) : '') ||
                  td?.querySelector('span')?.textContent?.trim().toLowerCase() || '';

    let resolved = null;
    if (/user|uid|member|last.user|banned/.test(label))
      resolved = resolveUser(text, cache);
    else if (/channel|cid/.test(label))
      resolved = resolveChannel(text, cache);
    else if (/role|rid/.test(label))
      resolved = resolveRole(text, cache);
    else if (/categor|cat/.test(label))
      resolved = resolveCategory(text, cache);
    else
      resolved = resolveUser(text, cache) || resolveChannel(text, cache) || resolveRole(text, cache);

    if (resolved) { el.title = text; el.textContent = resolved; }
  });

  // 3. Standalone <strong> in stat-rows (e.g. last_user_id, ai channel_id)
  document.querySelectorAll('.stat-row').forEach(row => {
    const label = row.querySelector('span')?.textContent?.trim().toLowerCase() || '';
    const val   = row.querySelector('strong');
    if (!val || val.dataset.uid || val.dataset.cid) return; // already handled above
    const text  = val.textContent.trim();
    if (!SNOWFLAKE_RE.test(text)) return;

    let resolved = null;
    if (/user|member/.test(label))  resolved = resolveUser(text, cache);
    else if (/channel/.test(label)) resolved = resolveChannel(text, cache);
    else if (/role/.test(label))    resolved = resolveRole(text, cache);
    else                            resolved = resolveUser(text, cache) || resolveChannel(text, cache);

    if (resolved) { val.title = text; val.textContent = resolved; }
  });
}

// Fire it up when the menu board loads
document.addEventListener('DOMContentLoaded', resolveAll);
