import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { manifests, releaseVersion } from '../build/manifests.mjs';
import { signedUpdates } from '../build/signed-updates.mjs';
const require = createRequire(new URL('../build/config.cjs', import.meta.url));
const yaml = require('js-yaml');
const root = new URL('../../', import.meta.url);

async function config(env = {}, platform = 'darwin') {
  const source = await fs.readFile(new URL('../build/config.cjs', import.meta.url), 'utf8');
  const context = { module: { exports: {} }, __dirname: '/build', process: { env, platform },
    require: name => name === 'node:fs' ? { existsSync: () => true } : require(name) };
  vm.runInNewContext(source, context);
  return context.module.exports;
}

test('ad-hoc signing replaces the linker signature, preserves certificate signing and notarization', async () => {
  const unsigned = await config();
  assert.equal(unsigned.mac.identity, '-');
  assert.equal(unsigned.mac.hardenedRuntime, false);
  assert.equal(unsigned.extraMetadata.desktopSigned, false);
  assert.equal(unsigned.mac.notarize, false);
  assert.equal(unsigned.afterSign, 'build/notarize.cjs');
  assert.equal(unsigned.win.signAndEditExecutable, false);
  assert.equal(unsigned.win.signtoolOptions, undefined);
  assert.equal(unsigned.win.signExecutable, undefined);
  for (const env of [{ CSC_LINK: 'placeholder' }, { CSC_NAME: 'placeholder' }]) {
    const signed = await config(env);
    assert.equal(signed.mac.identity, undefined);
    assert.equal(signed.mac.hardenedRuntime, undefined);
    assert.equal(signed.extraMetadata.desktopSigned, true);
    assert.equal(signed.afterSign, unsigned.afterSign);
  }
  assert.equal((await config({ WIN_CSC_LINK: 'placeholder' })).win.signAndEditExecutable, true);
  assert.equal((await config({ SIGNPATH_ORGANIZATION_ID: 'placeholder' })).win.signtoolOptions, undefined);
  const signPath = await config({ PIPER_SIGNPATH: '1' });
  assert.equal(signPath.win.signAndEditExecutable, true);
  assert.equal(signPath.win.signExecutable, false);
  assert.equal(signPath.win.signtoolOptions.publisherName, 'SignPath Foundation');
  assert.equal(signPath.extraMetadata.desktopSigned, false);
});

test('Store mode selects unsigned AppX targets and explicit identity, without changing NSIS defaults', async () => {
  const base = require('../package.json').build;
  for (const flag of [undefined, '0', 'true']) {
    const normal = await config({ PIPER_DESKTOP_STORE: flag, PIPER_STORE_IDENTITY_NAME: 'Ignored.Identity' }, 'win32');
    assert.deepEqual(JSON.parse(JSON.stringify(normal.win)), base.win);
    assert.equal(normal.nsis, base.nsis);
    assert.equal(normal.directories, base.directories);
    assert.equal(normal.appx, undefined);
    assert.equal(normal.beforePack, undefined);
  }
  const defaults = await config({ PIPER_DESKTOP_STORE: '1' }, 'win32');
  assert.deepEqual(JSON.parse(JSON.stringify(defaults.win.target)), [{ target: 'appx', arch: ['x64', 'arm64'] }]);
  assert.equal(defaults.appx.identityName, 'Piper.Desktop.CI');
  assert.equal(defaults.appx.publisher, 'CN=Piper CI');
  assert.equal(defaults.appx.publisherDisplayName, 'Piper contributors');
  for (const arch of ['x64', 'arm64']) {
    const store = await config({ PIPER_DESKTOP_STORE: '1', PIPER_DESKTOP_ARCH: arch, PIPER_SIGNPATH: '1',
      PIPER_STORE_IDENTITY_NAME: '12345.Piper', PIPER_STORE_PUBLISHER: 'CN=00000000-0000-0000-0000-000000000000',
      PIPER_STORE_PUBLISHER_DISPLAY_NAME: 'Piper tests' }, 'win32');
    assert.deepEqual(JSON.parse(JSON.stringify(store.win.target)), [{ target: 'appx', arch: [arch] }]);
    assert.equal(store.appx.identityName, '12345.Piper');
    assert.equal(store.appx.publisher, 'CN=00000000-0000-0000-0000-000000000000');
    assert.equal(store.appx.publisherDisplayName, 'Piper tests');
    assert.equal(store.appx.applicationId, 'Piper');
    assert.equal(store.appx.displayName, 'Piper');
    assert.equal(store.appx.artifactName, 'Piper-${version}-win-${arch}.${ext}');
    assert.equal(store.appx.addAutoLaunchExtension, false);
    assert.deepEqual(JSON.parse(JSON.stringify(store.appx.capabilities)), ['runFullTrust']);
    assert.equal(store.win.signExecutable, false);
    assert.equal(store.win.signtoolOptions, undefined);
    assert.equal(store.directories.output, 'out/store');
    assert.equal(store.directories.buildResources, 'resources');
    assert.equal(typeof store.beforePack, 'function');
    assert.equal(store.nsis, base.nsis);
  }
  assert.deepEqual(base.win.target, [{ target: 'nsis', arch: ['x64', 'arm64'] }]);
});

test('Store artifacts are unsigned submission inputs and excluded from GitHub release uploads', async () => {
  const workflow = yaml.load(await fs.readFile(new URL('.github/workflows/desktop.yml', root), 'utf8'));
  const steps = workflow.jobs.build.steps;
  const store = steps.find(s => s.name === 'Build unsigned Store package');
  assert.equal(store.if, "runner.os == 'Windows'");
  assert.equal(store.env.PIPER_DESKTOP_STORE, '1');
  for (const key of ['PIPER_STORE_IDENTITY_NAME', 'PIPER_STORE_PUBLISHER', 'PIPER_STORE_PUBLISHER_DISPLAY_NAME']) assert.ok(store.env[key].includes(`vars.${key}`));
  const artifact = steps.find(s => s.with?.name === 'desktop-store-${{ matrix.arch }}');
  assert.match(artifact.if, /refs\/tags\/desktop-v/);
  assert.equal(artifact.with.path, 'desktop/out/store/*.appx');
  const smoke = steps.find(s => s.name === 'Store x64 - sign a copy, install, launch by AUMID and remove');
  assert.equal(smoke.if, "runner.os == 'Windows' && matrix.arch == 'x64'");
  for (const command of ['Copy-Item $unsigned[0].FullName $signed', 'New-SelfSignedCertificate', 'Cert:/LocalMachine/TrustedPeople', 'Add-AppxPackage', 'shell:AppsFolder', 'Start-Sleep -Seconds 20', 'Remove-AppxPackage']) assert.ok(smoke.run.includes(command), command);
  const upload = workflow.jobs['draft-release'].steps.find(s => s.name === 'Attach installers to a draft release');
  assert.match(upload.run, /if folder.name.startswith\('desktop-store-'\):\s+continue/);
});

test('release tags are stable desktop versions, never core tags or paths', () => {
  assert.equal(releaseVersion('desktop-v0.1.2'), '0.1.2');
  assert.equal(releaseVersion('desktop-v12.34.56'), '12.34.56');
  for (const tag of ['v0.1.2', '0.1.2', 'desktop-v01.2.3', 'desktop-v1.2', 'desktop-v1.2.3-rc.1', 'desktop-v1.2.3/../../x', 'desktop-v1.2.3\n']) {
    assert.throws(() => releaseVersion(tag), /stable release tag/);
  }
});

test('release assets produce exact checksums, architecture URLs and multi-file manifests', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'piper-manifests-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const assets = path.join(dir, 'assets'); await fs.mkdir(assets);
  const fixtures = [
    ['mac-arm64.dmg', 'abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    ['mac-x64.dmg', '', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['win-x64.exe', 'hello', '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'],
    ['win-arm64.exe', 'test', '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08']
  ];
  for (const version of ['0.1.2', '12.34.56']) {
    for (const [suffix, bytes] of fixtures) await fs.writeFile(path.join(assets, `Piper-${version}-${suffix}`), bytes);
    const out = path.join(dir, version);
    await manifests(`desktop-v${version}`, assets, out);
    const cask = await fs.readFile(path.join(out, 'Casks/piper.rb'), 'utf8');
    assert.ok(cask.includes(`version "${version}"`));
    for (const [, , sha] of fixtures.slice(0, 2)) assert.ok(cask.includes(sha));
    assert.ok(cask.includes('arch arm: "arm64", intel: "x64"'));
    assert.ok(cask.includes('https://github.com/Kamest/Piper/releases/download/desktop-v#{version}/Piper-#{version}-mac-#{arch}.dmg'));
    assert.ok(cask.includes('regex(/^desktop-v(\\d+(?:\\.\\d+)+)$/i)'));
    const pkg = require('../package.json');
    const appName = pkg.productName || pkg.name;
    for (const text of ['depends_on macos: :ventura', 'app "Piper.app"', `~/Library/Application Support/${appName}`, `~/Library/Logs/${appName}`, 'Open Anyway', 'Docker']) assert.ok(cask.includes(text), text);
    const folder = path.join(out, 'manifests/k/Kamest/Piper', version);
    const names = (await fs.readdir(folder)).sort();
    assert.deepEqual(names, ['Kamest.Piper.installer.yaml', 'Kamest.Piper.locale.en-US.yaml', 'Kamest.Piper.yaml']);
    const data = await Promise.all(names.map(async name => yaml.load(await fs.readFile(path.join(folder, name), 'utf8'))));
    for (const manifest of data) {
      assert.equal(manifest.PackageIdentifier, 'Kamest.Piper');
      assert.equal(manifest.PackageVersion, version);
      assert.equal(manifest.ManifestVersion, '1.10.0');
    }
    const [installer, locale, index] = data;
    assert.equal(index.DefaultLocale, 'en-US');
    assert.equal(locale.PackageLocale, 'en-US');
    assert.equal(locale.Publisher, 'Piper contributors');
    assert.equal(locale.License, 'Apache-2.0');
    assert.equal(locale.PackageUrl, 'https://kamest.github.io/Piper/');
    assert.equal(locale.ReleaseNotesUrl, `https://github.com/Kamest/Piper/releases/tag/desktop-v${version}`);
    assert.equal(installer.InstallerType, 'nullsoft');
    assert.equal(installer.Scope, 'user');
    assert.deepEqual(installer.InstallerSwitches, { Silent: '/S', SilentWithProgress: '/S', Custom: '/currentuser' });
    assert.deepEqual(installer.Installers.map(i => i.Architecture), ['x64', 'arm64']);
    for (const [i, arch] of ['x64', 'arm64'].entries()) {
      assert.equal(installer.Installers[i].InstallerUrl, `https://github.com/Kamest/Piper/releases/download/desktop-v${version}/Piper-${version}-win-${arch}.exe`);
      assert.equal(installer.Installers[i].InstallerSha256, fixtures[i + 2][2].toUpperCase());
    }
  }
  const missing = path.join(dir, 'missing');
  await assert.rejects(manifests('desktop-v9.9.9', assets, missing), /ENOENT/);
  await assert.rejects(fs.access(missing), /ENOENT/);
});

test('external installer signing refreshes every updater channel and its differential blockmap', async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'piper-signed-'));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const out = path.join(dir, 'out'); await fs.mkdir(out);
  const name = 'Piper-0.1.2-win-x64.exe';
  const signed = path.join(dir, name);
  const bytes = Buffer.from('signed installer bytes');
  await fs.writeFile(signed, bytes);
  await fs.writeFile(path.join(out, name), 'unsigned');
  await fs.writeFile(path.join(out, name + '.blockmap'), 'stale');
  const channels = ['desktop.yml', 'desktop-beta.yml'];
  for (const file of channels) await fs.writeFile(path.join(out, file), yaml.dump({ version: '0.1.2', path: name, sha512: 'old', files: [{ url: name, sha512: 'old', size: 8 }] }));
  await signedUpdates(signed, out);
  assert.deepEqual(await fs.readFile(path.join(out, name)), bytes);
  for (const file of channels) {
    const data = yaml.load(await fs.readFile(path.join(out, file), 'utf8'));
    const hash = createHash('sha512').update(bytes).digest('base64');
    assert.equal(data.sha512, hash);
    assert.equal(data.files[0].sha512, hash);
    assert.equal(data.files[0].size, bytes.length);
    assert.equal(data.version, '0.1.2');
  }
  const blockmap = JSON.parse(gunzipSync(await fs.readFile(path.join(out, name + '.blockmap'))));
  assert.equal(blockmap.files.flatMap(f => f.sizes).reduce((a, b) => a + b, 0), bytes.length);
  await assert.rejects(signedUpdates(signed, dir), /No updater metadata/);
});

test('distribution workflows verify macOS signatures and gate signing and submissions', async () => {
  const build = yaml.load(await fs.readFile(new URL('.github/workflows/desktop.yml', root), 'utf8'));
  const job = build.jobs.build;
  assert.match(job.env.SIGNPATH_ENABLED, /matrix.platform == 'win32'.*refs\/tags\/desktop-v.*vars.SIGNPATH_ORGANIZATION_ID != ''/);
  const steps = job.steps;
  const signatures = steps.filter(s => s.uses?.startsWith('signpath/'));
  assert.equal(signatures.length, 2);
  for (const step of signatures) {
    assert.equal(step.uses, 'signpath/github-action-submit-signing-request@v3.0');
    assert.equal(step.if, "env.SIGNPATH_ENABLED == 'true'");
    assert.match(step.with['github-artifact-id'], /outputs.artifact-id/);
    assert.ok(step.with['output-artifact-directory']);
    const xml = await fs.readFile(new URL(`.signpath/${step.with['artifact-configuration-slug']}.xml`, root), 'utf8');
    assert.match(xml, /xmlns="http:\/\/signpath.io\/artifact-configuration\/v1"/);
    assert.match(xml, /<zip-file>[\s\S]*<pe-file path="Piper[^"/]*\.exe">[\s\S]*<authenticode-sign/);
  }
  const order = ['Package Windows app for SignPath', 'Sign Windows app and download signed directory', 'Build NSIS from the signed Windows app', 'Sign Windows installer and download it', 'Refresh updater hashes and blockmap after signing'];
  const indices = order.map(name => steps.findIndex(s => s.name === name));
  assert.ok(indices.every((n, i) => n >= 0 && (!i || n > indices[i - 1])));
  assert.match(steps.find(s => s.name === order[2]).run, /--prepackaged out\/signed-app/);
  assert.match(steps.find(s => s.name === 'Build installers (never publish from electron-builder)').if, /SIGNPATH_ENABLED != 'true'/);
  assert.match(steps.find(s => s.name === 'Verify macOS bundle signature').run, /codesign --verify --deep --strict/);
  const release = yaml.load(await fs.readFile(new URL('.github/workflows/desktop-release.yml', root), 'utf8'));
  assert.ok(release.jobs.smoke.steps.some(s => /codesign --verify --deep --strict "\$RUNNER_TEMP\/Piper.app"/.test(s.run || '')));
  const distribution = release.jobs.manifests;
  assert.equal(distribution.needs, 'smoke');
  assert.deepEqual(distribution.strategy.matrix.os, ['macos-15', 'windows-2025']);
  assert.ok(distribution.steps.some(s => /winget validate/.test(s.run || '')));
  assert.ok(distribution.steps.some(s => /brew style[\s\S]*brew audit --cask/.test(s.run || '')));
  assert.match(distribution.steps.find(s => s.name === 'Update the personal Homebrew tap').if, /HAS_TAP_TOKEN == 'true'/);
  assert.match(distribution.steps.find(s => s.name === 'Submit winget manifests').if, /HAS_WINGET_TOKEN == 'true'/);
});
