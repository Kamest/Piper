"""Bounded native script metadata, independent of JUnit's test-registration phase.

This channel is untrusted result data, never a filesystem/network capability. The
native JUnit report remains the authority for test outcomes. No user globals or
request variables are used as transport storage.
"""
from __future__ import annotations

import json
import threading
import time

MAX_LOG = 64 << 10
MAX_STATE = 1 << 20


def bootstrap(marker: str, request: int, phase: str, names: list[str], variables: list[str]) -> tuple[str, str]:
    """Return private prefix/suffix for one native script scope, including callbacks/exit."""
    # All generated literals are JSON and must not terminate the enclosing HTTP script.
    def literal(value):
        return json.dumps(value).replace('%', r'\u0025')

    symbol = '__piper_runtime_' + marker.rstrip('_').rsplit('_', 1)[-1]
    code = r''';const SYMBOL = (function(nativePrint, nativeLog, nativeTest, nativeExit) {
let left = LIMIT;
let rootDone = false, waitingTests = 0;
if (PHASE === "response") emit({kind:"identity"});
function emit(data) {
  data.request = INDEX; data.phase = PHASE;
  data.iteration = typeof request.iteration === "function" ? request.iteration() : null;
  const value = JSON.stringify(data);
  if (value.length > STATE_LIMIT) throw new Error("HTTP script runtime metadata exceeds 1 MiB");
  nativePrint(MARKER + value);
}
function state(done) {
  if (done === true) rootDone = true;
  const keys = NAMES.slice();
  Object.keys(client.global.vars || {}).forEach(n => { if (!keys.includes(n)) keys.push(n); });
  const globals = Object.create(null), unset = [], variables = Object.create(null);
  keys.forEach(n => { const value = client.global.get(n); if (value === undefined) unset.push(n); else globals[n] = value; });
  VARIABLES.forEach(n => { variables[n] = request.variables.get(n); });
  emit({kind:"state", globals, unset, variables});
  if (PHASE === "pre") emit({kind:"ready"});
  if (PHASE === "response" && rootDone && waitingTests === 0) emit({kind:"finish"});
}
client.log = function(text) {
  const result = nativeLog.apply(this, arguments);
  if (left > 0) {
    const line = typeof text === "string" ? text : text === null ||
      ["undefined","number","boolean","bigint"].includes(typeof text) ? String(text) : "[non-string log message]";
    const cut = line.length + 1 > left;
    emit({kind:"log", text:line.slice(0, Math.max(0, left - 1)), cut});
    left = cut ? 0 : left - line.length - 1;
  }
  return result;
};
if (typeof nativeTest === "function") client.test = function(name, callback) {
  if (typeof callback === "function") waitingTests++;
  return nativeTest.call(this, name, typeof callback !== "function" ? callback : function() {
    try { return callback.apply(this, arguments); } finally { waitingTests--; state(); }
  });
};
if (typeof nativeExit === "function") client.exit = function() {
  waitingTests = 0; state(true); emit({kind:"exit"}); return nativeExit.apply(this, arguments);
};
return state;
})(print, client.log, client.test, client.exit);'''
    replacements = {'SYMBOL': symbol, 'STATE_LIMIT': str(MAX_STATE), 'LIMIT': str(MAX_LOG),
                    'INDEX': str(request), 'PHASE': literal(phase), 'MARKER': literal(marker),
                    'NAMES': literal(names), 'VARIABLES': literal(variables)}
    # Replace tokens in one pass: JSON user data is never interpreted as a template.
    import re
    code = re.sub(r'\b(?:' + '|'.join(replacements) + r')\b', lambda m: replacements[m[0]], code)
    return code.replace('\n', ' '), ';' + symbol + '(true);'


def extract(console: str, marker: str) -> tuple[str, list[dict]]:
    """Strip only this run's JSON frames before parsing the native console as HTTP."""
    lines, events = [], []
    total = 0
    for line in console.split('\n'):
        if not line.startswith(marker):
            lines.append(line)
            continue
        payload = line[len(marker):]
        total += len(payload)
        if len(payload) > MAX_STATE or total > 16 << 20:
            raise ValueError('HTTP script runtime metadata exceeds its limit; requests may already have run; not retried')
        try:
            event = json.loads(payload)
        except ValueError:
            raise ValueError('invalid HTTP script runtime metadata; requests may already have run; not retried') from None
        if (not isinstance(event, dict) or type(event.get('request')) is not int or event['request'] < 0 or
                event.get('phase') not in ('pre', 'response') or event.get('kind') not in ('log', 'state', 'exit', 'identity', 'ready', 'finish') or
                event.get('iteration') is not None and type(event['iteration']) is not int):
            raise ValueError('invalid HTTP script runtime identity; requests may already have run; not retried')
        events.append(event)
    return '\n'.join(lines), events


class Live:
    """Observe the existing stdout pipe; never rewrite bytes or grant child capabilities.

    A consumer's pre-handler barrier orders all preceding response callbacks. State
    after that barrier cannot change an earlier export's unresolved destination.
    Both transport and retained metadata have fixed aggregate bounds.
    """
    def __init__(self, marker):
        self.marker = marker
        self.pending = bytearray()
        self.discard = False
        self.total = 0
        self.states, self.barriers = [], {}
        self.completed = {}
        self.sequence = 0
        self.closed = False
        self.condition = threading.Condition()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def feed(self, data):
        for part in data.splitlines(keepends=True):
            ended = part.endswith(b'\n')
            if not self.discard:
                self.pending.extend(part)
                if len(self.pending) > 4 * MAX_STATE + len(self.marker) + 8:
                    if self.pending.startswith(self.marker.encode()):
                        raise ValueError('HTTP script runtime metadata exceeds its limit')
                    self.pending.clear()
                    self.discard = True
            if ended:
                if not self.discard and self.pending.startswith(self.marker.encode()):
                    self._line(bytes(self.pending).rstrip(b'\r\n'))
                self.pending.clear()
                self.discard = False

    def _line(self, line):
        value = line.decode('utf-8', 'strict')
        self.total += len(value) - len(self.marker)
        if self.total > 16 << 20:
            raise ValueError('HTTP script runtime metadata exceeds its limit')
        _, events = extract(value, self.marker)
        with self.condition:
            for event in events:
                self.sequence += 1
                identity = event['request'], event['iteration']
                if event['kind'] == 'state' and event['phase'] == 'response':
                    if not isinstance(event.get('globals'), dict) or not isinstance(event.get('variables'), dict):
                        raise ValueError('invalid HTTP script runtime state')
                    self.states.append((self.sequence, identity, event))
                elif event['kind'] == 'ready' and event['phase'] == 'pre':
                    self.barriers[identity] = self.sequence
                elif event['kind'] == 'finish' and event['phase'] == 'response':
                    for _, candidate, state in reversed(self.states):
                        if candidate == identity:
                            self.completed[identity] = state
                            break
            self.condition.notify_all()

    def before(self, producer, iteration, consumer, consumer_iteration, deadline):
        def identities(index, value):
            return [(index, value)] + ([(index, 0 if value is None else None)] if value in (None, 0) else [])
        with self.condition:
            while True:
                barrier = next((self.barriers[key] for key in identities(consumer, consumer_iteration)
                                if key in self.barriers), None)
                if barrier is not None:
                    break
                if self.closed:
                    raise ValueError('HTTP script runtime stream ended before its dependency barrier')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('HTTP script runtime state deadline')
                self.condition.wait(min(.1, remaining))
            candidates = identities(producer, iteration)
            for sequence, identity, event in reversed(self.states):
                if sequence < barrier and identity in candidates:
                    return event
        raise ValueError('HTTP response runtime state is unavailable')

    def finished(self, producer, iteration, deadline):
        """A pre-import cannot wait on its own pre-handler suffix. Wait on the producer."""
        keys = [(producer, iteration)] + ([(producer, 0 if iteration is None else None)] if iteration in (None, 0) else [])
        with self.condition:
            while True:
                for key in keys:
                    if key in self.completed:
                        return self.completed[key]
                remaining = deadline - time.monotonic()
                if self.closed or remaining <= 0:
                    raise ValueError('HTTP response runtime state did not finish before its code dependency')
                self.condition.wait(min(.1, remaining))


def apply(responses: list[dict], events: list[dict]) -> None:
    """Associate script data with native response occurrence/iteration, never just its name."""
    by_identity = {}
    for row in responses:
        by_identity.setdefault((row.get('request_index'), row.get('iteration')), []).append(row)
    pending = {}
    for event in events:
        identity = event['request'], event['iteration']
        # A regular native request can report null or 0 depending on the engine phase.
        candidates = by_identity.get(identity)
        if not candidates and identity[1] in (None, 0):
            candidates = by_identity.get((identity[0], 0 if identity[1] is None else None))
        candidates = candidates or []
        if len(candidates) != 1:
            continue  # incomplete/ambiguous native reports cannot publish runtime state
        row = candidates[0]
        if event['kind'] == 'log' and isinstance(event.get('text'), str):
            if id(row) not in pending:
                row['script_log'] = ''
                row['script_log_cut'] = False
                pending[id(row)] = True
            remaining = max(0, MAX_LOG - len(row['script_log']))
            text = event['text'] + '\n'
            row['script_log'] += text[:remaining]
            row['script_log_cut'] |= bool(event.get('cut')) or len(text) > remaining
        elif event['kind'] == 'state' and isinstance(event.get('globals'), dict):
            row.update(globals=event['globals'], unset=event.get('unset', []),
                       variables=event.get('variables', {}), captured=True)
        elif event['kind'] == 'exit':
            row['script_exited'] = True
