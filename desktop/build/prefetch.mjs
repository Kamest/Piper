// Seed the documented Electron / electron-builder caches from the same official release assets.
// Pins below match electron-builder 26.18.0; its own checksum validation remains enabled.
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { githubAsset } from './download.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export function electronCachePath(cache, version, filename) {
  const directory = `https://github.com/electron/electron/releases/download/v${version}`;
  return path.join(cache, createHash('sha256').update(directory).digest('hex'), filename);
}

const tools = {
  '7zip@1.0.0': {
    '7zip-darwin-arm64.tar.gz': '496a341abe210aae1a25bc202ee97f6de6c76a3dc80f91d96616be05502d72c1',
    '7zip-darwin-x86_64.tar.gz': '496a341abe210aae1a25bc202ee97f6de6c76a3dc80f91d96616be05502d72c1',
    '7zip-linux-arm64.tar.gz': '5aff5034206b78f8261249ceb922b5c7e04c9bdb733784d8f5b6df9732cf1f79',
    '7zip-linux-x64.tar.gz': 'd151bb44b2a9d9bfc52813ce4cac042916394a0ab8a56bd5d221a5dc9ef8d5d7',
    '7zip-win-arm64.tar.gz': 'ac3f38f96ce7498096a123bb0862dd6db863a7353c9e9e1c15f73c183adf6620',
    '7zip-win-x64.tar.gz': 'be071f15bd6da2f78fe81c6ddef2009b0c4d8a51f36b780cb806c7e6df95e1b3'
  },
  'icons@1.2.3': { 'icons-bundle.tar.gz': '4e5bb546c9d3ab0c2bbacc22857467bf415f742a34c2be9dd138779d2fc066f9' },
  'dmg-builder@1.2.5': {
    'dmgbuild-bundle-arm64-75c8a6c.tar.gz': '793404d0c96687e27d5ee40a668d498c92e36a64d6c2906df511031adb33cbeb',
    'dmgbuild-bundle-x86_64-75c8a6c.tar.gz': '1664972f9cc2d6e8fce3b63e42cd30078aff602669c5856939c4519921200433'
  },
  'appimage-12.0.1': { 'appimage-12.0.1.7z': 'd12ff7eb8f1d1ec4652ca5237a7fbdca33acc0c758045636feca62dc6ecb8ec4' },
  'fpm@2.1.4': {
    'fpm-1.17.0-ruby-3.4.3-linux-amd64.7z': '44b0ec6025c14ec137f56180e62675c0eae36233cdce53d0953d9c73ced8989f',
    'fpm-1.17.0-ruby-3.4.3-linux-arm64v8.7z': '338b50cfa7f12d745a997d1a3d000bcd0410008050fa7d8c4476a78a61c0564e'
  },
  'winCodeSign-2.6.0': { 'winCodeSign-2.6.0.7z': 'cdaec7154dda7cc31f88d886e2489379a0625a737d610b5ae7f62a12f16743a4' },
  'nsis-3.0.4.1': { 'nsis-3.0.4.1.7z': '9877df902530f96357d13a7a31ae2b9df67f48b11ffc9a1700a7c961574ec5fa' },
  'nsis-resources-3.4.1': { 'nsis-resources-3.4.1.7z': '593a9a92ef958321293ac6a2ee61e64bf1bd543142a5bd6b3d310709cc924103' }
};

export function toolsetAssets(platform, arch) {
  if (!['darwin', 'linux', 'win32'].includes(platform) || !['x64', 'arm64'].includes(arch)) throw new Error('Unsupported build platform');
  const host = platform === 'win32' ? 'win' : platform;
  const hostArch = platform === 'darwin' && arch === 'x64' ? 'x86_64' : arch;
  const selected = [['7zip@1.0.0', `7zip-${host}-${hostArch}.tar.gz`], ['icons@1.2.3', 'icons-bundle.tar.gz']];
  if (platform === 'darwin') selected.push(['dmg-builder@1.2.5', `dmgbuild-bundle-${hostArch}-75c8a6c.tar.gz`]);
  if (platform === 'linux') selected.push(['appimage-12.0.1', 'appimage-12.0.1.7z'],
    ['fpm@2.1.4', `fpm-1.17.0-ruby-3.4.3-linux-${arch === 'x64' ? 'amd64' : 'arm64v8'}.7z`]);
  if (platform === 'win32') selected.push(['winCodeSign-2.6.0', 'winCodeSign-2.6.0.7z'],
    ['nsis-3.0.4.1', 'nsis-3.0.4.1.7z'], ['nsis-resources-3.4.1', 'nsis-resources-3.4.1.7z']);
  return selected.map(([tag, name]) => ({ tag, name, checksum: tools[tag][name] }));
}

async function save(file, bytes) {
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(`${file}.partial`, bytes);
  await rename(`${file}.partial`, file);
}

export async function prefetch(mode, { platform = process.platform, arch = process.arch, env = process.env, asset = githubAsset, saveFile = save } = {}) {
  const pkg = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
  if (mode === 'electron') {
    if (!env.ELECTRON_CACHE || env.electron_config_cache !== env.ELECTRON_CACHE) throw new Error('Explicit shared Electron build cache required');
    const version = pkg.devDependencies.electron;
    const name = `electron-v${version}-${platform}-${arch}.zip`;
    const result = await asset(`https://github.com/electron/electron/releases/download/v${version}/${name}`);
    await saveFile(electronCachePath(env.ELECTRON_CACHE, version, name), result.bytes);
    await saveFile(path.join(env.ELECTRON_CACHE, `SHASUMS256.txt-${version}`), `${result.checksum} *${name}\n`);
  } else if (mode === 'tools') {
    if (!env.ELECTRON_BUILDER_CACHE || pkg.devDependencies['electron-builder'] !== '26.18.0') throw new Error('Review toolset cache pins for this electron-builder version');
    for (const { tag, name, checksum } of toolsetAssets(platform, arch)) {
      const result = await asset(`https://github.com/electron-userland/electron-builder-binaries/releases/download/${encodeURIComponent(tag)}/${name}`, checksum);
      await saveFile(path.join(env.ELECTRON_BUILDER_CACHE, tag, name), result.bytes);
    }
  } else throw new Error('Expected electron or tools prefetch mode');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await prefetch(process.argv[2]);
