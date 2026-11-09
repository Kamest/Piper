"""Install a real server release and exercise its daemon/PTY across controller restarts.

Systemd activation is stubbed on macOS. Runtime executes the matching read-only source
checkout: the mandatory sandbox refuses importing application code from writable fixtures.
PIPER_REMOTE_TEST_RELEASE optionally selects an externally prepared read-only release.
"""
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

import _env
import common
import features
import remotepeer
import remotes
from ptyclient import PtyClient


class PythonInstallTest(unittest.TestCase):
    """Downloads and runtime execution are stubbed; all writes stay in a disposable home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.directory = self.home / '.local/opt/python3.12'
        self.target = 'aarch64-apple-darwin'
        self.url = ('https://github.com/astral-sh/python-build-standalone/releases/download/'
                    '20261026/cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only.tar.gz')
        patches = [mock.patch.object(remotepeer, 'HOME', self.home),
                   mock.patch.object(remotepeer.sys, 'platform', 'darwin'),
                   mock.patch.object(remotepeer.platform, 'machine', return_value='arm64')]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.download = mock.patch.object(remotepeer.urllib.request, 'urlopen').start()
        self.addCleanup(mock.patch.stopall)
        self.download.side_effect = AssertionError('Unexpected download')
        self.runtime = mock.patch.object(remotepeer, '_setup_run', side_effect=self.verify_runtime).start()

    def verify_runtime(self, argv, timeout):
        binary = Path(argv[0])
        self.assertTrue(binary.is_file())
        self.assertEqual(binary.name, 'python3.12')
        self.assertIn('import sys, ssl, sqlite3, venv', argv[2])
        self.assertEqual(timeout, 20)
        if binary.parent.parent != self.directory:
            self.assertFalse(self.directory.exists(), 'verify before publishing')
            self.assertEqual(binary.parent.parent.parent, self.directory.parent)
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    def content(self, extra=()):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            binary = tarfile.TarInfo('python/bin/python3.12')
            binary.size, binary.mode = 7, 0o755
            archive.addfile(binary, io.BytesIO(b'fixture'))
            link = tarfile.TarInfo('python/bin/python3')
            link.type, link.linkname = tarfile.SYMTYPE, 'python3.12'
            archive.addfile(link)
            for member in extra:
                archive.addfile(member)
        raw = stream.getvalue()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = raw
        response.geturl.return_value = self.url
        self.download.side_effect = None
        self.download.return_value = response
        patch = mock.patch.dict(remotepeer.PYTHON_SHA256, {self.target: hashlib.sha256(raw).hexdigest()})
        patch.start(); self.addCleanup(patch.stop)
        return raw, response

    def test_install_is_atomic_user_only_python39_and_reuses_healthy_runtime(self):
        _, response = self.content()
        with mock.patch.object(remotepeer.sys, 'version_info', (3, 9, 6)):
            self.assertEqual(remotepeer.install_python({}), {'installed': True, 'existing': False})
            for name in ('python3', 'python3.12'):
                (self.home / '.local/bin' / name).unlink()
            self.assertEqual(remotepeer.install_python({}), {'installed': True, 'existing': True})
        self.download.assert_called_once_with(self.url, timeout=30)
        response.read.assert_called_once_with((128 << 20) + 1)
        self.assertEqual(self.runtime.call_count, 2)
        for name in ('python3', 'python3.12'):
            link = self.home / '.local/bin' / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), self.directory / 'bin/python3.12')
        self.assertEqual((self.directory / 'bin/python3').resolve(), self.directory / 'bin/python3.12')
        self.assertEqual(list(self.directory.parent.glob('.python3.12-*')), [])
        self.assertFalse((self.home / '.zshrc').exists())
        self.assertFalse((self.home / '.bashrc').exists())

    def test_hash_mismatch_is_refused_before_extraction(self):
        self.content()
        with mock.patch.dict(remotepeer.PYTHON_SHA256, {self.target: '0' * 64}), \
                mock.patch.object(remotepeer, '_python_extract') as extract:
            with self.assertRaisesRegex(remotepeer.NotExecuted, 'SHA-256 mismatch'):
                remotepeer.install_python({})
        extract.assert_not_called()
        self.runtime.assert_not_called()
        self.assertFalse(self.directory.parent.exists())

    def test_unsafe_members_links_special_files_and_failed_runtime_are_never_published(self):
        for kind in ('traversal', 'absolute', 'symlink', 'hardlink', 'fifo', 'runtime'):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo('python/../escape' if kind == 'traversal' else
                                         '/absolute' if kind == 'absolute' else 'python/unsafe')
                if kind in ('symlink', 'hardlink'):
                    member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    member.linkname = '../../escape'
                if kind == 'fifo':
                    member.type = tarfile.FIFOTYPE
                self.content([] if kind == 'runtime' else [member])
                with mock.patch.object(remotepeer, '_python_ready', wraps=remotepeer._python_ready) as ready:
                    if kind == 'runtime':
                        ready.return_value = False
                    with self.assertRaisesRegex(remotepeer.NotExecuted, 'unsafe|verification failed'):
                        remotepeer.install_python({})
                self.assertFalse(self.directory.exists())
                self.assertFalse((self.home / '.local/bin').exists())
                self.assertEqual(list(self.directory.parent.glob('.python3.12-*')), [])

    def test_unknown_platform_arch_and_musl_are_not_executed(self):
        for system, arch in (('freebsd', 'x86_64'), ('darwin', 'riscv64'), ('linux', 'x86_64')):
            with self.subTest(system=system, arch=arch), \
                    mock.patch.object(remotepeer.sys, 'platform', system), \
                    mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                    mock.patch.object(remotepeer.os, 'confstr', return_value=None):
                with self.assertRaisesRegex(remotepeer.NotExecuted, r'install Python 3.10\+ in ~/.local/bin'):
                    remotepeer.install_python({})
        self.download.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_all_four_pins_are_complete_and_target_mapping_requires_glibc(self):
        for system, arch, expected in (('darwin', 'arm64', 'aarch64-apple-darwin'),
                                       ('darwin', 'x86_64', 'x86_64-apple-darwin'),
                                       ('linux', 'x86_64', 'x86_64-unknown-linux-gnu'),
                                       ('linux', 'aarch64', 'aarch64-unknown-linux-gnu')):
            with mock.patch.object(remotepeer.sys, 'platform', system), \
                    mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                    mock.patch.object(remotepeer.os, 'confstr', return_value='glibc 2.31'):
                self.assertEqual(remotepeer._python_target(), expected)
                self.assertRegex(remotepeer.PYTHON_SHA256[expected], r'^[0-9a-f]{64}$')

    def test_download_failure_and_size_cap_are_clear_retryable_refusals(self):
        for failure in (OSError('proxy credentials must not be returned'), None):
            with self.subTest(failure=type(failure).__name__):
                _, response = self.content()
                if failure:
                    self.download.side_effect = failure
                else:
                    # Exercise the size guard without allocating a 128 MiB fixture.
                    response.read.return_value = mock.MagicMock(__len__=lambda _: (128 << 20) + 1)
                with self.assertRaisesRegex(remotepeer.NotExecuted, 'Cannot download.*HTTPS_PROXY') as error:
                    remotepeer.install_python({})
                self.assertNotIn('credentials', str(error.exception))
                self.assertFalse(self.directory.exists())

    def test_existing_user_command_and_broken_install_are_preserved(self):
        link = self.home / '.local/bin/python3'
        link.parent.mkdir(parents=True)
        link.write_text('user command')
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'Preserving existing'):
            remotepeer.install_python({})
        self.assertEqual(link.read_text(), 'user command')
        link.unlink()
        self.directory.mkdir(parents=True)
        with mock.patch.object(remotepeer, '_setup_run', side_effect=FileNotFoundError), \
                self.assertRaisesRegex(remotepeer.NotExecuted, 'not working'):
            remotepeer.install_python({})
        self.download.assert_not_called()
        self.assertTrue(self.directory.exists())


class LingerTest(unittest.TestCase):
    """loginctl is stubbed; only the own account's lingering is requested, never with sudo."""

    def test_linux_requests_lingering_for_the_own_account_and_reports_the_result(self):
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/loginctl'), \
                mock.patch.object(remotepeer, '_setup_run') as run, \
                mock.patch.object(remotepeer, 'probe', return_value={'linger': True}):
            self.assertEqual(remotepeer.enable_linger({}), {'linger': True})
        run.assert_called_once_with(['loginctl', 'enable-linger'], 20)

    def test_refusal_or_missing_loginctl_leaves_lingering_off_without_error(self):
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/loginctl'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=subprocess.TimeoutExpired('loginctl', 20)), \
                mock.patch.object(remotepeer, 'probe', return_value={'linger': False}):
            self.assertEqual(remotepeer.enable_linger({}), {'linger': False})
        with mock.patch.object(remotepeer.sys, 'platform', 'darwin'), \
                mock.patch.object(remotepeer, '_setup_run') as run, \
                mock.patch.object(remotepeer, 'probe', return_value={'linger': True}):
            self.assertEqual(remotepeer.enable_linger({}), {'linger': True})
        run.assert_not_called()


class AgentInstallTest(unittest.TestCase):
    """Vendor responses and executable checks are fixtures; every write stays in a temporary home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.responses, self.reads, self.requests = {}, [], []
        self.version = '0.167.0'
        self.which = remotepeer.shutil.which
        patches = [mock.patch.object(remotepeer, 'HOME', self.home),
                   mock.patch.object(remotepeer.sys, 'platform', 'darwin'),
                   mock.patch.object(remotepeer.platform, 'machine', return_value='arm64'),
                   mock.patch.object(remotepeer.shutil, 'which', return_value=None),
                   mock.patch.dict(os.environ, CODEX_HOME=str(self.home / '.codex')),
                   mock.patch.object(remotepeer.urllib.request.OpenerDirector, 'open', side_effect=self.open),
                   mock.patch.object(remotepeer, '_setup_run', side_effect=self.verify_binary)]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.runtime = remotepeer._setup_run

    def open(self, request, timeout):
        self.assertEqual(timeout, 30)
        self.assertEqual(request.get_header('User-agent'), 'piper-setup')
        if request.full_url.startswith('https://api.github.com/'):
            self.assertEqual(request.get_header('Accept'), 'application/vnd.github+json')
        url = request.full_url
        self.requests.append(url)
        raw = self.responses[url]
        if isinstance(raw, Exception):
            raise raw
        stream = io.BytesIO(raw)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = url
        def read(size):
            self.assertGreater(size, 0, 'every response read must be bounded')
            self.reads.append(size)
            return stream.read(size)
        response.read.side_effect = read
        return response

    def verify_binary(self, argv, timeout, **kwargs):
        self.assertEqual(argv[1:], ['--version'])
        self.assertEqual(timeout, 30)
        path = Path(argv[0])
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        # Never execute a partial download or a shell script in these tests.
        self.assertEqual(path.read_bytes(), self.binary)
        if 'codex' in path.parts or path.name == 'codex':
            self.assertEqual(kwargs['extra_env']['CODEX_HOME'], os.environ['CODEX_HOME'])
        return subprocess.CompletedProcess(argv, 0, ('codex ' + self.version).encode(), b'')

    def claude(self, size=None, checksum=None, binary=None):
        self.binary = binary if binary is not None else b'CLAUDE FIXTURE' * 100000
        base = 'https://downloads.claude.ai/claude-code-releases'
        self.responses[base + '/latest'] = b'2.1.42\n'
        self.responses[base + '/2.1.42/manifest.json'] = json.dumps({'platforms': {'darwin-arm64': {
            'checksum': checksum or hashlib.sha256(self.binary).hexdigest(),
            'size': len(self.binary) if size is None else size}}}).encode()
        self.responses[base + '/2.1.42/darwin-arm64/claude'] = self.binary
        return self.home / '.local/share/claude/versions/2.1.42'

    def codex(self, target='aarch64-apple-darwin', unsafe=None, package=None):
        self.binary = b'CODEX FIXTURE'
        files = {'bin/codex': self.binary, 'bin/codex-code-mode-host': self.binary,
                 'bin/codex-exec-server': self.binary, 'codex-path/rg': self.binary,
                 'codex-resources/data.txt': b'resource', 'codex-resources/bwrap': self.binary,
                 'codex-package.json': json.dumps(package or {
                     'version': self.version, 'target': target, 'entrypoint': 'bin/codex'}).encode()}
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, raw in files.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(raw), 0o7777
                archive.addfile(member, io.BytesIO(raw))
            if unsafe is not None:
                archive.addfile(unsafe)
        raw = stream.getvalue()
        name = 'codex-package-' + target + '.tar.gz'
        sums = (hashlib.sha256(raw).hexdigest() + '  ' + name + '\n').encode()
        prefix = 'https://github.com/openai/codex/releases/download/rust-v' + self.version + '/'
        assets = [{'name': key, 'browser_download_url': prefix + key,
                   'digest': 'sha256:' + hashlib.sha256(value).hexdigest()}
                  for key, value in ((name, raw), ('codex-package_SHA256SUMS', sums))]
        self.api = 'https://api.github.com/repos/openai/codex/releases/latest'
        self.sums_url, self.package_url = prefix + 'codex-package_SHA256SUMS', prefix + name
        self.responses.update({self.api: json.dumps({'tag_name': 'rust-v' + self.version, 'assets': assets}).encode(),
                               self.sums_url: sums, self.package_url: raw})
        return self.home / '.codex/packages/standalone'

    def assert_clean_failure(self, agent):
        self.assertFalse(os.path.lexists(self.home / '.local/bin' / agent))
        self.assertEqual(list(self.home.rglob('.download-*')), [])
        self.assertEqual(list(self.home.rglob('.staging.*')), [])
        self.runtime.assert_not_called()

    def test_claude_streaming_layout_version_and_no_shell_profile_changes(self):
        binary = self.claude()
        result = remotepeer.install_agent({'agent': 'claude'})
        self.assertEqual(result, {'installed': True, 'existing': False})
        link = self.home / '.local/bin/claude'
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), binary)
        self.assertEqual(binary.read_bytes(), self.binary)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertGreater(self.reads.count(1 << 20), 0)
        self.assertLessEqual(max(self.reads), (1 << 20) + 1)
        self.assertEqual(list(binary.parent.glob('.download-*')), [])
        self.assertEqual(self.runtime.call_args.args[0], [str(binary), '--version'])
        self.assertEqual(set(p.name for p in self.home.iterdir()), {'.local'})

    def test_claude_bad_checksum_truncated_and_oversized_never_publish_or_execute(self):
        for size, checksum in ((None, '0' * 64), (2 << 20, None), (10, None), (601 << 20, None), (True, None)):
            with self.subTest(size=size, checksum=checksum):
                binary = self.claude(size, checksum)
                with self.assertRaisesRegex(remotepeer.NotExecuted, 'mismatch|size'):
                    remotepeer.install_agent({'agent': 'claude'})
                self.assertFalse(binary.exists())
                self.assert_clean_failure('claude')

    def test_existing_commands_and_symlinked_parents_are_preserved(self):
        for agent in ('claude', 'codex'):
            link = self.home / '.local/bin' / agent
            link.parent.mkdir(parents=True, exist_ok=True)
            link.write_text('existing user command')
            with self.subTest(agent=agent), self.assertRaisesRegex(remotepeer.NotExecuted, str(link)):
                remotepeer.install_agent({'agent': agent})
            self.assertEqual(link.read_text(), 'existing user command')
        self.assertEqual(self.requests, [])
        outside = self.home / 'outside'
        outside.mkdir()
        (self.home / '.local/share').symlink_to(outside, target_is_directory=True)
        (self.home / '.local/bin/claude').unlink()
        self.claude()
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'symlink'):
            remotepeer.install_agent({'agent': 'claude'})
        self.assertEqual(list(outside.iterdir()), [])

    def test_present_agents_are_not_touched_and_unknown_agents_are_refused(self):
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/existing/agent'):
            for agent in ('claude', 'codex'):
                self.assertEqual(remotepeer.install_agent({'agent': agent}), {'installed': True, 'existing': True})
        with self.assertRaises(remotepeer.NotExecuted):
            remotepeer.install_agent({'agent': 'shell'})
        self.assertEqual(self.requests, [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_downloaded_shell_script_is_refused_even_with_a_matching_vendor_checksum(self):
        self.claude(binary=b'#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'script.*native binary'):
            remotepeer.install_agent({'agent': 'claude'})
        self.assert_clean_failure('claude')

    def test_no_network_is_actionable_and_never_exports_exception_details(self):
        for agent in ('claude', 'codex'):
            self.claude() if agent == 'claude' else self.codex()
            self.responses = {url: OSError('private proxy detail') for url in self.responses}
            with self.subTest(agent=agent), self.assertRaisesRegex(remotepeer.NotExecuted,
                    'Cannot download ' + agent + '.*HTTPS_PROXY.*run Fix') as error:
                remotepeer.install_agent({'agent': agent})
            self.assertNotIn('private', str(error.exception))
            self.assert_clean_failure(agent)

    def test_interrupted_binary_stream_cleans_temporary_file(self):
        self.claude()
        original = self.open
        def interrupted(request, timeout):
            response = original(request, timeout)
            if request.full_url.endswith('/claude'):
                response.read.side_effect = [b'partial', OSError('private proxy detail')]
            return response
        with mock.patch.object(remotepeer.urllib.request.OpenerDirector, 'open', side_effect=interrupted), \
                self.assertRaisesRegex(remotepeer.NotExecuted, 'Cannot download claude.*downloads.claude.ai'):
            remotepeer.install_agent({'agent': 'claude'})
        self.assert_clean_failure('claude')

    def test_claude_platform_mapping_musl_arch_aliases_and_rosetta(self):
        for system, arch, glibc, musl, translated, expected in (
                ('darwin', 'arm64', None, False, False, 'darwin-arm64'),
                ('darwin', 'x86_64', None, False, False, 'darwin-x64'),
                ('darwin', 'amd64', None, False, True, 'darwin-arm64'),
                ('linux', 'aarch64', 'glibc 2.31', False, False, 'linux-arm64'),
                ('linux', 'amd64', 'glibc 2.31', False, False, 'linux-x64'),
                ('linux', 'x86_64', 'glibc 2.31', True, False, 'linux-x64-musl'),
                ('linux', 'arm64', None, False, False, 'linux-arm64-musl')):
            with self.subTest(expected=expected), mock.patch.object(remotepeer.sys, 'platform', system), \
                    mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                    mock.patch.object(remotepeer.os, 'confstr', return_value=glibc), \
                    mock.patch.object(remotepeer.Path, 'exists', return_value=musl), \
                    mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(
                        returncode=0, stdout=b'1' if translated else b'0')):
                self.assertEqual(remotepeer._agent_target('claude'), expected)
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.os, 'confstr', side_effect=ValueError), \
                mock.patch.object(remotepeer.Path, 'exists', return_value=False):
            self.assertEqual(remotepeer._agent_target('claude'), 'linux-arm64-musl')
        for agent in ('claude', 'codex'):
            for system, arch in (('win32', 'amd64'), ('linux', 'riscv64')):
                with mock.patch.object(remotepeer.sys, 'platform', system), \
                        mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                        self.assertRaisesRegex(remotepeer.NotExecuted, 'Install it yourself.*Fix'):
                    remotepeer.install_agent({'agent': agent})

    def test_codex_layout_current_marker_macos_host_link_and_custom_home(self):
        for custom in (False, True):
            with self.subTest(custom=custom):
                root = self.codex()
                if custom:
                    os.environ['CODEX_HOME'] = str(self.home / 'custom-codex')
                    root = self.home / 'custom-codex/packages/standalone'
                with mock.patch.object(remotepeer, '_agent_download', wraps=remotepeer._agent_download) as download:
                    remotepeer.install_agent({'agent': 'codex'})
                self.assertEqual(download.call_args.args[4], 400 << 20)
                directory = root / 'releases' / (self.version + '-aarch64-apple-darwin')
                self.assertTrue((root / 'current').is_symlink())
                self.assertEqual((root / 'current').resolve(), directory)
                self.assertEqual((root / 'auto-update-version').read_text(), directory.name)
                self.assertEqual(os.readlink(directory / 'codex'), 'bin/codex')
                for name in ('codex', 'codex-code-mode-host'):
                    link = self.home / '.local/bin' / name
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), directory / 'bin' / name)
                for name in ('bin/codex', 'bin/codex-code-mode-host', 'bin/codex-exec-server', 'codex-path/rg'):
                    self.assertEqual((directory / name).stat().st_mode & 0o777, 0o755)
                self.assertEqual((directory / 'codex-resources/data.txt').stat().st_mode & 0o777, 0o644)
                self.assertEqual(self.runtime.call_args.args[0], [str(self.home / '.local/bin/codex'), '--version'])
                self.assertEqual(list(root.glob('releases/.staging.*')), [])
                self.assertEqual(list(root.glob('releases/.download-*')), [])

    def test_codex_linux_always_static_musl_with_bwrap_and_no_host_link(self):
        self.codex('x86_64-unknown-linux-musl')
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.platform, 'machine', return_value='x86_64'):
            remotepeer.install_agent({'agent': 'codex'})
        directory = self.home / '.codex/packages/standalone/current'
        self.assertEqual((directory / 'codex-resources/bwrap').stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.home / '.local/bin/codex-code-mode-host').exists())

    def test_codex_platform_mapping_uses_package_targets(self):
        for system, arch, target in (('darwin', 'arm64', 'aarch64-apple-darwin'),
                                      ('darwin', 'amd64', 'x86_64-apple-darwin'),
                                      ('linux', 'aarch64', 'aarch64-unknown-linux-musl'),
                                      ('linux', 'x86_64', 'x86_64-unknown-linux-musl')):
            with self.subTest(target=target), mock.patch.object(remotepeer.sys, 'platform', system), \
                    mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                    mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=0, stdout=b'0')):
                self.assertEqual(remotepeer._agent_target('codex'), target)

    def test_codex_sums_and_archive_digest_failures_never_extract_or_execute(self):
        for kind in ('sums', 'archive'):
            with self.subTest(kind=kind):
                root = self.codex()
                self.responses[self.sums_url if kind == 'sums' else self.package_url] = b'tampered'
                with mock.patch.object(remotepeer, '_codex_extract') as extract, \
                        self.assertRaisesRegex(remotepeer.NotExecuted, 'SHA-256.*mismatch'):
                    remotepeer.install_agent({'agent': 'codex'})
                extract.assert_not_called()
                self.assertFalse((root / 'current').exists())
                self.assert_clean_failure('codex')

    def test_codex_unsafe_archive_members_are_refused_and_staging_is_removed(self):
        for kind in ('symlink', 'hardlink', 'traversal', 'absolute', 'device'):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo('codex-resources/../escape' if kind == 'traversal' else
                                         '/absolute' if kind == 'absolute' else 'codex-resources/unsafe')
                if kind in ('symlink', 'hardlink'):
                    member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    member.linkname = '/outside'
                elif kind == 'device':
                    member.type = tarfile.CHRTYPE
                self.codex(unsafe=member)
                with self.assertRaisesRegex(remotepeer.NotExecuted, 'Unsafe'):
                    remotepeer.install_agent({'agent': 'codex'})
                self.assert_clean_failure('codex')

    def test_codex_version_target_and_entrypoint_are_checked_before_publication(self):
        good = dict(version=self.version, target='aarch64-apple-darwin', entrypoint='bin/codex')
        for key in good:
            with self.subTest(key=key):
                self.codex(package=dict(good, **{key: 'wrong'}))
                with self.assertRaisesRegex(remotepeer.NotExecuted, 'version/target/entrypoint mismatch'):
                    remotepeer.install_agent({'agent': 'codex'})
                self.assert_clean_failure('codex')

    def test_https_downgrade_is_refused(self):
        handler = remotepeer._AgentRedirect()
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'HTTPS'):
            handler.redirect_request(mock.Mock(), None, 302, 'redirect', {}, 'http://example.com/binary')

    def test_service_path_finds_installed_agents_on_macos_and_linux(self):
        bindir = self.home / '.local/bin'
        bindir.mkdir(parents=True)
        for agent in ('claude', 'codex'):
            (bindir / agent).write_bytes(b'fixture')
            (bindir / agent).chmod(0o755)
        # The service inherits _tool_path; no shell profile or interactive login is involved.
        for system in ('darwin', 'linux'):
            with mock.patch.object(remotepeer.sys, 'platform', system):
                path = remotepeer._tool_path(str(self.home / 'system-bin'))
                self.assertEqual(remotepeer._tool_path(path), path)
                for agent in ('claude', 'codex'):
                    self.assertEqual(self.which(agent, path=path), str(bindir / agent))

    def test_archive_member_count_and_extracted_size_caps(self):
        for kind in ('count', 'size'):
            with self.subTest(kind=kind):
                stage = self.home / kind
                stage.mkdir()
                member = tarfile.TarInfo('codex-resources')
                member.type = tarfile.DIRTYPE
                member.size = (1 << 30) + 1 if kind == 'size' else 0
                members = [member] if kind == 'size' else []
                if kind == 'count':
                    for number in range(5001):
                        directory = tarfile.TarInfo('codex-resources/d%s' % number)
                        directory.type = tarfile.DIRTYPE
                        members.append(directory)
                archive = mock.MagicMock()
                archive.__enter__.return_value = archive
                archive.__iter__.return_value = iter(members)
                with mock.patch.object(remotepeer.tarfile, 'open', return_value=archive), \
                        self.assertRaisesRegex(remotepeer.NotExecuted, 'oversized'):
                    remotepeer._codex_extract(self.home / 'fixture.tar.gz', stage, self.version, 'aarch64-apple-darwin')
        self.runtime.assert_not_called()

    def test_prepublication_version_failures_clean_staging_and_do_not_publish(self):
        for agent in ('claude', 'codex'):
            self.claude() if agent == 'claude' else self.codex()
            with self.subTest(agent=agent), mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(
                    returncode=1, stdout=b'private diagnostic')), \
                    self.assertRaisesRegex(remotepeer.NotExecuted, 'version verification failed'):
                remotepeer.install_agent({'agent': agent})
            self.assert_clean_failure(agent)


class LaunchdServiceTest(unittest.TestCase):
    """All launchctl calls are stubbed, including status and the SSH user domain probe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launchd ' & ", dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.app, self.state = self.home / 'app', self.home / 'state'
        self.root = self.home / 'projects'
        self.which = remotepeer.shutil.which
        self.python = str(self.home / '.local/bin/python3.12')
        self.target = 'user/%d/%s' % (os.getuid(), remotepeer.SERVER_LABEL)
        self.loaded, self.pid, self.delayed, self.available = False, 0, 0, True
        self.commands = []
        patches = [mock.patch.multiple(remotepeer, HOME=self.home, APP=self.app, STATE=self.state),
                   mock.patch.object(remotepeer.sys, 'platform', 'darwin'),
                   mock.patch.object(remotepeer.sys, 'executable', self.python),
                   mock.patch.object(remotepeer.shutil, 'which', side_effect=lambda name: '/bin/launchctl' if name == 'launchctl' else None),
                   mock.patch.object(remotepeer, '_setup_run', side_effect=self.launchctl),
                   mock.patch.object(remotepeer, 'run', side_effect=self.mint),
                   mock.patch.object(remotepeer, '_service_info', side_effect=lambda: {'project_root': str(self.root)} if self.loaded else None),
                   mock.patch.object(remotepeer.time, 'sleep')]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        raw, sha = remotes.bundle()
        self.req = dict(archive=base64.b64encode(raw).decode(), sha=sha, setup=True, project_path=str(self.root))

    def mint(self, argv, **kw):
        self.assertEqual(argv[:2], [self.python, '-c'])
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    def launchctl(self, argv, *args, **kw):
        self.commands.append(argv)
        self.assertEqual(argv[0], 'launchctl')
        self.assertTrue(argv[2].startswith('user/'), argv)
        rc, output = 0, b''
        if argv[1] == 'print':
            if argv[2] != self.target:
                rc = 0 if self.available else 1
            elif self.delayed:
                self.delayed -= 1
                output = b'job still unloading'
            elif self.loaded:
                output = ('\tpid = %d\n\tenvironment = {\n\t\tPATH => /usr/bin:/bin\n\t}\n' % self.pid).encode()
            else:
                rc = 1
        elif argv[1] == 'bootout':
            self.delayed = 2 if self.loaded else 0
            rc = 0 if self.loaded else 3
            self.loaded = False
        elif argv[1] == 'bootstrap':
            self.assertFalse(self.delayed, 'bootstrap must wait for asynchronous bootout')
            self.assertEqual(argv[3], str(self.home / 'Library/LaunchAgents/dev.piper.server.plist'))
            self.loaded = True; self.pid += 1
        else:
            self.assertEqual(argv[1], 'enable')
        return subprocess.CompletedProcess(argv, rc, output, b'')

    def test_install_update_use_user_domain_selected_python_and_private_plist(self):
        for _ in range(2):
            result = remotepeer.install(self.req)
            self.assertTrue(result['installed'])
            self.assertTrue(result['service_running'])
            self.assertTrue(result['linger'])
            self.assertTrue(result['autonomous_after_logout'])
            self.assertEqual(result['notice'], '')
        self.assertEqual(self.pid, 2)
        path = self.home / 'Library/LaunchAgents/dev.piper.server.plist'
        agent = plistlib.loads(path.read_bytes())
        self.assertEqual(agent['ProgramArguments'], [self.python, str(self.app / 'current/serve.py')])
        self.assertEqual(agent['Label'], 'dev.piper.server')
        self.assertEqual(agent['WorkingDirectory'], str(self.home))
        self.assertTrue(all(agent[k] for k in ('RunAtLoad', 'KeepAlive', 'AbandonProcessGroup')))
        # Regression: a real Mac refused bootstrap into user/<uid> (error 5) without the Background session type.
        self.assertEqual(agent['LimitLoadToSessionType'], 'Background')
        # Regression: /usr/bin/java on macOS is a stub; tools installed for Piper in ~/.local/bin must come first.
        self.assertEqual(agent['EnvironmentVariables']['PATH'].split(':')[0], str(self.home / '.local/bin'))
        self.assertEqual(agent['StandardOutPath'], str(self.state / 'dev.piper.server.log'))
        self.assertEqual(agent['StandardErrorPath'], agent['StandardOutPath'])
        self.assertEqual(agent['EnvironmentVariables']['PIPER_DEFAULT_RUNTIME'], 'direct')
        self.assertEqual(agent['EnvironmentVariables']['PROJECT_PATH'], str(self.root))
        self.assertEqual(agent['EnvironmentVariables']['PIPER_HOME'], str(self.state))
        self.assertEqual(agent['EnvironmentVariables']['PIPER_PTY_SOCK'], str(self.state / 'ptyhost.sock'))
        self.assertEqual(agent['EnvironmentVariables']['PIPER_BIND'], '127.0.0.1')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / '.config/systemd').exists())
        self.assertEqual(remotepeer._service_env('PATH'), '/usr/bin:/bin')

    def test_macos_service_launch_path_exposes_both_agents_to_session_start(self):
        bindir = self.home / '.local/bin'
        bindir.mkdir(parents=True)
        for agent in ('claude', 'codex'):
            (bindir / agent).write_bytes(b'fixture')
            (bindir / agent).chmod(0o755)
        remotepeer.install(self.req)
        environment = plistlib.loads((self.home / 'Library/LaunchAgents/dev.piper.server.plist').read_bytes())[
            'EnvironmentVariables']
        launch_env = dict(os.environ, **environment)
        for key in ('PIPER_CLAUDE_BIN', 'PIPER_CODEX_BIN'):
            launch_env.pop(key, None)
        with mock.patch.dict(os.environ, launch_env, clear=True), mock.patch.object(remotepeer.shutil, 'which', wraps=self.which):
            self.assertEqual(features.session_agents(), {'claude': True, 'codex': True})

    def test_unavailable_launchd_is_not_executed_before_managed_writes(self):
        self.available = False
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'launchd user domain'):
            remotepeer._install(self.req)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / 'Library').exists())
        self.assertEqual([c[1] for c in self.commands], ['print'])

    def test_old_python_and_symlinked_launchagent_are_definite_refusals(self):
        with mock.patch.object(remotepeer.sys, 'version_info', (3, 9, 6)):
            with self.assertRaisesRegex(remotepeer.NotExecuted, r'Python 3.10.*~/.local/bin'):
                remotepeer._install(self.req)
        self.assertEqual(self.commands, [])
        directory = self.home / 'Library/LaunchAgents'
        directory.mkdir(parents=True)
        (directory / 'dev.piper.server.plist').symlink_to(self.home / 'unmanaged.plist')
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'symlinks'):
            remotepeer._install(self.req)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.root.exists())

    def test_a_job_without_a_pid_cannot_supply_live_environment(self):
        output = b'\tinherited environment = {\n\t\tPATH => /wrong\n\t}\n\tenvironment = {\n\t\tPATH => /live\n\t}\n'
        with mock.patch.object(remotepeer, '_setup_run', return_value=subprocess.CompletedProcess([], 0, output, b'')) as run:
            self.assertIsNone(remotepeer._service_env('PATH'))
            run.return_value.stdout = b'\tpid = 123\n' + output
            self.assertEqual(remotepeer._service_env('PATH'), '/live')

    def test_a_job_that_will_not_unload_is_bounded_and_never_bootstrapped(self):
        self.loaded = True
        def stuck(argv, *args, **kw):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'job still unloading', b'')
        with mock.patch.object(remotepeer, '_setup_run', side_effect=stuck):
            with self.assertRaisesRegex(ValueError, 'did not unload'):
                remotepeer._launch_restart(remotepeer.SERVER_LABEL)
        self.assertEqual(len(self.commands), 21)
        self.assertFalse(any(c[1] == 'bootstrap' for c in self.commands))

    def test_update_preserves_extension_environment(self):
        remotepeer.install(self.req)
        path = self.home / 'Library/LaunchAgents/dev.piper.server.plist'
        agent = plistlib.loads(path.read_bytes())
        agent['EnvironmentVariables']['PIPER_EXTENSIONS'] = str(self.app / 'extensions/snapshot/acme')
        path.write_bytes(plistlib.dumps(agent))
        remotepeer.install(self.req)
        self.assertEqual(plistlib.loads(path.read_bytes())['EnvironmentVariables']['PIPER_EXTENSIONS'],
                         agent['EnvironmentVariables']['PIPER_EXTENSIONS'])

    def test_bootstrap_failure_is_not_a_definite_prerequisite_refusal(self):
        real = self.launchctl
        def fail(argv, *args, **kw):
            if argv[1] == 'bootstrap':
                return subprocess.CompletedProcess(argv, 5, b'', b'private diagnostic')
            return real(argv, *args, **kw)
        with mock.patch.object(remotepeer, '_setup_run', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'LaunchAgent setup failed') as error:
                remotepeer.install(self.req)
        self.assertNotIsInstance(error.exception, remotepeer.NotExecuted)
        self.assertNotIn('private diagnostic', str(error.exception))
        self.assertTrue((self.app / 'current').exists())


class RemoteServiceTest(unittest.TestCase):
    def test_installed_release_claude_survives_controller_restart(self):
        self.exercise_release("claude")

    @unittest.skipUnless(sys.version_info >= (3,11), "effective Codex config fixture requires tomllib")
    def test_installed_release_codex_survives_controller_restart(self):
        self.exercise_release("codex")

    def exercise_release(self, agentname):
        with tempfile.TemporaryDirectory(prefix='rs-') as directory:
            home=Path(directory)
            app=home/'app'; state=home/'state'; root=home/'projects'; project=root/'repo'
            project.mkdir(parents=True)
            subprocess.run(['git','init','-q',str(project)],check=True)
            (project/'hello.txt').write_text('server-owned file\n')
            subprocess.run(['git','-C',str(project),'add','hello.txt'],check=True)
            subprocess.run(['git','-C',str(project),'commit','-qm','initial'],check=True)
            binary=home/'.local/bin';binary.mkdir(parents=True)
            agent=binary/agentname
            agent.write_text('#!'+sys.executable+'\nimport json,os,shlex,subprocess,sys,time\n'
                'if "--help" in sys.argv: print("--no-daemon");sys.exit(0)\n'
                'if "--settings" in sys.argv: cfg=json.load(open(sys.argv[sys.argv.index("--settings")+1]))\n'
                'else:\n'
                ' cfg={"hooks":{}}\n'
                ' for i,arg in enumerate(sys.argv[:-1]):\n'
                '  if arg=="-c" and sys.argv[i+1].startswith("hooks."):\n'
                '   cfg["hooks"].update(__import__("tomllib").loads(sys.argv[i+1])["hooks"])\n'
                'for ev in ("SessionStart","UserPromptSubmit"):\n'
                ' command=cfg["hooks"][ev][0]["hooks"][0]["command"]\n'
                ' subprocess.run(shlex.split(command),input=json.dumps({"session_id":"12345678-abcd","source":"startup","prompt":"test"}).encode(),check=True)\n'
                'print("server-agent-running",flush=True)\ntime.sleep(60)\n')
            agent.chmod(0o700)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            raw,sha=remotes.bundle()
            if agentname == 'claude':
                state.mkdir()
                (state/'features.json').write_text(json.dumps({'v':2,'base':'basic','modules':{'codex':False},'onboarded':True}))
            real_run=remotepeer.run
            def systemd(argv,**kw):
                if argv[0] in ('systemctl','loginctl'):
                    return subprocess.CompletedProcess(argv,0,stdout=b'yes\n',stderr=b'')
                if argv[:2] == [sys.executable,'-c']:
                    argv=list(argv)
                    # Use the sandbox's read-only source for minting, not writable test code.
                    argv[2]=argv[2].replace(str(app/'current/piper'),common.PIPER_DIR)
                    for release in app.glob('release-*'):
                        argv[2]=argv[2].replace(str(release/'piper'),common.PIPER_DIR)
                result=real_run(argv,**kw)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                return result
            env=dict(os.environ,HOME=str(home),PIPER_REAL_HOME=str(home),PATH='/usr/bin:/bin')
            for key in ('PIPER_CLAUDE_BIN', 'PIPER_CODEX_BIN'):
                env.pop(key, None)
            processes=[]
            log=(home/'daemon.log').open('ab')
            self.addCleanup(log.close)
            def start():
                release=os.environ.get('PIPER_REMOTE_TEST_RELEASE') or common.SK
                serverenv=dict(env,PIPER_HOME=str(state),PIPER_PTY_SOCK=str(state/'ptyhost.sock'),PIPER_SERVICE_LABEL='piper-server',PIPER_DEFAULT_RUNTIME='direct',PIPER_PORT=str(port),PIPER_BIND='127.0.0.1',PROJECT_PATH=str(root),PATH=service_env['PATH'])
                p=subprocess.Popen([sys.executable,os.path.join(release,'piper/piperd.py')],env=serverenv,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                processes.append(p)
                deadline=time.time()+10
                while time.time()<deadline:
                    try:
                        r=remotepeer.http(dict(mode='service',project_path=str(root),method='GET',path='/api/state',body='',ctype='application/json'))
                        if r['status']==200:return p,json.loads(base64.b64decode(r['body']))
                    except (OSError,ValueError):pass
                    if p.poll() is not None:break
                    time.sleep(.1)
                self.fail('installed server did not start: '+(home/'daemon.log').read_text()[-3000:])
            def request(method,path,body=None):
                r=remotepeer.http(dict(mode='service',project_path=str(root),method=method,path=path,body=base64.b64encode(json.dumps(body or {}).encode()).decode(),ctype='application/json'))
                return r['status'],json.loads(base64.b64decode(r['body']))
            real_which=__import__('shutil').which
            with mock.patch.multiple(remotepeer,HOME=home,APP=app,STATE=state,PORT=port), \
                    mock.patch.dict(os.environ,env,clear=True), \
                    mock.patch.object(remotepeer.sys,'platform','linux'), \
                    mock.patch.object(remotepeer.shutil,'which',side_effect=lambda name: '/bin/'+name if name in ('systemctl','loginctl') else real_which(name)), \
                    mock.patch.object(remotepeer,'run',side_effect=systemd):
                result=remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=sha,project_path=str(root)))
                self.assertTrue(result['installed'])
                self.assertTrue(result['autonomous_after_logout'])
                self.assertNotIn('token',result)
                # Use the installed runner's actual PATH with the sandbox's read-only core source.
                runner = ast.parse((app / 'current/serve.py').read_text())
                service_env = ast.literal_eval(runner.body[1].value.args[0])
                self.assertIn(str(binary), service_env['PATH'].split(':'))
                try:
                    p,st=start()
                    self.assertEqual(st['default_runtime'],'direct')
                    self.assertFalse(st['features']['modules']['codex'], 'installation never rewrites module preferences')
                    self.assertTrue(st['agent_availability'][agentname])
                    agent.rename(binary / (agentname + '.aside'))
                    self.assertFalse(request('GET','/api/state')[1]['agent_availability'][agentname])
                    code, missing = request('POST','/api/sessions',dict(project='repo',runtime='direct',agent=agentname))
                    self.assertEqual(code,409,missing)
                    self.assertIn('not installed',missing['error'])
                    self.assertEqual(request('GET','/api/state')[1]['sessions'],[])
                    (binary / (agentname + '.aside')).rename(agent)
                    self.assertTrue(request('GET','/api/state')[1]['agent_availability'][agentname])
                    self.assertEqual(st['projects'][0]['path'],str(project))
                    self.assertTrue(remotepeer.probe({'project_path': str(root) + '/'})['project_root_matches'])
                    for method in ('GET', 'POST'):
                        with self.assertRaisesRegex(ValueError, 'Projects folder differs'):
                            remotepeer.http(dict(mode='service',project_path=str(home/'other-projects'),method=method,
                                path='/api/state' if method=='GET' else '/api/sessions',body='',ctype='application/json'))
                    self.assertEqual(request('GET','/api/state')[1]['sessions'],[])
                    code,row=request('POST','/api/sessions',dict(project='repo',task='test native hooks',runtime='direct',agent=agentname,remember_for_project=True))
                    self.assertEqual(code,202,row)
                    self.assertEqual(row['project_defaults']['agent'],agentname)
                    sid=row['id']
                    deadline=time.time()+8
                    while time.time()<deadline:
                        st=request('GET','/api/state')[1]
                        s=next(x for x in st['sessions'] if x['id']==sid)
                        if s['state']=='working':break
                        if s['state']=='error':self.fail(s['detail'])
                        time.sleep(.1)
                    self.assertEqual(s['state'],'working',s)
                    self.assertEqual(s['runtime'],'direct')
                    self.assertEqual(s['claude_session'],'12345678-abcd')
                    original_pid=s['pid']
                    other = home / 'other-projects'; other.mkdir()
                    with self.assertRaisesRegex(remotepeer.RebindRefused, 'sessions are active'):
                        remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=sha,project_path=str(other)))
                    self.assertEqual(request('GET','/api/state')[1]['sessions'][0]['pid'], original_pid)
                    code,data=request('GET','/api/fs/read?project=repo&path=hello.txt')
                    self.assertEqual(code,200,data)
                    self.assertIn('server-owned file','\n'.join(data['lines']))
                    self.assertEqual(request('GET',f'/api/sessions/{sid}/git')[0],200)
                    if agentname == 'claude':
                        self.exercise_local_provider(home, state, project, port, env, sid)
                    # Restart only the throwaway controller, never the user's daemon/ptyhost.
                    p.terminate();p.wait(timeout=5)
                    p,st=start()
                    restored=next(x for x in st['sessions'] if x['id']==sid)
                    self.assertEqual(restored['pid'],original_pid)
                    self.assertIn(restored['state'],('working','ready','waiting'))
                    os.kill(original_pid,0)
                    pc=PtyClient(str(state/'ptyhost.sock'))
                    try:
                        reply=pc.call('subscribe',id=sid)
                        self.assertTrue(reply['ok'])
                        self.assertTrue(reply['info']['alive'])
                    finally:pc.close()
                    self.exercise_terminal_bridge(state, port, sid, original_pid)
                    self.assertEqual(request('POST',f'/api/sessions/{sid}/stop')[0],200)
                finally:
                    # Terminate children through the private test PTY socket and reap its test host.
                    try:
                        pc=PtyClient(str(state/'ptyhost.sock'))
                        hostpid=pc.call('ping')['pid']
                        for row in pc.call('list').get('sessions',[]):
                            if row['alive']:pc.call('signal',id=row['id'],sig='KILL')
                        pc.close()
                        os.kill(hostpid,signal.SIGTERM)
                    except OSError:pass
                    for p in processes:
                        if p.poll() is None:p.terminate();p.wait(timeout=5)
                    pidfile=state/'ptyhost.pid'
                    if pidfile.exists():
                        pid=int(pidfile.read_text());os.kill(pid,signal.SIGTERM)
                    log.close()

    def exercise_terminal_bridge(self, state, port, sid, agent_pid):
        """Actual binary PTY protocol over peer stdio; SSH process creation alone is substituted."""
        identity=remotepeer.peer_identity()
        entry=dict(id='fixture',host='fixture',project_path=str(state.parent/'projects'),mode='service',identity=identity)
        source=remotes.peer_source().replace("STATE = HOME / '.piper-server'",'STATE = Path('+repr(str(state))+')').replace('PORT = 17896','PORT = '+str(port))
        def local_transport(entry,command,**kw):
            return [sys.executable,*command[1:]]
        with mock.patch.object(remotes,'get',return_value=entry),mock.patch.object(remotes,'peer_source',return_value=source),mock.patch.object(remotes,'ssh',side_effect=local_transport):
            bridge=remotes.terminal('fixture',sid,lambda event: None)
            try:
                reply=bridge.call('subscribe',id=sid)
                self.assertTrue(reply['ok'])
                self.assertTrue(reply['info']['alive'])
                self.assertTrue(bridge.call('resize',id=sid,cols=100,rows=30)['ok'])
            finally: bridge.close()
        # Closing the client/transport is independent of the server-owned agent lifetime.
        os.kill(agent_pid,0)

    def exercise_local_provider(self, home, state, project, port, env, sid):
        """Real stdio MCP -> session mailbox -> host capability HTTP -> existing project HTTP tools, no mocked Manager."""
        (project/'http-client.env.json').write_text(json.dumps({'test':{'host':'http://127.0.0.1:9'}}))
        (project/'http-client.private.env.json').write_text(json.dumps({'test':{'token':'PLANTED_PROVIDER_SECRET'}}))
        grant=dict(id='a'*32,token='test-capability-placeholder',enabled=True,environment='fixture',remote_project='server/repo',local_project='repo')
        path=state/'provider-grants.json'
        path.write_text(json.dumps({'grants':[grant]})); path.chmod(0o600)
        config=state/'sessions'/sid/'local-provider.json'
        config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{port}/api/local-provider/'+grant['id'],token=grant['token'])))
        config.chmod(0o600)
        wrapper=os.path.join(common.PIPER_DIR,'sandbox/piper-local-mcp.py')
        def mcp(messages):
            result=subprocess.run([sys.executable,wrapper,'--mailbox',str(state/'sessions'/sid/'piper/piper-mailbox')],input=''.join(json.dumps(m)+'\n' for m in messages),text=True,capture_output=True,env=env,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('PLANTED_PROVIDER_SECRET',result.stdout)
            return [json.loads(line)['result'] for line in result.stdout.splitlines()]
        call=dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='http_envs',arguments={}))
        listed,reply,denied=mcp([dict(jsonrpc='2.0',id=1,method='tools/list'),call,dict(call,id=3,params=dict(name='git_run',arguments={}))])
        names={t['name'] for t in listed['tools']}
        self.assertIn('http_run',names)
        self.assertIn('db_query',names)
        self.assertNotIn('git_run',names)
        self.assertFalse(reply['isError'],reply)
        self.assertIn('test',reply['content'][0]['text'])
        self.assertTrue(denied['isError'])
        # Reverse SSH preserves the tunnel port in HTTP Host. Forward real bytes
        # through a distinct loopback port, while the private config names the
        # controller's expected Host. Do not weaken its global Host/Origin gate.
        import socketserver, selectors, threading
        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                with socket.create_connection(('127.0.0.1',port)) as upstream, selectors.DefaultSelector() as selector:
                    selector.register(self.request,selectors.EVENT_READ,upstream)
                    selector.register(upstream,selectors.EVENT_READ,self.request)
                    while True:
                        for key,_ in selector.select(timeout=5):
                            data=key.fileobj.recv(65536)
                            if not data:return
                            key.data.sendall(data)
        proxy=socketserver.ThreadingTCPServer(('127.0.0.1',0),Forward)
        proxy.daemon_threads=True
        thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
        try:
            config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{proxy.server_address[1]}/api/local-provider/'+grant['id'],host=f'127.0.0.1:{port}',token=grant['token'])))
            tunneled=mcp([call])[0]
            self.assertFalse(tunneled['isError'],tunneled)
            self.assertIn('test',tunneled['content'][0]['text'])
        finally:
            proxy.shutdown();proxy.server_close();thread.join(5)
        records=list((state/'provider-calls'/grant['id']).glob('*.json'))
        self.assertEqual(len(records),2)
        self.assertEqual(json.loads(records[0].read_text())['state'],'completed')
        self.assertNotIn('PLANTED_PROVIDER_SECRET',records[0].read_text())
        config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{port}/api/local-provider/'+grant['id'],token=grant['token'])))
        grant['enabled']=False
        path.write_text(json.dumps({'grants':[grant]}))
        refused=mcp([call])[0]
        self.assertTrue(refused['isError'])
        self.assertIn('revoked',refused['content'][0]['text'])
        self.assertIn('No operation was sent',refused['content'][0]['text'])
        self.assertEqual(len(list((state/'provider-calls'/grant['id']).glob('*.json'))),2)
