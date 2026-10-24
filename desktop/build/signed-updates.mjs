import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const yaml = require('js-yaml');
const { buildBlockMap } = require('app-builder-lib/out/targets/blockmap/blockmap.js');

// External Authenticode signing changes the installer bytes after electron-builder wrote update metadata.
export async function signedUpdates(installer, out) {
  const name = path.basename(installer);
  if (!/^Piper-.*-win-(x64|arm64)\.exe$/.test(name)) throw new Error('Expected a Piper Windows installer');
  const metadata = [];
  for (const file of await fs.readdir(out)) {
    if (!/^desktop.*\.yml$/.test(file)) continue;
    const data = yaml.load(await fs.readFile(path.join(out, file), 'utf8'));
    if (data.path === name && data.files?.some(f => f.url === name)) metadata.push({ file, data });
  }
  if (!metadata.length) throw new Error('No updater metadata for the signed installer');
  const target = path.join(out, name);
  await fs.copyFile(installer, target);
  const { sha512, size } = await buildBlockMap(target, 'gzip', target + '.blockmap');
  for (const { file, data } of metadata) {
    data.sha512 = sha512;
    for (const entry of data.files) {
      if (entry.url === name) { entry.sha512 = sha512; entry.size = size; delete entry.blockMapSize; }
    }
    await fs.writeFile(path.join(out, file), yaml.dump(data));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (process.argv.length !== 4) throw new Error('Usage: signed-updates.mjs <signed-installer> <out-dir>');
  await signedUpdates(process.argv[2], process.argv[3]);
}
