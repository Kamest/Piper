// Every GUI test file imports this first: the tests run only inside the OS sandbox of scripts/piper-sandbox.py
// One check for both suites: piper/testsandbox.py, which exits non-zero outside it.
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const check = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'piper', 'testsandbox.py');
try {
  execFileSync('python3', [check], { stdio: ['ignore', 'inherit', 'inherit'] });
} catch {
  process.exit(3);
}
