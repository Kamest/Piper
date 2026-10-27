"""Untrusted native RPC cannot redirect host reads/writes or outlive its run."""
import json
import os
import tempfile
import time
import unittest

import _env
from httpbroker import Broker, Inputs, Generated, preview


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = self.temp.name

    def request(self, broker, payload, number=0):
        path = os.path.join(broker.path, 'request-%d.json' % number)
        with open(path, 'w') as output:
            json.dump(payload, output)
        return self.reply(broker.path, number)

    def reply(self, directory, number=0):
        path = os.path.join(directory, 'reply-%d.json' % number)
        deadline = time.monotonic() + 2
        while not os.path.isfile(path) and time.monotonic() < deadline:
            time.sleep(.01)
        with open(path) as result:
            return json.load(result)

    def test_refusal_does_not_return_sensitive_callback_exception_or_repeat_the_callback(self):
        calls = []
        def callback(message, deadline):
            calls.append(message)
            self.assertGreater(deadline, time.monotonic())
            raise ValueError('synthetic-private-credential')
        with Broker(self.work, callback, 2) as broker:
            reply = self.request(broker, {'kind': 'file', 'reference': 'inside'})
            self.assertFalse(reply['ok'])
            self.assertNotIn('synthetic-private-credential', json.dumps(reply))
            self.assertEqual(len(calls), 1)
        self.assertFalse(broker.thread.is_alive())

    def test_message_symlinks_and_hardlinks_never_reach_the_host_callback(self):
        for link in ('symlink', 'hardlink'):
            with self.subTest(link=link), tempfile.TemporaryDirectory(dir=self.work) as directory:
                target = os.path.join(directory, 'outside')
                with open(target, 'w') as output:
                    output.write('{"kind":"file"}')
                calls = []
                with Broker(directory, lambda *args: calls.append(args) or {}, 2) as broker:
                    destination = os.path.join(broker.path, 'request-0.json')
                    (os.symlink if link == 'symlink' else os.link)(target, destination)
                    self.assertFalse(self.reply(broker.path)['ok'])
                self.assertEqual(calls, [])
                with open(target) as original:
                    self.assertEqual(original.read(), '{"kind":"file"}')

    def test_reply_temp_symlink_cannot_overwrite_a_host_file(self):
        target = os.path.join(self.work, 'outside')
        with open(target, 'w') as output:
            output.write('preserve')
        with Broker(self.work, lambda *args: {'path': 'approved-snapshot'}, 2) as broker:
            os.symlink(target, os.path.join(broker.path, 'reply-0.json.tmp'))
            with open(os.path.join(broker.path, 'request-0.json'), 'w') as output:
                output.write('{}')
            broker.thread.join(1)
        with open(target) as original:
            self.assertEqual(original.read(), 'preserve')

    def test_replacing_the_runtime_directory_does_not_redirect_host_writes(self):
        calls = []
        with Broker(self.work, lambda *args: calls.append(args) or {'path': 'snapshot'}, 2) as broker:
            pinned = broker.path + '-original'
            os.rename(broker.path, pinned)
            outside = os.path.join(self.work, 'replacement')
            os.mkdir(outside)
            os.symlink(outside, broker.path)
            with open(os.path.join(pinned, 'request-0.json'), 'w') as output:
                output.write('{}')
            self.assertTrue(self.reply(pinned)['ok'])
            self.assertEqual(os.listdir(outside), [])
        self.assertEqual(len(calls), 1)

    def test_oversized_and_non_object_messages_are_rejected(self):
        for data in ('[]', '{', 'x' * (65536 + 1)):
            with tempfile.TemporaryDirectory(dir=self.work) as directory:
                calls = []
                with Broker(directory, lambda *args: calls.append(args) or {}, 2) as broker:
                    with open(os.path.join(broker.path, 'request-0.json'), 'w') as output:
                        output.write(data)
                    self.assertFalse(self.reply(broker.path)['ok'])
                self.assertFalse(calls)

    def test_consumed_messages_are_removed_and_collections_can_exceed_old_256_rpc_limit(self):
        calls = []
        with Broker(self.work, lambda message, deadline: calls.append(message) or {}, 15) as broker:
            for number in range(260):
                self.assertTrue(self.request(broker, {'kind':'cookie_select', 'url':'https://example.test'}, number)['ok'])
                os.unlink(os.path.join(broker.path, 'reply-%d.json' % number))
            deadline = time.monotonic() + 1
            while os.listdir(broker.path) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(os.listdir(broker.path), [])
        self.assertEqual(len(calls), 260)

    def test_aggregate_traffic_remains_bounded(self):
        from unittest import mock
        calls = []
        with mock.patch('httpbroker.MAX_TOTAL', 5), Broker(self.work, lambda *args: calls.append(args) or {}, 2) as broker:
            self.assertFalse(self.request(broker, {'kind': 'cookie_select'})['ok'])
            broker.thread.join(1)
            self.assertFalse(broker.thread.is_alive())
        self.assertFalse(calls)


class InputsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.join(self.temp.name, 'project')
        self.work = os.path.join(self.temp.name, 'native')
        os.makedirs(self.root + '/api')
        self.approved = []
        self.inputs = Inputs(self.root, self.work, lambda p: p.startswith('hidden/'),
                             lambda info, deadline: self.approved.append(info), max_bytes=1024)
        self.addCleanup(self.inputs.close)
        self.reference = self.inputs.add('api/main.http', '{{file}}').removeprefix('piper-runtime:').split(':')[0]

    def read(self, reference):
        return self.inputs({'kind':'file', 'reference':self.reference + ':' + reference}, time.monotonic() + 2)

    def write(self, path, data=b'original'):
        with open(os.path.join(self.root, path), 'wb') as output:
            output.write(data)

    def test_approval_binds_exact_bytes_even_if_original_changes_during_the_card(self):
        self.write('api/input.json', b'{"original":true}')
        def approve(info, deadline):
            self.approved.append(info)
            self.write('api/input.json', b'{"replaced":true}')
        self.inputs.approve = approve
        result = self.read('input.json')
        with open(result['path'], 'rb') as data:
            self.assertEqual(data.read(), b'{"original":true}')
        self.assertEqual(self.approved[0]['path'], 'api/input.json')
        self.assertEqual(self.approved[0]['size'], 17)
        self.assertTrue(result['path'].endswith('.json'))

    def test_the_same_dependency_uses_its_approved_snapshot_for_the_rest_of_the_run(self):
        self.write('api/input.bin', b'approved')
        first = self.read('input.bin')
        self.write('api/input.bin', b'replaced')
        second = self.read('./input.bin')
        self.assertEqual(first, second)
        self.assertEqual(len(self.approved), 1)
        with open(second['path'], 'rb') as data:
            self.assertEqual(data.read(), b'approved')

    def test_new_generated_bytes_get_a_new_snapshot_and_approval_instead_of_reusing_an_older_file(self):
        self.write('api/input.bin', b'old-project-file')
        produced = {'data':b'first-response', 'stage':'private-stage-0'}
        self.inputs.generated = lambda *args: dict(produced)
        first = self.read('input.bin')
        with open(first['path'],'rb') as data:
            self.assertEqual(data.read(), b'first-response')
        self.assertEqual(self.read('input.bin'), first)
        produced.update(data=b'second-response', stage='private-stage-1')
        second = self.read('input.bin')
        self.assertNotEqual(first['path'], second['path'])
        with open(second['path'],'rb') as data:
            self.assertEqual(data.read(), b'second-response')
        self.assertEqual(len(self.approved), 2)
        self.assertTrue(all(info['generated'] for info in self.approved))

    def test_hidden_outside_git_and_unresolved_paths_never_get_an_approval_or_snapshot(self):
        for path in ('../../outside', '../hidden/password', '../.git/config', '{{missing}}', '~/.ssh/key',
                     'file:///private/key', 'input\\evil', 'nul\0name'):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.read(path)
        self.assertEqual(self.approved, [])
        self.assertEqual(os.listdir(self.inputs.directory), [])

    def test_symlinks_and_hardlinks_never_read_a_runtime_dependency(self):
        self.write('api/original')
        os.symlink('original', self.root + '/api/symlink')
        os.link(self.root + '/api/original', self.root + '/api/hardlink')
        for path in ('symlink', 'hardlink'):
            with self.assertRaises((OSError, ValueError)):
                self.read(path)
        self.assertEqual(self.approved, [])

    def test_unknown_messages_and_byte_limit_never_reach_approval(self):
        self.write('api/large', b'x' * 1025)
        with self.assertRaises(ValueError):
            self.read('large')
        for message in ({'kind':'command', 'reference':self.reference + ':inside'},
                        {'kind':'file', 'reference':'unknown:inside'},
                        {'kind':'file', 'reference':self.reference + ':inside', 'work':'/host'}):
            with self.assertRaises(ValueError):
                self.inputs(message, time.monotonic() + 2)
        self.assertEqual(self.approved, [])

    def test_input_cannot_template_host_environment_and_refusal_writes_nothing(self):
        import re
        self.inputs.forbidden = re.compile(r'\{\{\$processEnv')
        self.write('api/input.json', b'{"bad":"{{$processEnv HOME}}"}')
        with self.assertRaises(ValueError):
            self.read('input.json')
        self.write('api/input.json')
        self.inputs.approve = lambda *args: (_ for _ in ()).throw(ValueError('declined'))
        with self.assertRaisesRegex(ValueError, 'declined'):
            self.read('input.json')
        self.assertEqual(os.listdir(self.inputs.directory), [])


class GeneratedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = self.temp.name
        self.root = os.path.join(self.work, 'project')
        os.makedirs(self.root)
        self.base = os.path.join(self.work, 'response')
        self.directives = [dict(request=0, staged=self.base, overwrite=True)]
        def name(root, path, overwrite, reserved):
            original, index = path, 0
            while not overwrite and (os.path.exists(os.path.join(root,path)) or path in reserved):
                index += 1
                path = original + '-' + str(index)
            return path
        self.generated = Generated(self.root, self.work, self.directives, lambda d,p,i:p, name, 1024)

    def stage(self, iteration, path, data):
        stage = self.base + ('-' + str(iteration) if iteration else '')
        with open(stage,'wb') as output:
            output.write(data)
        with open(stage + '.target.json','w') as output:
            json.dump({'path':path}, output)
        return stage

    def read(self, path, index=1):
        return self.generated(path, {'index':index}, time.monotonic() + 2)

    def test_native_iteration_targets_keep_distinct_bytes_and_last_overwrite_wins(self):
        self.stage(0,'first',b'first-response')
        self.stage(1,'second',b'second-response')
        self.assertEqual(self.read('first')['data'], b'first-response')
        self.assertEqual(self.read('second')['data'], b'second-response')
        self.stage(2,'first',b'last-response')
        self.assertEqual(self.read('first')['data'], b'last-response')
        self.assertIsNone(self.read('unrelated'))
        self.assertIsNone(self.read('first', index=-1))

    def test_non_overwrite_names_include_existing_files_and_earlier_unpublished_exports(self):
        with open(os.path.join(self.root,'existing'),'wb') as output:
            output.write(b'preserve')
        self.directives[0]['overwrite'] = False
        self.stage(0,'existing',b'new-first')
        self.stage(1,'existing',b'new-second')
        self.assertIsNone(self.read('existing'))
        self.assertEqual(self.read('existing-1')['data'], b'new-first')
        self.assertEqual(self.read('existing-2')['data'], b'new-second')

    def test_incomplete_symlink_hardlink_or_malformed_stages_never_supply_bytes(self):
        stage = self.stage(0,'target',b'valid')
        os.unlink(stage)
        self.assertIsNone(self.read('target'))
        outside = os.path.join(self.work,'outside')
        with open(outside,'wb') as output:
            output.write(b'not-a-source-capability')
        os.symlink(outside,stage)
        with self.assertRaises((OSError,ValueError)):
            self.read('target')
        os.unlink(stage)
        os.link(outside,stage)
        with self.assertRaises(ValueError):
            self.read('target')
        os.unlink(stage)
        self.stage(0,'target',b'valid')
        with open(stage + '.target.json','w') as output:
            json.dump({'path':'target','extra':True},output)
        with self.assertRaises(ValueError):
            self.read('target')



class PreviewTest(unittest.TestCase):
    def test_masks_a_complete_secret_before_clipping_and_obeys_utf8_line_and_byte_limits(self):
        secret = 's' * 5000
        result = preview(('first=' + secret + '\n' + 'é' * 1000 + '\n' * 30).encode(),
                         lambda text: text.replace(secret, '••••'))
        text = result['preview']
        self.assertIn('••••', text)
        self.assertNotIn('ssss', text)
        self.assertLessEqual(len(text.encode()), 2048)
        self.assertLessEqual(len(text.splitlines()), 12)
        self.assertTrue(all(len(line) <= 160 for line in text.splitlines()))
        self.assertIsNone(result['binary'])
        self.assertIn('bounded', result['preview_note'])

    def test_binary_has_no_accidental_decoded_preview(self):
        for data in (b'\x00private', b'\xffprivate'):
            result = preview(data, lambda value: value)
            self.assertIsNone(result['preview'])
            self.assertEqual(result['binary'], 'application/octet-stream')


if __name__ == '__main__':
    unittest.main()
