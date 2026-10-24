// Screenshots leave the read-only sandbox through the TAP stream. node --test splits long output into several
// "# ..." diagnostic lines at arbitrary points, so a shot carries its length and an end marker and the host
// harness joins the continuation lines.
export const shotLine = (name, bytes) => `PIPER_SHOT ${name} ${bytes.length} ${bytes.toString('base64')} PIPER_SHOT_END`;

const LIMIT = 16 * 1024 * 1024;
const SHOT = /^PIPER_SHOT ([A-Za-z0-9-]{1,40}) (\d{1,9}) ([A-Za-z0-9+/=]+) PIPER_SHOT_END$/;

/** Feed TAP lines; returns null for a line to print, or { done, shot?: {name, bytes}, error? } while collecting. */
export function shotCollector() {
  let pending = null;
  return function feed(line) {
    const text = line.replace(/^\s*# ?/, '');
    if (pending === null) {
      if (!/^\s*# PIPER_SHOT /.test(line)) return null;
      pending = '';
    }
    pending += text;
    if (!pending.endsWith(' PIPER_SHOT_END') && pending.length < LIMIT) return { done: false };
    const match = SHOT.exec(pending); pending = null;
    if (!match) return { done: true, error: 'malformed screenshot output' };
    const bytes = Buffer.from(match[3], 'base64');
    if (bytes.length !== Number(match[2])) return { done: true, error: `screenshot ${match[1]} is incomplete` };
    return { done: true, shot: { name: match[1], bytes } };
  };
}
