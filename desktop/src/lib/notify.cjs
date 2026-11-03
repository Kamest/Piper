'use strict';

/** Session states the web GUI announces, with its labels (`LABEL` in piper/web/src/main.ts). */
const LABEL = { waiting: 'waiting for you', attention: 'needs attention', trust: 'waiting for trust' };
/** Queue levels that block work (the GUI's NEEDS_LEVELS without `waiting`). */
const BLOCKING = ['security', 'permission', 'confirm', 'blocked'];

const { cardChoices, subjectSession } = require('./cards.cjs');

/** A session set aside for later waits quietly, unless it asks for trust or a blocking action points at it. */
function laterQuiet(s, actions) {
  if (!s.later || s.state === 'trust') return false;
  return !actions.some(a => BLOCKING.includes(a.level) && subjectSession(a) === s.id);
}

/**
 * The desktop notifications of one `/api/state` poll, decided in the main process so they arrive with the
 * window hidden or closed. Mirrors the web GUI's notifyTransitions(): a session that changes into
 * waiting/attention/trust and a new confirmation card. The first poll (memo null) only records. While a Piper
 * window has focus nothing is returned: the GUI itself notifies then, as in a browser.
 * Pure: returns the next memo and the notifications to post; `card` / `reply` say how one can be answered in place.
 */
function notifyPlan(memo, state, focused) {
  if (!state) return { memo: null, notes: [] };
  const sessions = Array.isArray(state.sessions) ? state.sessions : [];
  const pending = Array.isArray(state.pending) ? state.pending : [];
  const actions = Array.isArray(state?.queue?.actions) ? state.queue.actions : [];
  const next = { states: {}, pending: pending.map(p => String(p.id)) };
  const notes = [];
  for (const s of sessions) {
    const id = String(s.id);
    next.states[id] = s.state;
    const was = memo?.states[id];
    if (was && was !== s.state && LABEL[s.state] && !laterQuiet(s, actions)) {
      // a session waiting at its prompt can be answered from the notification (Reply); the others only open
      notes.push({ title: `${s.title || id}: ${LABEL[s.state]}`, body: String(s.detail || s.project || ''), tag: id, session: id,
        reply: s.state === 'waiting' ? id : '' });
    }
  }
  if (memo) {
    for (const p of pending) {
      const id = String(p.id);
      if (memo.pending.includes(id)) continue;
      // piperd words the card in its queue action; an older piperd without it gets the kind
      const action = actions.find(a => a.key === 'p:' + id);
      const title = action?.title || `${p.title || 'Piper'}: ${String(p.kind || 'confirmation').replace(/_/g, ' ')}`;
      // card: the Allow / Deny buttons the notification may carry (cardChoices: never a red or a prompt card's Allow)
      notes.push({ title, body: 'Waiting for your confirmation', tag: 'p:' + id, session: String(p.session || ''), card: cardChoices(action) });
    }
  }
  return { memo: next, notes: focused ? [] : notes.map(n => ({ ...n, title: n.title.slice(0, 200), body: n.body.slice(0, 2000) })) };
}

/** A notification the GUI posts that the main process already posts itself (session state or confirmation card). */
function ownedByMain(data) {
  const tag = String(data?.tag || ''), title = String(data?.title || '');
  return tag.startsWith('p:') || Object.values(LABEL).some(label => title.endsWith(': ' + label));
}

/** osascript arguments for the unsigned-macOS fallback: the text travels as argv, never as script source. */
function osascriptArgs(title, body) {
  return ['-e', 'on run argv', '-e', 'display notification (item 2 of argv) with title (item 1 of argv)', '-e', 'end run',
    '--', String(title).slice(0, 200), String(body).slice(0, 2000)];
}

module.exports = { notifyPlan, ownedByMain, osascriptArgs, BLOCKING, LABEL };
