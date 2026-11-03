'use strict';
const { BLOCKING } = require('./notify.cjs');

/** Accept only the local service's root URL, never credentials or a foreign port. */
function parseServiceURL(value, port) {
  const lines = String(value).trim().split(/\r?\n/);
  if (lines.length !== 1) throw new Error('Piper did not return one local URL');
  let u;
  try { u = new URL(lines[0]); } catch { throw new Error('Piper returned an invalid URL'); }
  const token = new URLSearchParams(u.hash.slice(1)).get('token');
  if (u.origin !== `http://127.0.0.1:${port}` || u.pathname !== '/' || u.search || u.username || u.password || !/^[\w-]{16,512}$/.test(token || '')) {
    throw new Error('Piper returned an invalid local URL');
  }
  return { url: u.href, origin: u.origin, token };
}

/** Redact at the process boundary, including installer output and CLI errors. */
function redact(value) {
  return String(value).replace(/((?:#|[?&])token=)[^\s&]+/gi, '$1[redacted]')
    .replace(/(Bearer\s+)\S+/gi, '$1[redacted]')
    .replace(/((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,]+/gi, '$1[redacted]')
    .replace(/(https?:\/\/)[^/\s@]+@/g, '$1[redacted]@');
}

function navigation(url, origin) {
  try {
    const u = new URL(url);
    if (u.origin === origin && !u.username && !u.password) return 'internal';
    if (['https:', 'http:', 'mailto:'].includes(u.protocol) && !u.username && !u.password) return 'external';
  } catch { /* invalid URL */ }
  return 'deny';
}

/** Packaged Store apps are updated exclusively by the Store. */
function updatesEnabled(packaged, windowsStore) {
  return packaged && windowsStore !== true;
}

/** The desktop channel uses desktop-v tags, separate from core releases. */
function updateDecision(platform, signed, version, url) {
  if (!/^desktop-v\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(version || '')) return 'ignore';
  try {
    const u = new URL(url);
    if (u.origin !== 'https://github.com' || !u.pathname.startsWith('/Kamest/Piper/releases/')) return 'ignore';
  } catch { return 'ignore'; }
  return platform === 'darwin' && !signed ? 'download' : 'install-on-quit';
}

/**
 * API queue actions are authoritative; session states are a fallback for old piperd. The badge counts only what
 * blocks work (confirmation cards, permission/trust, blocked or security actions; an action without a level too).
 * Sessions waiting at their prompt and the inbox (CI, forge, production) are separate counts.
 */
function trayModel(state, online = true) {
  const sessions = Array.isArray(state?.sessions) ? state.sessions : [];
  const actions = Array.isArray(state?.queue?.actions) ? state.queue.actions : null;
  let needs, waiting, inbox;
  if (actions) {
    needs = actions.filter(a => !a.level || BLOCKING.includes(a.level));
    waiting = actions.filter(a => a.level === 'waiting').length;
    inbox = actions.length - needs.length - waiting;
  } else {
    const pending = Array.isArray(state?.pending) ? state.pending : [];
    needs = [...pending, ...sessions.filter(s => ['attention', 'trust', 'blocked'].includes(s.state))];
    waiting = sessions.filter(s => s.state === 'waiting').length;
    inbox = 0;
  }
  return { online, sessions: sessions.length, count: needs.length, waiting, inbox,
    items: needs.slice(0, 8).map(a => String(a.title || a.text || a.label || a.kind || 'Waiting for you').slice(0, 140)),
    poll: online ? 5000 : 30000 };
}

module.exports = { parseServiceURL, redact, navigation, updatesEnabled, updateDecision, trayModel };
