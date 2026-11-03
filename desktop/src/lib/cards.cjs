'use strict';

/** The session a queue action points at (its subject), '' when none. */
const subjectSession = a => String(a?.subject?.session ?? (a?.subject?.type === 'session' ? a.subject.key : '') ?? '');

/**
 * How a confirmation card can be answered outside a Piper window (tray menu, notification buttons), from the
 * queue action piperd words for it (feed.py: key `p:<id>`, level `confirm`, decide ops). Deny is always offered.
 * Allow only where the GUI shows a plain Allow that `y` may press: never a red (danger: production, a host outside
 * the environment, weaker isolation, a hidden file) or a prompt card (a passphrase, a browser sign-in), never an
 * "always" grant. null: not a card this app can answer (an older piperd without decide ops, another level).
 */
function cardChoices(action) {
  if (!action || action.level !== 'confirm' || typeof action.key !== 'string' || !action.key.startsWith('p:')) return null;
  const ops = (Array.isArray(action.actions) ? action.actions : []).filter(o => o && o.op === 'decide' && o.args && typeof o.args === 'object');
  const deny = ops.find(o => o.args.ok === false);
  if (!deny || deny.args.id === undefined || deny.args.id === null) return null;
  const id = String(deny.args.id);
  if (!/^[\w.:-]{1,128}$/.test(id) || action.key !== 'p:' + id) return null;
  const allowOp = ops.find(o => o.args.ok === true && !o.args.always && !o.args.session_always && String(o.args.id) === id);
  const allow = !!allowOp && !allowOp.args.danger && !allowOp.args.prompt;
  return { id, allow, title: String(action.title || 'Confirmation').slice(0, 140), session: subjectSession(action) };
}

/** A session waiting at its prompt can get a typed reply (the queue's Reply op), else ''. */
function replyTarget(action) {
  const op = (Array.isArray(action?.actions) ? action.actions : []).find(o => o && o.op === 'reply');
  const id = String(op?.args?.session ?? '');
  return /^[\w.:-]{1,128}$/.test(id) ? id : '';
}

module.exports = { cardChoices, replyTarget, subjectSession };
