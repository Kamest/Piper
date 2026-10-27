"""scripts/piper-build.sh: a global tsc of another version than TSC_VERSION is not used (its source maps differ, so
--check failed on CI runners that ship one); npx with the pinned version is."""
import _env  # noqa: F401
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PiperBuildTest(unittest.TestCase):
    def check(self, global_version):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / 'scripts').mkdir()
        shutil.copy(ROOT / 'scripts/piper-build.sh', root / 'scripts/piper-build.sh')
        for v in ('xterm', 'codemirror', 'iroh', 'qrcode'):   # every vendored library the build verifies
            d = root / 'piper/web/vendor' / v
            d.mkdir(parents=True)
            (d / 'a.js').write_text('x')
            (d / 'SHA256SUMS').write_text(hashlib.sha256(b'x').hexdigest() + '  a.js\n')
        (root / 'piper/web/dist').mkdir()
        log = root / 'calls'
        (root / 'bin').mkdir()
        for name, version in (('tsc', global_version), ('npx', '')):
            p = root / 'bin' / name
            p.write_text('#!/bin/sh\n[ "$1" = -v ] && { echo "Version %s"; exit 0; }\necho "%s $*" >> %s\n'
                         % (version, name, log))
            p.chmod(0o700)
        env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'], TSC_VERSION='5.9.3')
        r = subprocess.run(['sh', str(root / 'scripts/piper-build.sh'), '--check'], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return log.read_text().splitlines()

    def test_global_tsc_of_another_version_falls_back_to_pinned_npx(self):
        calls = self.check('6.0.2')
        self.assertEqual([c.split()[:3] for c in calls], [['npx', '-y', '-p']])
        self.assertIn('typescript@5.9.3', calls[0])

    def test_global_tsc_of_the_pinned_version_is_used(self):
        calls = self.check('5.9.3')
        self.assertEqual([c.split()[0] for c in calls], ['tsc'])


if __name__ == '__main__':
    unittest.main()
