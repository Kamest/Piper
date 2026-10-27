"""Runtime metadata never alters native tests, scope, errors or exit behavior."""
import json
import subprocess
import time
import unittest

import _env
import httpclient as hc
import httpruntime as rt


class RuntimeTest(unittest.TestCase):
    def execute(self, body, phase='response'):
        prefix, suffix = rt.bootstrap('__piper_runtime_abc_', 2, phase, ['old'], ['output'])
        code = '''const frames=[], tests={}, store={old:1};
const request={iteration:()=>null, variables:{get:n=>n==='output'?'path.xml':undefined}};
const print=s=>frames.push(s);
const nativeExit={};
const client={global:{vars:store,get:n=>store[n]}, log:()=>{},
  test:function(n,f){tests[n]=f;},exit:()=>{throw nativeExit;}};
try { PREFIX BODY SUFFIX } catch(e) { if(e!==nativeExit) throw e; }
for(const callback of Object.values(tests)) { try {callback();} catch(e) {} }
process.stdout.write(JSON.stringify({frames,keys:Object.keys(store),tests:Object.keys(tests)}));
'''.replace('PREFIX', prefix).replace('BODY', body).replace('SUFFIX', suffix)
        native = subprocess.run(['node', '-e', code], check=True, capture_output=True, text=True)
        result = json.loads(native.stdout)
        _, result['events'] = rt.extract('\n'.join(result.pop('frames')), '__piper_runtime_abc_')
        return result

    def test_pre_logs_callbacks_and_exit_do_not_use_private_global_keys_or_fake_tests(self):
        result = self.execute('client.log("pre"); client.global.vars.new=2;', 'pre')
        self.assertEqual(result['keys'], ['old', 'new'])
        self.assertEqual(result['tests'], [])
        self.assertEqual(result['events'][0]['text'], 'pre')
        result = self.execute('client.test("user",function(){client.log("callback");client.global.vars.callback=3;});')
        self.assertEqual(result['tests'], ['user'])
        self.assertEqual([e for e in result['events'] if e['kind'] == 'state'][-1]['globals']['callback'], 3)
        self.assertEqual(result['events'][-1]['kind'], 'finish')
        result = self.execute('client.log("before exit"); client.global.vars.exit=4; client.exit(); throw Error("unreachable");')
        self.assertEqual(result['events'][-1]['kind'], 'exit')
        self.assertEqual([e for e in result['events'] if e['kind'] == 'state'][-1]['globals']['exit'], 4)

    def test_failed_callback_keeps_its_error_and_last_state(self):
        result = self.execute('client.test("failing",()=>{client.global.vars.changed=5;throw Error("expected");});')
        self.assertEqual([e for e in result['events'] if e['kind'] == 'state'][-1]['globals']['changed'], 5)
        self.assertEqual(result['tests'], ['failing'])

    def test_response_finish_waits_for_every_callback_and_never_needs_a_consumer_pre_barrier(self):
        result = self.execute('client.test("first",()=>{client.global.vars.value=1;}); '
                              'client.test("second",()=>{client.global.vars.value=2;});')
        kinds = [event['kind'] for event in result['events']]
        self.assertEqual(kinds.count('finish'), 1)
        self.assertEqual(kinds[-1], 'finish')
        marker = '__piper_runtime_finish_'
        live = rt.Live(marker)
        for event in result['events']:
            live.feed((marker + json.dumps(event) + '\n').encode())
        self.assertEqual(live.finished(2, 0, time.monotonic() + 1)['globals']['value'], 2)
        self.assertEqual(live.barriers, {})
        with self.assertRaisesRegex(ValueError, 'did not finish'):
            live.finished(9, None, time.monotonic())

    def test_extract_does_not_treat_body_lookalikes_as_frames_and_rejects_invalid_data(self):
        marker = '__piper_runtime_secret_'
        body = 'body __piper_runtime_secret_{}\n__piper_runtime_other_{}'
        self.assertEqual(rt.extract(body, marker), (body, []))
        for payload in ('{}', 'null', 'invalid', json.dumps(dict(kind='state', request=True, phase='pre'))):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'runtime'):
                rt.extract(marker + payload, marker)
        with self.assertRaisesRegex(ValueError, 'limit'):
            rt.extract(marker + 'x' * (rt.MAX_STATE + 1), marker)

    def test_callbacks_attach_to_request_iteration_and_keep_combined_log_limit(self):
        rows = [dict(request_index=2, iteration=0), dict(request_index=2, iteration=1)]
        events = [dict(request=2, iteration=i, phase='response', kind='log', text=str(i)) for i in (0, 1)]
        events += [dict(request=2, iteration=1, phase='pre', kind='log', text='x' * rt.MAX_LOG)]
        rt.apply(rows, events)
        self.assertEqual(rows[0]['script_log'], '0\n')
        self.assertTrue(rows[1]['script_log'].startswith('1\n'))
        self.assertEqual(len(rows[1]['script_log']), rt.MAX_LOG)
        self.assertTrue(rows[1]['script_log_cut'])
        ambiguous = [dict(request_index=0, iteration=None), dict(request_index=0, iteration=None)]
        rt.apply(ambiguous, [dict(request=0, iteration=None, kind='state', globals={'unsafe':'ignored'})])
        self.assertNotIn('globals', ambiguous[0])
        exited = [dict(request_index=1, iteration=0)]
        rt.apply(exited, [dict(request=1, iteration=None, kind='exit')])
        self.assertTrue(exited[0]['script_exited'])

    def test_runtime_instrumentation_preserves_modules_and_original_line_map(self):
        source = '< {% client.log("pre"); %}\nGET https://example.test/\n\n> {% import "./x.js"; // end\n%}\n'
        rows = hc.runtime_rows([(line, i) for i, line in enumerate(source.split('\n'))],
                               '__piper_runtime_abc_', [], [], {})
        self.assertTrue(all('\n' not in line for line, _ in rows))
        self.assertIn('import "./x.js";', '\n'.join(line for line, _ in rows))
        self.assertTrue(all(origin is None or 0 <= origin < len(source.split('\n')) for _, origin in rows))
        self.assertNotIn('%}', rt.bootstrap('__piper_runtime_abc_', 0, 'pre', ['%}'], [])[0])

    def test_live_state_uses_completed_response_before_consumer_barrier_and_exact_iteration(self):
        marker = '__piper_runtime_live_'
        live = rt.Live(marker)
        def frame(kind, request, iteration, phase='response', **data):
            return (marker + json.dumps(dict(kind=kind, request=request, iteration=iteration, phase=phase, **data)) + '\n').encode()
        data = b'ordinary native output\n' + frame('state', 0, 0, globals={'path':'initial'}, variables={})
        data += frame('state', 0, 0, globals={'path':'callback-final'}, variables={})
        data += frame('state', 0, 1, globals={'path':'iteration-one'}, variables={})
        data += frame('ready', 1, None, 'pre')
        data += frame('state', 0, 0, globals={'path':'too-late'}, variables={})
        for byte in data:
            live.feed(bytes([byte]))
        deadline = time.monotonic() + .1
        self.assertEqual(live.before(0, 0, 1, 0, deadline)['globals']['path'], 'callback-final')
        self.assertEqual(live.before(0, 1, 1, None, deadline)['globals']['path'], 'iteration-one')
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            live.before(9, 0, 1, None, deadline)
        with self.assertRaisesRegex(ValueError, 'deadline'):
            live.before(0, 0, 8, None, time.monotonic())
        live.close()
        with self.assertRaisesRegex(ValueError, 'stream ended'):
            live.before(0, 0, 8, None, time.monotonic() + 30)

    def test_live_ignores_long_body_lines_but_bounds_frames_and_validates_state(self):
        marker = '__piper_runtime_live_'
        live = rt.Live(marker)
        live.feed(b'x' * (rt.MAX_STATE + 100) + b'\n')
        self.assertEqual(len(live.pending), 0)
        with self.assertRaisesRegex(ValueError, 'limit'):
            live.feed(marker.encode() + b'x' * (rt.MAX_STATE + 100) + b'\n')
        live = rt.Live(marker)
        with self.assertRaisesRegex(ValueError, 'state'):
            live.feed((marker + json.dumps(dict(kind='state', request=0, iteration=None, phase='response', globals=[], variables={})) + '\n').encode())

    def test_live_preserves_existing_character_limits_for_multibyte_state(self):
        marker = '__piper_runtime_live_'
        live = rt.Live(marker)
        event = dict(kind='state', request=0, iteration=None, phase='response', globals={'text':'😀' * 300000}, variables={})
        line = (marker + json.dumps(event, ensure_ascii=False) + '\n').encode()
        self.assertGreater(len(line), rt.MAX_STATE)
        for offset in range(0, len(line), 65536):
            live.feed(line[offset:offset + 65536])
        ready = dict(kind='ready', request=1, iteration=None, phase='pre')
        live.feed((marker + json.dumps(ready) + '\n').encode())
        self.assertEqual(live.before(0, 0, 1, None, time.monotonic() + 1)['globals']['text'], event['globals']['text'])

    def test_synthetic_pre_barriers_preserve_request_and_handler_ownership(self):
        source = 'GET https://example.test/first\n\n### Second\nGET https://example.test/second\n'
        rows = hc.runtime_rows([(line, i) for i, line in enumerate(source.split('\n'))],
                               '__piper_runtime_abc_', [], [], {}, barriers=True)
        requests = hc.parse('\n'.join(line for line, _ in rows))['requests']
        self.assertEqual([request['url'] for request in requests], ['https://example.test/first', 'https://example.test/second'])
        self.assertTrue(all(request['pre'] for request in requests))
