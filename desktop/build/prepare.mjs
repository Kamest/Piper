import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.dirname(root);
const resources = path.join(root, 'resources');
await mkdir(resources, { recursive: true });
const python = JSON.parse(await readFile(path.join(root, 'build/python.json'), 'utf8'));
await writeFile(path.join(root, 'src/runtime.json'), JSON.stringify({ python: python.version }, null, 2) + '\n');
// Copy shared tokens AND controls, stopping before the web app's shell layout.
const css = await readFile(path.join(repo, 'piper/web/app.css'), 'utf8');
const base = css.slice(0, css.indexOf('/* the shell'));
if (!base.includes('--page:') || !base.includes('button.primary')) throw new Error('Shared styles could not be extracted');
const themes = JSON.parse(await readFile(path.join(repo, 'piper/web/themes.json'), 'utf8'));
await writeFile(path.join(root, 'src/ui/themes.json'), JSON.stringify({ dark: themes.themes.dark, light: themes.themes.light }, null, 2) + '\n');
const light = Object.entries(themes.themes.light.tokens).map(([k,v]) => `--${k}:${v};`).join('');
await writeFile(path.join(root, 'src/ui/shared.css'), base + `\n@media(prefers-color-scheme:light){:root{${light}color-scheme:light;}}\n`);
await copyFile(path.join(repo, 'install.sh'), path.join(resources, 'install.sh'));
const extfind = process.env.PIPER_DESKTOP_EXTFIND || path.join(repo, 'piper/extfind.py');
// Discovery is copied from the core, never independently implemented in the desktop app.
await copyFile(extfind, path.join(resources, 'extfind.py'));
const extensions = await readFile(path.join(path.dirname(extfind), 'extensions.py'), 'utf8');
const api = /^API\s*=\s*(\d+)\s*$/m.exec(extensions)?.[1];
if (!api) throw new Error('Extension API version was not found');
await writeFile(path.join(resources, 'extensions.py'), `# Build-time API constant; discovery must not import extension code.\nAPI = ${api}\n`);
await copyFile(path.join(repo, 'piper/web/icon.svg'), path.join(resources, 'icon.svg'));
await copyFile(path.join(repo, 'piper/web/icon-192.png'), path.join(resources, 'tray.png'));
await copyFile(path.join(repo, 'piper/web/icon-512.png'), path.join(resources, 'icon.png'));
await copyFile(path.join(root, 'build/podman.json'), path.join(resources, 'podman.json'));
