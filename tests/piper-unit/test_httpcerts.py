"""Configured client identities are bounded, immutable and never silently downgraded."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import _env
import httpcerts


class CertificateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        os.mkdir(self.root + '/env')
        self.work = self.root + '/work'
        os.mkdir(self.work)
        for name, data in [('client.crt', b'synthetic-certificate'), ('client.key', b'synthetic-private-key-value')]:
            with open(self.root + '/env/' + name, 'wb') as output:
                output.write(data)
        self.config = dict(clientCertificate='client.crt', clientCertificateKey='client.key')

    def snapshot(self, config=None):
        return httpcerts.snapshot(self.root, 'env', self.config if config is None else config, self.work)

    def test_relative_and_absolute_material_is_read_once_and_not_returned_in_approval_metadata(self):
        self.config['clientCertificateKey'] = self.root + '/env/client.key'
        result = self.snapshot()
        self.assertEqual(len(result['fingerprints']), 2)
        self.assertNotIn('synthetic-private', json.dumps(result['fingerprints']))
        self.assertIn('synthetic-private-key-value', result['secrets'])
        with open(self.work + '/ssl-client.json') as source:
            config = json.load(source)
        self.assertIn(config['key']['path'], result['secrets'])
        with open(self.root + '/env/client.key', 'wb') as output:
            output.write(b'changed-after-approval')
        with open(config['key']['path'], 'rb') as source:
            self.assertEqual(source.read(), b'synthetic-private-key-value')
        self.assertEqual(config['certificate']['format'], 'PEM')
        self.assertEqual(os.stat(config['key']['path']).st_mode & 0o777, 0o400)

    def test_p12_and_explicit_der_formats_are_preserved_without_guessing_unknown_extensions(self):
        result = self.snapshot({'clientCertificate': {'path': 'client.crt', 'format': 'P12'}})
        self.assertEqual(len(result['fingerprints']), 1)
        with open(self.work + '/ssl-client.json') as source:
            self.assertNotIn('key', json.load(source))

    def test_link_material_never_becomes_a_native_key(self):
        for link in (os.symlink, os.link):
            with self.subTest(link=link.__name__), tempfile.TemporaryDirectory(dir=self.root) as work:
                path = self.root + '/env/link.key'
                if os.path.lexists(path):
                    os.unlink(path)
                link(self.root + '/env/client.key', path)
                with self.assertRaisesRegex(ValueError, 'safely read'):
                    httpcerts.snapshot(self.root, 'env', dict(self.config, clientCertificateKey='link.key'), work)

    def test_invalid_missing_empty_oversized_or_provider_passphrase_fails_before_native_execution(self):
        for override in ({'clientCertificate': '../unknown.extension'},
                         {'clientCertificateKey': '{{key}}'}, {'clientCertificate': ['client.crt']},
                         {'clientCertificate': {'path':'client.crt', 'format':'invalid'}},
                         {'clientCertificate': {'path':'client.crt', 'format':[]}},
                         {'clientCertificateKey': 'missing.key'}, {'clientCertificateKey': None}):
            with self.subTest(override=override), tempfile.TemporaryDirectory(dir=self.root) as work:
                with self.assertRaises(ValueError):
                    httpcerts.snapshot(self.root, 'env', dict(self.config, **override), work)
        with open(self.root + '/env/client.key', 'wb') as output:
            output.write(b'x' * (httpcerts.MAX_FILE + 1))
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            self.snapshot()

    def test_no_identity_does_not_create_native_configuration_or_runtime_permissions(self):
        self.assertIsNone(self.snapshot({}))
        self.assertEqual(os.listdir(self.work), [])
        with self.assertRaisesRegex(ValueError, 'requires'):
            self.snapshot({'clientCertificateKey': 'client.key'})

    def test_host_token_context_loads_only_frozen_paths_and_never_prompts_or_exposes_key_errors(self):
        from unittest import mock
        self.snapshot()
        context = mock.Mock()
        with open(self.root + '/env/client.key', 'wb') as output:
            output.write(b'changed-source-after-approval')
        httpcerts.configure_context(context, self.work)
        context.load_cert_chain.assert_called_once_with(self.work + '/tls/certificate', self.work + '/tls/key', password=b'')
        context.load_cert_chain.side_effect = ValueError('synthetic-private-key-value ' + self.work)
        with self.assertRaises(ValueError) as caught:
            httpcerts.configure_context(context, self.work)
        self.assertNotIn('synthetic-private', str(caught.exception))
        self.assertNotIn(self.work, str(caught.exception))

    def test_encrypted_identity_prompts_without_reading_any_provider(self):
        result = self.snapshot(dict(self.config, hasCertificatePassphrase=True))
        self.assertTrue(result['needs_passphrase'])
        self.assertEqual(result['entry']['file'], 'client.crt')
        self.assertEqual(len(result['entry']['sha256']), 64)

    def test_unlock_passphrase_never_enters_argv_file_or_error_and_failure_keeps_frozen_identity(self):
        from unittest import mock
        self.snapshot(dict(self.config, hasCertificatePassphrase=True))
        before = Path(self.work + '/ssl-client.json').read_text()
        observed = []
        def fail(argv, **kwargs):
            observed.append((argv, dict(kwargs['env'])))
            return mock.Mock(returncode=1, stdout=b'', stderr=b'synthetic-secret-password')
        with mock.patch('httpcerts.subprocess.run', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'passphrase is wrong') as caught:
                httpcerts.unlock(self.work, 'synthetic-secret-password')
        self.assertNotIn('synthetic-secret-password', str(caught.exception))
        self.assertNotIn('synthetic-secret-password', str(observed[0][0]))
        self.assertEqual(observed[0][1]['PIPER_HTTP_CERT_PASSPHRASE'], 'synthetic-secret-password')
        self.assertNotIn('PIPER_HTTP_CERT_PASSPHRASE', os.environ)
        self.assertEqual(Path(self.work + '/ssl-client.json').read_text(), before)
        self.assertFalse(os.path.exists(self.work + '/tls/unlocked.pem'))

    def test_unlock_uses_frozen_bytes_and_registers_the_decrypted_identity_for_redaction(self):
        from unittest import mock
        self.snapshot(dict(self.config, hasCertificatePassphrase=True))
        key = b'-----BEGIN PRIVATE KEY-----\nsynthetic-decrypted-key-line\n-----END PRIVATE KEY-----\n'
        with mock.patch('httpcerts.subprocess.run', return_value=mock.Mock(returncode=0, stdout=key)) as run:
            secrets = httpcerts.unlock(self.work, 'line1\nline2')
        self.assertEqual(run.call_args.args[0][3], self.work + '/tls/key')
        self.assertEqual(Path(self.work + '/tls/unlocked.pem').read_bytes(), key)
        self.assertEqual(os.stat(self.work + '/tls/unlocked.pem').st_mode & 0o777, 0o400)
        self.assertIn('synthetic-decrypted-key-line', secrets)
        self.assertNotIn('line1\nline2', secrets)

    def test_pkcs12_is_split_into_certificate_and_key_before_native_loading(self):
        from unittest import mock
        self.snapshot({'clientCertificate':{'path':'client.crt','format':'P12'}, 'hasCertificatePassphrase':True})
        certificate = b'-----BEGIN CERTIFICATE-----\nY2VydA==\n-----END CERTIFICATE-----'
        key = b'-----BEGIN PRIVATE KEY-----\na2V5\n-----END PRIVATE KEY-----'
        mixed = b'Bag Attributes\n' + key + b'\nBag Attributes\n' + certificate + b'\n'
        with mock.patch('httpcerts.subprocess.run', return_value=mock.Mock(returncode=0, stdout=mixed)):
            httpcerts.unlock(self.work, 'synthetic-passphrase')
        config = json.loads(Path(self.work + '/ssl-client.json').read_text())
        self.assertEqual(Path(config['certificate']['path']).read_bytes(), certificate + b'\n')
        self.assertEqual(Path(config['key']['path']).read_bytes(), key + b'\n')

    def test_project_path_cannot_choose_the_host_passphrase_process(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {'PATH':self.root}), mock.patch('httpcerts.shutil.which', return_value='/usr/bin/openssl') as which:
            self.assertEqual(httpcerts._openssl(), '/usr/bin/openssl')
        self.assertNotIn(self.root, which.call_args.kwargs['path'].split(os.pathsep))
