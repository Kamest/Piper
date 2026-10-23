"""Blind properties boundary: parser oracle, no disclosure, fresh cards, paths and write guards."""
import base64
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import propertyfiles as pf
import sessions


class ParserTest(unittest.TestCase):
    def keys(self, text):
        return [e.key for e in pf.entries(text)]

    def test_separators_escaped_keys_unicode_and_empty_keys(self):
        text = "# comment=secret\n! other:secret\n a = hidden\nb:secret\nc secret\nbare\n=empty-key-value\n" + \
               r"space\ key=secret" + "\n" + r"colon\:key:secret" + "\n" + r"eq\=key=secret" + "\n" + \
               r"\u0061lias=secret" + "\n" + r"emoji\ud83d\ude00=secret"
        self.assertEqual(self.keys(text), ['a', 'b', 'c', 'bare', '', 'space key', 'colon:key', 'eq=key', 'alias', 'emoji😀'])

    def test_continued_values_never_become_keys_or_comments(self):
        for newline in ('\n', '\r', '\r\n'):
            text = ('password=part\\' + newline + '   secret=value\\' + newline + ' #still-secret' + newline +
                    'real=yes' + newline + '# ignored\\' + newline + 'next=yes')
            with self.subTest(newline=newline):
                self.assertEqual(self.keys(text), ['password', 'real', 'next'])
                result = pf.update(text, {'password': 'new'})
                self.assertNotIn('still-secret', result)
                self.assertEqual(self.keys(result), ['password', 'real', 'next'])

    def test_only_java_line_breaks_split_records(self):
        self.assertEqual(self.keys('password=secret\vlooksLikeKey=value\u0085other=value\u2028another=value\nreal=yes'),
                         ['password', 'real'])
        self.assertEqual(self.keys('a\fb\n'), ['a'])

    def test_backslash_parity_and_continued_keys(self):
        self.assertEqual(self.keys('a=even\\\\\nnext=x'), ['a', 'next'])
        self.assertEqual(self.keys('a=odd\\\\\\\nnext=x'), ['a'])
        self.assertEqual(self.keys('long\\\n key=value'), ['longkey'])
        self.assertEqual(self.keys('\\\n#comment\nreal=value'), ['real'])

    def test_preserves_unselected_bytes_comments_crlf_and_no_final_newline(self):
        original = '# private comment\r\n  leave : exact\\ value  \r\nchange : old\r\nlast=untouched'
        changed = pf.update(original, {'change': 'new'})
        self.assertEqual(changed, '# private comment\r\n  leave : exact\\ value  \r\nchange=new\r\nlast=untouched')
        self.assertEqual(pf.update('last=old', {'last': 'new'}), 'last=new')

    def test_updates_all_duplicate_decoded_keys(self):
        original = 'key=old\nother=stay\n\\u006bey:older\nkey\\\n =oldest\n'
        self.assertEqual(pf.update(original, {'key': 'new'}), 'key=new\nother=stay\nkey=new\nkey=new\n')

    def test_added_values_cannot_inject_keys_comments_or_continuations(self):
        values = {'injected\nkey: =#!': 'first\r\nsecond=bad\n#comment\\', 'unicode': 'ζωή😀', '': '\x01\t '}
        output = pf.update('keep=private', values)
        self.assertEqual(self.keys(output), ['keep', *values])
        self.assertIn('keep=private\n', output)
        self.assertTrue(output.isascii())
        for value in values.values():
            self.assertEqual(pf.unescape(pf.escape(value)), value)

    def test_rejects_bad_unicode_binary_bom_and_dangling_continuations_without_echo(self):
        for text in ('k=secret\\uXXYY', 'k=secret\\u1', 'k=secret\\', 'k=secret\\\n', '\ufeffk=secret', 'k=secret\x00'):
            with self.subTest(text=text), self.assertRaises(pf.PropertiesError) as e:
                pf.entries(text)
            self.assertNotIn('secret', str(e.exception))

    def test_output_limit(self):
        with mock.patch.object(pf, 'MAX_FILE', 16), self.assertRaises(pf.PropertiesError):
            pf.update('keep=1234567890\n', {'new': '123456789'})

    @unittest.skipUnless(shutil.which("java"), "Java oracle needs a JDK; core parser/security tests do not")
    def test_differential_java_oracle_for_keys_and_roundtrip_values(self):
        # One JVM for a deterministic corpus. Never production data; Java's loader is the independent oracle.
        source = r'''import java.util.*; import java.io.*; import java.nio.charset.StandardCharsets;
class PropertyOracle {
 public static void main(String[] args) throws Exception {
  var reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
  var enc = Base64.getEncoder(); var dec = Base64.getDecoder();
  for (String line; (line=reader.readLine())!=null;) {
   var p=new Properties(); p.load(new StringReader(new String(dec.decode(line),StandardCharsets.UTF_8)));
   var result=new ArrayList<String>();
   for (var key:p.stringPropertyNames()) result.add(enc.encodeToString(key.getBytes(StandardCharsets.UTF_8))+":"+
       enc.encodeToString(p.getProperty(key).getBytes(StandardCharsets.UTF_8)));
   Collections.sort(result); System.out.println(String.join(",", result));
  }
 }
}'''
        rng = random.Random(41021)
        cases = ['\\\n#comment\nreal=yes', 'a=secret\vnotakey=value\nnext=yes', 'a\\\n b: value',
                 'a=first\r\na=last', '\\#key=value', 'k=one\\\n\nnext=two']
        expected = []
        alphabet = 'abCD  =:#!\\\t\n\r\fζ😀'
        for _ in range(200):
            values = {''.join(rng.choices(alphabet, k=rng.randrange(1, 18))): ''.join(rng.choices(alphabet, k=20))
                      for _ in range(4)}
            text = '# comment=not-a-key\r\nuntouched=secret\r\n'
            changed = pf.update(text, values)
            cases.append(changed)
            expected.append({'untouched': 'secret', **values})
        # Additional physical spellings exercise recognition rather than just our own escape encoder.
        for _ in range(200):
            key = rng.choice(['plain', r'a\=b', r'a\ b', r'a\:b', r'\u0061', r'\!name'])
            value = rng.choice(['secret', 'one\\\n continued=value', r'one\\', r'\u00e9', ''])
            cases.append(rng.choice(['', ' ', '\t', '\f']) + key + rng.choice(['=', ':', ' ', ' = ']) + value + '\nnext=ok')
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, 'PropertyOracle.java'); f.write_text(source)
            proc = subprocess.run(['java', str(f)], input='\n'.join(base64.b64encode(c.encode()).decode() for c in cases)+'\n',
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[:1000])
        result = []
        for line in proc.stdout.splitlines():
            d = {}
            for pair in line.split(',') if line else []:
                k, v = pair.split(':')
                d[base64.b64decode(k).decode()] = base64.b64decode(v).decode()
            result.append(d)
        self.assertEqual(len(result), len(cases))
        for i, (case, java) in enumerate(zip(cases, result)):
            with self.subTest(case=i):
                self.assertEqual(set(self.keys(case)), set(java))
        self.assertEqual(result[6:206], expected)


class PropertiesToolTest(unittest.TestCase):
    def setUp(self):
        self.m = _env.manager(self)
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rel = 'config/secrets/app.properties'
        self.file = self.root / self.rel
        self.file.parent.mkdir(parents=True)
        self.original = b'# comment-secret\npassword=old-secret\nkeep=keep-secret\n'
        self.file.write_bytes(self.original)
        self.file.chmod(0o640)
        self.s = sessions.Session(kind='project', project='test/properties', path=str(self.root), state='exited')
        self.m.sessions[self.s.id] = self.s
        self.addCleanup(self.m.sessions.pop, self.s.id, None)
        self.patch = mock.patch.object(self.m, '_session_top', return_value=str(self.root))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.m.ask = mock.Mock(return_value=True)

    def call(self, tool='properties_set', **kw):
        return self.m.tool(self.s, tool, {'file': self.rel, **kw})

    def test_keys_only_no_comments_values_hashes_or_permission_card(self):
        result = self.call('properties_keys')
        self.assertEqual(result, {'keys': ['password', 'keep']})
        self.m.ask.assert_not_called()
        self.assertNotIn('secret', json.dumps(result))

    def test_write_preserves_mode_inode_other_values_and_never_returns_old_values(self):
        before = self.file.stat()
        with mock.patch.object(common, 'audit') as audit:
            result = self.call(values={'password': 'new', 'newKey': 'newValue'})
        self.assertEqual(result, {'ok': True})
        self.assertEqual(self.file.stat().st_ino, before.st_ino)
        self.assertEqual(self.file.stat().st_mode, before.st_mode)
        self.assertEqual(self.file.read_bytes(), b'# comment-secret\npassword=new\nkeep=keep-secret\nnewKey=newValue\n')
        for output in (str(result), str(self.m.ask.call_args), str(audit.call_args_list)):
            self.assertNotIn('old-secret', output)
            self.assertNotIn('keep-secret', output)
            self.assertNotIn('comment-secret', output)
        self.assertNotIn('newValue', str(audit.call_args_list))

    def test_denial_and_timeout_never_write(self):
        self.m.ask.return_value = False
        with self.assertRaisesRegex(pf.PropertiesError, 'not approved'):
            self.call(values={'password': 'new'})
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_stale_content_and_mode_are_refused(self):
        for mutate in (lambda: self.file.write_bytes(b'password=concurrent\n'), lambda: self.file.chmod(0o600)):
            self.file.write_bytes(self.original); self.file.chmod(0o640)
            def approve(*a, **kw):
                mutate(); return True
            self.m.ask.side_effect = approve
            with self.assertRaisesRegex(pf.PropertiesError, 'changed'):
                self.call(values={'password': 'new'})
            self.assertNotIn(b'password=new', self.file.read_bytes())

    def test_unknown_keys_are_added_duplicate_keys_all_replaced(self):
        self.file.write_bytes(b'key=one\nkey:two\n')
        self.call(values={'key': 'new', 'added': 'x'})
        self.assertEqual(self.file.read_bytes(), b'key=new\nkey=new\nadded=x\n')

    def test_encodings_and_error_messages(self):
        self.file.write_bytes('café=privé\n'.encode('iso-8859-1'))
        with self.assertRaises(pf.PropertiesError) as error:
            self.call('properties_keys')
        self.assertNotIn('priv', str(error.exception))
        self.assertEqual(self.call('properties_keys', encoding='iso-8859-1'), {'keys': ['café']})
        self.call(values={'café': 'ζωή'}, encoding='iso-8859-1')
        self.assertEqual(self.file.read_bytes(), b'caf\\u00e9=\\u03b6\\u03c9\\u03ae\n')

    def test_bad_arguments_fail_before_card(self):
        for values in ({}, [], None, {'a': None}, {'a': 1}, {'a': {'b': 'c'}}, {'a': ['b']}, {'a': 'x'*16001}):
            with self.subTest(values=type(values)), self.assertRaises(pf.PropertiesError):
                self.call(values=values)
        with self.assertRaises(pf.PropertiesError):
            self.call(values={'a': 'b'}, encoding='utf-16')
        self.m.ask.assert_not_called()
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_invalid_paths_and_nonproject_role(self):
        for file in ('/tmp/x.properties', '../x.properties', './x.properties', 'a/../x.properties', '.git/x.properties',
                     '.GIT/x.properties', 'x.txt', 'a\\x.properties', 'a//x.properties', 'x\0.properties', None):
            with self.subTest(file=file), self.assertRaises(pf.PropertiesError):
                self.call(file=file, values={'k': 'v'})
        self.s.kind = 'advisor'
        with self.assertRaises(pf.PropertiesError):
            self.call('properties_keys')
        self.m.ask.assert_not_called()

    def test_symlink_leaf_directory_and_hardlinks_refused(self):
        target = self.root / 'outside'; target.write_text('sensitive=secret')
        link = self.root / 'link.properties'; link.symlink_to(target)
        directory = self.root / 'dirlink'; directory.symlink_to(self.file.parent, target_is_directory=True)
        hard = self.root / 'hard.properties'; os.link(target, hard)
        for rel in ('link.properties', 'dirlink/app.properties', 'hard.properties'):
            for tool in pf.TOOLS:
                with self.subTest(rel=rel, tool=tool), self.assertRaises(pf.PropertiesError):
                    self.call(tool, file=rel, values={'k': 'v'})
        self.m.ask.assert_not_called()
        self.assertEqual(target.read_text(), 'sensitive=secret')

    def test_symlink_swap_after_approval_does_not_touch_target(self):
        target = self.root / 'outside'; target.write_bytes(b'sensitive=secret')
        def approve(*a, **kw):
            self.file.unlink(); self.file.symlink_to(target); return True
        self.m.ask.side_effect = approve
        with self.assertRaises(pf.PropertiesError):
            self.call(values={'password': 'new'})
        self.assertEqual(target.read_bytes(), b'sensitive=secret')

    def test_missing_directory_device_and_large_files_refused(self):
        (self.root / 'directory.properties').mkdir()
        os.mkfifo(self.root / 'fifo.properties')
        for rel in ('missing.properties', 'directory.properties', 'fifo.properties'):
            with self.subTest(rel=rel), self.assertRaises(pf.PropertiesError):
                self.call('properties_keys', file=rel)
        with mock.patch.object(pf, 'MAX_FILE', 10), self.assertRaises(pf.PropertiesError):
            self.call('properties_keys')
        self.m.ask.assert_not_called()

    def test_hidden_coverage_and_pinned_write_refusals_never_change_file(self):
        for method in ('_hidden_write_guard', '_ops_pin'):
            with mock.patch.object(self.m, method, side_effect=ValueError('sensitive details')):
                with self.assertRaises(pf.PropertiesError) as error:
                    self.call(values={'password': 'new'})
                self.assertNotIn('sensitive details', str(error.exception))
                self.assertEqual(self.file.read_bytes(), self.original)

    def test_real_live_uncovered_session_refuses_write(self):
        self.s.state = 'preflight'
        with self.assertRaisesRegex(pf.PropertiesError, 'isolation'):
            self.call(values={'password': 'new'})
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_real_cards_ignore_security_off_and_old_grants_and_deny_forged_always(self):
        self.m.ask = sessions.Manager.ask.__get__(self.m)
        self.m.policy['ask'] = []
        self.m.policy['auto_approve'] = ['properties_set']
        self.s.approvals.append(self.m.approval_scope('properties_set', {'file': self.rel}))
        for approved in (True, False, True):
            replies, errors = [], []
            def worker():
                try: replies.append(self.call(values={'password': 'new'}))
                except pf.PropertiesError as e: errors.append(str(e))
            thread = threading.Thread(target=worker); thread.start()
            try:
                deadline = time.monotonic() + 5
                while not self.m.pending_list() and time.monotonic() < deadline: time.sleep(.01)
                cards = self.m.pending_list()
                self.assertEqual(len(cards), 1)
                card = cards[0]
                self.assertEqual(card['kind'], 'properties_set')
                self.assertNotIn('session_always', card)
                self.assertNotIn('always', card)
                self.assertNotIn('old-secret', json.dumps(card))
                self.m.decide(card['id'], approved, always=True, session_always=True)
            finally:
                for card in self.m.pending_list():
                    try: self.m.decide(card['id'], False)
                    except KeyError: pass
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(replies, [{'ok': True}] if approved else [])
            self.assertEqual(bool(errors), not approved)


if __name__ == '__main__':
    unittest.main()
