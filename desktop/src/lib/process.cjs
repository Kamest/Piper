'use strict';
const { StringDecoder } = require('node:string_decoder');
const { redact } = require('./policy.cjs');
const path = require('node:path');

/** No shell interpolation. Stream whole lines so split tokens cannot escape redaction. */
function runner(spawn, env) {
  return (command, args = [], { onLine, timeout = 30000, acceptFailure = false } = {}) => new Promise((resolve, reject) => {
    let child, output = '', settled = false;
    const finish = (err, code = 0) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      const label = path.basename(command);
      if (err || (code && !acceptFailure)) reject(new Error(err ? `${label}: ${err.message === 'timeout' ? 'command timed out' : 'could not start command'}` : `${label}: setup command exited with code ${code}`));
      else resolve({ code, stdout: output });
    };
    const timer = setTimeout(() => { child?.kill(); finish(new Error('timeout')); }, timeout);
    try { child = spawn(command, args, { env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }); }
    catch (e) { finish(e); return; }
    for (const [name, stream] of [['stdout', child.stdout], ['stderr', child.stderr]]) {
      const decoder = new StringDecoder('utf8'); let pending = '';
      const emit = line => {
        // stdout is private (the URL command returns its token here); only redacted lines leave this module.
        if (name === 'stdout') output = (output + line + '\n').slice(-4 * 1024 * 1024);
        onLine?.(redact(line));
      };
      stream.on('data', chunk => {
        pending += decoder.write(chunk);
        let i;
        while ((i = pending.indexOf('\n')) >= 0) { emit(pending.slice(0, i).replace(/\r$/, '')); pending = pending.slice(i + 1); }
        // Bound a malicious unterminated line without emitting a potentially partial secret.
        if (pending.length > 65536) pending = '[output line too long]';
      });
      stream.on('end', () => { pending += decoder.end(); if (pending) emit(pending); });
    }
    child.on('error', e => finish(e));
    child.on('close', code => finish(null, code ?? 1));
  });
}
module.exports = { runner };
