"""Cookie credentials retain scope, flags, expiry and origin across native runs."""
import json
import os
import tempfile
import time
import unittest
import zipfile

import _env
import httpcookies as cookies


class CookieTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        path = os.path.join(self.root, 'jdk/lib/security')
        os.makedirs(path)
        with zipfile.ZipFile(os.path.join(path, 'public_suffix_list.dat'), 'w') as z:
            z.writestr('com', b'\0com\n')
            # The real database has >1000 distinct rules; generate a valid fixture.
            z.writestr('io', b'\0io\n\1github.io\n' + b''.join(('\1test%d.io\n' % i).encode() for i in range(1001)))
            z.writestr('ck', b'\0*.ck\n\0!www.ck\n')
            z.writestr('uk', b'\0uk\n\0co.uk\n')
        self.suffixes = cookies.Suffixes(os.path.join(self.root, 'jdk/bin/java'))
        self.secrets = []
        self.jar = cookies.Jar(os.path.join(self.root, 'state'), 'scope-a', self.suffixes, self.secrets)
        self.jar.allowed = {('api.example.com', 443), ('api.example.com', 80), ('sub.api.example.com', 443),
                            ('other.example.com', 443), ('127.0.0.1', 443)}

    def save(self, *headers, url='https://api.example.com/api/login', jar=None):
        return (jar or self.jar)({'kind': 'cookie_save', 'url': url, 'headers': list(headers)}, time.monotonic() + 5)

    def select(self, url='https://api.example.com/api/echo', jar=None):
        return (jar or self.jar)({'kind': 'cookie_select', 'url': url}, time.monotonic() + 5)['cookies']

    def test_psl_includes_private_wildcard_exception_and_unknown_tld(self):
        for name in ('com', 'co.uk', 'github.io', 'a.ck', 'unknown'):
            with self.subTest(public=name):
                self.assertTrue(self.suffixes.public(name))
        for name in ('example.com', 'a.github.io', 'www.ck', 'sub.www.ck', 'a.b.ck', 'a.unknown'):
            with self.subTest(private=name):
                self.assertFalse(self.suffixes.public(name))

    def test_cross_run_preserves_host_only_and_secure(self):
        self.save('sid=synthetic-secret; Secure; HttpOnly; Path=/; SameSite=Lax')
        restored = cookies.Jar(self.jar.directory, 'scope-a', self.suffixes, [])
        restored.allowed = self.jar.allowed
        self.assertEqual(self.select(jar=restored)[0]['value'], 'synthetic-secret')
        self.assertEqual(self.select('http://api.example.com/api/echo', jar=restored), [])
        self.assertEqual(self.select('https://sub.api.example.com/api/echo', jar=restored), [])
        self.assertEqual(self.select('https://other.example.com/api/echo', jar=restored), [])
        other = cookies.Jar(self.jar.directory, 'scope-b', self.suffixes, [])
        other.allowed = self.jar.allowed
        self.assertEqual(self.select(jar=other), [])
        self.assertIn('synthetic-secret', self.secrets)
        self.assertEqual(os.stat(os.path.join(self.jar.directory, self.jar.filename)).st_mode & 0o777, 0o600)

    def test_domain_cookies_and_path_order_do_not_match_siblings_or_prefixes(self):
        self.save('a=wide; Domain=example.com; Path=/', 'a=narrow; Path=/api', 'b=default')
        self.assertEqual([(x['name'], x['value']) for x in self.select()], [('a', 'narrow'), ('b', 'default'), ('a', 'wide')])
        self.assertEqual([x['value'] for x in self.select('https://other.example.com/apix')], ['wide'])
        self.assertEqual(self.select('https://api.example.com/apix')[0]['value'], 'wide')
        self.assertFalse(cookies.domain_match('notexample.com', 'example.com'))
        self.assertFalse(cookies.domain_match('127.0.0.1', '0.0.1'))

    def test_supercookies_and_malformed_headers_are_ignored(self):
        self.save('a=x; Domain=com', 'a=x; Domain=other.example.com', 'a=x; Domain=example.com.',
                  'bad name=x', 'a=x\r\nInjected: true', 'a="x;y"', 'a=x\\y', 'a=' + 'x' * 4096)
        self.assertEqual(self.select(), [])
        for domain in ('github.io', 'co.uk', 'a.ck'):
            self.assertIsNone(cookies.parse('a=x; Domain=' + domain, 'https://app.' + domain, time.time(), self.suffixes))

    def test_prefixes_and_insecure_cookie_overlay(self):
        self.save('__Secure-good=x; Secure; Path=/', '__Host-good=y; Secure; Path=/',
                  '__Secure-bad=x', '__Host-bad=x; Secure; Domain=api.example.com; Path=/',
                  '__Host-path=x; Secure; Path=/api', 'sid=secure; Secure; Path=/')
        self.save('sid=insecure; Path=/', 'sid=deleted; Max-Age=0; Path=/', 'new=x; Secure',
                  url='http://api.example.com/api/login')
        self.assertEqual({x['name'] for x in self.select()}, {'__Secure-good', '__Host-good', 'sid'})
        self.assertEqual(next(x for x in self.select() if x['name'] == 'sid')['value'], 'secure')

    def test_expiry_deletion_replacement_and_max_age_precedence(self):
        self.save('a=x; Path=/; Max-Age=100; Expires=Wed, 09 Jun 2021 10:18:14 GMT',
                  'b=x; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT', 'c=; Path=/')
        self.assertEqual([x['name'] for x in self.select()], ['a', 'c'])
        self.save('a=y; Path=/')
        self.assertEqual([x['name'] for x in self.select()], ['a', 'c'])
        self.save('a=; Path=/; Max-Age=0')
        self.assertEqual([x['name'] for x in self.select()], ['c'])
        self.assertEqual(cookies.cookie_date('Wednesday, 09-Jun-21 10:18:14 GMT'), 1623233894)
        self.assertIsNone(cookies.cookie_date('Wed, 32 Jun 2021 10:18:14 GMT'))

    def test_rpc_cannot_query_other_origin_or_write_arbitrary_paths(self):
        for message in ({'kind': 'cookie_select', 'url': 'file:///etc/passwd'},
                        {'kind': 'cookie_select', 'url': 'https://user@api.example.com/api'},
                        {'kind': 'cookie_save', 'url': 'https://evil.example/api', 'headers':['sid=forged']},
                        {'kind': 'cookie_save', 'url': 'https://api.example.com', 'headers': 'sid=x'},
                        {'kind': 'cookie_save', 'url': 'https://api.example.com', 'headers': [], 'path': '/host'}):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.jar(message, time.monotonic() + 5)
        self.assertFalse(os.path.exists(self.jar.directory))
        self.save('sid=private; Path=/')
        self.assertEqual(self.select('https://evil.example/api'), [])
        self.assertEqual(self.select('https://api.example.com:444/api'), [])

    def test_state_cannot_be_a_symlink_or_hardlink(self):
        self.save('a=x')
        path = os.path.join(self.jar.directory, self.jar.filename)
        os.rename(path, path + '.target')
        os.symlink(path + '.target', path)
        with self.assertRaises((ValueError, OSError)):
            cookies.Jar(self.jar.directory, 'scope-a', self.suffixes, [])
        os.unlink(path)
        os.link(path + '.target', path)
        with self.assertRaises((ValueError, OSError)):
            cookies.Jar(self.jar.directory, 'scope-a', self.suffixes, [])

    def test_unavailable_psl_cannot_broaden_domain_scope(self):
        psl = cookies.Suffixes('/no/jdk/bin/java')
        self.assertIsNone(cookies.parse('a=x; Domain=example.com', 'https://api.example.com', time.time(), psl))
        item = cookies.parse('a=x; Domain=api.example.com', 'https://api.example.com', time.time(), psl)
        self.assertTrue(item['host_only'])
        self.assertTrue(cookies.parse('a=x', 'https://api.example.com', time.time(), psl)['host_only'])

    def test_storage_limits_evict_oldest_and_never_create_unreadable_state(self):
        from unittest import mock
        with mock.patch.object(cookies, 'MAX_COOKIES', 3):
            self.save('a=first; Path=/', 'b=second; Path=/', 'c=third; Path=/', 'd=fourth; Path=/')
            self.assertEqual([e['name'] for e in self.select()], ['b', 'c', 'd'])
        with mock.patch.object(cookies, 'MAX_STATE', 600):
            self.save('e=' + 'x' * 200 + '; Path=/')
            self.assertLessEqual(os.path.getsize(os.path.join(self.jar.directory, self.jar.filename)), 600)
            restored = cookies.Jar(self.jar.directory, 'scope-a', self.suffixes, [])
            restored.allowed = self.jar.allowed
            self.assertEqual(self.select(jar=restored), self.select())

    def test_expired_secure_cookie_does_not_block_new_plain_cookie_and_age_zero_prefix_is_not_truncated(self):
        from unittest import mock
        self.save('sid=secure; Secure; Path=/; Max-Age=10')
        with mock.patch.object(cookies.time, 'time', return_value=time.time() + 20):
            self.save('sid=plain; Path=/', url='http://api.example.com/api/login')
            self.assertEqual(self.select('http://api.example.com/api/echo')[0]['value'], 'plain')
        self.save('leading=valid; Path=/; Max-Age=' + '0' * 100 + '100')
        self.assertIn('leading', [e['name'] for e in self.select()])
