import { mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pins = JSON.parse(await readFile(path.join(root, 'build/python.json'), 'utf8'));
const platform = process.argv[2] || process.platform;
const arch = process.argv[3] || process.arch;
const pin = pins[`${platform}-${arch}`];
if (!pin) throw new Error('No Python pin for this platform/architecture');
const folder = path.join(root, 'vendor', `${platform === 'win32' ? 'win' : platform === 'darwin' ? 'mac' : platform}-${arch}`);
await mkdir(folder, { recursive: true });
const archive = path.join(folder, 'python.tar.gz');
const response = await fetch(pin.url);
if (!response.ok) throw new Error('Python download failed');
const bytes = Buffer.from(await response.arrayBuffer());
if (createHash('sha256').update(bytes).digest('hex') !== pin.sha256) throw new Error('Python SHA-256 mismatch');
await writeFile(archive, bytes);
// The archive includes python/LICENSE.* and its third-party licenses; preserve the whole tree.
await rm(path.join(folder, 'python'), { recursive: true, force: true });
// terminfo holds names differing only by case (hp2621A, hp2621a): extraction or packaging on a case-insensitive
// file system (macOS, Windows building the WSL runtime) fails. Piper never uses curses.
execFileSync('tar', ['-xzf', archive, '-C', folder, '--exclude', 'python/share/terminfo']);
await rm(archive);
console.log(`Python ${pins.version} prepared for ${platform}-${arch}`);
