#!/usr/bin/env python3
"""Rebuild/check Piper's own Java17 helper; never modify or package the installed CLI.

Usage: JAVA_HOME=<JDK21> python3 scripts/piper-http-compat-build.py <ijhttp> [--check]
The committed artifact is built with javac 21, --release 17 and no debug metadata.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    if len(sys.argv) not in (2, 3) or len(sys.argv) == 3 and sys.argv[2] != '--check':
        sys.exit(__doc__)
    binary = Path(sys.argv[1]).resolve()
    javac = str(Path(os.environ['JAVA_HOME']) / 'bin/javac') if os.environ.get('JAVA_HOME') else shutil.which('javac')
    if not javac or not subprocess.check_output([javac, '-version'], text=True).startswith('javac 21.'):
        sys.exit('The reproducible compatibility build requires JDK 21 (Java17 runtime bytecode).')
    directory_source = Path(__file__).resolve().parent.parent / 'piper/httpcompat'
    sources = sorted(directory_source.glob('Piper*.java'))
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run([javac, '--release', '17', '-g:none', '-cp', str(binary.parent / 'lib/*'),
                        '-d', directory, *map(str, sources)], check=True)
        result = {path.name:path.read_bytes() for path in (Path(directory) / 'piper/bridge').glob('Piper*.class')}
    if '--check' in sys.argv:
        if {path.name:path.read_bytes() for path in directory_source.glob('Piper*.class')} != result:
            sys.exit('Native compatibility classes differ from their sources; rebuild the helpers.')
        print('Native compatibility classes match their sources (Java17 bytecode).')
    else:
        for name, data in result.items():
            (directory_source / name).write_bytes(data)
        for path in directory_source.glob('Piper*.class'):
            if path.name not in result:
                path.unlink()
        print('Built native compatibility classes: ' + ', '.join(sorted(result)))


if __name__ == '__main__':
    main()
