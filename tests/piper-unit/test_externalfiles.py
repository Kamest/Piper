"""Human-only external file access, without changing the project/agent path resolver."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import _env
import common
import projectfs
import workbench


class ExternalFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TEST_SANDBOX_WORK'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.wb = workbench.Workbench()
        self.wb.sessions = {}
        self.enterContext(mock.patch.object(workbench.projects, 'load', return_value={}))
        self.enterContext(mock.patch.object(workbench.extensions, 'collect', return_value=[]))

    def write(self, path, text='hello\n'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    def test_read_is_bounded_readonly_and_keeps_every_line(self):
        path = self.write(self.root / 'notes.md', 'line\n' * 6000)
        result = self.wb.fs_external(path)
        self.assertEqual(result['total_lines'], 6000)
        self.assertEqual(len(result['lines']), 6000)
        self.assertTrue(result['external'])
        self.assertNotIn('sha', result)
        large = self.wb.fs_external(self.write(self.root / 'large', 'x' * (projectfs.MAX_READ + 1)))
        self.assertEqual(large['preview'], 'download')
        self.assertTrue(large['downloadable'])
        (self.root / 'binary').write_bytes(b'x\0y')
        binary = self.wb.fs_external(str(self.root / 'binary'))
        self.assertEqual(binary['preview'], 'download')
        self.assertNotIn('lines', binary)

    def test_external_folder_navigation_is_bounded_readonly_and_never_registers(self):
        folder = self.root / 'documents'
        self.write(folder / 'nested' / 'notes.md')
        self.write(folder / 'z.txt')
        self.write(folder / '.git' / 'config')
        (folder / 'private').symlink_to(common.PIPER_HOME, target_is_directory=True)
        result = self.wb.fs_external(str(folder))
        self.assertEqual(result['kind'], 'dir')
        self.assertEqual([entry['name'] for entry in result['entries']], ['nested', 'z.txt'])
        nested = self.wb.fs_external(str(folder / 'nested'), root=str(folder))
        self.assertEqual(nested['root'], str(folder))
        self.assertEqual(nested['entries'][0]['name'], 'notes.md')
        with self.assertRaisesRegex(projectfs.FsError, 'no longer exists'):
            self.wb.fs_external(str(folder / 'missing'), root=str(folder))
        with self.assertRaises(projectfs.FsError):
            self.wb.fs_external(str(self.root), root=str(folder))
        with self.assertRaises(projectfs.FsError):
            self.wb.fs_external(str(folder / 'private'), root=str(folder))
        # A registered nested checkout still uses read-only external context; merely
        # browsing its parent must not register a project or widen any agent grants.
        p = {'key': 'acme/nested', 'path': str(folder / 'nested')}
        with mock.patch.object(workbench.projects, 'load', return_value={'acme/nested': p}):
            file = self.wb.fs_external(str(folder / 'nested' / 'notes.md'), root=str(folder))
            self.assertEqual(file['preview'], 'text')
        self.assertEqual(self.wb.sessions, {})

    def test_images_documents_and_binary_download_preserve_bytes_and_caps(self):
        for name, raw, preview in [('sample.png', b'\x89PNG\r\n\x1a\nfixture', 'image'),
                                   ('guide.pdf', b'%PDF-1.4\nfixture', 'download'),
                                   ('archive.zip', b'PK\x03\x04\0fixture', 'download')]:
            path = self.root / name
            path.write_bytes(raw)
            result = self.wb.fs_external(str(path))
            self.assertEqual(result['preview'], preview)
            self.assertTrue(result['downloadable'])
            self.assertEqual(self.wb.fs_external_download(str(path))[1], raw)
        path = self.root / 'oversize.bin'
        with path.open('wb') as file:
            file.truncate(workbench.RAW_MAX + 1)
        result = self.wb.fs_external(str(path))
        self.assertEqual(result['preview'], 'download')
        self.assertFalse(result['downloadable'])
        with self.assertRaises(projectfs.TooLarge):
            self.wb.fs_external_download(str(path))

    def test_listing_bounds_candidates_even_if_all_are_refused(self):
        folder = self.root / 'many'
        folder.mkdir()
        for index in range(1100):
            self.write(folder / ('file-%04d.txt' % index))
        original = self.wb.fs_target
        started = time.monotonic()
        with mock.patch.object(self.wb, 'fs_target', wraps=original) as classify:
            result = self.wb.fs_external(str(folder))
        elapsed = time.monotonic() - started
        self.assertEqual(len(result['entries']), projectfs.MAX_LIST)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(classify.call_count, 1 + 2 * projectfs.MAX_LIST)
        print('EXTERNAL_LIST_TIMING entries=%s seconds=%.3f classifications=%s' % (len(result['entries']), elapsed, classify.call_count), flush=True)
        with mock.patch.object(common, 'safe_listdir', return_value=['refused-%04d' % i for i in range(5000)]), \
                mock.patch.object(self.wb, 'fs_target', wraps=original) as classify:
            result = self.wb.fs_external(str(folder))
        self.assertEqual(result['entries'], [])
        self.assertTrue(result['truncated'])
        self.assertLessEqual(classify.call_count, 1 + 2 * projectfs.MAX_LIST)

    def test_target_classifies_project_symlinks_and_executable(self):
        path = self.write(self.root / 'project' / 'notes.md')
        p = {'key': 'acme/docs', 'path': str(self.root / 'project')}
        with mock.patch.object(workbench.projects, 'load', return_value={'acme/docs': p}):
            target = self.wb.fs_target(path)
            self.assertEqual((target['project'], target['relative'], target['external']), ('acme/docs', 'notes.md', False))
            with self.assertRaises(projectfs.FsError): self.wb.fs_external(path)
        link = self.root / 'link.md'
        link.symlink_to(path)
        self.assertEqual(self.wb.fs_target(str(link))['path'], path)
        self.assertTrue(self.wb.fs_target(str(link))['link'])
        os.chmod(path, 0o755)
        self.assertFalse(self.wb.fs_target(path)['openable'])
        self.assertFalse(self.wb.fs_target(self.write(self.root / 'run.command'))['openable'])
        (self.root / 'Demo.app').mkdir()
        self.assertFalse(self.wb.fs_target(str(self.root / 'Demo.app'))['openable'])
        self.assertFalse(self.wb.fs_target(str(self.root / 'project'))['openable'])
        self.assertEqual(self.wb.fs_target(str(self.root / 'missing'))['kind'], 'missing')

    def test_native_launch_is_unavailable_for_documents_launchers_bundles_and_links(self):
        # A filename/exec-bit classifier cannot make Launch Services safe against a later path swap.
        for name in ('notes.txt', 'guide.pdf', 'picture.png', 'run.terminal', 'run.tool', 'run.jnlp',
                     'run.pkg', 'run.mpkg', 'run.workflow', 'run.inetloc', 'run.fileloc', 'unknown'):
            with self.subTest(name=name):
                self.assertFalse(self.wb.fs_target(self.write(self.root / name))['openable'])
        for name in ('folder', 'Demo.app', 'Build.workflow', 'Installer.pkg'):
            folder = self.root / name
            folder.mkdir()
            with self.subTest(name=name):
                self.assertFalse(self.wb.fs_target(str(folder))['openable'])
        alias = self.root / 'alias.txt'
        alias.symlink_to(self.root / 'run.terminal')
        target = self.wb.fs_target(str(alias))
        self.assertTrue(target['link'])
        self.assertFalse(target['openable'])

    def test_runtime_state_and_extension_secrets_are_refused_including_symlinks(self):
        state = self.write(Path(common.PIPER_HOME) / 'external-test' / 'state', 'private')
        link = self.root / 'notes'
        link.symlink_to(state)
        for path in (state, str(link), self.write(self.root / '.git' / 'config')):
            with self.assertRaises(projectfs.FsError): self.wb.fs_external(path)
            with self.assertRaises(projectfs.FsError): self.wb.fs_external_download(path)
        private = self.root / 'provider-state'
        path = self.write(private / 'state')
        for hook in ('protected', 'forbidden'):
            with mock.patch.object(workbench.extensions, 'collect', side_effect=lambda name, *args: [str(private)] if name == hook == 'protected' else [(str(private), 'any')] if name == hook == 'forbidden' else []):
                with self.assertRaises(projectfs.FsError): self.wb.fs_external(path)
                with self.assertRaises(projectfs.FsError): self.wb.fs_external_download(path)
                with self.assertRaises(projectfs.FsError): self.wb.fs_external(str(private))
                alias = private.with_name(private.name.upper()) / 'state'
                if alias.exists() and os.path.samefile(alias, path):
                    with self.assertRaises(projectfs.FsError): self.wb.fs_external(str(alias))
                    with self.assertRaises(projectfs.FsError): self.wb.fs_target(str(alias.parent / 'missing'))

    def test_parent_swap_after_classification_never_follows_runtime_state(self):
        path = self.write(self.root / 'folder' / 'state', 'public')
        state = Path(common.PIPER_HOME) / 'external-race'
        self.write(state / 'state', 'private')
        original = self.wb.fs_target
        def race(value):
            target = original(value)
            os.rename(self.root / 'folder', self.root / 'original')
            (self.root / 'folder').symlink_to(state, target_is_directory=True)
            return target
        with mock.patch.object(self.wb, 'fs_target', side_effect=race):
            with self.assertRaises(projectfs.FsError): self.wb.fs_external(path)
        with self.assertRaises(projectfs.FsError): projectfs._resolve(str(self.root), '../elsewhere', lambda _: False)

    def test_download_and_listing_do_not_follow_parent_swaps(self):
        for operation in ('download', 'list'):
            folder = self.root / operation
            path = self.write(folder / 'state', 'public')
            private = Path(common.PIPER_HOME) / ('external-race-' + operation)
            self.write(private / 'state', 'private')
            original = self.wb.fs_target
            def race(value):
                target = original(value)
                if folder.exists() and not folder.is_symlink():
                    folder.rename(self.root / (operation + '-original'))
                    folder.symlink_to(private, target_is_directory=True)
                return target
            with mock.patch.object(self.wb, 'fs_target', side_effect=race):
                with self.assertRaises(projectfs.FsError):
                    if operation == 'download': self.wb.fs_external_download(path)
                    else: self.wb.fs_external(str(folder))
